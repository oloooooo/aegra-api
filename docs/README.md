# aegra-api docs

本目录是 aegra-api 作为 Deep Agents async subagent 通信面（Agent Protocol，简称 ACP）的文档。旧版需求文档属于已废弃的独立包 `aegra-protocol`（该仓库已不存在）。

| 文件 | 内容 |
| - | - |
| [design.md](./design.md) | **需求与设计（§7 五项改动已全部实现，§10 测试全部通过）**：deepagents 实发的五类请求、状态/动作词表三方对照、端点契约、`interrupted_by` 数据模型、状态机、取消与 Supersede 时序、本期改动清单、`aegra.json` 示例、已知限制、测试清单 |
| [configuration.md](./configuration.md) | **配置参考**：`aegra.json` 逐键说明与启动期报错、graph key / `assistant_id` 契约、全部环境变量、日志（stdout + `logs/aegra.log` 轮转与保留）、OpenTelemetry / Prometheus、排错对照表 |
| [streaming.md](./streaming.md) | **流式参考**：`client.stream` 结构与全部 9 种模式实测矩阵、aegra 模式处理与 `version="v2"` 硬编码、deepagents/`astream_events` 适配、v3 进程内✅/HTTP❌、前端 vs 调试选型、子图流不回传等坑、子 agent 流代理与 `async_tasks` 僵尸状态 |
| [frontend-api.md](./frontend-api.md) | **前端接口参考**：`custom/` 下 10 个前端接口（`frontend_api.py` 7 个 + `task_control_api.py` 的 `cancel` + `uploads.py` 的 md 上传与会话大表上传）+ 2 个投递 ack 内部接口、鉴权与 token 时长、原生接口清单、SSE 帧契约（增量 / interrupt-resume / 回写消息识别）、定时任务实测行为、会话删除与已知限制 |
| [large-files.md](./large-files.md) | **会话文件与大表（实现说明）**：md→Store / xlsx→S3 双载体、`sessions/{h}/{thread}/` 前缀、文件名 `<space>_*.xlsx` 只做路由、签名 `session_scope`、ingest → SQLite、多副本一致性、已知限制与测试清单 |
| [S3.md](./S3.md) | **对象存储操作手册**：起 SeaweedFS、全部相关 env（大小/配额/缓存/镜像）、「本地文件怎么进 S3」三条路（OKF 推送脚本 / 会话大表 HTTP / 运维直连）、key 布局、上限判定顺序、校验与排错、清理与容量对账 |
| [crash_10_22.md](./crash_10_22.md) | **故障排查手册（2026-10-22）**：崩溃/复活/「后端卡死」的现象、租约与 reaper 的处置流程、多 worker 与 Redis 的依赖关系；适用代码状态写在该文件开头，用前先对一下 |
| [adr/0001-acp-lives-in-aegra-api.md](./adr/0001-acp-lives-in-aegra-api.md) | ACP 内置于 aegra-api，不再抽独立包（supersedes 旧 ADR-0004） |
| [adr/0002-status-and-cancel-vocabulary.md](./adr/0002-status-and-cancel-vocabulary.md) | 服务端不引入 `cancelled`；取消与 HITL 共用 `interrupted` + `interrupted_by`；`rollback` 未实现 |
| [adr/0003-multitask-strategy-interrupt-supersede.md](./adr/0003-multitask-strategy-interrupt-supersede.md) | `multitask_strategy="interrupt"` 真执行 Supersede（行为变更） |
| [adr/0004-static-service-identity-for-async-subagents.md](./adr/0004-static-service-identity-for-async-subagents.md) | async subagent 用构建期铸的静态服务 token（跳过 DB 直放行） |
| [adr/0005-spaces-forwarded-via-run-config.md](./adr/0005-spaces-forwarded-via-run-config.md) | 用户 spaces 经 run config 转发，由拦截层塞入 `start_async_task` |
| [adr/0006-completion-notify-mints-parent-token.md](./adr/0006-completion-notify-mints-parent-token.md) | ~~完成通知回写父线程：现铸短时父身份 token，不用服务 token~~（已由 ADR-0010 的发件箱分发者取代） |
| [adr/0007-frontend-access-to-async-tasks.md](./adr/0007-frontend-access-to-async-tasks.md) | 子 agent 的流与状态经 `custom/` 代理暴露（不放开 ownership）；派活前核实 `async_tasks` 真实状态 |
| [adr/0008-active-task-limit-is-per-process.md](./adr/0008-active-task-limit-is-per-process.md) | ~~`MAX_ACTIVE_TASKS` 靠进程内账本 + 锁保证原子；多 worker 部署下上限按进程各算~~（已由 ADR-0012 取代） |
| [adr/0009-worker-mode-requires-redis-broker.md](./adr/0009-worker-mode-requires-redis-broker.md) | worker / 多副本必须开 Redis broker：不开时跨进程取消约 50% 静默丢失（实测证据与三种部署对比） |
| [adr/0010-task-delivery-idempotency.md](./adr/0010-task-delivery-idempotency.md) | 任务结果投递：事务性发件箱 + 单一分发者；发件箱行状态管跨进程幂等，`delivered_run_id` 管同 run 判重 |
| [adr/0011-space-agent-is-dynamic.md](./adr/0011-space-agent-is-dynamic.md) | 空间子 agent 动态化：内容放 `<space>/AGENT.md`、单个 `domain_agent` 图 + 工厂图现算名单，加/删 space 不碰代码 |
| [adr/0012-thread-quota-replaces-task-limit.md](./adr/0012-thread-quota-replaces-task-limit.md) | 用 thread 配额（`MAX_THREADS_PER_USER`）取代派活并发上限：子线程带 `owner_identity` metadata，建会话时计额、删会话时级联删子线程 |
| [adr/0013-session-files-live-in-store.md](./adr/0013-session-files-live-in-store.md) | 会话文件放 LangGraph Store，按（上传者, 主管 thread）隔离；不进 Space、随会话删除、孤儿靠 TTL 兜底 |
| [adr/0014-no-hitl-for-eval.md](./adr/0014-no-hitl-for-eval.md) | 子任务只做取消与状态通知，不做 `eval` 的 HITL 审批（2026-10-25 复审：暂停/继续仍不做） |
| [adr/0015-large-tables-in-object-storage.md](./adr/0015-large-tables-in-object-storage.md) | 会话文件：md 进 Store、xlsx 进对象存储，**上传者私有 + 随会话删**；文件名 `<space>_*` 只做主题路由；子 agent 靠**签名 `session_scope`** + `CompositeBackend` 路由读文件 |
| [adr/0016-spaces-in-object-storage.md](./adr/0016-spaces-in-object-storage.md) | 空间内容（OKF）进 S3，`OKF_BACKEND=local\|s3` 两套实现（`s3` 读本地镜像）；`scripts/okf_push_s3.py` / `okf_pull_s3.py` |

其他相关文档：

- 术语表：[`../CONTEXT.md`](../CONTEXT.md)
- 配置参考：[`configuration.md`](./configuration.md)
- 快速上手与运行命令：[`../README.md`](../README.md)
