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

# Dev entry point — the selector loop Windows needs is already wired up
uv run python src/aegra_api/main.py

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

Two files with two different jobs:

| | `aegra.json` | `.env` / environment variables |
|---|---|---|
| **What goes in it** | What this deployment *serves*: graphs, custom HTTP app, store/auth/checkpointer policy | How it *runs and connects*: host/port, PostgreSQL, Redis, workers, cron, logging, OpenTelemetry, LLM keys |
| **Read by** | `src/aegra_api/config.py` | `src/aegra_api/settings.py` (`load_dotenv()` at import; real env vars win over the file) |
| **Format** | JSON | `KEY=VALUE` in the project-root `.env` |
| **Secrets** | never | yes |

Rule of thumb: machine/deployment specific values (credentials, hostnames, ports, capacity, feature switches) are environment variables; anything that describes *what* this server hosts (which graphs, which auth handler, retention policy) belongs in `aegra.json`.

### `aegra.json`

Path resolution: `AEGRA_CONFIG` env var → `./aegra.json` → `./langgraph.json` (compatibility fallback). Relative paths inside the file resolve against the config file's directory. A full example lives in `aegra_example.json`.

```json
{
  "dependencies": ["./examples"],
  "graphs": {
    "researcher": "./graphs/researcher.py:graph"
  },
  "http": {
    "app": "./custom_routes.py:app",
    "enable_custom_route_auth": true,
    "cors": { "allow_origins": ["https://app.example.com"] }
  },
  "store": {
    "scopes": { "orgs": ["org_id"] },
    "index": { "dims": 1536, "embed": "openai:text-embedding-3-small", "fields": ["$"] }
  },
  "checkpointer": {
    "durability": "async",
    "ttl": { "strategy": "delete", "default_ttl": 10080, "sweep_interval_minutes": 30 }
  },
  "auth": {
    "path": "./auth.py:auth",
    "disable_studio_auth": false
  }
}
```

| Key | Purpose |
|-----|---------|
| `graphs` | Graph name → `path/to/module.py:attr`. The name doubles as the `assistant_id` clients send. |
| `dependencies` | Extra directories added to `sys.path` before graphs are imported (shared code living outside the project root). |
| `http.app` | Custom FastAPI/Starlette app merged into the server — see `examples/custom_routes_example.py`. |
| `http.enable_custom_route_auth` | Apply the Aegra auth dependency to the custom app's routes. |
| `http.cors` | CORS overrides: `allow_origins`, `allow_origin_regex`, `allow_credentials`, `allow_methods`, `allow_headers`, `expose_headers`, `max_age`. |
| `store.scopes` | Namespace prefix → list of user attributes allowed to scope it. |
| `store.index` | Turns on pgvector semantic search: `dims`, `embed` (`provider:model`), `fields` (JSON paths to embed, default `["$"]`). |
| `checkpointer.durability` | Default checkpoint durability for runs that don't set one: `sync`, `async` or `exit`. |
| `checkpointer.ttl` | Thread retention: `strategy` (`delete` or `keep_latest`), `default_ttl` (minutes), `sweep_interval_minutes`, `sweep_limit`. |
| `auth.path` | Auth handler import path (`./auth.py:auth` or `mypackage.auth:auth`). |

Two of these keys have an env override — env wins over `aegra.json`, and a per-run value wins over both:
`AEGRA_THREAD_TTL` replaces `checkpointer.ttl`, `AEGRA_CHECKPOINT_DURABILITY` replaces `checkpointer.durability`.

### Environment Variables (`.env`)

Loaded once at import; real environment variables take precedence over the file, so the same `.env` works in Docker/K8s with a few keys overridden. Defaults for everything below are in `src/aegra_api/settings.py`.

```bash
# --- Server ---
HOST=0.0.0.0
PORT=2026
AEGRA_CONFIG=aegra.json       # path to the aegra.json described above
DEBUG=false
AUTH_TYPE=noop                # noop | custom
LOG_LEVEL=INFO
RUN_MIGRATIONS_ON_STARTUP=true   # false for multi-pod: run `aegra db upgrade` out-of-band

# --- PostgreSQL (DATABASE_URL wins when both are set) ---
# DATABASE_URL=postgresql://postgres:postgres@localhost:5432/aegra
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=aegra

# --- Redis broker (multi-instance streaming + crash recovery) ---
REDIS_BROKER_ENABLED=false
REDIS_URL=redis://localhost:6379/0

# --- Workers (used only when REDIS_BROKER_ENABLED=true) ---
WORKER_COUNT=3
N_JOBS_PER_WORKER=10
BG_JOB_TIMEOUT_SECS=3600
BG_JOB_MAX_RETRIES=3

# --- Pools, cron, retention ---
SQLALCHEMY_POOL_SIZE=10
LANGGRAPH_MAX_POOL_SIZE=20
CRON_ENABLED=true
AEGRA_THREAD_TTL=10080        # minutes, or JSON: {"strategy":"keep_latest","default_ttl":...}
AEGRA_CHECKPOINT_DURABILITY=async

# --- LLM (read by langchain-openai inside your graphs) ---
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1   # only for OpenAI-compatible endpoints

# --- Observability (optional) ---
OTEL_TARGETS=LANGFUSE,PHOENIX
ENABLE_PROMETHEUS_METRICS=false
```

`AEGRA_THREAD_TTL` takes either a bare number (default TTL in minutes) or a JSON object with `strategy` / `default_ttl` / `sweep_interval_minutes` / `sweep_limit`; `LANGGRAPH_THREAD_TTL` is accepted as an alias.

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
