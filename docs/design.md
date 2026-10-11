# aegra-api 作为 Deep Agents async subagent 的 ACP 服务

为 Deep Agents 的 async subagent 提供 Agent Protocol（ACP）服务面（术语见 [`../CONTEXT.md`](../CONTEXT.md)）。**本文是需求与设计文档：先定稿，再据此改 `aegra-api` 代码。**

- 旧版需求文档（已废弃的独立包 `aegra-protocol`）在仓库根 [`../../docs/`](../../docs/)；本文取代其地位，旧文档不再更新。
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

**新增 `values`**：取自该线程最新 checkpoint，序列化复用 `GET /threads/{tid}/state` 的路径（`ThreadStateService`）；无 graph_id / 无 checkpoint 时返回 `{}`。`interrupts` 本期不补（deepagents 不读；LangGraph SDK 的 `Thread` 是 TypedDict，不做运行时校验）。

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

| # | 改动 | 原因 |
|---|---|---|
| 1 | `GET /threads/{tid}` 补 `values` | deepagents 读结果 |
| 2 | `Run` 补 `error`（与 `error_message` 并存） | deepagents 读错误 |
| 3 | `Run` 增 `interrupted_by`（列 + 上述写入点） | 区分取消 vs HITL |
| 4 | 实现 `multitask_strategy="interrupt"` Supersede | `update_async_task` 的硬要求 |
| 5 | cancel 默认 action 改 `interrupt` | 规范/SDK 对齐（零行为变化） |

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
4. `multitask_strategy` 的 `reject` / `enqueue` / `rollback` 仍被忽略（同线程并发 run 可并存）。
5. `GET /threads/{tid}` 不返回 `interrupts`。
6. 独立包 `aegra-protocol` 与根 `docs/` 保留不动，仅作历史参照。

## 10. 测试清单（e2e，真 PG）

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
