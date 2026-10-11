# aegra-api

Aegra API - Self-hosted Agent Protocol server.

Aegra is an open-source, self-hosted alternative to LangSmith Deployments. This package provides the core API server that implements the Agent Protocol, allowing you to run AI agents on your own infrastructure without vendor lock-in.

## Features

- **Agent Protocol Compliant**: Works with Agent Chat UI, LangGraph Studio, CopilotKit
- **Drop-in Replacement**: Compatible with the LangGraph SDK
- **Self-Hosted**: Run on your own PostgreSQL database
- **Streaming Support**: Real-time streaming of agent responses
- **Human-in-the-Loop**: Built-in support for human approval workflows
- **Async Subagent Ready**: Serves as the Agent Protocol backend for Deep Agents async subagents
- **Scheduled Cron Jobs**: Trigger runs on a schedule with timezone support and multi-instance safe claim
- **Vector Store**: Semantic search capabilities with PostgreSQL

## Installation

```bash
pip install aegra-api
```

## Quick Start

The easiest way to get started is with the [aegra-cli](../aegra-cli/README.md):

```bash
# Install the CLI
pip install aegra-cli

# Initialize a new project (interactive)
aegra init
cd <your-project>

# Configure environment
cp .env_example .env
# Add your OPENAI_API_KEY to .env

# Install dependencies and start developing
uv sync
uv run aegra dev
```

### Manual Setup

If you prefer manual setup:

```bash
# Install dependencies
pip install aegra-api

# Set environment variables
export POSTGRES_USER=aegra
export POSTGRES_PASSWORD=aegra_secret
export POSTGRES_HOST=localhost
export POSTGRES_DB=aegra

# Run migrations
alembic upgrade head

# Start server
uvicorn aegra_api.main:app --port 2026 --reload
```

## Tutorial: running from this repo

A complete walkthrough for running the server locally and wiring a Deep Agents
supervisor to it (the exact steps used to verify the async-subagent contract).

### 1. Install and configure

```bash
cd aegra-api
uv sync                      # creates .venv, installs aegra-api in editable mode
cp .env_example .env         # then fill in real values
```

`.env` is loaded automatically at startup (`settings.py` calls `load_dotenv()`;
real environment variables win over the file). Minimum contents:

```bash
# PostgreSQL — either a single URL ...
# DATABASE_URL=postgresql://postgres:password@localhost:5432/aegra
# ...or the individual vars (used when DATABASE_URL is unset)
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=aegra

# OpenAI-compatible LLM API (read by langchain-openai inside your graphs)
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1   # or any OpenAI-compatible endpoint
OPENAI_MODEL=gpt-4o-mini                     # picked up by graph code via os.environ
```

### 2. Register your graphs in `aegra.json`

```json
{
  "graphs": {
    "researcher": "./graphs/researcher.py:graph",
    "supervisor": "./graphs/supervisor.py:graph"
  }
}
```

Each value is `path/to/module.py:attr` exporting a compiled LangGraph
`graph`. Do **not** pass a checkpointer when compiling — the server injects one
per request. Graph name doubles as the `assistant_id` clients send; no manual
assistant registration is needed.

> **Windows**: run uvicorn with the selector event loop —
> `--loop "asyncio:SelectorEventLoop"` (psycopg/asyncpg are incompatible with
> the default Proactor loop).

### 3. Migrate and start

```bash
uv run alembic upgrade head

# Local mode (single process, in-memory broker)
uv run uvicorn aegra_api.main:app --port 2026 --reload --loop "asyncio:SelectorEventLoop"

# Worker mode (Redis job queue + crash recovery) — requires a running Redis
REDIS_BROKER_ENABLED=true uv run uvicorn aegra_api.main:app --port 2026 --loop "asyncio:SelectorEventLoop"
```

Verify: `curl http://localhost:2026/health` →
`{"status":"healthy","database":"connected",...}`.

