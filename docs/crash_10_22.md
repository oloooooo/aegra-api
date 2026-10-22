# 崩溃、复活与"后端卡死"：排查手册 + 处置方案

> 记录日期 2026-10-22。适用代码状态：提交 `f061bf0` 之后（发件箱投递 + 两 worker 部署）。
> 参数取自制表时的 `settings.py` 默认值与 `.env`。

---

## 1. 现象

用户侧看到的三件事，其实是同一条链上的三个环节：

| 现象 | 用户原话 |
|---|---|
| 后端整体卡住，请求不出结果 | "多次 crash 导致后端卡死" |
| 日志一直在刷恢复，但什么都没恢复出来 | "一直在恢复" |
| 没点过的任务自己又开始跑 | "突然之间又复活了" |

---

## 2. 机制：为什么"复活"

### 2.1 三个参数决定的判定

| 参数 | 值 | 含义 |
|---|---|---|
| `LEASE_DURATION_SECONDS` | 30 | 租约时长 |
| `HEARTBEAT_INTERVAL_SECONDS` | 10 | 心跳续租间隔 |
| `REAPER_INTERVAL_SECONDS` | 15 | reaper 扫描间隔 |

### 2.2 复活链（`src/aegra_api/services/lease_reaper.py` + `worker_executor.py`）

```
进程/事件循环卡住（>30s）或进程真的死了
  → _heartbeat_loop 停止续租（worker_executor.py:543，它是**同一个事件循环里的 asyncio 任务**）
  → lease_expires_at 过期
  → reaper 15s 后扫到 status='running' 且租约过期
  → 改回 pending、claimed_by=NULL、_retry_count+1、重新入队
  → 另一个 worker 捞起**同一个 run_id**，从最后一个 checkpoint 重跑
  → 原来那个进程如果只是冻住：解冻后心跳发现 claimed_by 变了 → 自己取消
```

### 2.3 关键认知：租约过期 ≠ 进程死了

租约只说明"没人续租"。而**续租是跑在业务同一个事件循环里的协程**——事件循环一停，续租立刻停。

这个部署里事件循环被焊住是**可达的**：子 agent 的 `CodeInterpreterMiddleware`（`graphs/domain_agents.py:53`，`timeout=120`）在进程内跑 JS（`langchain_quickjs/_repl.py:719` 的 `ctx.eval_async`），最长可占住 120 秒 = 租约的 4 倍。

所以"活着但卡住"和"死了"在现有实现里一模一样，系统一律按"死了"处理 → 复活。

### 2.4 复活的两种代价

| 代价 | 说明 |
|---|---|
| 重复花 token | 冻住期间 provider 那边照样在生成、在计费；解冻后到被取消之前还要再算一段；同时新 worker 从 checkpoint 重跑，等于把没落盘的工作重做一遍 |
| checkpoint 分叉 | 两个执行者持有同一个 `thread_id`，各自的 checkpoint 链独立（按 `(thread_id, checkpoint_ns, checkpoint_id)` 存）。`/threads/{id}/state` 会取"最后一个写的人"那条链，`check_async_task` 读到的 `run.status` 与 `thread.values` 可能来自不同执行者 |

状态本身不会冲突：`finalize_run` 是 CAS 的（`WHERE status IN ('pending','running')`），谁先落终态谁赢，输的那次只记一条 `Skipped finalizing terminal run`；被取消的一方走 `_lease_loss_cancellations` 分支**故意不 finalize**（`run_executor.py:80`），把终态让给接手者。丢的是那次的工作量和 token。

### 2.5 数据：这件事有多常见

库里历史累计（`runs.execution_params->>'_retry_count'`）：

| `_retry_count` | 最终状态 | 数量 |
|---|---|---|
| 1 | success | 3 |
| 0 | success | 388 |
| 0 | interrupted | 124 |
| 0 | error | 19 |

即：**只有 3 个 run 被复活过，且都最终成功**。这不是高频成本问题，是语义问题——没人希望自己没点过的东西自己重跑。

---

## 3. 排查手册（复制即用）

### 3.1 先分清"卡在哪一类"

