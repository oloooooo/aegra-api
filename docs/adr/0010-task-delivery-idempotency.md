# 任务结果投递：事务性发件箱 + 单一分发者

结果投递（见 `docs/design.md §2.4`）要满足两件事，而它们在多进程下会互相干扰：

1. **不丢**：子 agent 跑完时父线程可能早就答完、没人在等它，结果得主动送过去；
2. **不重**：同一个 `(task_id, run_id)` 的结果，只能出现在父线程的对话里一次。

本文记录为什么放弃"投递认领 + 时间窗口"，改成"发件箱 + 只由一个组件开唤醒 run"。

## 旧设计的三个毛病（实测踩到）

旧实现是 `async_task_delivery` 认领表 + 两个时间常量（`_IN_FLIGHT_SECONDS=8`、
`_STALE_SECONDS=15`）：

- **用时间窗口冒充状态**。8 秒和 15 秒都在猜"投递大概多久落地""进程大概什么时候死"。
- **一张表干两件不相关的事**：`(task_id, run_id)` 主键管结果幂等（正确性），
  `parent_thread_id + claimed_at` 管"同线程只允许一个投递 run"（调度）。后者塞进前者后，
  查询只能按 `parent_thread_id` 扫、认不出调用方，于是**同一次 hook 里第 1 个任务认领后
  就把同批后面的全卡住**——三个子 agent 几乎同时完成时漏投 1/3（日志复现：
  `task_delivery_skipped_claim` 连续出现，剩最后一个直到用户再说一句话才补）。
- **认领与投递不原子**，"15 秒过期"是在猜崩溃，不是在检测崩溃。

外部结论也一致：Azure 的 Idempotent Consumer 模式指出消息投递只能做到 at-least-once，
正确的做法是**消费者侧持久去重键**，而不是幻想跨系统 exactly-once；Stripe 的幂等键把
幂等放在"写"这一层，不是放在"投递前抢票"这一层。

## 新机制：发件箱 + 单一分发者

### 一、产生：`finalize_run` 写一行（src）

任何 run 转终态都会经过 `run_status.finalize_run`。若该 run 所属线程的 metadata 里有
`parent_thread_id`（派活时 `AsyncTaskGuard._thread_meta` 写入的归属标记，主管线程没有），
就在**同一个事务**里插一行 `async_task_outbox`：

```sql
INSERT INTO async_task_outbox (task_id, run_id, parent_thread_id, agent_name, terminal_status, summary)
ON CONFLICT (task_id, run_id) DO NOTHING      -- worker lease 过期重跑也不会插第二行
```

- `task_id` = 子线程 id，`run_id` = 这次执行；
- `summary` = 终态 state 里最后一条消息（截断），分发者不需要再读子图；
- `interrupted` 不入账：那说明子 run 被取消或被 `update_async_task` 顶掉，这一轮结果不完整，
  后面那次 run 会自己入账。

### 二、分发：只有一个组件会开唤醒 run（custom）

`custom/outbox_dispatcher.py`，与 `lease_reaper` / `cron_scheduler` 同款的进程内循环：

```
每 1s（可调）：
  1. 领批  SELECT ... WHERE status='pending' AND next_attempt_at<=now()
           AND (lease_expires_at IS NULL OR lease_expires_at<now())
           ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 50
           并写 lease_expires_at = now()+30s
  2. 按 parent_thread_id 分组
  3. 每组：SELECT thread FOR UPDATE          ← 锁住线程行
           有 pending/running run?  → 有：退避 2s 后再看（那轮 hook 会投）
                                     无：建**一个**唤醒 run，input = 这批结果的消息
           建完把这批行标 delivered
```

三个关键性质：

- **不依赖 Redis**。正本是 DB 轮询；Redis 只在 `executor.submit` 那一步被用到，挂了就退避
  重试，恢复后自动补投（旧版是"只试一次，然后永久丢失"）。
- **不重复**。唤醒 run 用**确定性 run_id**（`uuid5(parent + 排序后的 (task,run))`）：
  崩在"建 run 成功、还没标 delivered"之间，重试会撞主键，据此认定"已经建过了"。
- **不开并发的 run**。`SELECT thread FOR UPDATE` + 活跃 run 检查在同一事务里，而 run INSERT
  会对 thread 行取 `FOR KEY SHARE`——多进程下第二个分发者会阻塞到第一个提交，然后看到新
  run 并让路（与 `thread_ttl.py` 处理同一竞态的做法一致）。