### 4. Smoke-test the Agent Protocol surface

```bash
# Create a thread (body may be {})
TID=$(curl -s -X POST localhost:2026/threads -H 'Content-Type: application/json' -d '{}' | python -c 'import sys,json;print(json.load(sys.stdin)["thread_id"])')

# Start a run — assistant_id is the graph name from aegra.json
RUN_ID=$(curl -s -X POST "localhost:2026/threads/$TID/runs" -H 'Content-Type: application/json' \
  -d '{"assistant_id":"researcher","input":{"messages":[{"role":"user","content":"hello"}]}}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')

# Poll the run, then read the result from the thread's latest values
curl -s "localhost:2026/threads/$TID/runs/$RUN_ID"   # -> status (success/error), error, interrupted_by
curl -s "localhost:2026/threads/$TID"                # -> values.messages[-1].content
```

### 5. Point a Deep Agents supervisor at it

```python
# graphs/supervisor.py
import os
from deepagents import AsyncSubAgent, create_deep_agent

graph = create_deep_agent(
    model=f"openai:{os.environ['OPENAI_MODEL']}",
    subagents=[
        AsyncSubAgent(
            name="researcher",
            description="Research agent for information gathering",
            graph_id="researcher",                              # aegra.json key
            url=os.environ.get("AEGRA_SERVER_URL", "http://localhost:2026"),
        )
    ],
)
```

Run the supervisor the same way as any other graph. Its async subagent tasks
start, poll, update and cancel over this server's `/threads` + `/runs`
endpoints; each subagent's memory lives in its own thread in PostgreSQL.

### 6. Run the tests

```bash
uv run pytest tests/unit tests/integration -q          # no external deps

# e2e hits a LIVE server (SERVER_URL, default http://localhost:2026)
# with graphs registered in aegra.json — start the server first, then:
uv run pytest tests/e2e -q                              # all e2e
uv run pytest tests/e2e/test_runs/test_supersede_e2e.py -q   # one case
```

`@pytest.mark.prod_only` tests need worker mode (`REDIS_BROKER_ENABLED=true`
with a reachable Redis); they exercise lease/crash-recovery paths that do not
exist in local mode.

## Configuration

### aegra.json

Define your agent graphs in `aegra.json`:

```json
{
  "graphs": {
    "agent": "./graphs/my_agent/graph.py:graph",
    "assistant": "./graphs/assistant/graph.py:graph"
  },
  "http": {
    "app": "./custom_routes.py:app"
  }
}
```

### Environment Variables

```bash
# Database
POSTGRES_USER=aegra
POSTGRES_PASSWORD=aegra_secret
POSTGRES_HOST=localhost
POSTGRES_DB=aegra

# Authentication
AUTH_TYPE=noop  # Options: noop, custom

# Server
HOST=0.0.0.0
PORT=2026

# Configuration
AEGRA_CONFIG=aegra.json

# LLM (for example agents)
OPENAI_API_KEY=sk-...

# Observability (optional)
OTEL_TARGETS=LANGFUSE,PHOENIX
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/assistants` | POST | Create assistant from graph_id |
| `/assistants/search` | POST | Search assistants |
| `/assistants/{assistant_id}` | GET | Get assistant details |
| `/threads` | POST | Create conversation thread |
| `/threads/{thread_id}/state` | GET | Get thread state |
| `/threads/{thread_id}/runs` | POST | Execute graph (background) |
| `/threads/{thread_id}/runs/stream` | POST | Execute graph (streaming) |
| `/threads/{thread_id}/runs/{run_id}/cancel` | POST | Cancel a run |
| `/runs/crons` | POST | Create a stateless cron job |
| `/threads/{thread_id}/runs/crons` | POST | Create a thread-bound cron job |
| `/runs/crons/{cron_id}` | PATCH/DELETE | Update or delete a cron job |
| `/runs/crons/search` | POST | Search cron jobs |
| `/runs/crons/count` | POST | Count cron jobs |
| `/store/items` | PUT | Save to vector store |
| `/store/items/search` | POST | Semantic search |
| `/store/namespaces` | POST | List store namespaces |
| `/health` | GET | Health check |

