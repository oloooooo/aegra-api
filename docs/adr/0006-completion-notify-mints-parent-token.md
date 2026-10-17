# 完成通知回写父线程：现铸短时父身份 token，不用服务 token

async subagent 默认只有主管主动 `check_async_task` 才拿得到结果，没有"完成即推送"。
官方例子 `custom/middlewares/completion_notifier.py` 想补这个缺口，但直接跑不通，因为叠了三层断点：

1. 主管的 `start_async_task` 被 `graphs/async_task_guard.py` 拦截后只转发了 `user_spaces`，
   没转发 `parent_thread_id/parent_assistant_id/parent_user_identity/notify_url`，
   notifier 的 `_should_notify()` 拿不到父上下文 → 直接不发。
2. 例子用 `get_client()`（无 URL）→ 试图走 in-process ASGI transport，那只在官方
   LangGraph Agent server 里成立；aegra 是自己的 FastAPI（:2026），连不上 → 静默失败。
3. 就算连上，aegra `runs.create` 只许线程属主写（`runs.py` 查 `thread.user_id != user.identity`
   → 404）。子 agent 以**服务身份**（ADR-0004）跑，写不了 alice 的线程 → 404，且被 except 吞掉。

因此决定（ADR-0005 的延伸）：回写那一次调用**现铸一枚短时父身份 token**，以父用户自己写自己的线程。
- 拦截层把父上下文经子 run 的 `configurable` 转发（`parent_user_identity` 取服务端注入的
  `langgraph_auth_user`，客户端伪造不了）；没有鉴权上下文（本地 demo）就不转发，notifier 安静跳过。
- notifier 在 hook 内 `get_config()` 读到父上下文后，用 env 的 `JWT_SECRET` 铸一枚 `sub=父identity`、
  `exp=+5min` 的 HS256 token（不写进 config/DB，用完即弃），显式 `get_client(url=notify_url, headers=Bearer)` 回写。
- 于是唤醒 run 以**父用户身份**跑：spaces 正常、主管被唤醒后能正常读文件转达；归属校验通过。
- 去重按 `run_id` 而非 bool 位：静态图的 middleware 实例在同 worker 内被并发 run 共享，bool 会吞掉另一个 run 的通知。

考虑过的选项：
- **直接把调用者原始 JWT 塞 configurable 转发**——原始 token 会进 DB 的 run config，泄露面更大。
- **改 `runs.py` 给服务身份开跨线程写**——省掉铸 token，但要动 `src/`（AGENTS.md 默认禁），服务 token
  泄露即能写任意用户线程；且唤醒 run 会以服务身份跑（`user_spaces=[]` → `SpaceBackend` fail-closed），
  主管醒来只能转述内嵌摘要、读不了文件。要保住父身份就得让服务端信任客户端传的 `langgraph_auth_user`
  = 全量 impersonation，更糟。
- **不推送、只靠 `check_async_task` 拉**——最简零风险，但非主动，用户得再问一次。
