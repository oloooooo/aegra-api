# ACP 内置于 aegra-api，不再抽独立包

上一轮的决定（旧独立包 `aegra-protocol` 的 ADR-0004；该仓库已不存在）是新建独立包：宿主传 `build_agent(checkpointer)` 工厂、包自建 checkpointer 与专用 schema、一个进程服务一个 agent。用户现在改主意：**不抽独立服务，只改 aegra-api**——要的是"服务型的 ACP"，随 aegra-api 一起部署、用 `aegra.json` 注册图。

**决定**：deepagents async subagent 的通信面直接落在 aegra-api 既有的 `/threads`、`/threads/{tid}/runs` 等端点上，不新增路由、不做适配层；子 agent 图与 aegra-api 的其它图一样在 `aegra.json` 的 `graphs` 里注册（graph 名即 deepagents 的 `assistant_id`）。独立包 `aegra-protocol` 及其文档不再维护。

## Considered Options

- **继续维护独立包**：协议面最小、与 aegra 数据隔离；但重复实现了 aegra 已有的 threads/runs/checkpointer 机制，且不是用户要的"服务型"（多图、随 aegra 部署）。
- **独立包只做薄适配层、转发到 aegra-api**：多一跳进程、两套部署，收益不明。
- **改 aegra-api 既有端点**（选中）：五个端点已存在，缺的只是三处 wire 形状与 Supersede（见 [`../design.md`](../design.md) §7）。

## Consequences

- 子 agent 与 supervisor 可同进程/同库部署（thread 隔离），复用 aegra 的迁移、worker 模式与（本期 noop 的）auth。
- 仓库同时存在两套实现；读文档以 `aegra-api/docs/` 为准，根 `docs/` 是旧包的历史记录。
- "取消不被复活"不再需要自研：活动态守卫已在 `finalize_run` / `interrupt_unowned_run` 中；多实例语义交由 aegra 的 worker 模式。