## Creating Graphs

Agents are Python modules exporting a compiled `graph` variable:

```python
# graphs/my_agent/graph.py
from typing import TypedDict
from langgraph.graph import StateGraph, START, END

class State(TypedDict):
    messages: list[str]

def process_node(state: State) -> State:
    messages = state.get("messages", [])
    messages.append("Processed!")
    return {"messages": messages}

# Build the graph
builder = StateGraph(State)
builder.add_node("process", process_node)
builder.add_edge(START, "process")
builder.add_edge("process", END)

# Export as 'graph'
graph = builder.compile()
```

## Async Subagents (Deep Agents)

aegra-api doubles as the Agent Protocol server for [Deep Agents](https://github.com/langchain-ai/deepagents)
**async subagents**: a supervisor launches background runs over HTTP, polls them, sends follow-up
instructions, and cancels them, while each subagent's memory lives in your PostgreSQL.

Register the subagent graph in `aegra.json` like any other graph:

```json
{
  "graphs": {
    "researcher": "./graphs/researcher.py:graph"
  }
}
```

```python
# graphs/researcher.py — compiled without a checkpointer; aegra injects one per request
from deepagents import create_deep_agent

graph = create_deep_agent(model="openai:gpt-5.5", system_prompt="You are a thorough research agent.")
```

Point the supervisor at this server (`graph_id` is the `aegra.json` key):

```python
from deepagents import AsyncSubAgent, create_deep_agent

supervisor = create_deep_agent(
    model="openai:gpt-5.5",
    subagents=[AsyncSubAgent(name="researcher", description="...", graph_id="researcher", url="http://localhost:2026")],
)
```

See [docs/design.md](docs/design.md) for the exact contract, the status vocabulary, and known limits
(HITL subagents cannot be resumed by deepagents).

## Architecture

```
+---------------------------------------------------------+
|  FastAPI HTTP Layer (Agent Protocol API)                |
|  - /assistants, /threads, /runs, /store endpoints       |
+---------------------------------------------------------+
|  Middleware Stack                                        |
|  - Auth, CORS, Structured Logging, Correlation ID       |
+---------------------------------------------------------+
|  Service Layer (Business Logic)                          |
|  - LangGraphService, AssistantService, StreamingService |
+---------------------------------------------------------+
|  LangGraph Runtime                                       |
|  - Graph execution, state management, tool execution    |
+---------------------------------------------------------+
|  Database Layer (PostgreSQL)                             |
|  - AsyncPostgresSaver (checkpoints), AsyncPostgresStore |
+---------------------------------------------------------+
```

## Package Structure

```
libs/aegra-api/
├── src/aegra_api/
│   ├── api/              # Agent Protocol endpoints
│   │   ├── assistants.py # /assistants CRUD
│   │   ├── threads.py    # /threads and state management
│   │   ├── runs.py       # /runs execution and streaming
│   │   └── store.py      # /store vector storage
│   ├── services/         # Business logic layer
│   ├── core/             # Infrastructure (database, auth, orm)
│   ├── models/           # Pydantic request/response schemas
│   ├── middleware/       # ASGI middleware
│   ├── observability/    # OpenTelemetry tracing
│   ├── utils/            # Helper functions
│   ├── main.py           # FastAPI app entry point
│   ├── config.py         # HTTP/store config loading
│   └── settings.py       # Environment settings
├── tests/                # Test suite
├── alembic/              # Database migrations
└── pyproject.toml
```

## Related Packages

- **aegra-cli**: Command-line interface for project management

## Documentation

- Async subagent (ACP) design: [docs/design.md](docs/design.md)
- Glossary: [CONTEXT.md](CONTEXT.md)
