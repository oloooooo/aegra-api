# aegra-api 作为 Deep Agents async subagent 的 ACP 服务

为 Deep Agents 的 async subagent 提供 Agent Protocol（ACP）服务面（术语见 [`../CONTEXT.md`](../CONTEXT.md)）。**本文是需求与设计文档。状态：§7 五项改动已全部实现并验证（2026-10-11）**，详见 §7 表内标注与 §10 测试结果。

- 旧版需求文档属于已废弃的独立包 `aegra-protocol`（该仓库已不存在）；本文取代其地位。
- 术语以 [`../CONTEXT.md`](../CONTEXT.md) 为准；决策记录见 [`./adr/`](./adr/)。
- 已核实版本：deepagents 0.7.23、langgraph 1.2.14、langgraph-sdk 0.4.6、langchain 1.4.4、agent-protocol 0.1.6。

## 1. 目标与非目标

**目标**

1. deepagents 官方 `AsyncSubAgentMiddleware` 的五个工具（start / check / update / cancel / list）能直接指向 aegra-api，不需要任何适配层或独立服务。
2. 子 agent 的图用 `aegra.json` 的 `graphs` 注册，与 aegra-api 既有部署方式一致：一个进程服务多个图，用 graph 名（deepagents 发的 `assistant_id`）路由。
3. 取消必须真的停止执行，且不被随后完成的写入复活。
4. `multitask_strategy="interrupt"`（`update_async_task`）必须真的生效：杀掉旧 run、保留线程历史、新建 run。
5. 状态词表与 agent-protocol 规范和 deepagents 客户端一致：服务端 `Run.status` 不引入 `cancelled`。

**非目标（本期）**

- 不新增端点；不改流式（SSE / commands）；不碰 crons / store / 多实例 worker 语义。
- 不实现 `action=rollback`（维持 422）。
- 不做鉴权（aegra-api 默认 noop；自定义 auth 与 `AsyncSubAgent.headers` 的组合不在本期验证范围）。
- 不解决"deepagents 无法续跑 HITL 子 agent"（客户端能力所限，见 §9）。
- 不维护独立包 `aegra-protocol`（保留不动，见 ADR-0001）。

## 2. 事实依据

### 2.1 客户端到底打哪些请求

deepagents 0.7.23 经 `langgraph_sdk` 0.4.6 只发五类请求（`deepagents/middleware/async_subagents.py`）：

| 工具 | 请求 | 客户端读什么 |
|---|---|---|
| `start_async_task` | `POST /threads`（body `{}`）→ `POST /threads/{tid}/runs` | `thread["thread_id"]`、`run["run_id"]` |
| `check_async_task` | `GET /threads/{tid}/runs/{rid}`；仅当 status 为 `success` 再 `GET /threads/{tid}` | run `status`；失败时 `run["error"]`；成功时 `values.messages[-1].content` |
| `update_async_task` | `POST /threads/{tid}/runs`（带 `multitask_strategy:"interrupt"`） | `run["run_id"]` |
| `cancel_async_task` | `POST /threads/{tid}/runs/{rid}/cancel?wait=0&action=interrupt`（空 body） | **不读响应、不轮询**，随后把本地 task 记为 `cancelled` |
| `list_async_tasks` | 对非终态任务逐个 `GET /threads/{tid}/runs/{rid}` | run `status` |

`POST /threads/{tid}/runs` 的实际 body（SDK 只剔除 `None`，`false` 会保留）：

```json
{
  "input": {"messages": [{"role": "user", "content": "..."}]},
  "stream_mode": "values",
  "stream_subgraphs": false,
  "stream_resumable": false,
  "assistant_id": "<graph_id>"
}
```

客户端的终态集合是 `{cancelled, success, error, timeout, interrupted}`（`async_subagents.py` 的 `_TERMINAL_STATUSES`），其余一切值都当"还在跑"。注意 `cancelled` 是客户端**自己**写的本地 task 状态，服务端的 `Run.status` 枚举里没有它（见 2.2）。

### 2.2 状态/动作词表的三方对照

