# `multitask_strategy="interrupt"` 从静默忽略改为真执行 Supersede

> **已归档**：本文是历史记录，不再约束当前代码；现行决定见 [`../README.md`](../README.md) 总表。

deepagents 的 `update_async_task` 靠 `POST /threads/{tid}/runs` 带 `multitask_strategy="interrupt"` 表达"杀掉当前 run、保留线程历史、用新消息开新 run"。aegra-api 目前把该字段存进 `RunJob.behavior` 后**没有任何消费**——旧 run 继续跑，与新 run 并存于同一线程。

**决定**：实现 `interrupt` 策略（Supersede）。语义：建新 run 之前，对该线程所有 `pending | running` 的 run 走一次标准取消（无主 → DB 落 `interrupted`；有主 → broker 信号 + 执行器落 `interrupted`，`interrupted_by="cancel"`），checkpoint / 线程历史不动，再照常建新 run。其余策略值（`reject` / `enqueue` / `rollback`）维持现状（收下不执行），另开期处理。

## Considered Options

- **维持忽略**：改动最小，但 `update_async_task` 结果不可用（旧 run 继续跑、两个 run 同写一个线程）。
- **新 run 直接 409 拒绝**：安全但违背 deepagents 契约——update 工具会失败。
- **删除旧 run**：与规范 `rollback` 混淆，客户端与日志都会丢失旧 run 记录。

## Consequences

- `update_async_task` 全链路可用；旧 run 的终态与 `interrupted_by` 可查。
- 本期唯一有破坏性的行为变更：以前同线程并发 run 是"并存"，此后带 `interrupt` 的新 run 会真杀旧 run。`reject` / `enqueue` 的缺位仍是已知限制。
- Supersede 的取消必须在建新 run **之前**完成，保证"旧 run 收尾 → 新 run 开跑"的顺序；活动态守卫兜底，迟到的旧 run 完成写入不会复活它。
