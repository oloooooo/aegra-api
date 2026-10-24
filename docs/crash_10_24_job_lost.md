# run 卡死 125 秒：job 从 Redis 队列里静默丢失

> 记录日期 2026-10-24。适用代码状态：`worker_executor.py` 的 `_dequeue` 用 `blpop(key, timeout=5)`，
> `settings.py` 的 `STUCK_PENDING_THRESHOLD_SECONDS = 120`，redis-py 8.1.0。
> 本文只做诊断与处置建议；§7 的方案 1、2 已于 2026-10-24 落地（`worker_executor._dequeue`）。

---

## 1. 现象

用户在前端发：「cbam-reporting、hybrid-bonding、sbol3-synbio 这三个主要讲了啥，请你派遣子agent调查」
→ 界面卡住约 2 分钟没有任何输出、没有报错，用户以为是服务挂了。

实际结果：答案在 22:35:18 已经写进线程（父线程 4 个 run 全 `success`，3 个子 agent 都跑完了）。
**服务没挂，是这一次 run 的调度丢了，被兜底机制迟到了 125 秒才捞回来。**

---

## 2. 时间线（`logs/aegra.log`）

| 时间 | 事件 | 说明 |
|---|---|---|
| 22:32:26.057 | `Scheduling run 553da643` | `run_preparation._prepare_run` |
| 22:32:26.064 | `Enqueued run_id to job queue`（`aegra:jobs`） | 已进队列 |
| 22:32:26 → 22:34:31 | **无 `Worker picked up run`** | DB 里 run 一直 `status=pending`、`claimed_by=NULL`；`LLEN aegra:jobs` = 0 → job 被取走却没被认领 |
| 22:33:57 | 前端重新初始化（whoami + threads/search） | 用户刷新页面，SSE 流断开 |
| 22:34:31.101 | `Re-enqueueing stuck pending runs` + `Re-enqueued recovered run` | `lease_reaper` 的 stuck-pending 兜底（阈值 120s） |
| 22:34:31.107 | `Worker picked up run` | 重新入队后 **3ms** 被领走 → worker、队列本身都正常 |
| 22:34:35 → 22:35:18 | 主管派 3 个子 agent，全部 success | 结果已入库 |

关键判据：`runs` 行 `created_at == updated_at`、`claimed_by` 为空，说明**从未有 worker 碰过这行**；
而 `_execute_with_lease` 里凡成功认领都会先打 `Worker picked up run` 并把状态改成 `running`。
所以不是「执行卡住」，是「调度丢了」。

---

## 3. 根因：BLPOP 的服务端超时撞上客户端 socket 超时

- worker 循环：`await client.blpop("aegra:jobs", timeout=5)`（`worker_executor._dequeue`）
- redis-py 8.1 的默认 socket 超时：`redis/_defaults.py: DEFAULT_SOCKET_TIMEOUT = 5`（项目未覆盖）
- `RedisManager` 建池时没有传 `socket_timeout`，于是**客户端读超时 5s == 服务端阻塞超时 5s**

两者相等 → 每 5 秒精确制造一次竞态：

```
worker: BLPOP aegra:jobs 5        # 服务端 5s 后回 nil
客户端: 读超时 5s                 # 同一时刻放弃这个 socket
   → TimeoutError: Timeout reading from 127.0.0.1:6379
   → 连接被丢弃、下一轮重连
```

如果恰好在这个窗口里有人 `RPUSH` 了 run_id，服务端**已经弹出并把它写进那条即将被丢弃的 socket**，
客户端再也读不到 → job 静默蒸发，`_dequeue()` 的 `except RedisTimeoutError: return None` 连日志都不打。

旁证：Redis `CLIENT LIST` 里 4 个 worker 循环的 BLPOP 连接**每 5 秒换一次地址**
（正常应复用连接、`age` 持续增长），`total_connections_received` 持续上涨。
这是「每轮阻塞读都以超时收场」的直接证据。

---

## 4. 复现（本地，约 30 行）

用项目同样的连接池配置起 4 个并发 BLPOP（等同 2 进程 × 2 worker），在 5 秒边界上推 15 个值：

```python
pool = aioredis.ConnectionPool.from_url(
    "redis://127.0.0.1:6379/0", max_connections=250, decode_responses=True,
    health_check_interval=30,
    retry=Retry(ExponentialBackoff(cap=1.0, base=0.05), 3),
    retry_on_error=[ConnectionError, TimeoutError],
)
# 4 个 loop: await c.blpop(Q, timeout=5)
# 推值偏移：[4.95,4.98,4.99,5.00,5.01,5.02,5.05, 9.95,9.99,10.00,10.01,10.05, 15.00,15.02, 20.0]
```