| | agent-protocol 0.1.6 规范 | 官方 Agent Server（生产） | langgraph_sdk 0.4.6 |
|---|---|---|---|
| `Run.status` | `pending, error, success, timeout, interrupted` | 含 `running`，仍无 `cancelled` | 同生产 |
| cancel 动作 | `{interrupt, rollback}`，默认 `interrupt` | 同规范 | `runs.cancel(action="interrupt")` 为默认，且**总是显式发送** |
| `Thread` 响应 | 含 `values` | 同规范 | `Thread.values`、`Thread.interrupts` |

规范的 cancel 语义原文：*"`interrupt` will simply cancel the run. `rollback` will cancel the run and delete the run and associated checkpoints afterwards."* `cancelled` 在规范里只出现在散文里，不在任何枚举中。

**本服务的词表**：终态 `success | error | timeout | interrupted`，活动态 `pending | running`；取消与 HITL 中断都落 `interrupted`，用非规范字段 `interrupted_by: "cancel" | "hitl" | null` 标明归因。不引入 `cancelled`。

### 2.3 aegra-api 现状（已核实）

- 五个端点全部存在，路径与 deepagents 实发一致。
- **`assistant_id` 已能直连 graph**：启动时 `LangGraphService._ensure_default_assistants()` 为 `aegra.json` 每个 graph 建一个 `user_id="system"` 的默认 assistant（id 为 `uuid5(NAMESPACE, graph_id)`）；`resolve_assistant_id()` 用同一个 uuid5。deepagents 发的 `assistant_id=<graph_id>` 无需预注册即可命中。
- **取消落点正确**：本地模式 `run_executor` 捕获 `CancelledError` → `finalize_run(status="interrupted", thread_status="idle")`；worker 模式 `_cleanup_cancelled_execution()` → 同样的 finalize；无主 run（租约过期/无 owner）由 API 侧 `interrupt_unowned_run()` 直接落 `interrupted` + thread `idle`。三条路径都带活动态守卫（`WHERE status IN ('pending','running')`），先写者赢。
- **HITL 落点正确**：`run_executor` 在 `final_output.has_interrupt` 时落 `interrupted` + thread `interrupted`。
- **`action=cancel` 与 `action=interrupt` 当前行为完全相同**：`broker.request_cancel()` 忽略 action、直接 `task.cancel()`；redis `_execute_cancel()` 同样忽略 action。aegra 文档里"interrupt 是协作式中断、图能拦截保存中间状态"的说法**代码并未实现**，本文以代码为准。
- **`multitask_strategy` 被静默忽略**：`RunCreate` 接收该字段并存进 `RunJob.behavior`，但执行层没有任何消费。
- **三处 wire 形状缺口**：`GET /threads/{tid}` 无 `values`（只有线程元数据）；`Run` 响应只有 `error_message`、无 `error`；`Run` 无 `interrupted_by`。

### 2.4 子 agent 结果投递（task delivery）

> 背景：子 agent 跑完后要把结果送回 supervisor 的 thread，让它不必被问就汇报。**已接线**，
> 当前实现是两条路：主管 run 的首尾由拦截层写进自己的 state；主管已空闲时由子 agent 中间件开新 run 回写。

**为什么不是一条路**：LangGraph 没有向运行中的图投递消息的 API；从外部能做的只有读状态、
杀 run（`runs/cancel`）、另开 run。所以 run 中途想拿结果只能由模型自己 `check_async_task`，
而「主管早已答完」只能靠另开一个 run 叫醒。

**投递规则**（`graphs/async_task_guard.py::abefore_agent/aafter_agent` + `custom/middlewares/completion_notifier.py`）：

| 父线程当时 | 谁投递 | 行为 |
|---|---|---|
| `busy` / 本次 run 内 | 主管侧 guard（`before_agent` / `after_agent`，run 首尾各一次） | 写一条带 `task_id:` 标记的消息进自己的 state；不打断、不开新 run |
| run 中途 | 模型自己 | `check_async_task`——它返回**全文**，且 guard 会记下 `delivered_run_id`，之后不再重复投 |
| `interrupted` | 谁都不动 | 硬 resume 会把用户正等着回答的答案顶掉（同 §9 #1）；用户答完后下一次 run 的 hook 自动补上 |
| `idle` / `error` | `CompletionNotifier` | 以父身份开新 run 投递，**只试一次**（不重试、不延迟复查） |

