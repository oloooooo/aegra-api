# 服务端不引入 cancelled；取消与 HITL 共用 interrupted，用 interrupted_by 归因

> **已归档**：本文是历史记录，不再约束当前代码；现行决定见 [`../README.md`](../README.md) 总表。

三套词表打架：agent-protocol 0.1.6 与官方 Agent Server 的 `Run.status` 枚举都没有 `cancelled`（`[pending, running, error, success, timeout, interrupted]`）；deepagents 客户端把 `cancelled` 写在自己本地的 task 缓存里（`_TERMINAL_STATUSES`），且 cancel 工具**从不读服务端响应**；aegra 的 cancel 端点则收 `action=cancel|interrupt`（两者行为相同）且没有 `rollback`。

**决定**：服务端 `Run.status` 保持规范枚举（含与生产一致的 `running`），不引入 `cancelled`；取消与 HITL 都落 `interrupted`，新增非规范字段 `interrupted_by: "cancel" | "hitl" | null` 归因；cancel 动作词表取规范的 `{interrupt, rollback}`，`cancel` 作为 `interrupt` 的别名保留，默认值从 `cancel` 改为 `interrupt`；`rollback` 维持 422 不实现。

## Considered Options

- **服务端引入 `cancelled`**：客户端能识别，但规范与生产都容不下；deepagents 本地已记 `cancelled`，会形成双词表。
- **只用 `interrupted`、不加归因字段**：与生产一字不差，但运营无法区分"被取消（不可续跑）"与"HITL 暂停（可续跑）"。
- **实现 `rollback`**：规范要求"取消并删除该 run 及其 checkpoint"，官方 checkpointer 没有按-run 删除的实现（`langgraph-checkpoint-postgres` 只有整线程 `adelete_thread`），deepagents 也不发它。

## Consequences

- deepagents 侧：本地 `cancelled` 与服务端 `interrupted` 并存，两者都在它的终态集合里。`check_async_task` 总是查服务端，会把本地 `cancelled` 覆盖成 `interrupted`；`list_async_tasks` 对终态任务不发起实时查询，保留本地值。两种结果都是客户端自身行为，服务端无需适配。
- Supersede 杀的旧 run 也记 `interrupted_by="cancel"`（不为它引入第三个值）。
- 与规范有一处显式缺口：`rollback` 未实现（422），需在 OpenAPI/README 写明。