结果：

| 配置 | 结果 |
|---|---|
| `blpop(timeout=5)` | sent 15 / recv 14，**LOST `v12@15.0`（整 5 秒边界那个）**，`error: Timeout reading from 127.0.0.1:6379`，队列剩余 0 |
| `blpop(timeout=5)`、`retry_on_error=[ConnectionError]` | 同样丢 `v12@15.0` |
| `blpop(timeout=4)` | sent 13 / recv 13，0 超时，0 丢失 |

丢失概率取决于「push 落在 5 秒边界的毫秒级窗口内」，粗估 4 个 worker 循环下每次提交约 0.1%~1%，
表现为偶发「提交后长时间没反应」，不是必现。

---

## 5. 兜底为什么救得回来、但太慢

`lease_reaper._reap()` 每 15s 扫一次 `status=pending AND claimed_by IS NULL AND created_at < now-120s`，
再用 `_missing_from_queue()` 确认它确实不在 `aegra:jobs` 里，才 `_reenqueue()`。
所以「丢了」最终会恢复，只是要等 **120s 阈值 + 最多 15s 扫描间隔 ≈ 125~135s**。
本次就是 22:32:26 → 22:34:31 = 125s。

---

## 6. 用户侧看到的「卡死」还有什么放大器

| 环节 | 表现 |
|---|---|
| SSE | 流在 22:33:57 由客户端断开（用户刷新页面），断开前后一个事件都没有；`sse()` 里没设超时，也没人告诉用户「run 还没开始跑」 |
| 前端 | `streamRun` 正常结束（无 error）→ `refreshState()` + 4s 轮询，界面上就是「一直转圈，没有任何提示」 |
| 后端 | `_dequeue` 吃到 `RedisTimeoutError` 时静默 return None，**日志零痕迹**，只有 reaper 的 `Re-enqueueing stuck pending runs` 事后才暴露 |

---

## 7. 修复方案（按性价比排序）

1. **`_dequeue` 的 BLPOP 超时留余量**（1 行，已实测有效）——✅ 已实施
   `blpop(key, timeout=5)` → `timeout=4`：服务端 4s 回 nil，客户端 5s 才超时，竞态消失。
   更彻底的做法是给 worker 用一个 `socket_timeout=None`（或 ≥15s）的专用连接池，
   但 `RedisManager` 是全局单例，需要区分池用途，改动面较大。
2. **超时不再静默**——✅ 已实施：`except RedisTimeoutError` 分支加 `logger.warning`，
   并在单测里断言 `timeout < redis._defaults.DEFAULT_SOCKET_TIMEOUT`。
3. **兜底提速**（未实施）：`.env` 里 `STUCK_PENDING_THRESHOLD_SECONDS=120` → `30`。
   治标：丢失仍可能发生，但用户最多等 30 秒而不是 125 秒。
4. （可选，结构性）把 `BLPOP` 换成 `BLMOVE queue → inflight:{worker}` 的「取走必留痕」模式，
   崩溃/丢弃的 job 都能被扫描恢复，而不是依赖 pending 超时。改动大，需单独 ADR。

> 不建议的做法：给 `RedisManager` 加 `retry_on_error` 重试 BLPOP。
> 实测 `retry_on_error=[TimeoutError]` 与不加都会丢，重试只是重连，救不回已被弹出的元素。

---

## 8. 如果再次遇到（操作手册）

```bash
# 1) 这个 run 还在不在？状态是什么？
#    pending + claimed_by 为空 + updated_at == created_at → 调度丢了（本文场景）
#    running + lease_expires_at 过期 → worker 崩了，靠 reaper 重试
#    success → 早就跑完了，刷新前端线程即可

# 2) 队列里有没有它
docker exec redis6 redis-cli llen aegra:jobs

# 3) 日志里搜 run_id：只有 Scheduling + Enqueued、没有 Worker picked up = 本文场景
grep <run_id> logs/aegra.log

# 4) 想立刻恢复：等 120s 让 reaper 自己捞，或手动补一条
docker exec redis6 redis-cli rpush aegra:jobs <run_id>
```

（手动 `rpush` 是安全的：认领走的是 `UPDATE ... WHERE status='pending' AND claimed_by IS NULL`，
重复入队会被 `rowcount == 0` 挡掉。）
