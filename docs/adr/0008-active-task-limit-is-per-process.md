# 异步派活的并发上限只在单进程内成立

`MAX_ACTIVE_TASKS = 3` 是业务约束（用户要求「一个主 agent 最多 N 个后台子 agent」）。实现它需要
「读活跃数 → 判上限 → 占名额」这一步原子，而这一步天然要和并发打架。

## 背景：并发是真实存在的，不是假想

- LangGraph 的 `ToolNode` 对同一条 AIMessage 里的多个 tool_call **并发执行**：
  异步路径 `asyncio.gather`（`langgraph/prebuilt/tool_node.py:858`），
  同步路径 `executor.map`（同文件 `:822`，线程池）。
- `async_tasks` 的 reducer 是 merge（`deepagents/middleware/async_subagents.py:122`），
  所以两条并发派活都会落库，不存在「丢更新」帮忙兜住上限。
- 实测（同一份 pre-batch state 下连续两次 `_start_args`）：两次都放行 → 活跃任务 4 个，超过 3。

## 决定（本期）：进程内账本 + 预留-结算

`graphs/async_task_guard.py` 里：

- `self._mu = threading.Lock()`（**不是** `asyncio.Lock`：同步路径跑在线程池，异步跑在事件循环，
  只有 `threading.Lock` 同时管住两条路）；
- `self._ledger: dict[parent_thread_id, dict[task_id, task]]` 记录「已预留但尚未写进 state」的任务；
- `_reserve()`：锁内「合并 state + 核实结果 + 账本 → 判上限 → 写一个 `pending-<uuid>` 占位」，
  **锁内不含任何 `await`**；网络创建放在锁外，靠占位保证名额不被重复使用；
- `_settle()`：创建成功用真实 task 替换占位；失败则直接退位（名额立刻归还）；
- 账本里已被 state 记账、或已终态的条目自动退位（state 是权威）。

前提：同一进程内所有 run 共用**同一个** guard 实例。本仓库的图是静态图，aegra 缓存编译结果
（`langgraph_service.py:75 _base_graph_cache`），所以这个前提当前成立。若将来把 supervisor 改成
factory 式图（按 run 重建），账本会变成每次请求一本空账，**连单进程都不成立**。

## 已知边界：多进程部署下上限按进程各算

账本和锁都在进程内存里。aegra 的 worker 模式（`REDIS_BROKER_ENABLED=true`，见
`src/aegra_api/services/executor.py:17` / `worker_executor.py`）会跑多个 worker 进程，同一父线程的两次
派活可能落到不同进程：

```
进程 A：账本 0 条 → 放行 → 账本 1
进程 B：账本 0 条 → 放行 → 账本 1
→ 实际活跃 2、…、2×3 = 6，业务约束退化为「每进程 3 个」
```

**当前部署不受影响**：`.env` 未开 `REDIS_BROKER_ENABLED`，启动日志是 `Using local asyncio executor (in-process tasks)`。
**什么时候必须升级**：开启 worker 模式、或多副本部署（同一 `aegra.json` 起多个 uvicorn）、
或把 supervisor 改成 factory 图的那一天。

### 升级时怎么改（届时再选，别现在做）

1. **服务端登记表（推荐）**：`custom/` 增一张 `async_task_dispatch(task_id, parent_thread_id,
   agent_name, status, updated_at)`，guard 派活前经接口做
   `SELECT count(*) … WHERE parent_thread_id=? AND status NOT IN (终态)` 并用
   `SELECT … FOR UPDATE` / 按 `parent_thread_id` 的 advisory lock 保证跨进程原子；派活成功写一行。
   顺带把 `/custom/tasks` 列表与三处「核实子 run 状态」的实现（`graphs/async_task_guard.py::_refreshed_sync/_refreshed_async`、
   `custom/frontend_api.py::_fresh_status`）收敛到同一张表。
2. **查 `runs` 表现有数据**：子 run 的 `config.configurable.parent_thread_id` 已经带上了父线程
   （ADR-0005），但 aegra 没为它建索引、也没暴露「按 config 查 run」的接口，只能全表扫 JSONB，
   等于把成本压到每次派活上。不推荐，除非顺带在 `src/` 加索引与查询接口。

## Considered Options

- **现在就建登记表**：一次到位、跨进程正确；但 `graphs/` 目前没有 DB 通道（要经 HTTP 接口），
  且单进程部署下纯属为将来的形态付今天的复杂度（还要处理「派活了但没写表」的补偿）。
- **查 `runs` 的 JSONB**：不改表、不改接口，但每次派活一次全表扫，且依赖 aegra 未承诺的列结构。
- **进程内账本（选中）**：零新依赖、单进程完全正确、失败自动退位；代价是边界要写清楚。

## Consequences

- 单进程部署：上限准确，并发派活被拒绝时会返回一条可读错误给模型，由它自己等待或 check。
- 多进程部署：上限按进程放大。**这是已知缺陷，不是 bug**——部署形态变化时必须先处理本条 ADR。
- 自查方法：看启动日志是否出现 `Using local asyncio executor`；出现即单进程，上限可信。
- 账本只存在内存：进程重启后清空；此时以父 state 的 `async_tasks` 为准（重启前已派发的任务仍是活跃的，
  因为它们在 state 里）。
