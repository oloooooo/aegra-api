# 任务结果投递的幂等与并发

结果投递（见 `docs/design.md §2.4`）有两条路，且会在两种情况下并发：同一轮里多个后台任务
同时完成、多个子 agent 在几乎同一时刻收尾，以及通知路与主管 hook 同时决定投递。
本文记录为什么不只靠 state 判重、认领表负责什么、以及三种并发下的行为。

## 两条路的分工

| 父线程此刻 | 谁投递 | 动作 |
|---|---|---|
| `busy`（主管的 run 还在跑） | `AsyncTaskGuard.abefore_agent` / `aafter_agent` | 把结果写进**自己 run 的 state**；不打断、不另开 run |
| `interrupted`（在等人工回答） | 谁都不动 | 硬 resume 会顶掉用户正等着回答的答案；用户答完后下一次 run 的 hook 会补上 |
| `idle` / `error` | `CompletionNotifierMiddleware` | 用父身份开一轮新 run，**只试一次** |

hook 只在 run 首尾各跑一次——这是有意的：LangGraph 没有向运行中的图投递消息的 API，
run 中途想拿结果只能由模型自己 `check_async_task`；而 run 首尾是主管唯一能确定
"这件事还没进我上下文"的时刻。所以 `check` 是 run 中途的正路，通知是空闲时的兜底。

## 判重为什么不能只放在 state 里

投递消息统一为（前端靠前缀识别成「子 agent 返回」）：

```
[Async subagent '<name>' has completed] Result: …（摘要，超 500 字带截断标记）
<空行>
task_id: <id>
run_id: <完成这次执行的 run id>
```

按消息判重最省事，但有两个硬伤：

1. **跨进程不原子**。两个 worker 同时"查空 → 创建"，两边都通过；同一父线程上还会出现
   两个投递 run 并发写同一线程。
2. **认不出模型自己 check 过的结果**。`check_async_task` 返回的是 `ToolMessage(json)`，
   不是这条带前缀的消息，扫消息会把"已经从工具拿到结果"的任务当成没投过，再投一遍。

用 `task_id` 单键判重还会吞掉 `update_async_task` 的第二次结果（同一任务、新 run）。

## 机制：认领表 + `delivered_run_id`

### 认领表负责「两条路互斥」

`async_task_delivery(task_id, run_id)`，主键即判重键，另有 `parent_thread_id` 与 `claimed_at`。
接口在 `custom/delivery_api.py`：

```
POST /custom/tasks/{task_id}/deliver-claim   {parent_thread_id, run_id}
  → {"claimed": true,  "reason": "claimed" | "reclaimed"}
  → {"claimed": false, "reason": "delivered" | "in_flight"}
DELETE /custom/tasks/{task_id}/deliver-claim?parent_thread_id=&run_id=
```

规则（全部在一个事务里，配 `pg_advisory_xact_lock(hashtext(parent_thread_id))` 串行化同线程认领）：

| 情况 | 结果 | 调用方该做什么 |
|---|---|---|
| 主键已存在且 `claimed_at` 在 15 秒内 | `delivered` | 跳过——这次执行的结果已经投过 |
| 主键已存在但超过 15 秒（认领后进程死掉、没真投出去） | `reclaimed`，刷新时间 | 照常投递 |
| 同线程上 8 秒内已有别的任务认领 | `in_flight` | 跳过——那条投递正在落地 |
| 其它 | `claimed`，插入一行 | 去投递；**投递失败时调 `DELETE` 撤回认领** |

两个调用方都用它：`AsyncTaskGuard._claim_async`（服务身份）与
`CompletionNotifierMiddleware`（现铸的父身份 token）。授权：`Thread.user_id == 调用者`
或调用者是服务身份，否则 403；线程不存在 404。

### `delivered_run_id` 负责「同一个 run 只投一次」

投递成功的任务在 state 的 `async_tasks[task_id]` 上记 `delivered_run_id = run_id`。
它同时覆盖三件事：

- 模型自己 `check_async_task` 拿到终态结果时，guard 在 `awrap_tool_call` 里补记这个字段
  （否则下一个 hook 会把同一个结果再写一遍）；
