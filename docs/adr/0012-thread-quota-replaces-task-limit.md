# 用 thread 配额替代派活并发上限

2026-10-19 起，`MAX_ACTIVE_TASKS`（一个主管最多几个并发子 agent）连同它的进程内账本
一起删除，业务约束改成：**一个账号最多拥有 N 个会话（thread），子 agent 的线程也计入**。

## 为什么不是"再加一道限额"

旧机制（[ADR-0008](./0008-active-task-limit-is-per-process.md)，已 superseded）要判
"读活跃数 → 判上限 → 占名额"原子，而这份账本在进程内存里，多 worker 部署下上限必然
退化成 `N × MAX_ACTIVE_TASKS`。要么加数据库名额表（一次派活一次事务），要么接受不准。

新约束换了个计数对象：

| | 旧：活跃子任务数 | 新：用户拥有的 thread 数 |
|---|---|---|
| 计数在哪 | 进程内存（`_LEDGER`） | 数据库（`thread` 表） |
| 跨进程准确 | ❌ 需要新表 | ✅ 天然准确 |
| 谁来判 | 图里的拦截层（`AsyncTaskGuard`） | HTTP 层的 `@auth.on.threads.create` |
| 超额行为 | 工具返回错误文本，模型自己重试 | 403，用户自己删会话 |
| 能否自己恢复 | 等任务完成 | 删掉不用的会话 |

关键在于：**子 agent 的每一轮执行都占一个 thread**，所以限制 thread 数顺带就限制住了
子 agent 的总量，而且这个数在一个天然唯一的地方（数据库）维护，不需要额外的账本。

## 机制

### 1. 子线程记归属（`graphs/async_task_guard.py`）

子线程是服务账号用 service token 建的，`thread.user_id = subagent_service`，按属主根本
数不到终端用户头上。所以派活时把父用户写进 metadata：

```python
client.threads.create(metadata={
    "owner_identity": <主管的用户 identity>,   # 取自服务端注入的 langgraph_auth_user
    "parent_thread_id": <主管 thread_id>,
})
```

### 2. 建会话时判配额（`custom/custom_auth.py`）

`POST /threads` 已经会 dispatch `@auth.on.threads.create`（`src/aegra_api/api/threads.py`），
所以不用改 `src/`：

```python
@auth.on.threads.create
async def _quota_threads_create(ctx, value):
    # 超管放行
    used = count(*) where metadata->>'owner_identity' = 调用者
                   and metadata->>'parent_thread_id' is null     # 只数主管线程
    if used >= MAX_THREADS_PER_USER: raise 403
    # 顺手把 owner_identity 写进 metadata（普通用户建的主管线程）
```

- 只数 `parent_thread_id IS NULL` 的线程：子线程跟着主管线程走，单独数会把同一份
  占用算两遍（建一个主管 + 派 3 个子 = 4，但用户眼里只开了 1 个会话）。
- 超管（权限含 `write`）不受限：管理员要能排查别人的问题。
- 上限在 `custom/quota.py::MAX_THREADS_PER_USER`（env `MAX_THREADS_PER_USER`，默认 200）。

### 3. 删会话时级联删子线程（`custom/custom_auth.py`）

子线程归服务账号，用户既看不见也删不掉。不级联的话它们成为永久孤儿：一直占着
checkpoint 存储，而且**因为父线程被删了，连数都数不回来**。

```python
@auth.on.threads.delete
async def _cascade_child_threads(ctx, value):
    # aegra 在 ownership 校验之前调用本 handler，所以自己先确认这个 thread 属于调用者
    if owner != ctx.user.identity and 非超管: return None   # 交给原生接口 404
    children = 所有 metadata->>'parent_thread_id' = 本线程 的行
    for c in children: adelete_thread(c)      # 先删 checkpoint
    delete from thread where thread_id in children   # 再删行
    # 父线程交给原生 DELETE /threads/{id} 继续删
```

顺序不能反（与 aegra 原生 `delete_thread` 同一约定）：先删行的话，`adelete_thread` 一失败就留下
再也找不到的孤儿 checkpoint；先删 checkpoint 则失败时行还在，客户端重试即可。

