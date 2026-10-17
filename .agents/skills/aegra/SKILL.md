---
name: aegra
description: Use when deploying LangGraph agents to production, building multi-turn conversational AI systems, managing agent state and persistence, implementing human-in-the-loop workflows, or setting up self-hosted agent infrastructure with authentication and observability.
metadata:
    mintlify-proj: aegra
    version: "1.0"
---

# Aegra Skill

## Product summary

Aegra is an open-source, self-hosted Agent Protocol server for running LangGraph agents on your own infrastructure. It provides production-ready features including persistent state via PostgreSQL, real-time streaming with Server-Sent Events, human-in-the-loop approval gates, configurable authentication (JWT/OAuth/Firebase), semantic search with pgvector, and deployment to Docker, PaaS, or Kubernetes. Use the LangGraph SDK you already know — Aegra is a drop-in replacement for LangSmith Deployments with zero code changes.

**Key files and commands:**
- `aegra.json` — Configuration file defining graphs, auth, HTTP routes, store settings, and checkpointer behavior
- `aegra dev` — Start development server with hot reload and managed PostgreSQL
- `aegra serve` — Start production server (requires external PostgreSQL)
- `aegra up` — Start full stack in Docker (PostgreSQL + Redis + app)
- `.env` — Environment variables for API keys, database, and Redis configuration

**Primary docs:** https://docs.aegra.dev

## When to use

Reach for this skill when:
- **Deploying agents to production** — You need a self-hosted server for LangGraph graphs with persistence and authentication
- **Building multi-turn conversations** — You're creating chatbots or assistants that maintain state across interactions
- **Implementing approval workflows** — You need human-in-the-loop gates where users approve or edit agent actions before execution
- **Managing agent state** — You need to inspect, update, or replay conversations from specific checkpoints
- **Setting up authentication** — You're adding JWT, OAuth, Firebase, or custom auth to control who can use your agents
- **Streaming responses** — You need real-time token-by-token output or tool call updates to clients
- **Scaling horizontally** — You're deploying multiple instances with Redis for job dispatch and crash recovery
- **Observability** — You need to trace agent execution to OpenTelemetry backends (Langfuse, Phoenix, etc.)

## Quick reference

### CLI commands

| Command | Purpose | When to use |
|---------|---------|------------|
| `aegra init` | Create new project | Starting a new agent project |
| `aegra dev` | Dev server with hot reload | Local development |
| `aegra serve` | Production server | PaaS, Docker, Kubernetes, bare metal |
| `aegra up` | Full Docker stack | Self-hosted production with PostgreSQL + Redis |
| `aegra down` | Stop Docker services | Shutting down local stack |
| `aegra db upgrade` | Apply migrations | Multi-pod deployments before rolling out |

### Configuration sections in aegra.json

| Section | Purpose | Example |
|---------|---------|---------|
| `graphs` | Register LangGraph agents | `{"agent": "./src/graph.py:graph"}` |
| `auth` | Enable authentication | `{"path": "./auth.py:auth"}` |
| `http` | Custom FastAPI routes | `{"app": "./routes.py:app"}` |
| `store` | Semantic search with embeddings | `{"index": {"dims": 1536, "embed": "openai:text-embedding-3-small"}}` |
| `checkpointer` | Thread TTL and durability | `{"ttl": {"strategy": "delete", "default_ttl": 43200}}` |

### Graph registration patterns

```json
{
  "graphs": {
    "static_graph": "./path/graph.py:compiled_graph",
    "factory_graph": "./path/graph.py:graph_factory"
  }
}
```

- **Static graph** — `builder.compile()` result, cached once at startup
- **Factory function** — Callable accepting `config` and/or `ServerRuntime`, called per-request for user-specific customization

### Environment variables (key ones)

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL connection (production) |
| `POSTGRES_*` | Individual database fields (dev) |
| `REDIS_BROKER_ENABLED` | Enable Redis for job dispatch (production) |
| `REDIS_URL` | Redis connection string |
| `AEGRA_CONFIG` | Path to custom config file |
| `AEGRA_CHECKPOINT_DURABILITY` | Default checkpoint mode: `sync`, `async`, or `exit` |
| `AEGRA_THREAD_TTL` | Thread expiry in minutes |
| `RUN_MIGRATIONS_ON_STARTUP` | Auto-run migrations (set `false` for multi-pod) |

