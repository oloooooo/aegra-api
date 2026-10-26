# worker / 多副本模式必须开 Redis broker

> **已归档**：本文是历史记录，不再约束当前代码；现行决定见 [`../README.md`](../README.md) 总表。

## 背景：不开 Redis 时，跨进程取消是静默失效的

一个进程内的 run 由 `active_runs[run_id]`（进程内存 dict）持有 asyncio task，
`RunBroker.request_cancel()` 只会 `task.cancel()` 本地那一个。worker 进程模式下同一线程的 run
可能被 A 进程执行、取消请求落到 B 进程：

```
A: 执行 run（in-process）
B: 收到 POST /runs/cancel
   → interrupt_unowned_run() 直接把 DB 写成 interrupted（本地模式 run 从不 claim lease，看起来"无主"）
   → request_cancel() 在 B 的 active_runs 里找不到 → 静默 return
结果：DB 说已取消，A 里的图继续跑、继续写 checkpoint
```

实测（2 个 uvicorn worker，`REDIS_BROKER_ENABLED=false`，用新建连接发取消以绕开 keep-alive 粘同一 worker）：

```
轮1: 取消后 state 消息数 9 → 11 → 13 → 14    ← 取消了还在跑
轮2: 15 → 15 …                              ← 真停了
轮3: 12 → 14 → 15                            ← 还在跑
轮4: 12 → 12 …                              ← 真停了
```

日志对应 `Interrupted unowned run`。**约 50% 丢失**，与 worker 数一致。

## 决定：worker / 多副本部署一律 `REDIS_BROKER_ENABLED=true`

- 队列：run 进 Redis List，由 worker 循环认领；每个 run 带 `claimed_by` + `lease_expires_at`
  （`worker_executor.py`），别的进程取消时能看到"有主"，走 broker 广播而不是直接改 DB。
- 取消：`RedisRunBroker.request_cancel()` 发 pub/sub，属主进程收到后 `task.cancel()`。
- 事件流：事件进 Redis，任一实例都能 attach 并重放，SSE 不再依赖"连到属主进程"。

## 当前配置

`.env`：`REDIS_BROKER_ENABLED=true`、`REDIS_URL=redis://127.0.0.1:6379/0`、`WORKER_COUNT=2`。
启动日志会从 `Using local asyncio executor (in-process tasks)` 变成 worker 队列模式。

## Considered Options

- **保持单进程（local executor）**：取消、超时、leak 全都正确，但无法多副本，重启丢进行中的 run。
  ADR-0008 里记的"派活并发上限按进程各算"也只在单进程下成立。
- **多进程 + 不开 Redis（本次实测的坏状态）**：吞吐上去了，取消一半失效、进行中的 run 无处认领，
  且启动时**没有任何告警**。不要用。
- **worker + Redis（选中）**：跨进程语义完整，代价是多一个基础设施依赖与 at-least-once 重投语义
  （崩溃后 lease 过期会被重新执行，幂等由业务方负责）。

## Consequences

- 部署时必须连得上 Redis；连不上时 worker 队列不工作（启动即报错，而不是降级）。
- `CRON` 抢占本来就是 `SELECT … FOR UPDATE SKIP LOCKED`，多 worker 下不会重复触发，无需额外配置。
- 任务结果投递（ADR-0006 / `docs/design.md §2.4`）依赖的 `Thread.status` 三态判断在 worker 模式下同样成立，
  且比 local 模式更准（run 有真实 owner）。
- 单机开发若不想起 Redis，把 `REDIS_BROKER_ENABLED` 改回 `false` 并**只用单进程**启动即可，
  但不要用 `uvicorn --workers N`。
