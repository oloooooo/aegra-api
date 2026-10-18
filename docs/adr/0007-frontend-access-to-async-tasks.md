# 子 agent 的流与状态经 custom/ 代理暴露；派活前核实 async_tasks 真实状态

前端要在一个界面里同时看到「主管的对话流」和「每个后台子 agent 的实时进度」。这暴露了两个此前
没有的问题，两个决定一起记在这份 ADR 里（一个是访问控制，一个是状态一致性）。

## 决定 1：子 agent 的流与状态走 `custom/` 代理，不放开 ownership

子 agent 的 run 在**独立 thread** 上，属主是服务身份 `subagent_service`（ADR-0004）。终端用户拿
`task_id` 直接开原生流会被挡：

```
GET /threads/{子线程}/state   → 404 not_found   （实测，root 也不行）
```

aegra 的每个 thread 查询都带 `user_id == user.identity` 过滤（`threads.py`）。

**选中的做法**：在 `custom/frontend_api.py` 加带鉴权的代理——
`GET /custom/tasks/{task_id}` 与 `/custom/tasks/{task_id}/stream?parent_thread_id=...`。
授权条件是「父线程属于当前用户（借原生接口的 ownership 校验）**且** 父 state 的 `async_tasks` 里
确实有这个 `task_id`」；通过后再用服务 token 去读子线程 / 订阅子 run。服务 token 不下发浏览器。

### Considered Options

- **放开 `threads.py` 的 ownership 过滤**（让父线程属主也能读子线程）：改动最小，但这是把
  「按用户隔离」的安全模型改成「按调用链推导」，一旦漏判就跨用户读数据；且要改 `src/`。
- **让拦截层把子 agent 的输出经 `runtime.stream_writer` 转发到父流**：需要改 deepagents 的
  async subagent 中间件或自建一套 topic 中继，且父 run 在子 run 运行期间往往已经结束（主管派完活
  就返回），没有可写的地方。
- **只轮询父 state 的 `async_tasks`**（不加接口）：看不到子 agent 的中间输出，只有 running/success。
- **`custom/` 代理**（选中）：不动 `src/`，鉴权复用既有 thread 归属 + 任务注册表，两端解耦。

### Consequences

- 前端多一跳，且必须带 `parent_thread_id`——它是授权凭证，不是可选参数。
- 代理是**单次 Run 订阅**：子 agent 被 `update_async_task` 再跑一轮时会换 `run_id`，前端要重连。
- 子 run 必须在创建时声明 `stream_mode=["messages","custom"]`（拦截层已声明）。不声明时 aegra
  默认只产 `values`（`run_executor._resolve_stream_modes` 缺省 `["values"]`），代理订不到 token 流。
- 若将来要支持「一次订阅覆盖该 task 的全部轮次」，改法是在代理里按 thread 而非 run 订阅。

## 决定 2：派活前向子 run 核实状态，不信 state 里的 `async_tasks.status`

`async_tasks` 的 `status` 只由 `check_async_task` 更新。走完成回写路径时（`CompletionNotifierMiddleware`
→ 唤醒主管 → 主管直接转达）**不经过 check**，于是实测出现：

```
async_tasks: {'d9eb5ea6': 'running'}   ← 子 run 早已 success，父 state 仍是 running
```

`AsyncTaskGuard._active_count` 把非终态都算活跃，因此 3 个已成功的任务会**永久占满**
`MAX_ACTIVE_TASKS=3` 的名额，第 4 次派活直接被拒。

**选中的做法**：`_start_sync` / `_astart` 在判上限前先 `_refreshed_sync` / `_refreshed_async`
——对每个非终态任务用 spec 的客户端查一次子 run 的真实 `status`，把变化写回 state 参与计数，
并随本次 `Command` 一起落库。查询失败时保持原状态（不阻断派活）。

### Considered Options

- **提高 `MAX_ACTIVE_TASKS` 或改成按派发次数计数**：治不了「名额被僵尸占满」，只是把上限挪远。
- **回写完成后让 notifier 顺手把状态改成终态**：notifier 拿不到父 state 的写权限（它只是往父线程
  发一条消息），且需要新表或新接口来落状态。
- **前端各自刷新**：只解决显示，不解决派活被拒。
- **拦截层派活前核实**（选中）：唯一的判上限的地方就是拦截层，修在这里最靠根因。

### Consequences

- 每次派活多 N 次 `runs.get`（N = 当时非终态任务数，≤3），可接受。
- `GET /custom/tasks/{id}` 复用同一套核实逻辑，前端拿到的状态和拦截层判上限的口径一致。
- state 里的 `status` 仍是「上次核实时」的快照，不是实时值；文档（`docs/streaming.md` §8.2、
  `docs/frontend-api.md` §5.4）明确要求前端用代理接口而非直接读 state。