## Decision guidance

### When to use each deployment command

| Scenario | Command | Why |
|----------|---------|-----|
| Local development | `aegra dev` | Starts PostgreSQL automatically, hot reload, simplest setup |
| Self-hosted production | `aegra up` | Full stack in Docker, includes Redis for scaling |
| PaaS (Railway, Render, Fly.io) | `aegra serve` | No Docker needed, you provide PostgreSQL + Redis |
| Kubernetes | `aegra serve` in pod spec | Stateless, works with managed databases |
| Single-instance production | `aegra serve` with `REDIS_BROKER_ENABLED=false` | Simpler, no Redis needed |

### When to use static vs factory graphs

| Need | Use | Example |
|------|-----|---------|
| Simple agent, same for all users | Static graph | `builder.compile()` |
| Customize tools per user | Factory with `ServerRuntime` | Check `runtime.user.permissions` to add/remove tools |
| Manage resources (MCP, DB connections) | Factory with async context manager | Allocate resources at factory time, release on cleanup |
| Access user data in nodes | `Runtime[T]` parameter on node | Receive typed context via `runtime.context` |

### When to use each stream mode

| Mode | Use when | Example |
|------|----------|---------|
| `values` | You need full state snapshots | Updating UI with complete conversation state |
| `updates` | You only want state deltas | Efficient incremental updates |
| `messages` | You're building a chat UI | Token-by-token LLM output |
| `custom` | You emit domain-specific data | Progress updates, intermediate results |
| `events` | You need fine-grained tracing | Debugging, detailed execution logs |

### Checkpoint durability modes

| Mode | Checkpoint timing | Recovery behavior | Use when |
|------|-------------------|-------------------|----------|
| `sync` | After every step | Resumes from last completed step | You need precise recovery, can tolerate latency |
| `async` (default) | Background after each step | Resumes from last finished checkpoint | Standard production use |
| `exit` | Once at run end | Restarts from thread's pre-run state | High-volume runs, no mid-run recovery needed |

## Workflow

### 1. Set up a new Aegra project

```bash
pip install aegra-cli
aegra init
# Choose template (simple-chatbot or react-agent)
cd <project>
cp .env.example .env
# Add OPENAI_API_KEY to .env
uv sync
uv run aegra dev
```

Visit `http://localhost:2026/docs` to explore the API.

### 2. Define your LangGraph agent

Write your graph in `src/agent/graph.py`:

```python
from langgraph.graph import StateGraph
from langgraph.types import Command, interrupt

# Define state, nodes, edges
builder = StateGraph(State)
builder.add_node("agent", call_model)
builder.add_node("tools", tool_node)
# ... wire up edges
graph = builder.compile()
```

### 3. Register in aegra.json

```json
{
  "graphs": {
    "agent": "./src/agent/graph.py:graph"
  }
}
```

Aegra automatically creates a default assistant for each graph.

### 4. Create threads and run conversations

```python
from langgraph_sdk import get_client

client = get_client(url="http://localhost:2026")
thread = await client.threads.create()

async for chunk in client.runs.stream(
    thread_id=thread["thread_id"],
    assistant_id="agent",
    input={"messages": [{"type": "human", "content": "Hello"}]},
):
    print(chunk)
```

### 5. Add authentication (if needed)

Create `auth.py`:

```python
from langgraph_sdk import Auth

auth = Auth()

@auth.authenticate
async def authenticate(headers: dict) -> dict:
    token = headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        raise Exception("Missing token")
    # Verify token (JWT, OAuth, etc.)
    return {"identity": "user123", "display_name": "Alice"}
```

Update `aegra.json`:

```json
{
  "auth": {
    "path": "./auth.py:auth"
  }
}
```

### 6. Deploy

**Docker (self-hosted):**
```bash
aegra up
```

