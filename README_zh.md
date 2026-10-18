# 启动指南（中文）

从零把 aegra-api（后端）+ `web/`（前端）跑起来，并做出第一次「主管 → 领域子 agent」对话。

> 全部命令都在**仓库根目录**执行，除非注明 `cd web`。
> 环境：Windows / macOS / Linux 均可；Windows 相关坑见 [§8](#8-常见问题)。

---

## 1. 这个项目是什么

- **后端**：自托管的 Agent Protocol 服务（`src/aegra_api`），在 `aegra.json` 里注册图。
- **图**（`graphs/`）：
  - `supervisor` — 主 agent，负责理解意图、拆清单、把活派给领域子 agent；
  - `cbam_reporting` / `hybrid_bonding` / `sbol3_synbio` — 三个领域子 agent，各自绑定一个知识空间（Space），异步执行并主动回写结果。
- **知识空间**：`graphs/okf/<空间名>/`，每个空间是一份 OKF（Markdown + YAML frontmatter，可带 xlsx 表格）。
- **前端**（`web/`）：Vue 3 + Vite，左边会话、中间对话、右边面板（子任务实时进度 / 知识库文件树 / 定时任务）。

---

## 2. 前置条件

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | ≥ 3.12 | `pyproject.toml` 的 `requires-python` |
| [uv](https://docs.astral.sh/uv/) | 最新 | 依赖与虚拟环境 |
| PostgreSQL | 13+ | 持久化 thread/run/checkpoint，**必须有** |
| Node.js | ≥ 18（实测 24） | 只有前端需要 |

```bash
python --version && uv --version && node -v
psql --version   # 或确认 PG 已在 localhost:5432 监听
```

---

## 3. 配置 `.env`

```bash
cp .env_example .env
```

至少改这几项（其余可保持默认）：

```ini
# --- 数据库 ---
POSTGRES_USER=postgres
POSTGRES_PASSWORD=你的密码
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=aegra          # 库要先建好：createdb aegra

# --- 模型（OpenAI 兼容接口） ---
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com      # 用官方则可留空
OPENAI_MODEL=deepseek-flash

# --- 鉴权：两个密钥必须设置，否则启动就报错 ---
# 生成：python -c "import secrets;print(secrets.token_urlsafe(48))"
JWT_SECRET=换成长随机串
PASSWORD_PEPPER=换成长随机串

# --- 服务 ---
HOST=127.0.0.1
PORT=2026
AEGRA_CONFIG=aegra.json
```

> `PASSWORD_PEPPER` 一旦改动，**已注册的账号全部无法登录**（bcrypt 哈希不再匹配），需要重新注册或重置密码。

建库（若还没有）：

```bash
createdb -U postgres aegra
# 或：psql -U postgres -c "CREATE DATABASE aegra;"
```

---

## 4. 安装依赖 + 建表

```bash
uv sync                 # 装 Python 依赖（含 deepagents / langgraph / pandas / openpyxl）
uv run alembic upgrade head
```

`alembic upgrade head` 会建出 `thread` / `run` / `checkpoint*` / `space` / `user_tables` / `user_space` 等表。

---

## 5. 初始化账号与知识空间

`/custom/register` 只能注册**普通用户**，而「建空间 / 分配空间」需要超级用户。所以第一次要按下面顺序做：先注册账号，再用 SQL 提权并分配空间。

### 5.1 先起服务（见 §6），然后在另一个终端：

```bash
curl -X POST http://127.0.0.1:2026/custom/register \
  -H 'Content-Type: application/json' \
  -d '{"identity":"root","display_name":"root","password":"1234"}'
# → {"message":null,"user_identity":"root"}
```

### 5.2 提权 + 登记三个知识空间

```bash
psql -U postgres -d aegra <<'SQL'
-- 1) 超管（只有超管能建空间 / 分配空间）
update user_tables set is_super = true where identity = 'root';

-- 2) 登记知识空间（path 相对项目根，不是绝对路径）
insert into space(name, path) values
  ('cbam-reporting', 'graphs/okf/cbam-reporting'),
  ('hybrid-bonding', 'graphs/okf/hybrid-bonding'),
  ('sbol3-synbio',   'graphs/okf/sbol3-synbio')
on conflict (name) do nothing;

-- 3) 把空间分配给 root（子 agent 只能看到派给它的那一个）
insert into user_space(identity, space_name) values
  ('root','cbam-reporting'),
  ('root','hybrid-bonding'),
  ('root','sbol3-synbio')
on conflict do nothing;
SQL
```

### 5.3 验证

```bash
curl -s -X POST http://127.0.0.1:2026/custom/login \
  -H 'Content-Type: application/json' \
  -d '{"identity":"root","password":"1234"}'
# 拿到 access_token 后：
curl -s http://127.0.0.1:2026/custom/whoami -H "Authorization: Bearer <token>"
# → {"identity":"root", ..., "permissions":["read","write"]}

curl -s http://127.0.0.1:2026/custom/user/spaces -H "Authorization: Bearer <token>"
# → {"space":["cbam-reporting","hybrid-bonding","sbol3-synbio"], ...}
```

> 更省事的方式：直接用仓库自带的 `custom_test.py`（它会自己登录、跑一轮、等子 agent 回写）：
> ```bash
> uv run python custom_test.py "CBAM 默认排放因子里铝的因子是多少？"
> ```

---

## 6. 启动后端

```bash
# 指定 PYTHONPATH：custom/ 是仓库根的本地包，脚本方式启动时不在 sys.path 里
PYTHONPATH=. uv run python -u src/aegra_api/main.py
```

看到这两行就算成功：

```
Custom app loaded successfully
Uvicorn running on http://127.0.0.1:2026
```

验证：

```bash
curl -s http://127.0.0.1:2026/health
# {"status":"healthy","database":"connected","langgraph_checkpointer":"connected","langgraph_store":"connected"}
```

开发时想自动重载：

```bash
PYTHONPATH=. uv run uvicorn aegra_api.main:app --port 2026 --reload \
  --loop "asyncio:SelectorEventLoop"
```

> Windows 必须显式 `--loop "asyncio:SelectorEventLoop"`（psycopg/asyncpg 不兼容默认的 Proactor 循环）；
> `src/aegra_api/main.py` 里已经处理好了，所以直接用它最省心。

> **改了 `aegra.json` 一定要重启**：图注册表在启动时读取一次。

---

## 7. 启动前端

```bash
cd web
npm install
npm run dev          # http://localhost:5273
```

- 默认连 `http://127.0.0.1:2026`，要改就 `cp .env.example .env` 后编辑 `VITE_API_BASE`。
- 用 `root / 1234` 登录。
- 后端已开 CORS（`Access-Control-Allow-Origin: *`），前后端不同端口不需要代理配置。

不想开浏览器、只想验证接口契约：

```bash
cd web && node scripts/smoke.mjs      # 20 个断言：登录→建会话→流式→追问 resume→任务代理→定时任务→删会话
```

---

## 8. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `ModuleNotFoundError: No module named 'custom'` | 用脚本方式启动时没带 `PYTHONPATH=.`。加上，或改用 `uvicorn aegra_api.main:app` |
| `KeyError: 'JWT_SECRET'` / `'PASSWORD_PEPPER'` | `.env` 缺这两个键，补齐后重启 |
| `RuntimeError: Database not initialized` | 用了 `get_session_maker()` 但没走服务生命周期；只在服务内可用 |
| `psycopg_pool.PoolTimeout: couldn't get a connection` | PG 没起 / 连接数打满 / 密码错。先 `psql` 手动连一次确认 |
| 登录 401「账号或密码错误」 | 账号没注册，或 `PASSWORD_PEPPER` 被改过 |
| `用户没有空间 'xxx' 的权限，无法启动该子 agent` | 该账号没有这个 Space 的 `user_space` 行；按 §5.2 分配 |
| 建空间接口 403「只有超级用户可以创建微服务空间」 | 账号 `is_super=false`；按 §5.2 提权 |
| 前端登录后一片空白 | 后端没起 / `VITE_API_BASE` 指错；浏览器控制台看 `Failed to fetch` |
| 改了 `graphs/` 下的图或 `aegra.json` 但没生效 | 后端启动时只读一次注册表，**重启服务** |
| 子 agent 一直显示 `running` | 父 state 里的状态是快照（只在 check 时更新）。看真实状态用 `GET /custom/tasks/{id}` |
| 端口 2026 被占 | `netstat -ano \| grep :2026` 找到 PID 后结束，或改 `.env` 的 `PORT`（前端同步改 `VITE_API_BASE`） |

---

## 9. 一次完整对话长什么样

1. 在中间输入框问：「CBAM 默认排放因子里铝的因子是多少？」
2. 主管先 `write_todos` 拆任务 → `start_async_task` 派给 `cbam_reporting` → 回你一句「已派给 CBAM 领域 agent（task_id: …）」。
3. 右侧「任务」面板出现一张卡：状态从 `running` → `success`，卡片里能看到子 agent 的实时输出与工具步骤（`xlsx_info` / `xlsx_query`）。
4. 子 agent 跑完会**主动回写**父线程（`CompletionNotifierMiddleware`），主管被唤醒并把结果转达给你——所以不用手动催「跑完了吗」。

## 10. 父子 agent 之间的信息流通

父（主管）与子（领域 agent）跑在**两个不同的 Thread** 上，记忆互不相通。它们之间只有下面 5 条通道，没有任何其它隐式共享。

```
┌─────────────────────── 主管 Thread（父，supervisor）───────────────────────┐
│  state.messages[]        对话历史（子 agent 看不到）                          │
│  state.async_tasks{}     task_id → {agent_name, thread_id, run_id, status}   │
│  工具：start / check / update / cancel / list_async_task、write_todos        │
└───┬──────────────────────────────────────────────────────────▲─────────────┘
    │ ① 任务描述（消息，单向给出）                                │ ③ 完成回写（消息，单向推回）
    │  runs.create(thread_id=子线程,                              │  runs.create(thread_id=父线程,
    │    input={messages:[{role:user, content:description}]})     │    input={messages:[{role:user, content:
    │  ❗只传这段 description，父对话历史一概不传                    │    "[Async subagent 'X' has completed] Result: …"}]})
    │                                                            │  结果取子 agent 最后一条消息，截断 500 字符
    │ ② config.configurable（元数据，不进消息）                   │  身份：现铸父身份短时 token（300s）
    │  user_spaces = [ 本任务的那一个 space ]                      │
    │  parent_thread_id / parent_assistant_id                     │ ④ 拉取（工具调用，非推送）
    │  parent_user_identity / notify_url                          │  check_async_task → runs.get → status + result
    │  ❗服务端注入的 user_spaces 收窄成单空间，越权由后端拒绝         │  顺带把 state.async_tasks 改成真实状态
    ▼                                                            │
┌─────────────────────── 子 Thread（cbam_reporting 等）──────────────────────┐
│  自己的 messages[]、自己的 checkpoint、自己的 space 可见范围                 │
│  收尾钩子：CompletionNotifierMiddleware.aafter_agent → 触发 ③                │
│  出错时：awrap_model_call 推一条 "… encountered an error" 同样走 ③          │
└──────────────────────────────┬────────────────────────────────────────────┘
                               │ ⑤ 实时旁路（前端直连，不经过父 agent）
                               ▼
         GET /custom/tasks/{task_id}/stream?parent_thread_id=…
         服务 token 订阅子 run 的 messages/custom → 转成 SSE 给浏览器
```

| # | 方向 | 载体 | 谁写 | 代码位置 | 失败时 |
|---|---|---|---|---|---|
| ① | 父 → 子 | 子线程第一条 `user` 消息 | 拦截层 | `graphs/async_task_guard.py::_start_sync/_astart` | 工具返回 `Failed to launch…` |
| ② | 父 → 子 | `config.configurable` | 拦截层 | 同上 `_config_for()` | 无权限时直接拒绝派活 |
| ③ | 子 → 父 | 父线程里一条 `user` 消息 | 子 agent 的中间件 | `custom/middlewares/completion_notifier.py` | 缺父上下文 → 静默跳过并告警 |
| ④ | 子 → 父 | 工具返回值 + state 更新 | 主管主动调用 | `deepagents/…/async_subagents.py::_build_check_result` | 返回 `Failed to get run status` |
| ⑤ | 子 → 前端 | SSE 帧 | 代理接口 | `custom/frontend_api.py::stream_task` | 发 `error` 帧后 `end` |

`update_async_task(task_id, message)` 是 ① 的复用：往**同一个**子线程追加一条 `user` 消息并开新 run（`multitask_strategy="interrupt"` 顶掉旧 run）。

### 明确**不**流通的东西

- **父对话历史**：子 agent 只拿到 `description`，看不到用户之前问了什么。所以派活时要把路径、问题、约束一次写清。
- **子 agent 的 token 流 / 工具事件**：不会回到父的 SSE。想在界面上看，只能走 ⑤ 的代理。
- **其它任务的信息**：子 agent 只知道自己的 space，`GET /custom/tasks` 也只放行本父线程下的 task。
- **权限的外溢**：② 里下发的是 `[本任务 space]` 而不是用户全部空间；子 agent 的 `SpaceBackend` 和 xlsx 工具都按这个列表判定。
- **记忆的延续**：子 agent 的 checkpoint 在自己的 thread 上；父线程只存 task 句柄，不存子 agent 的中间状态。

---

更多接口与流式细节见 [`docs/frontend-api.md`](docs/frontend-api.md)、[`docs/streaming.md`](docs/streaming.md)；
术语见 [`CONTEXT.md`](CONTEXT.md)。