### 三、消费：父 run 的 hook 写 state 并 ack（graphs）

父线程有活跃 run 时，分发者不动手——那一轮的 `AsyncTaskGuard.abefore_agent` /
`aafter_agent` 会把终态结果写进 state。写之前先 `POST /custom/tasks/{id}/deliver-claim`
**把发件箱行从 pending 原子翻成 delivered**：

```sql
UPDATE async_task_outbox
   SET status='delivered', delivered_at=now()
 WHERE task_id=:t AND run_id=:r AND status IN ('pending','failed')
RETURNING task_id          -- 0 行 = 别人已经投了，放弃
```

抢到翻牌权才有资格投递。**没有 advisory lock、没有让路窗口**——单行条件 UPDATE 就是原子的，
一行一判，同批 N 个任务互不影响（这正是旧版漏投的根因）。

写 state 失败时调 `DELETE` 把行退回 `pending`，分发者会接手。

### 四、判重仍然两层，各管一段

| 层 | 位置 | 管什么 |
|---|---|---|
| 发件箱行状态 | `async_task_outbox.status` | **这次结果投没投过**（跨进程、跨 run 的唯一真相） |
| `delivered_run_id` | 父 state 的 `async_tasks[task_id]` | **同一个 run 只投一次**（模型自己 `check_async_task` 也算已投，见 `_mark_checked`） |
| 投递消息本身 | 父 state 的 messages | 兜底：消息在、行状态丢了（比如 ack 前崩溃）也不重复写 |

## 三种并发场景的行为

| 场景 | 行为 | 幂等 |
|---|---|---|
| 同一轮里多个任务几乎同时终态，主管在跑 | 那轮的 `after_agent` 逐个 ack + 合并成一条 state 更新 | 一次 checkpoint，天然幂等 |
| 多个子 agent 同时收尾，主管空闲 | 分发者一个 tick 领到全部 → 只开**一轮**唤醒 run，input 拼了所有结果 | 一批一个 run，确定性 run_id |
| 结果到达时主管正在跑唤醒 run | 分发者看到活跃 run → 退避重看；那轮的 hook 会 ack 并写 state | 不产生第二个 run |
| 分发者崩在"建 run 后、标 delivered 前" | 租约过期后重试 → 撞确定性 run_id → 按已投递处理 | 不开第二轮 |
| 建 run 失败（Redis 挂 / 接口 500） | `attempts++`，指数退避（封顶 60s），最多 10 次后标 `failed` | 结果保留，恢复后补投 |
| `update_async_task` 后的新 run | 新 `run_id` → 新主键 → 再投一次 | 有意为之 |

## Considered Options

- **旧版：认领表 + 8s/15s 时间窗口**（已废弃）：能互斥，但时间窗口在"同一 hook 多个任务"
  这个必然场景下会自阻塞，且崩溃语义靠猜。
- **给 `runs.create` 加客户端幂等键 + `multitask_strategy=reject`**（平台原生）：把"一线程一
  活跃 run"交给平台，同样能删掉认领表；需要改平台契约（`RunCreate.run_id`、`reject` 的
  原子实现），且只在"发 run"这一层判重，不解决"结果从哪来、谁负责重试"。
- **事务性发件箱 + 单一分发者（选中）**：产生端与投递端解耦，重试与崩溃恢复有明确落点，
  Redis 变成纯加速项而不是正确性依赖。代价是多一张表 + 一个后台循环。
- **LangGraph `interrupt()` + `Command(resume=...)`**：框架原生，但父线程整个等待期都停在
  `interrupted`（busy），用户中途发言要处理打断；interrupt 本意是给人工审批的。

## Consequences

- **多一张表**：`async_task_outbox`，一行 = 一次子 run 的终态结果。仍没有清理任务；
  需要时按 `delivered_at` 定期删旧行（删了最坏效果是"远古任务被重新投一次"）。
- **多一个后台循环**：每个进程一个，靠 `SKIP LOCKED` + 租约分工；空表时每 tick 一次索引扫描。
- **延迟**：正常情况下 ≤ 一个轮询间隔（默认 1s）；父线程忙时等那轮 run 的 hook，与旧版一致。
- **保底语义是 at-least-once**：ack 与 state 写入不在同一事务里，崩溃窗口内可能多投一次；
  投递消息带 `task_id` / `run_id` 标记，前端与 guard 都按标记判重，重复卡片比丢结果更可接受。