**去重**：两条路的消息都以 `[Async subagent '<name>' …] Result: …` 开头（前端 `toView`
靠此前缀识别成「子 agent 返回」），结尾带 `task_id: <id>` 与 `run_id: <id>`。

认领表负责两路互斥：`async_task_delivery(task_id, run_id)`（主键即判重键），
接口 `POST /custom/tasks/{task_id}/deliver-claim` 抢认领，`DELETE` 撤回
（`custom/delivery_api.py`）。只有认领成功的一方才投递；投递失败会撤回认领。
判重键含 `run_id`，因为 `update_async_task` 会给同一任务开新 run。
同一路内（hook 与 hook、notifier 与 notifier）的判重靠 state 里的 `delivered_run_id` 字段，
**不靠扫消息**——`check_async_task` 返回的是 `ToolMessage(json)`，扫不到会被当成未投递再投一遍。

**成本**：每次 run 只在首尾两次核对非终态任务的真实状态（无 TTL 缓存，任务终态后不再查）；
run 中途不轮询。投递判重是一次主键查询（认领表），不读 thread state；notifier 读一次
`GET /threads/{tid}` 看状态，`interrupted` 分支已删，不再读快照。

**摘要与全文**：投递消息只带前 500 字摘要，超长时追加 `…[结果已截断…调 check_async_task 取全文]`
（`async_task_guard._shorten` / `completion_notifier._shorten` 同一口径）。全文只有
`check_async_task` 能取（deepagents 侧不截断）。

**并发与幂等**（详见 [adr/0010](./adr/0010-task-delivery-idempotency.md)）：

- 同一轮里多个任务同时终态 → 一次 `after_agent` 合并成一条 state 更新，天然幂等。
- 多个子 agent 同时收尾而主管**正忙** → notifier 看到 `busy` 直接返回，不认领、不生成 run。
- 多个子 agent 同时收尾而主管**空闲** → 第一个抢到认领并开 run；8 秒内后到的拿到
  `in_flight` 让路（同一时刻最多一个投递 run）。
- 同一任务被两个 hook 投递 → 第二个看到 `delivered_run_id`，只投一次。
- `runs.create` 没有幂等键：创建失败会先撤回认领，不会留下"已认领但没投"的死行。

**仍存在的空隙**：

- 子 agent 恰好在主管生成最终答案的那一刻完成时，`after_agent` 的那条消息模型没机会转述，
  用户看到「答案 + 一张结果卡片」；想让它开口只能另开一轮 run。
- notifier 只试一次：父线程 `busy` 时它直接放弃，这期间完成的结果要等用户再说一句话
  （下一次 run 的 `before_agent`）才会出现。

以下为接线前核实过的事实，仍然适用：

- **stock 派活不传父上下文**：`deepagents/middleware/async_subagents.py` 的四处 `runs.create`（264、304、499、538 行）只发 `thread_id` / `assistant_id` / `input`（update 路径多一个 `multitask_strategy="interrupt"`），全包 grep `parent_thread_id` / `parent_assistant_id` 零命中。父 ID 必须由 supervisor 侧显式带过去。
- **aegra 照单接收 `config.configurable`**：`RunCreate.config`（`models/runs.py:46`）→ `run_preparation.py:328` 合并进 `assistant.config` → `create_run_config()`（`langgraph_service.py:717`）交给图；只强制覆盖 `thread_id` / `run_id`（同文件 721-722 行），其余键透传。所以 `parent_thread_id` 能原样到达子 agent 的**图工厂**。
- **aegra 不往 `configurable` 注入 `assistant_id`**：只注入 `thread_id`、`run_id`、`user_id`、`user_display_name`、`langgraph_auth_user`（`langgraph_service.py:721-722`、684）。父的 assistant_id 只能静态取（用 graph key `"supervisor"`，aegra 会 uuid5 解析，`utils/assistants.py:23`）。
- **工具能拿到父 thread_id**：`ToolRuntime.config` 就是该 run 的 `RunnableConfig`（`langchain/agents/middleware/types.py` 构造处），自建派活工具可直接读 `runtime.config["configurable"]["thread_id"]`，无需外部传参。
- **工厂图这条路通**：1 参数 `config: RunnableConfig` 工厂（`graph_factory.py:209`）与 async contextmanager 返回值（`generate_graph`，`graph_factory.py:394`）aegra 都支持，按 run 重建，不缓存工厂图（`_base_graph_cache` 只缓存静态图，`langgraph_service.py:300`）。已用探针验证 `@asynccontextmanager async def graph(config: RunnableConfig)` 被 `classify_factory` 判为 factory。
- **传输**：`get_client()` 不传 url 走进程内 ASGI（需服务端注册，aegra 未见注册）；传 url 走真 HTTP。`langgraph_sdk` 0.4.6 的 `runs.create` 接受 `config` 与 `context` 关键字（`_async/runs.py:364`）。
- **无并发保护**（同 §9 #4）：通知 run 会与父 run 并发写同一 thread，aegra 不拦。`POST /threads/{tid}` 的 `GET /threads/{tid}` 响应含 `status`（`idle` / `busy` / `interrupted` / `error`，`models/threads.py:98`），可作为发通知前的忙闲判据。
- **图文件**（方案变化过，以当前 `aegra.json` 为准）：现在只注册 `supervisor`（`./graphs/deepagents_nodes.py:graph`）与 `domain_agent`（`./graphs/domain_agents.py:graph`）——两个都是工厂图：主管每次 run 按自己拥有的 spaces 现算子 agent 名单，子 agent 按 run config 里的 space 现读 `graphs/okf/<space>/AGENT.md`（ADR-0011）。旧的三张领域图与 `_DOMAIN_SPECS` 已删；`graphs/xlsx_process.py` 及备份 `graphs/xlsx_process copy.py` 也不再是活图。

