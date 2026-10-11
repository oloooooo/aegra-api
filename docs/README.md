# aegra-api docs

本目录是 aegra-api 作为 Deep Agents async subagent 通信面（Agent Protocol，简称 ACP）的文档。旧版需求文档属于已废弃的独立包 `aegra-protocol`（该仓库已不存在）。

| 文件 | 内容 |
| - | - |
| [design.md](./design.md) | **需求与设计**：deepagents 实发的五类请求、状态/动作词表三方对照、端点契约、`interrupted_by` 数据模型、状态机、取消与 Supersede 时序、本期改动清单、`aegra.json` 示例、已知限制、测试清单 |
| [adr/0001-acp-lives-in-aegra-api.md](./adr/0001-acp-lives-in-aegra-api.md) | ACP 内置于 aegra-api，不再抽独立包（supersedes 旧 ADR-0004） |
| [adr/0002-status-and-cancel-vocabulary.md](./adr/0002-status-and-cancel-vocabulary.md) | 服务端不引入 `cancelled`；取消与 HITL 共用 `interrupted` + `interrupted_by`；`rollback` 未实现 |
| [adr/0003-multitask-strategy-interrupt-supersede.md](./adr/0003-multitask-strategy-interrupt-supersede.md) | `multitask_strategy="interrupt"` 真执行 Supersede（行为变更） |

其他相关文档：

- 术语表：[`../CONTEXT.md`](../CONTEXT.md)
- 运行与配置：[`../README.md`](../README.md)