- **`interrupted` 的结果不主动投**：取消/被顶掉的子 run 没有完整结论，用户下次发言时由
  `check_async_task` 或下一次 hook 带出。
- 主管 run 中途想拿结果仍然只能靠模型自己 `check_async_task`（提示词里写成硬口径）；
  `after_agent` 写进 state 的结果模型已没机会转述，用户看到的是"答案 + 结果卡片"。

## 已知问题与残留风险

按“影响从大到小”排。每一条都注明了触发条件与目前的处理方式，改代码前先对一下。

| # | 问题 | 触发条件 | 影响 | 现状 |
|---|---|---|---|---|
| 1 | **超级用户的 space 语义有两套**：`allowed_spaces()` 只读 `user_space` 表，`frontend_api._own_space_names()` 却按“有 write 权限 ⇒ 全部 Space” | `is_super=true` 但 `user_space` 里没有行 | 前端能看到全部 Space，但主管/唤醒 run 算出 `specs = {}` → `Available types:` 为空、`check_async_task` 不在工具表 | 既有不一致，本次未收敛；给账号补 `user_space` 行即可绕过 |
| 2 | **唤醒 run 用的是“投递时”的权限，不是“派活时”的权限**（`_parent_user` 现查库） | 派活之后回收了某个 Space | 唤醒 run 少一个 spec → guard 的 `_spec_of()` 拿不到那条子 run 的 spec；结果仍由分发者投出，但 `state.async_tasks` 该条不会刷成终态 | 有意取舍（用当前权限更安全）；要严格就按“派活时把 spaces 写进子线程 metadata、顺流到发件箱行” |
| 3 | **发件箱的 `summary` 列对子 run 恒为 `(no output)`**：子 run 只声明 `stream_mode=["messages","custom"]`，没有 `values` 事件，`finalize_run` 的 `output` 是空字典（`run_executor` 只在 `values` 事件里填 `result.data`） | 恒成立 | 投递正文必须现查子线程；一旦服务自调失败（超时/网络/鉴权），卡片退化成 `(no output)` | 已用 `_child_summary` 现查兜底（5s 超时）；`summary` 列目前只是兜底值 |
| 4 | **“取全文”只有 `check_async_task` 一条路** | specs 为空（#1/#2）或工具未挂 | 模型无法自救，只能等投递或自己读空间文件 | 卡片只有 500 字摘要（带截断标记），所以这条路断了就是“看不到全文” |
| 5 | **分发者不知道“父线程长期 busy”** | 用户一直不停发消息 | 发件箱行长期 `pending`（`next_attempt_at` 每 2s 后移），没有告警 | 结果是“晚到”不是“丢”；需要时加 metric/告警 |
| 6 | **`_claim_batch` 没有 `claimed_by` 列**（只有租约时间） | 两个进程同时领到同一行 | 极小窗口内可能开两轮唤醒 run | 已留 `shortcut:` 注释；要根治就加 `claimed_by`（与 `runs` 表同款租约） |
| 7 | **ack 与 state 写入不同事务** | 进程在两者之间崩溃 | 可能多一张卡片（不会重复写 state，消息带 `task_id`/`run_id` 标记） | at-least-once 的固有代价 |
| 8 | **`failed` 行不再被分发者重试** | 连续 10 次建 run 失败 | 只剩父 run 的 hook 能把它写进 state；用户不再说话就一直看不到 | 有意为之（避免无限重试打爆日志/队列）；`delivery_api` 仍允许 guard 抢 `failed` 行 |
| 9 | **用户手动发起的 run 仍没有幂等键** | 前端重复提交 / 两个标签页 | 可能与唤醒 run 并发写同一线程（aegra 不拦同线程并发 run，`design.md §9 #4`） | `run_id` 幂等键只在分发者内部用；平台层要根治见下方 Considered Options |
| 10 | **子线程当父线程（嵌套派活）会缺 spaces** | 未来支持嵌套 | 子线程属主是服务账号，`user_space` 里没有它的行 → 唤醒 run specs 为空 | 当前禁止嵌套，不影响 |

配套的用户可见症状与处置见 `README_zh.md §8 常见问题`。