## 3. 端点契约

> 只列与 deepagents 契约相关的行为；未列出的字段与端点保持现状。

### `POST /threads`

body 可为 `{}`（`ThreadCreate` 全字段可选）。响应含 `thread_id`，deepagents 只读它。现状已满足。

### `POST /threads/{tid}/runs`

deepagents 实发 `input`、`assistant_id`、`stream_mode`、`stream_subgraphs`、`stream_resumable`（最后一个目前被 Pydantic 忽略，属预期）。

| 字段 | 语义 |
|---|---|
| `input` | 图输入，形如 `{"messages": [{role, content}]}` |
| `assistant_id` | graph_id；经 uuid5 命中系统默认 assistant，也接受显式注册的 assistant |
| `multitask_strategy` | `interrupt` → 对本线程活动 run 执行 Supersede（§6）；其余值维持现状（收下不执行） |

响应（`Run`）：现有字段 + **新增 `error`**（与 `error_message` 并存，客户端读 `error`）+ **新增 `interrupted_by`**。

### `GET /threads/{tid}/runs/{rid}`

返回同一个 `Run`。`status` 取值见 §2.2。

### `GET /threads/{tid}`

**新增 `values`**：取自该线程最新 checkpoint。这是 deepagents 取结果的唯一入口——`check_async_task` 在 run 为 `success` 后固定读 `values.messages[-1].content`；现状只返回 `thread_id / status / metadata / user_id / created_at / updated_at`，SDK 读到空。`interrupts` 本期不补（deepagents 不读；LangGraph SDK 的 `Thread` 是 TypedDict，不做运行时校验）。

**响应模型**（`src/aegra_api/models/threads.py`）：`Thread` 增加 `values: dict[str, Any] = Field(default_factory=dict)`；`_serialize_thread()` 增加可选 `values` 参数，**仅 `get_thread` 传入**。list / search / POST / PATCH 不读 checkpoint、`values` 保持 `{}`，不给这些端点加额外开销。

**`get_thread` 算法**（`src/aegra_api/api/threads.py`）：

1. 现有 thread 查询与 auth 不变；
2. 从 `thread.metadata_json` 取 `graph_id`；为空 → `values = {}`（不报错）；
3. 非空 → `get_langgraph_service()` + `create_thread_config(thread_id, user)` + `access_context="threads.read"` 打开图，`agent.aget_state(config)`；
4. 有 snapshot → `values = snapshot.values`（与 `GET /threads/{tid}/state` 同一来源；序列化交给 Pydantic，LangChain 消息会带 `content` / `type`）；
5. 无 snapshot（尚无 checkpoint）→ `values = {}`；
6. 读取失败（图加载 / checkpoint 异常）→ 照常 5xx；deepagents 会吞掉异常并退化为占位文案。

