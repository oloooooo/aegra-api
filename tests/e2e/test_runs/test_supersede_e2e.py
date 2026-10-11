"""E2E: multitask_strategy="interrupt" really supersedes (design §10-#4, ADR-0003).

deepagents' update_async_task sends POST /runs with multitask_strategy="interrupt":
kill the active run, keep thread history, start the new run.
"""

import asyncio
import json

import pytest

from tests.e2e._utils import await_terminal_run, elog, get_e2e_client

# stress_test sleeps `delay` seconds per step — long enough to cancel mid-flight.
_SLOW_INPUT = {"messages": [{"role": "user", "content": json.dumps({"delay": 8.0, "steps": 2})}]}
_FAST_INPUT = {"messages": [{"role": "user", "content": json.dumps({"delay": 0.0, "steps": 1})}]}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_supersede_interrupts_old_run_and_keeps_history_e2e():
    client = get_e2e_client()
    thread = await client.threads.create()
    thread_id = thread["thread_id"]

    # Seed a completed turn so "history preserved" has something to check.
    await client.runs.wait(thread_id=thread_id, assistant_id="stress_test", input=_FAST_INPUT)
    history_before = await client.threads.get_history(thread_id)
    turns_before = len(history_before)

    # Start a slow run, then supersede it before it can finish.
    old_run = await client.runs.create(thread_id, assistant_id="stress_test", input=_SLOW_INPUT)
    await asyncio.sleep(1.0)  # let it reach running

    new_run = await client.runs.create(
        thread_id,
        assistant_id="stress_test",
        input=_FAST_INPUT,
        multitask_strategy="interrupt",
    )
    elog("Supersede", {"old_run_id": old_run["run_id"], "new_run_id": new_run["run_id"]})

    assert new_run["run_id"] != old_run["run_id"], "supersede must create a fresh run"

    # Old run settles as interrupted (cancel attribution), not success.
    old_final = await await_terminal_run(client, thread_id, old_run["run_id"], timeout=30.0)
    assert old_final["status"] == "interrupted", f"expected old run interrupted, got {old_final['status']}"
    assert old_final["interrupted_by"] == "cancel", f"expected interrupted_by='cancel', got {old_final!r}"

    # New run completes normally.
    new_final = await await_terminal_run(client, thread_id, new_run["run_id"], timeout=30.0)
    assert new_final["status"] == "success", f"expected new run success, got {new_final['status']}"

    # Thread history survived the supersede (turns before >= 1, still present).
    history_after = await client.threads.get_history(thread_id)
    assert len(history_after) >= turns_before, (
        f"history shrank after supersede: before={turns_before} after={len(history_after)}"
    )
