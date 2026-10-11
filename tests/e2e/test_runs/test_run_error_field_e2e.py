"""E2E: failed run exposes `error` alongside `error_message` (design §10-#5).

deepagents reads `run["error"]` on failure; the server keeps both keys in sync.
"""

import json

import pytest

from tests.e2e._utils import await_terminal_run, get_e2e_client


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_failed_run_has_error_and_error_message_e2e():
    """A graph raise lands in status=error with both error keys non-empty."""
    client = get_e2e_client()
    thread = await client.threads.create()
    thread_id = thread["thread_id"]

    # stress_test raises RuntimeError when {"fail": true} reaches the target step.
    run = await client.runs.create(
        thread_id,
        assistant_id="stress_test",
        input={"messages": [{"role": "user", "content": json.dumps({"delay": 0.0, "steps": 1, "fail": True})}]},
    )
    final = await await_terminal_run(client, thread_id, run["run_id"], timeout=30.0)

    assert final["status"] == "error", f"expected error status, got {final}"
    assert final["error_message"], "error_message should be non-empty"
    assert final["error"] == final["error_message"], "error must mirror error_message"