**注意**：不要复用 `/state` 的 404 分支（`"No state found for thread"`）——那是 `/state` 的契约，`GET /threads/{tid}` 必须 200。

**代价**：每次 `GET /threads/{tid}` 多一次图加载 + checkpoint 读（factory 图会重建）；deepagents 只在 run `success` 后读一次，可接受。

**施工后要重跑**：threads 相关 e2e——响应多一个字段，若有用例断言精确键集合需同步。

#### `check_async_task` 的结果取用路径（为什么可能没有内容）

deepagents 的结果分两步取：`GET /runs/{rid}` 拿状态，**仅当 `success`** 再 `GET /threads/{tid}` 拿 `values.messages[-1].content`。因此"没有内容"只可能出在：

| 场景 | 客户端表现 | 归属 |
|---|---|---|
| 线程响应里没有 `values`（**本期修复前的现状**） | `"(completed with no output messages)"` | 服务端，本期修复 #1 |
| 线程尚无 graph_id / 尚无 checkpoint | 同上 | 服务端：`values` 要返回 `{}`（200），不能 404 |
| `values` 里没有 `messages` 键（非 Deep Agent 图） | 同上 | 图约定：子 agent 状态需有 `messages` |
| run 状态不是 `success`（cancel / HITL / Supersede / error / timeout） | 只有 `status`，**没有 result 键** | 客户端设计，不可修 |
| 最后一条消息 `content` 为空（如只有 tool_calls 的 AIMessage） | `result: ""` | 客户端设计，不可修 |
| `content` 是内容块列表 | `result` 原样为列表（不转字符串） | 客户端设计，文档说明 |
| `check` 与 `update` 并发：`runs.get` 与 `threads.get` 之间新 run 已开跑 | 可能返回新 run 起始的**用户消息**作为结果 | 客户端竞态，已知限制 |

**已排除**：checkpoint 落盘竞态。`durability="async"` 只是不让 checkpoint 阻塞下一个超级步——图流结束前 `AsyncBackgroundExecutor.__aexit__` 会 `await asyncio.wait(tasks)` 等完所有未完成的 checkpoint 写入（langgraph 1.2.14 `pregel/_executor.py`），而 aegra 是在流被完整消费之后才 `finalize_run(success)`。因此不存在"run 已成功、线程 values 还是旧的"。

### `POST /threads/{tid}/runs/{rid}/cancel`

Query：`wait: 0|1`（默认 0）、`action: interrupt|cancel`（**默认改为 `interrupt`**，与规范/SDK 对齐；两个动作当前行为相同）。`rollback` 维持 422（`Literal` 校验自动产生）。已终态 run 是幂等 no-op，仍返回当前 `Run`（客户端不看响应）。

## 4. 数据模型变更

```
runs 表新增一列（需要 alembic 迁移）：
  interrupted_by text null   -- 'cancel' | 'hitl' | null
```

- 不新增其他列；`values` 只来自 checkpoint，不落快照列。
- 归因写入点（都经 `finalize_run` 或 `interrupt_unowned_run`）：

| 场景 | `interrupted_by` |
|---|---|
| 显式取消（本地 / worker / API reconcile） | `"cancel"` |
| Supersede 杀旧 run（复用取消路径） | `"cancel"` |
| HITL 暂停（`has_interrupt`） | `"hitl"` |
| 正常终态（success / error / timeout） | `null` |

## 5. 状态机

```
pending ──(执行器开始)──> running
running/pending ──图正常结束────────> success         thread → idle
running/pending ──抛异常───────────> error           thread → error
running/pending ──图 HITL 暂停──────> interrupted     thread → interrupted  interrupted_by=hitl
running/pending ──客户端 cancel─────> interrupted     thread → idle         interrupted_by=cancel
running/pending ──被新 run Supersede─> interrupted     thread → busy（新 run）interrupted_by=cancel
```

所有终态写入都必须带"仅活动态"守卫，先写者赢。

## 6. 取消与 Supersede 时序

取消（照 aegra 现有顺序，DB 先行）：

