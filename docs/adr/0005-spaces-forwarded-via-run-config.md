# 用户 spaces 经 run config 转发，由拦截层塞入 start_async_task

一个用户可有多个 Space，子 agent 的工具必须知道"这次任务允许哪些空间"。权威来源是主管 run 的 `configurable["langgraph_auth_user"].user_spaces`（服务端注入，客户端伪造不了）；但 deepagents 的 `start_async_task` 创建子 run 时不带任何 `config`（deepagents 0.7 的 AsyncSubAgent spec 没有 config 字段，也没有关闭内置工具的开关）。而 aegra 会原样透传客户端 body 里的 `config.configurable` 任意键（只强制覆盖 thread_id/run_id/langgraph_auth_user）。

因此决定：在 `graphs/` 写一个自定义 middleware，用 `wrap_tool_call` 拦截 `start_async_task`——跳过 deepagents 的 handler，自己用服务 token（ADR-0004）建 thread + `runs.create(..., config={"configurable": {"user_spaces": [主管当时的真实 spaces]}})`，并在此处执行"活动任务 ≥3 拒绝"的并发上限；`check/update/cancel` 沿用内置工具。子 agent 图侧从 `configurable["user_spaces"]` 读允许集（缺失即 fail-closed 空集），文件访问由共享的 `SpaceBackend` 单点兜底，只有绕过后端的 XLSX_TOOLS 在 tool 层校验。

考虑过的选项：spaces 写进任务描述文本——LLM 可被 prompt injection 操纵，是假隔离；monkey-patch deepagents 的 `_build_start_tool`——升级即碎；服务账号拥有全部 spaces——把攻击面留成"诱导主管替你读别的空间"。
