# 流式（streaming）参考

`client.stream` 能吐什么、aegra 支持哪些模式、deepagents / `astream_events` 与 v3 的适配情况。
本文结论全部来自对本仓库的**实测**（真起服务、跑 supervisor 与 xlsx 子图、逐事件抓取），
环境：`langgraph-sdk` / `langchain-core 1.6.9` / `langchain 1.4.4` / `deepagents 0.7.23`。

## 1. 怎么用

```python
from langgraph_sdk import get_client

client = get_client(url=BASE, headers={"Authorization": f"Bearer {tok}"})
stream = client.runs.stream(
    thread_id, "supervisor",
    input={"messages": [{"role": "user", "content": "..."}]},
    stream_mode=["values", "updates", "messages", "custom"],  # 可多选
)
async for part in stream:
    mode, data = part.event, part.data   # part 还有 .id
    ...
```

- 每次迭代产出 `StreamPart(event=, data=, id=)`（**不是 dict，也不是 2 元组**）。
- `.event` 就是 SSE 事件名（= 模式名），`.data` 是该事件载荷。
- 固定首尾：开头 `metadata`（`{run_id, attempt}`）、结尾 `end`（`{status: success|error|...}`）。
- 想要 `(mode, payload)` 形式，自行取 `part.event, part.data`。

> 别混淆 SDK 的 `client.stream(version=...)`：那是 **StreamPart 传输格式**版本
> （`"v1"` 原始 / `"v2"` dict，`langgraph_sdk/_async/runs.py:225`），跟
> `astream_events` 的事件 schema 版本毫无关系。

## 2. 模式矩阵（实测全绿）

| `stream_mode` | 实测 | 载荷内容 | 适合 |
| - | - | - | - |
| `values` | ✅ | 完整 state 快照（`messages` + `async_tasks` …） | 前端整态刷新 / 恢复、渲染后台任务列表 |
| `updates` | ✅ | 每节点 diff，如 `{"PatchToolCallsMiddleware.before_agent": null}` | 「正在跑哪一步」面板 |
| `messages` | ✅ | 产出 `messages/metadata`、`messages/partial`（**逐 token**）、`messages/complete` | 聊天气泡 + 打字机 |
| `messages-tuple` | ✅ | 归一成 `messages` 事件，带 chunk 原始 tuple | 要原始 chunk 时 |
| `custom` | ✅ | `runtime.stream_writer` 写的业务事件，如 `{"tool":"xlsx_info","event":"start/error/done"}` | 工具进度条 / 状态 chip |
| `debug` | ✅ | `checkpoint` / `task` / `task_result` 内部（middleware 名 + 入参出参） | 调试：谁跑了、传了啥 |
| `events` | ✅ | 完整 `astream_events(v2)`：`on_chain_*` / `on_chat_model_*` / `on_tool_*` | 最深追踪（量极大） |
| `tasks` | ✅ | 任务起止（节点名 + input） | 调试执行树 |
| `checkpoints` | ✅ | checkpoint config | 调试持久化 |

单轮 supervisor（`ls` + 读文件）实测量级参考：`messages/partial ×166`、`debug ×16`、
`updates ×5`、`values ×4`、`messages/complete ×2`、`metadata ×1`、`end ×1`。

## 3. aegra 怎么处理这些模式

- 入口 `aegra_api/services/graph_streaming.py::stream_graph_events`：
  - 默认走 `graph.astream(...)`；**当 `"events" in stream_mode` 或是 JS 图**时改走
    `graph.astream_events(..., version="v2")`（`graph_streaming.py:209`）。
  - 内部总会多算 `debug` + `updates`（`graph_streaming.py:158,177`），但
    `_process_stream_event` 里 `elif mode in stream_mode` 把关——**只发你请求的模式**。
  - `messages-tuple` 对非 JS 图会被归一成 `messages`（`graph_streaming.py:170`）。
- 模式来源：`RunCreate.stream_mode`（`models/runs.py:66`）→ `_prepare_run` →
  `run_executor._resolve_stream_modes`（缺省 `["values"]`）。
- `only_interrupt_updates` 分支：未显式请求 `updates` 时，把含 `__interrupt__` 的更新
  改名成 `values` 发出（向后兼容）。

## 4. deepagents / `astream_events` 适配吗 → **适配**

- aegra 就是调 `graph.astream_events(input, config, version="v2", ...)`；deepagents 图本质是
  编译好的 Pregel，直接兼容。
- 实测子图 `events` 模式返回 2154 个事件，metadata 带 `ls_integration: deepagents`、
  `lc_versions:{deepagents:0.7.23, langchain:1.4.4}`，能清楚看到
  `CodeInterpreterMiddleware.after_agent`、`PatchToolCallsMiddleware.before_agent` 等
  deepagents middleware 的起止。
- **`create_agent(...)` 没有任何 stream 参数**（查 `langchain/agents/factory.py` 签名确认）——
  流式全靠图级 `astream` / `astream_events`，与 deepagents 无耦合，所以天然适配。

## 5. `version="v3"` 支持吗 → 分场景

### A. 进程内 `graph.astream_events(request, version="v3")` → 支持 ✅

```python
from graphs.deepagents_nodes import graph   # CompiledStateGraph (Pregel)

stream = await graph.astream_events(request, version="v3")   # v3 先 await！
async for ev in stream:
    ...
```