```
client ──POST .../cancel?action=interrupt──> API
  API: 无主 run → 事务内 UPDATE runs SET status='interrupted', interrupted_by='cancel'
                                    WHERE status IN ('pending','running') AND 无有效租约
                  → thread 归 idle
      有主 run → broker 信号 → worker/本地 executor: task.cancel()
                  → CancelledError → finalize_run(interrupted / idle, interrupted_by='cancel')
  迟到完成写入被守卫拒绝
```

Supersede（`multitask_strategy="interrupt"`，**在创建新 run 之前**）：

1. 查出该线程 `pending | running` 的活动 run；
2. 对每个活动 run 走一次标准取消（无主 → DB 落库；有主 → 信号 + 执行器落库）；
3. checkpoint / 线程历史**不动**；
4. 再照常建新 run（`set_thread_status(busy)`、提交执行）。

## 7. 本期改动清单

| # | 改动 | 原因 | 状态 |
|---|---|---|---|
| 1 | `GET /threads/{tid}` 补 `values` | deepagents 读结果 | ✅ 已实现（`_serialize_thread(values=...)` + `_load_thread_values`；无 graph_id/无 checkpoint → `{}` 200，读取失败 → 5xx） |
| 2 | `Run` 补 `error`（与 `error_message` 并存） | deepagents 读错误 | ✅ 已实现（`Run._mirror_error_message` 每次响应镜像） |
| 3 | `Run` 增 `interrupted_by`（列 + 上述写入点） | 区分取消 vs HITL | ✅ 已实现（迁移 `c4d5e6f7a8b9`；cancel/Supersede → `cancel`，HITL → `hitl`，正常终态 → null） |
| 4 | 实现 `multitask_strategy="interrupt"` Supersede | `update_async_task` 的硬要求 | ✅ 已实现（`run_preparation._supersede_active_runs`：建新 run 前走标准取消，历史不动） |
| 5 | cancel 默认 action 改 `interrupt` | 规范/SDK 对齐（零行为变化） | ✅ 已实现（单 run cancel 端点默认值；bulk 端点本就是 `interrupt`） |

**已满足、不必改**：五个端点与路径、`assistant_id=graph_id` 直连、取消落 `interrupted` + 活动态守卫、HITL 落 `interrupted` + thread `interrupted`、`rollback` 已自动 422。

## 8. aegra.json 与图定义

服务端只认 `aegra.json`（与 aegra-api 既有约定一致）；deepagents supervisor 的配置在**调用方**（`AsyncSubAgent`）。

```json
{
  "dependencies": ["."],
  "graphs": {
    "researcher": "./graphs/researcher.py:graph",
    "supervisor": "./graphs/supervisor.py:graph"
  }
}
```

```python
# graphs/researcher.py —— 子 agent。不要在编译期自带 checkpointer：aegra 每请求注入。
from deepagents import create_deep_agent

graph = create_deep_agent(
    model="openai:gpt-5.5",
    system_prompt="You are a thorough research agent.",
)
```

```python
# 调用方（supervisor）侧：graph_id 必须与 aegra.json 的键一致。
from deepagents import AsyncSubAgent, create_deep_agent

supervisor = create_deep_agent(
    model="openai:gpt-5.5",
    subagents=[
        AsyncSubAgent(
            name="researcher",
            description="Research agent for information gathering and synthesis",
            graph_id="researcher",              # aegra.json 的键
            url="http://localhost:2026",        # aegra-api
        )
    ],
)
```

要点：`url` 指向 aegra-api；`graph_id` 是 `aegra.json` 的键；子 agent 的记忆（thread + checkpoint）落在 aegra-api 的 PostgreSQL 里，与 supervisor 的记忆按 thread 隔离。

## 9. 已知限制