| 症状 | 指向 | 一句话确认 |
|---|---|---|
| 日志有大段时间空档（≥60s），空档前最后一条是子 agent 的工具调用 | **事件循环被焊住**（quickjs / 同步 IO） | 看空档前的 `tool` 日志 + 进程 CPU |
| `pg_stat_activity` 里有 `idle in transaction` 且很老 | 挂死事务占着锁 | 见 §3.3 |
| 请求 30s 后报 `PoolTimeout` / `QueuePool limit` | 连接池被占满 | 见 §3.4 |
| 日志每几十秒出现一次 `Lease reaper started` | worker 在反复重启 | 见 §3.5 |
| `pending` run 越积越多，reaper 一直在 `Re-enqueueing stuck pending runs` | 并发额度不够或 worker 全卡住 | 见 §3.6 |
| Redis 相关 warning，`Redis unavailable` | Redis 抖动 | 见 §3.7 |
| 崩溃前是读/写大 xlsx | 大 state 序列化 / Windows 杀软扫描 | 见 §3.8 |

### 3.2 日志

```bash
# 恢复类事件的总量（正常部署里这些数字应该很小）
grep -c 'Reaping crashed worker runs'            logs/aegra.log
grep -c 'Re-enqueueing stuck pending runs'       logs/aegra.log
grep -c 'Run exceeded max retries'               logs/aegra.log
grep -c 'Lease lost, cancelling job'             logs/aegra.log

# 重启风暴：每次启动每个 worker 打一条，出现 2 条 = 一轮启动
grep -c 'Lease reaper started'                   logs/aegra.log

# 真正的错误
grep -E 'Error in lease reaper|PoolTimeout|QueuePool limit|deadlock|Worker job failed' logs/aegra.log | tail -20

# 时间空档：找出本进程生命周期内 ≥60s 没有日志的区间，空档前最后一条就是焊住的位置。
# 注意：aegra.log 会跨多次重启，跨重启的空档没有意义（看相邻两条是否落在同一次启动内）。
# 本机 PATH 上的 `python` 是 Windows Store 壳（exit 49），用 uv 的；中文输出要带 UTF-8 环境变量：
PYTHONIOENCODING=utf-8 uv run python -c "
import re, datetime
spots = re.findall(r'\"timestamp\": \"([^\"]+)\"', open('logs/aegra.log', encoding='utf-8', errors='ignore').read())
ts = [datetime.datetime.fromisoformat(s) for s in spots]
for g, a in sorted(((b - a).total_seconds(), a) for a, b in zip(ts, ts[1:]))[-5:]:
    print(f'{g:7.1f}s  起于 {a.time()}')
"
```

### 3.3 数据库：锁与挂死事务

```sql
-- 谁在等锁 / 谁挂着事务不提交
select pid, state, now() - state_change as stuck_for, left(query, 80)
from pg_stat_activity
where datname = current_database() and state <> 'idle'
order by state_change
limit 10;

-- 长事务（最危险：持锁但不动）
select pid, now() - xact_start as xact_age, left(query, 80)
from pg_stat_activity
where datname = current_database() and state like 'idle in transaction%'
order by xact_start;

-- 超时配置（这三个为 0 = 可以无限等，见 §5）
show statement_timeout;
show lock_timeout;
show idle_in_transaction_session_timeout;
```

解除阻塞（确认是自己人之后再执行）：

```sql
select pg_terminate_backend(<pid>);
```

### 3.4 连接池

```bash
grep -cE 'PoolTimeout|QueuePool limit|could not obtain' logs/aegra.log
```

`SQLALCHEMY_POOL_SIZE + SQLALCHEMY_MAX_OVERFLOW`（asyncpg 引擎）与 `LANGGRAPH_MAX_POOL_SIZE`（checkpointer/Store 共用的 psycopg 池）之和要能覆盖 `WORKER_COUNT × N_JOBS_PER_WORKER`。**两个池都没有设 `server_settings`**（`src/aegra_api/core/database.py`），所以拿不到连接时会等 30s 再抛。

### 3.5 重启风暴

```bash
grep -c 'Lease reaper started' logs/aegra.log
grep -c 'Worker started'       logs/aegra.log
```