**PaaS (Railway, Render, Fly.io):**
- Create PostgreSQL addon
- Create Redis addon (optional but recommended)
- Set `DATABASE_URL`, `REDIS_URL`, `REDIS_BROKER_ENABLED=true`
- Set start command: `aegra serve --host 0.0.0.0 --port $PORT`

**Kubernetes:**
- Use `aegra serve` as container command
- Provide PostgreSQL and Redis as managed services or StatefulSets
- Use readiness probe: `GET /ready`
- Use liveness probe: `GET /live`

## Common gotchas

- **Graph not loading** — Check that the import path in `aegra.json` is correct and the variable is actually exported. Relative paths are resolved from the config file's directory.

- **Auth not working** — Verify the auth handler is registered with `@auth.authenticate` and raises exceptions to reject requests (don't return a guest user to mean "no").

- **Migrations hang on startup (Kubernetes)** — Multiple pods race for Alembic's advisory lock. Set `RUN_MIGRATIONS_ON_STARTUP=false` and run `aegra db upgrade` once per release from a Helm pre-upgrade Job.

- **Checkpoint durability confusion** — `exit` mode only writes one checkpoint per run, so you can't inspect or fork from steps inside the run. Use `sync` or `async` if you need mid-run recovery or time travel.

- **Thread TTL not working** — TTL applies only to threads created *after* it's enabled. Pre-existing threads are not backfilled. Threads with pending/running runs are skipped and swept later.

- **Custom routes not authenticated** — By default, custom FastAPI routes don't require auth. Set `enable_custom_route_auth: true` in `aegra.json` to enforce auth on all custom routes.

- **Streaming reconnection lost** — Track the last event ID you received and reconnect with the `Last-Event-ID` header. Events are retained for 10 minutes after the run completes.

- **Factory graph called multiple times** — Factory graphs are called once at startup (for schema extraction) and once per request (for execution). Use `runtime.execution_runtime` to detect which context you're in.

- **Metadata merging on update** — When updating an assistant, `metadata` merges into existing metadata (doesn't replace). Use `config` or `context` if you need to clear fields.

- **User isolation missing** — When auth is enabled, threads are scoped to the authenticated user. Without auth, all users share the `anonymous` identity and see each other's threads.

## Verification checklist

Before deploying or submitting work:

- [ ] `aegra.json` is valid JSON and all graph import paths exist
- [ ] All required environment variables are set (`.env` file or platform config)
- [ ] Database connection works: `aegra dev` or `aegra serve` starts without connection errors
- [ ] Migrations applied: check logs for "alembic upgrade head" or run `aegra db current`
- [ ] Graph loads: visit `http://localhost:2026/docs` and check `/assistants` endpoint
- [ ] Authentication handler (if configured) raises exceptions to reject invalid tokens
- [ ] Thread creation works: `POST /threads` returns a thread with `thread_id`
- [ ] Run creation works: `POST /threads/{id}/runs` returns a run with `run_id`
- [ ] Streaming works: `GET /threads/{id}/runs/{id}/stream` returns SSE events
- [ ] State persists: create a thread, run a conversation, retrieve state with `GET /threads/{id}/state`
- [ ] Custom routes (if any) are registered in `aegra.json` and accessible
- [ ] Health checks pass: `GET /health`, `GET /ready`, `GET /live` all return 200
- [ ] For production: Redis is running if `REDIS_BROKER_ENABLED=true`
- [ ] For Kubernetes: `RUN_MIGRATIONS_ON_STARTUP=false` and migrations run out-of-band

## Resources

**Full documentation navigation:** https://docs.aegra.dev/llms.txt

**Critical pages:**
- [Configuration reference](https://docs.aegra.dev/reference/configuration) — Complete aegra.json schema
- [Authentication guide](https://docs.aegra.dev/guides/authentication) — JWT, OAuth, Firebase, custom auth
- [Deployment guide](https://docs.aegra.dev/guides/deployment) — Docker, PaaS, Kubernetes setup

---

> For additional documentation and navigation, see: https://docs.aegra.dev/llms.txt