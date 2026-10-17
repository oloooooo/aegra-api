# async subagent 使用静态服务身份

deepagents 的 async subagent 通过 HTTP 调用本服务时只带构建期写死的 `headers`，不携带终端用户的 JWT（`runs.create` 只发 thread_id/assistant_id/input）。鉴权启用后子 agent 的 run 会被 401 拒绝，因此我们决定：给子 agent 一枚构建期用 `JWT_SECRET` 铸造的长期静态 token，身份为固定服务账号（`subagent_service`，可用 `SERVICE_IDENTITY` 覆盖），`custom/custom_auth.py` 对该 identity 免 DB 查询直接放行（只读、无关联 Space）。

考虑过的选项：给 AsyncSubAgent spec 传 per-user token——deepagents 按 `(url, headers)` 缓存 client 且 spec 是图构建期固定的，做不到 per-request；改/patch deepagents 转发用户身份——升级即碎，且违反"只动 custom/、graphs/"的约束。

**后果**：该 token 泄露即可冒充服务账号读取知识空间根（仅限子 agent 图自身的工具范围内）；轮换方式是改 `SERVICE_IDENTITY` 或改铸造逻辑。子 agent 的 per-user 空间隔离不依赖此身份，见 ADR-0005。