- hook 写 state 失败、认领被撤回后，字段没写进去，下一次 run 的 hook 会重新尝试；
- 判重不再需要扫 `state.messages`（消息在、字段不在的情况仍用消息兜底，用于跨 run 的
  notifier 投递）。

## 三种并发场景的行为

| 场景 | 行为 | 幂等 |
|---|---|---|
| 同一轮里多个任务同时终态，主管在跑 | 一次 `after_agent` 合并成一条 state 更新；每个任务各自认领一次 | 天然幂等（一次 checkpoint） |
| 多个子 agent 同时收尾，主管正忙 | notifier 看到 `busy` 直接返回，不认领、不生成 run；结果由下次 run 的 hook 写 | 不会产生 run |
| 多个子 agent 同时收尾，主管空闲 | 第一个抢到认领并开 run；8 秒内后到的拿到 `in_flight` 让路（下次 run 的 hook 兜住） | 同一时刻最多一个投递 run |
| notifier 读到 `idle` 后用户立刻发消息 | 服务端在跑图之前就把线程置 `busy`；两条路谁先认领谁投，另一个拿到 `delivered` | 只投一次 |
| 同一任务被两个 hook 投递 | 第二个看到 `delivered_run_id` 或拿到 `delivered` | 只投一次 |
| `update_async_task` 后的新 run | 新 `run_id` → 新主键 → 再投一次 | 有意为之 |

### 两个时间参数

| 参数 | 位置 | 当前值 | 作用 |
|---|---|---|---|
| `_STALE_SECONDS` | `custom/delivery_api.py` | 15s | 认领多久算"过期、允许重投"。**它决定崩溃后结果最多晚多久到达** |
| `_IN_FLIGHT_SECONDS` | `custom/delivery_api.py` | 8s | 同一父线程上，刚认领后的让路窗口；防止两个投递 run 同时写一个线程 |

投递动作本身是毫秒级，且通知路既没有重试也没有延迟复查，所以这两个值不再互相牵制
（旧版那条"`_STALE_SECONDS` 必须大于重试 + 复查总和"的不变式随复查一起删掉了）。
崩溃后没投出去的结果，在下一次父 run 开始时由 hook 重算补上——用户不发新消息就不会补，
这是"只试一次"接受的代价。

## Considered Options

- **只靠 state 消息标记**（更早的实现）：零新表，但每次判重读整份 state，且跨进程不原子；
  也认不出 `check_async_task` 已经拿到的结果。
- **在 `before_model` 每次模型调用前注入**（上一版实现）：run 中途就能拿到结果，但每个
  step 都要轮询子 run 状态（带 5s TTL 缓存），还逼出"重试 + 延迟复查"和与之绑定的过期窗口。
- **数据库认领表 + `delivered_run_id`（选中）**：认领表解决跨路互斥，字段解决同路判重；
  hook 只在 run 首尾各跑一次，轮询次数从"每个 step"降到"每次 run 两次"。
- **在服务端直接创建投递 run**：能让"认领 + 创建"彻底原子，但要把 aegra 的 run 创建流程
  搬进 `custom/`，耦合面太大，不做。

## Consequences

- 主管 run 中途想拿结果，只能靠模型自己 `check_async_task`——提示词里写成硬口径
  （需要结果才能往下走、或用户问进度时才查）。
- `after_agent` 写进 state 的结果，模型已没机会转述：用户看到的是"答案 + 一张结果卡片"。
  这是 run 首尾投递的固有形态，换来的是不开新 run、不重复生成答案。
- 投递消息只带 500 字摘要并带截断标记；全文只能从 `check_async_task` 取（那边不截断）。
- 认领是先写后投：进程在"认领成功"与"真投出去"之间崩溃时，这次结果 15 秒内不会重投；
  正常路径下失败会先 `DELETE` 撤回，不留死行。
- 认领表会长大：一行对应一次 `(task, run)` 投递，随任务数线性增长。当前没有清理任务；
  需要时按 `claimed_at` 定期删旧行即可（删了最坏效果是"远古任务被重新投一次"）。
- 认领行与 `Run` 没有外键：删线程不会级联清理认领行，理由同上。
