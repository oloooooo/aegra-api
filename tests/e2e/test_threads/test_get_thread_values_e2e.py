"""E2E: GET /threads/{tid} contract for deepagents' check_async_task result path.

deepagents reads `values.messages[-1].content` from the thread response after a
run succeeds. A thread without graph_id or checkpoint must return 200 with
`values: {}` (placeholder text on the client), never 404.
"""

import pytest

from tests.e2e._utils import get_e2e_client


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_get_thread_no_checkpoint_returns_empty_values_e2e():
    """Fresh thread: 200 + values {} (not 404)."""
    client = get_e2e_client()
    thread = await client.threads.create()
    thread_id = thread["thread_id"]

    fetched = await client.threads.get(thread_id)

    assert fetched["thread_id"] == thread_id
    assert fetched.get("values", None) == {}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_get_thread_after_run_returns_values_e2e():
    """After a successful run, GET /threads/{tid} carries the latest values."""
    client = get_e2e_client()
    thread = await client.threads.create()
    thread_id = thread["thread_id"]

    await client.runs.wait(
        thread_id=thread_id,
        assistant_id="researcher",
        input={"messages": [{"role": "user", "content": "summarize async subagents in one line"}]},
    )

    fetched = await client.threads.get(thread_id)
    messages = fetched["values"]["messages"]
    assert messages, "expected values.messages after a successful run"
    assert messages[-1]["content"], "expected non-empty last message content"
