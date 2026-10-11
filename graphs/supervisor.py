"""Supervisor graph for the deepagents async-subagent integration test (design §8, §10-#11).

The AsyncSubAgent points at this same aegra-api server; graph_id must match an
aegra.json graphs key ("researcher").
"""

import os

from deepagents import AsyncSubAgent, create_deep_agent

graph = create_deep_agent(
    model=f"openai:{os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')}",
    system_prompt="You are a supervisor. Delegate research questions to the researcher subagent and summarize its result.",
    subagents=[
        AsyncSubAgent(
            name="researcher",
            description="Research agent for information gathering and synthesis",
            graph_id="researcher",  # aegra.json key
            url=os.environ.get("AEGRA_SERVER_URL", "http://localhost:2026"),
        )
    ],
)