- `graph` 是 `CompiledStateGraph`（= `Pregel` / `CompiledGraph`），命中 v3 白名单
  `BaseChatModel` / `CompiledGraph`（`langchain_core/runnables/base.py:1609`）；
  `Pregel.stream_events` 有显式 `if version == "v3":` 分支（`langgraph/pregel/main.py:3771`）。
- 这一步**不经过 agra**（自己进程里直接跑图对象）。

**两个 v3 专属坑：**
1. **v3 下 `astream_events` 返回 coroutine，必须先 `await` 再迭代**，否则
   `TypeError: 'async for' requires an object with __aiter__, got coroutine`。
   （同步版 `graph.stream_events(request, version="v3")` 直接返回 `GraphRunStream`，可直接迭代。）
2. v3 是 **beta、schema 与 v2 完全不同**：
   - v2：`on_chat_model_stream` + `AIMessageChunk`
   - v3：`{'type':'event','method':'values'|'messages','params':{namespace,timestamp,data}}`，
     内层是 `message-start` / `content-block-delta` / `message-finish`（带 `usage`）
   - v3 不接受部分 kwargs（`_reject_v3_invariant_kwargs`，v3 自己接管）。

### B. 走 HTTP `client.stream(...)`（经 aegra）→ 不支持 ❌

- aegra 把版本**写死 `"v2"`**（`graph_streaming.py:209`）。
- `RunCreate` 没有 version 字段，请求里无任何参数能改。
- 想让前端走 v3 只有两条路：① 进程内跑（绕开 aegra）；② 改
  `src/aegra_api/services/graph_streaming.py:209` 的 `version="v2"`——
  **动 `src/`，且改所有客户端的线上事件格式，属破坏性变更**，前端须同步适配 v3 schema。

## 6. 选型建议

- **前端**：`messages`（逐 token）+ `custom`（工具进度）+ `updates`（阶段）+
  `values`（含 `async_tasks`，渲染后台任务）+ `end`。按 `.event` 分流即可。
- **调试**：`debug`（middleware 级 trace）或 `events`（全生命周期）+ `tasks`。
- `events` / `debug` 量极大（一轮 2154 / 16 条），生产前端别默认开，留作调试开关。

## 7. 已知坑

1. **子 agent 的流不会回传到父线程。** 异步子图跑在独立 run/thread
   （`graphs/async_task_guard.py` 建的），主管的流里**只有
   `start_async_task` / `check_async_task` 两次工具调用**；子图的 token / 工具 / `custom`
   进度全在**子图自己的 thread** 上。deepagents `middleware/async_subagents.py` 里没有任何流转发（已 grep 确认）。
   要订閲子图必须先拿到 `task_id`（即子线程 id，存在 `async_tasks` 里），但它是服务身份的
   thread，终端用户直接读会被 ownership 过滤挡成 404 —— 走 `custom/frontend_api.py` 的
   `GET /custom/tasks/{task_id}/stream` 代理（见 §8），不要绕着开原生流。
2. **`custom` 只在代码调 `runtime.stream_writer` 时才有**——主管的 `ls/read_file` 不打
   （实测主管 `custom ×0`），`graphs/xlsx_tools.py` 打（16 条）。想在主管侧也看到工具进度，
   需去 `graphs/` 给对应工具补 `stream_writer`。
3. `values` 里带 `async_tasks`（后台任务注册表），正好给前端渲染任务列表用——但
   `status` 会滞后（见 §8.2），要真实状态查 `GET /custom/tasks/{id}`。

## 8. 子 agent 的流：代理与两个前提

### 8.1 代理接口

`GET /custom/tasks/{task_id}/stream?parent_thread_id=...`（`custom/frontend_api.py`）把子 run 的
事件转成同样的 SSE 帧（`event:` + `data:`）转发给浏览器。授权靠「父线程属于当前用户 +
父 state 的 `async_tasks` 里确实有这个 task_id」，服务 token 只在服务端内部使用。

帧序列：`metadata` → `values`（连接前的快照，补齐历史）→ `messages/partial` | `custom` |
`messages/complete` → `end`。任务已结束则只发 `metadata` + `values` + `end`。

实测一次子 agent 执行：`messages/partial ×3418`、`custom ×18`、`messages/complete ×9`、`end ×1`
——前端必须节流，`messages/partial` 是逐 token 全量文本。

### 8.2 两个前提（都已落地）

1. **子 run 必须声明流模式**：拦截层 `runs.create(..., stream_mode=["messages", "custom"])`。
   不声明时 aegra 默认只产 `values`（`run_executor._resolve_stream_modes` 缺省 `["values"]`），
   代理再订閲 `messages/custom` 也拿不到东西。
2. **`async_tasks.status` 会僵尸**：它只在 `check_async_task` 或拦截层核实后更新；走完成投递路径
   （主管侧注入 / 发件箱分发者唤醒）时，如果主管之后没再跑过模型调用，
   state 里的 `status` 会停在 `running`。
   拦截层现在派活前会向子 run 核实真实状态并回写（`_refreshed_sync/_refreshed_async`），
   这个核实服务于结果投递（`_deliver` 靠它判断子任务是否已终态）。名额机制已换成 thread 配额（ADR-0012）。

前端对接全文（接口清单、SSE 帧、interrupt/resume、定时任务、删除）见
[`frontend-api.md`](./frontend-api.md)。
