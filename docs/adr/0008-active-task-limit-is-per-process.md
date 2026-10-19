# 异步派活的并发上限只在单进程内成立

> **Status: Superseded by [ADR-0012](./0012-thread-quota-replaces-task-limit.md)（2026-10-19）**
>
> 业务改成「不再限制一个主管派多少子 agent，改成限制一个用户拥有多少会话（thread）」。
> `MAX_ACTIVE_TASKS`、进程内账本（`_LEDGER` / `_LEDGER_LOCK`）、派活前的名额判定与
> `graphs/limits.py` 已全部删除。本文保留作历史记录：它描述的问题（`ToolNode` 并发、
> state 落盘时机、跨进程不共享内存）依然成立，只是不再用名额限制解决。

`MAX_ACTIVE_TASKS`（默认 3，可用环境变量覆盖，见下方「怎么改这个上限」）是业务约束
（用户要求「一个主 agent 最多 N 个后台子 agent」）。实现它需要
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

前提：同一进程内所有 run 共用**同一本账**。原先靠「图是静态图、guard 实例共享」满足；
supervisor 改成工厂图之后（ADR-0011，每 run 重建一次 guard），账本**已改为模块级**
（`graphs/async_task_guard.py` 的 `_LEDGER` / `_LEDGER_LOCK`），前提重新成立，且不再依赖
"图被缓存"这个巧合。

## 本期决定：不改（记于 2026-10-18）

多进程部署下上限偏大，实际影响只是「可能多派一两个子 agent」——多占 worker slot、多花一点钱，
**不丢数据、不重复执行**；单个 run 内部的并行派活由锁串行，是准确的。当前 2 worker 部署
最坏 6 个，业务上可接受。

所以先保留进程内账本，不加数据库名额。**触发升级的条件**（满足任一再做）：

1. 多副本成为常态（不止 2 个 worker / 单机）；
2. 出现因超额导致的资源问题（worker 队列打满、任务互相拖慢）；
3. 业务方要求「上限必须严格」——例如按次计费或对外承诺并发数。

升级方案见文末，与任务投递的认领表（ADR-0010）是同一套做法：advisory lock + 一行计数。

## 怎么改这个上限

单一来源在 `graphs/limits.py`：

```bash
# .env
MAX_ACTIVE_TASKS=3      # 正整数；非法值退回默认 3，启动不会因为错字崩掉
```

- 拦截层（`graphs/async_task_guard.py`）按它判断名额；
- 主 prompt 里“同时最多 N 个后台任务”那句由 `graphs/prompts/__init__.py` 用同一个常量替换
  （`main.md` 里写的是占位符 `{max_active_tasks}`）——**不要直接改 `main.md` 里的数字**，
  否则提示词和实际拦截会漂移。
- 改完重启服务生效（常量在 import 时读取）。前端不需要改：任务面板只显示实际任务，
  超限时的错误文本由拦截层动态生成。

## 已知边界：多进程部署下上限按进程各算

账本和锁都在进程内存里。判断条件是**图执行是否分散在多个进程**，与 Redis 无关
（Redis 修的是跨进程取消与事件流，见 ADR-0009，不共享这本账）。以下部署都命中：

- `uvicorn --workers N`（N > 1）；
- 多副本（同一 `aegra.json` 起多个进程/容器）；
- `REDIS_BROKER_ENABLED=true` 的 worker 队列：run 由不同进程认领，同一个父线程的两次派活
  因此可能落在不同进程。

```
上限 3，状态里已有 2 个活跃：
进程 A：state 2 + 账本 0 → 放行 X → 账本 1（state 还没落盘）
进程 B：state 2 + 账本 0 → 放行 Y → 账本 1（它看不到 A 的账本）
→ 落盘后实际活跃 4，业务约束退化为「每进程 3 个」，最坏 N × MAX_ACTIVE_TASKS
```

触发需要「同一父线程上两个并发 run 各自派活」：用户在主管运行期间又发一句（aegra 不拦并发 run）、
或 cron 与用户同时触发。单个 run 内部的并行 `start_async_task` 由 `threading.Lock` 串行，**不受影响**。

**当前部署命中这条**：`.env` 是 `REDIS_BROKER_ENABLED=true` + `--workers 2`，最坏 2 × 3 = 6。
实际影响只是"可能多派一两个子 agent"，不丢数据、不重复执行。
**要多副本且严格要求上限**：见下面的升级方案（改成数据库认领/计数，与投递认领表同一套做法）。

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
- 多进程部署：单个 run 内部（并行工具调用）仍准确；跨 run/跨进程的并发窗口能超出上限。
- 多进程部署：上限按进程放大。**这是已知缺陷，不是 bug**——部署形态变化时必须先处理本条 ADR。
- 自查方法：看进程数——单进程（日志 `Using local asyncio executor` 且没有 `--workers`）上限可信；
  多进程部署按"最坏 N × 上限"估算。
- 账本只存在内存：进程重启后清空；此时以父 state 的 `async_tasks` 为准（重启前已派发的任务仍是活跃的，
  因为它们在 state 里）。