## 删掉的东西

| 位置 | 删了什么 |
|---|---|
| `graphs/limits.py` | 整个文件（`MAX_ACTIVE_TASKS` 的唯一来源） |
| `graphs/async_task_guard.py` | `_LEDGER` / `_LEDGER_LOCK` / `_active_count` / `_over_limit` / `_reserve` / `_settle` / `_start_args`、`_parent_key`、`threading` 与 `uuid` 导入 |
| `graphs/prompts/__init__.py` | `MAIN_SYSTEM_PROMPT` 里的 `{max_active_tasks}` 替换 |
| `graphs/prompts/main.md` | "同时最多 N 个后台任务" → "可以同时派多个，它们在服务端排队执行" |

派活路径现在是：校验 `subagent_type` → 转 spaces → 建子线程（带归属 metadata）→ 建子 run。
没有名额判定，也没有占位/结算。

## 保留的东西

- **`_refreshed_sync` / `_refreshed_async`**：不再是"判上限前刷新"，但仍用于
  `_deliver` 判断子任务是否已终态。`async_tasks[id].status` 只是快照，不核实就会漏投。
- **`spec_name()`**：任务记录里还要写 `agent_name`。

## 代价与边界

- **超额是硬拒**：403，用户必须删会话。不像排队那样自动恢复，但语义清楚
  （"你开了太多会话"而不是"系统忙"）。
- **子 agent 的算力不再有业务上限**：一个用户可以用 1 个会话派出 20 个子 agent 并发跑。
  计算开销由 worker 队列承接（`WORKER_COUNT × N_JOBS_PER_WORKER`，当前 2×10 = 20 个并发
  run），超出就排队。要按用户限流得在 run 层做，不在本 ADR 范围。
- **配额不是严格的**：`@auth.on.threads.create` 里 `count` 与 `insert` 不在同一事务
  （aegra 在 handler 之后才 insert）。同一用户在毫秒内并发建 N 个会话，可能多建几个。
  `WORKER_COUNT=2` 下这是已知的小窗口；要严格就得把计数与插入塞进一个事务，
  那要改 `src/`，本期不做。
- **子线程仍是服务账号持有**：前端看子 agent 的输出必须经 `custom/frontend_api.py` 的代理
  （ADR-0007 不变）。本 ADR 只加了 metadata，没改归属。

## Considered Options

- **数据库名额表**（ADR-0008 的升级方案）：跨进程严格，但每次派活一次事务 + 一张新表 +
  "派活了但没写表"的补偿。为 2 worker 的部署付这个复杂度不值。
- **不计子线程，只限制主管会话数**：最省，但一个用户开 1 个会话派 100 个子 agent 也不受限，
  等于把限额做成了装饰。
- **按活跃 run 数限流**：语义最贴资源，但 aegra 的 `runs` 表没有按用户的索引，且子 run 的
  `user_id` 是服务账号，仍要先解决归属问题——绕回本文的方案。
- **thread 配额（选中）**：计数对象天然唯一（数据库）、跨进程准确、不动 `src/`，
  代价是"余额要用户自己删会话腾"。

## Consequences

- 新增 env：`MAX_THREADS_PER_USER`（默认 200）。改完重启生效（`custom/quota.py` 在 import 时读）。
- 建会话被拒时返回 **403**，body 是 `{"detail": "会话数已达上限（N），请先删除不用的会话再新建"}`
  ——前端已按 `detail` 取文案（`web/src/api/client.ts::errorText`），无需改代码。
- 删会话现在会连带删子线程的 checkpoint：一次删除可能比过去慢（每个子线程一次
  `adelete_thread`）。若某个子线程删除失败，整次请求 500，父线程仍在（可重试）。
- 旧数据（本 ADR 之前的子线程）没有 `parent_thread_id` metadata，不会被级联删；
  它们的 checkpoint 会一直留着。需要清理时按 `user_id = 'subagent_service'` 找孤儿。
- 提示词不再提并发上限，模型可以一次派多个；它的"等得久"来自服务端排队，不是被拒。