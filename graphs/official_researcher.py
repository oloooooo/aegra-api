"""Researcher subagent graph.

A specialized agent for information gathering, analysis, and research tasks.
This graph runs as an async subagent -- the supervisor launches it as a
background job and checks on it later.

When deployed via `langgraph.json`, this graph is addressable by its graph ID
("researcher") and the supervisor reaches it via ASGI transport.

## Completion notifications

和所有异步子 agent 一样，结果由服务端的发件箱分发者在父线程空闲时送回
（`custom/outbox_dispatcher.py`，ADR-0010）。图本身不需要任何通知中间件。
"""

from __future__ import annotations

import contextlib

from langchain.agents import create_agent
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from graphs.llms import model

SYSTEM_PROMPT = """\
You are a research agent specializing in information gathering and analysis.

Your job is to thoroughly research the topic or question you've been given and
produce a comprehensive, well-structured response. Focus on:

1. Accuracy -- verify claims and note when information is uncertain
2. Completeness -- cover all relevant aspects of the topic
3. Structure -- organize your findings clearly with headings and bullet points
4. Sources -- mention where information could be verified when relevant

Take your time to be thorough. The supervisor agent will check on your results
when they're needed.
"""


@tool
def analyze_topic(query: str) -> str:
    """Analyze a topic by breaking it down into key aspects and considerations.

    Args:
        query: The topic or question to analyze.
    """
    # In a real deployment, this could call a search API, RAG pipeline, etc.
    return (
        f"Analysis of '{query}':\n"
        f"- Key aspects identified\n"
        f"- Related concepts mapped\n"
        f"- Potential sources noted\n"
        f"\nThis is a placeholder. In production, connect to your preferred "
        f"search/RAG tools here."
    )


@tool
def summarize_findings(content: str, format: str = "bullet_points") -> str:
    """Summarize research findings into a structured format.

    Args:
        content: The raw research content to summarize.
        format: Output format -- 'bullet_points', 'narrative', or 'table'.
    """
    return (
        f"Summary ({format}):\n"
        f"- Condensed key findings from provided content\n"
        f"- Organized by relevance and importance\n"
        f"\nThis is a placeholder. The LLM handles the actual summarization."
    )




# --- Static graph (no completion notifications) ---

# graph = create_agent(
#     model=model,
#     tools=[analyze_topic, summarize_findings],
#     system_prompt=SYSTEM_PROMPT,
#     name="researcher",
# )


# --- Graph factory ---
# 结果回写不在这里做：服务端发件箱分发者负责（ADR-0010），所以图本身与普通图一样。


@contextlib.asynccontextmanager
async def graph(config: RunnableConfig):
    """Graph factory。`config` 保留给 aegra 的工厂图约定，内容当前不需要。"""
    yield create_agent(
        model=model,
        tools=[analyze_topic, summarize_findings],
        system_prompt=SYSTEM_PROMPT,
        name="researcher",
    )