1. **HITL 子 agent 不能被 deepagents 续跑**：deepagents 没有 resume 工具，`update_async_task` 只会发新 input。而 LangGraph 1.2.14 的 `interrupt()` 只有 `Command(resume=...)` 才续跑；纯 input 打在被中断的线程上会让中断节点**从头重执行**，无 resume 值则再次中断——不续跑，且会重复一次模型调用。**子 agent 图不要配 `interrupt_on`**；需要人工审批的环节放在 supervisor 自己的图里。
2. `action=rollback` 未实现（422）。
3. 鉴权不在本期范围（默认 noop）。
4. `multitask_strategy` 的 `reject` / `enqueue` / `rollback` 仍被忽略（同线程并发 run 可并存）。因此投递不会用 `multitask_strategy` 抢占，而是按父线程状态分派（见 §2.4）。
5. `GET /threads/{tid}` 不返回 `interrupts`。
6. 独立包 `aegra-protocol` 与根 `docs/` 保留不动，仅作历史参照。
7. **任务投递的三个边界**（详见 [adr/0010](./adr/0010-task-delivery-idempotency.md#consequences)）：
   同线程 8 秒 `in_flight` 窗口内后到的任务不开 run，改由下次 run 的 hook 补上（延迟一个 step，不丢）；
   认领行随任务数线性增长且当前没有清理任务（按 `claimed_at` 定期删旧行即可）；
   认领后崩溃时该结果 15 秒内不投（过期后允许重投）；notifier 只试一次，父线程 `busy`
   期间完成的结果要等用户再说一句话才出现。两个时间参数见
   [adr/0010 §两个时间参数](./adr/0010-task-delivery-idempotency.md#两个时间参数)。

## 10. 测试清单（e2e，真 PG）

> **状态：全部通过（2026-10-11）**。local executor 与 Redis worker（`REDIS_BROKER_ENABLED=true`）两种模式下均验证；#11 用真实 deepagents 0.7.23 + supervisor 图联测（start / check / cancel 三轮对话，Supersede 由 #4 的服务端 e2e 覆盖）。唯一注意：HITL 用例依赖模型主动发起 tool_calls，非 OpenAI 系模型需用含明确地点的提示词（已修入用例）。

1. 全生命周期：`POST /threads` → `POST runs`（`assistant_id=graph_id`）→ 轮询 `GET run` 到 `success` → `GET /threads/{tid}` 拿到 `values.messages[-1].content`。
2. 客户端契约：按 §2.1 的真实请求形状打一遍（含多发的 `stream_mode` / `stream_resumable`、空 body 的 cancel、body `{}` 的 thread create）。
3. **取消不可复活**：慢 agent + cancel → run 落 `interrupted`，等原任务结束后再查仍为 `interrupted`。
4. **Supersede**：先起慢 run，再带 `multitask_strategy=interrupt` 起新 run → 旧 run 落 `interrupted`、线程历史保留、新 run 拿到新 `run_id`。
5. **error 键**：失败 run 的响应里 `error` 与 `error_message` 同时存在且非空。
6. **interrupted_by 三态**：cancel → `"cancel"`；HITL → `"hitl"`；正常 → `null`。
7. **rollback**：`action=rollback` → 422，且 run 状态不受影响。
8. 幂等：对已终态 run 再取消返回当前状态、不报错。
9. **graph_id 直连**：只用 aegra.json 的 graph id 作 `assistant_id`、不预注册 assistant，可跑通。
10. **无 checkpoint 的线程**：`GET /threads/{tid}` 返回 200 且 `values: {}`（不是 404），保证 deepagents 得到占位文案而不是异常。
11. 真 deepagents 联测（可选，需 LLM key）：`AsyncSubAgentMiddleware` 五个工具对真实服务跑一遍。

### 本项目自定义部分的验证（2026-10-19，Redis / 无 Redis 两种模式均过）

**离线自检**（不需要 DB/LLM，改代码后先跑这两个）：

```bash
PYTHONPATH=. PYTHONUTF8=1 uv run python scripts/check_task_delivery.py   # 投递/判重/认领 29 项
PYTHONPATH=. PYTHONUTF8=1 uv run python scripts/check_thread_quota.py     # 配额与级联顺序 14 项
```

**真服务 e2e**：

- **结果投递**（§2.4）：主管 run 首尾写 state、空闲时 notifier 开新 run；两种模式下都观察到
  `[Async subagent '<space>' has completed]` 带 `task_id:` / `run_id:` 的 `human` 消息落地。
- **会话配额**（ADR-0012）：第 N+1 个 `POST /threads` 返 403 且文案可读；删一个后能再建；
  超管不受限；子线程不计入配额。
- **级联删除**：删主管会话 → 它的子线程（含 checkpoint）一并消失；别人删我的会话返 404。
- **子线程归属**：真跑一轮 supervisor，子线程 `user_id=subagent_service`，
  metadata 带 `owner_identity` / `parent_thread_id`。