两者数量一致是正常的（每个进程各一条）。**每出现一轮（=2 条）就代表一次重启**，与 `Lease lost` / `Worker job failed` 的时间戳对齐，就能定位"是谁让进程死的"。

### 3.6 并发额度

```sql
-- 堆积的老 pending
select count(*), min(created_at), max(created_at)
from runs where status = 'pending';

-- 同时 running 的峰值
select count(*) from runs where status = 'running';
```

注意 `worker_executor.py:227` 是**先 `semaphore.acquire()` 再 BLPOP**：每个空闲 worker loop 会白占一个槽位等队列，实际可跑并发略低于 `WORKER_COUNT × N_JOBS_PER_WORKER`（当前 2 × 10 = 20）。

### 3.7 Redis

```bash
docker ps --filter name=redis6
docker exec redis6 redis-cli ping           # 期望 PONG
docker exec redis6 redis-cli llen aegra:jobs
docker exec redis6 redis-cli info clients
```

队列是纯 list（`rpush` / BLPOP）：**pop 之后进程死掉，job 就没了**，只能等 reaper 的 `pending` 兜底（`STUCK_PENDING_THRESHOLD_SECONDS=120`）。

### 3.8 大 state / 磁盘

```sql
-- 最大的消息通道（工具把整张表塞进上下文后会很大）
select thread_id, length(blob)
from checkpoint_blobs where channel = 'messages'
order by 2 desc limit 5;
```

```powershell
Get-Process python | Sort-Object WorkingSet -Descending |
  Select-Object -First 4 Id, CPU, @{n='MB'; e={[int]($_.WorkingSet/1MB)}}
```

---

## 4. 处置方案 v2

### 4.1 目标

**崩溃 → 直接终态失败，不复活。** 恢复由人来决定，不由系统自动重跑。

### 4.2 要做的四条（一次上线，顺序不能拆）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src/aegra_api/services/lease_reaper.py` | 删掉"改回 pending + requeue + `_retry_count` 累加"，改为 `finalize_run(status="error", error="worker lost")`；返回 False 直接退出（**幂等**） |
| 2 | `src/aegra_api/services/worker_executor.py` | 起**一个进程级**存活线程：每 5s `SET aegra:alive:<instance_id> 1 EX 15`。注意是每进程 1 线程 + 1 key，不是每 run |
| 3 | `src/aegra_api/services/lease_reaper.py` | 判死三段：① 租约过期 + alive 在 → **续租**（进程活着，只是冻住）；② 租约过期 + alive 不在 → `error`；③ alive 在但超过 `BG_JOB_TIMEOUT_SECS` → `timeout` |
| 4 | `src/aegra_api/settings.py` | 移除 `BG_JOB_MAX_RETRIES` 的用途（配置项可留，不再读） |

**1 和 2 必须同一次上线。** 只做 1 会把"焊住 30 秒的正常 run"判成失败，比现状更糟。

### 4.3 明确不做

| 不做 | 原因 |
|---|---|
| 按 run 建 alive key | 进程级就够，少一份状态 |
| epoch / fencing token | 要拦旧 worker 的 checkpoint 写入得改 checkpointer，第三方包，收益远小于成本 |
| 任何形式的自动重试（哪怕一次） | 这正是要消掉的行为 |
| 动 `stuck_pending` | 那是"从没开始跑"，与崩溃无关 |
| 改 quickjs 的 `timeout=120` | 有了 alive key 它冻住不再导致误判；性能问题单独议 |

### 4.4 发版仍会接着跑

优雅停机路径 `_requeue_drained_runs`（`worker_executor.py:495`）自己把 run 重置成 `pending` 再 rpush，**不依赖 reaper**。所以：

- 正常发版 / Ctrl+C → 在跑的 run 被接手，继续跑（行为不变，`WORKER_DRAIN_TIMEOUT=30`）
- 硬崩（kill -9 / OOM / 进程消失）→ 变 `error`，不复活

### 4.5 验收

