# 任务结果投递的幂等与并发

结果投递（见 `docs/design.md §2.4`）有两处会并发：同一轮里多个后台任务同时完成、以及
多个子 agent 在几乎同一时刻收尾。本文记录为什么用数据库认领、每种并发下的行为。

## 判据为什么不能只放在 state 里

投递消息统一为（前端靠前缀识别成「子 agent 返回」）：

```
[Async subagent '<name>' has completed] Result: …
<空行>
task_id: <id>
run_id: <完成这次执行的 run id>
```

按消息判重看起来最省事，但有两个硬伤：

1. **判重要读整份 state**。`GET /threads/{id}/state` 会把全部 messages 序列化回来——
   一次投递为了查一个标记要拉几十~几百 KB；重试与延迟复查同样如此。
2. **跨进程不原子**。两个 worker 同时"查空 → 创建"，两边都通过；同一父线程上还会出现
   两个投递 run 并发写同一线程。

用 `task_id` 单键判重还会吞掉 `update_async_task` 的第二次结果（同一任务、新 run）。

## 机制：一张两列主键的认领表

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
| 主键已存在且 `claimed_at` 在 10 分钟内 | `delivered` | 跳过——这次执行的结果已经投过 |
| 主键已存在但超过 10 分钟（认领后进程死掉、没真投出去） | `reclaimed`，刷新时间 | 照常投递 |
| 同线程上 8 秒内已有别的任务认领 | `in_flight` | 跳过——它的投递 run 正在路上；主管边界的注入会兜住本次结果 |
| 其它 | `claimed`，插入一行 | 去投递；**投递 run 创建失败时调 `DELETE` 撤回认领** |

两个调用方：

- `AsyncTaskGuard.abefore_model`（主管正在跑，注入消息）——用服务身份认领；
- `CompletionNotifierMiddleware`（主管空闲，开新 run）——用现铸的父身份 token 认领。

授权：`Thread.user_id == 调用者` 或调用者是服务身份，否则 403；线程不存在 404。

## 三种并发场景的行为

| 场景 | 行为 | 幂等 |
|---|---|---|
| 同一轮里多个任务同时终态，主管在跑 | 一次 `before_model` 合并成一条 state 更新；每个任务各自认领一次，全部抢到 | 天然幂等（一次 checkpoint） |
| 多个子 agent 同时收尾，主管正忙 | notifier 看到 `busy` 完全不认领，只安排延迟复查；结果由 ① 注入 | 不会产生 run |
| 多个子 agent 同时收尾，主管空闲 | 第一个抢到认领并开 run；8 秒内后到的拿到 `in_flight` 直接放弃（主管下一轮注入兜住） | 同一时刻最多一个投递 run |
| 同一任务被重试/双 hook 投递 | 第二次拿到 `delivered` | 只投一次 |
| `update_async_task` 后的新 run | 新 `run_id` → 新主键 → 再投一次 | 有意为之 |

## Considered Options

- **只靠 state 消息标记**（原实现）：零新表，但每次判重读整份 state，且跨进程不原子。
- **进程内锁 + 本地认领**（原实现的加固版）：单进程确定，多 worker 仍有窗口，且不省 state 读取。
- **数据库认领表（选中）**：一次小写入同时给出幂等与跨进程串行；顺带把"判重"从
  "读整份对话"降成"查一行主键"，投递路径的 state 读取降到 0（`interrupted` 分支除外，
  那里仍需读 `interrupts`）。
- **在服务端直接创建投递 run**：能让"认领 + 创建"彻底原子，但要把 aegra 的 run 创建流程
  搬进 `custom/`，耦合面太大，不做。

## Consequences

- 投递判重不再依赖 state，也不再扫消息：**路径上没有任何大对象读取**。
- 认领是先写后投：进程在"认领成功"与"run 创建成功"之间崩溃时，这次结果会被判为已投递，
  **10 分钟内不会被投出去**（`claimed_at` 过期后允许 `reclaimed` 重投）；正常路径下
  创建失败会先 `DELETE` 撤回，不留死行。
- `in_flight` 的 8 秒窗口意味着：两个任务在同一瞬间完成时，第二个不会单独开 run，
  它的结果会由主管边界的注入送达（延迟一个 step，不丢）。
- 认领表会长大：一行对应一次 `(task, run)` 投递，随任务数线性增长。当前没有清理任务；
  需要时按 `claimed_at` 定期删旧行即可（删了最坏效果是"远古任务被重新投一次"）。
- 认领行与 `Run` 没有外键：删线程不会级联清理认领行，理由同上。