| 场景 | 期望 |
|---|---|
| `taskkill /F` 掉一个 worker | run → `error`；父线程收到 `has stopped (error)` 卡片；**没有**新 run 出现 |
| 子 agent 里跑一段 60s 的 JS（人为焊住循环） | 租约被续，run 正常跑完，不出现 error |
| 优雅重启 | 在跑的 run 被接手跑完 |
| reaper 连跑两轮同一批 | 只投一张卡、只记一次 error（幂等） |

单测：4 条（alive 在→续租、alive 不在→error、CAS 晚到 success 不覆盖、stuck_pending 不受影响）。

### 4.6 残余风险

存活线程自己挂掉（未捕获异常）→ 进程被误判成死。所以那个线程体必须写成 `while True: try: ... except: log`，**绝不能让它退出**。

### 4.7 下一批

`POST /custom/tasks/{task_id}/retry` + 前端按钮（终态 `error` / `timeout` 才显示）。
现在失败卡片已经能送到父线程，用户能看到 `stopped (error)` 再重新发问，所以按钮不是上线阻塞项。

---

## 5. 顺带发现（未改，建议单独立项）

**数据库三个超时全是 0**，且两个连接池都没有 `server_settings`（`src/aegra_api/core/database.py`）：

```
statement_timeout                    = 0   ← 语句可以无限等
lock_timeout                         = 0   ← 等锁可以无限等
idle_in_transaction_session_timeout  = 0   ← 挂着的事务永不回收
```

一条卡住的语句会永久占着一个连接；占满后 `N_JOBS_PER_WORKER × WORKER_COUNT` 个 job 全停下，正好喂给 `stuck_pending` 的无界重入队循环。

建议：`lock_timeout=5s`、`idle_in_transaction_session_timeout=60s`，`statement_timeout` 留 300s 或不开。**把"永久挂住"变成"报错 + 重试"**，是这一整篇里性价比最高的一处。

另外已修：`/custom/spaces` 与 `/custom/spaces/{name}/files` 原本在 `async def` 里同步 `rglob`（`f061bf0` 已挪进 `asyncio.to_thread`）。

---

## 6. 外部依据

| 方案 | 结论 | 来源 |
|---|---|---|
| Airflow zombie task | 调度器独立检测心跳超时 → **直接标 failed，不复活**；同一 zombie 被重复处理会导致重复回调（我们因此要求判死幂等） | [Airflow Troubleshooting](https://airflow.apache.org/docs/apache-airflow/3.0.5/troubleshooting.html)、[airflow#51969](https://github.com/apache/airflow/issues/51969) |
| Celery `acks_late` | worker 丢失即重入队（at-least-once），前提是任务幂等；踩过"业务阻塞把 ack 冻住"的坑——与本文 §2.3 同病 | [acks_late 实践](https://www.php.cn/faq/1931226.html) |
| Temporal | activity 级 `heartbeat_timeout` + `start_to_close_timeout` 是两个**独立**判据；重放依赖确定性 + 幂等 activity | [Durable Execution 对比](https://blog.csdn.net/gitblog_00695/article/details/153081149) |
| Kleppmann fencing token | 租约只声明"协调系统认可谁"，抢不走旧进程的 CPU；要拦旧写入得靠存储层拒绝旧 token | [Epoch Fencing](https://zhuanlan.zhihu.com/p/2071270019913273721) |
| LangGraph durable execution | 恢复是"从节点起点重放"，副作用必须包进 task；我们的副作用是 LLM 调用，重放就是重复花钱 | [durable execution](https://blog.csdn.net/czhcc/article/details/153637566) |

**为什么抄 Airflow 而不是 Celery/Temporal**：后两者敢重放，是因为要求任务幂等。我们的子 agent 不幂等（LLM 花钱、写文件、跑计算），at-least-once 重放的成本是真金白银。所以"判死即失败 + 人工重试"才是对的选择。

---

## 7. 待办

- [ ] §4.2 四条改动（1 + 2 同批）
- [ ] §4.5 四个真机场景
- [ ] ADR：为什么不复活、为什么不做 fencing
- [ ] `retry` 端点 + 前端按钮（下一批）
- [ ] §5 三个 DB 超时（单独立项）
