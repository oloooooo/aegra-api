# aegra-api — async subagent 的 ACP 服务面

aegra-api 是自托管的 Agent Protocol 服务；本 context 覆盖它作为 Deep Agents async subagent 通信面（即 Agent Protocol，简称 ACP）时承载的 Run/Thread 生命周期与状态词表。需求与设计见 [`docs/design.md`](./docs/design.md)。

## Language

**Agent Protocol (ACP)**:
外部权威协议，规范见 langchain-ai/agent-protocol；本仓库也简称 ACP。语义以规范为准，传输形状以真实客户端（langgraph_sdk / deepagents）实发为准；冲突处按 ADR 记录。
_Avoid_: Agent API、Aegra 协议

**Agent Server**:
该协议的官方托管实现（LangSmith Deployment），是"生产怎么跑"的参照物。
_Avoid_: 平台、LangGraph Platform

**Run**:
一次 agent 调用。要么属于某个 Thread（会更新其状态），要么是 stateless run。
_Avoid_: 任务、job、invocation

**Thread**:
一段对话状态（values/messages）的载体，可被多次 Run 更新。async subagent 的记忆就在子 agent 自己的 Thread 上，与 supervisor 的记忆互不相通。
_Avoid_: session、conversation、记忆

**Run status**:
Run 的唯一状态字段。终态只有 success / error / timeout / interrupted；活动态为 pending（排队中）与 running（已开始执行）。服务端没有 cancelled。
_Avoid_: 状态机阶段、phase

**Cancel action**:
取消动作的取值，取规范的 interrupt（仅取消）与 rollback（取消后删除该 Run 及其 checkpoint）；本服务额外接受 cancel 作为 interrupt 的别名，且 rollback 未实现。
_Avoid_: 把 Cancel action 当成 Run status

**Cancel（取消）**:
客户端请求某个 Run 立刻停止。Run 落终态 interrupted，Thread 归 idle，**不可续跑**。
_Avoid_: 撤销、中止

**HITL interrupt（人工中断）**:
图自己在需要人工审批处暂停（`interrupt_on` + checkpointer）。Run 落终态 interrupted，Thread 落 interrupted，**可续跑**。
_Avoid_: 把 HITL interrupt 说成 interrupt（与 Cancel action 同词不同义）

**Supersede（并发覆盖）**:
同一 Thread 上已有活动 Run 时，新 Run 顶替旧 Run（`multitask_strategy`）。客户端用 `multitask_strategy: "interrupt"` 表达"杀掉旧 Run、**保留 Thread 历史**、新建 Run"。
_Avoid_: 把 Supersede 叫 interrupt；把它与 Cancel 混为一谈

**Async subagent**:
Deep Agents supervisor 通过 Agent Protocol 把另一个 agent 当作后台任务调用。任务句柄是 thread_id，进度靠轮询 Run 状态，结果取自 Thread 的 values.messages。
_Avoid_: 后台任务、worker

**Supervisor**:
发起并轮询 async subagent 的 Deep Agents 主机。它只看到任务的本地状态；`cancelled` 是 supervisor 自己记录的 task 状态，不是服务端的 Run status。
_Avoid_: 主 agent、调度器

**Default assistant（默认 assistant）**:
`aegra.json` 里每个 graph 自动拥有、供 `assistant_id=graph_id` 直连的 assistant；无需显式注册（graph 名即 deepagents 的 assistant_id）。
_Avoid_: 内置 assistant、系统图

## 配置词表

**Graph key（图名）**:
`aegra.json` 中 `graphs` 的键，同时是这份部署对外暴露的那个默认 assistant 的身份；客户端可以直接把它当 `assistant_id` 用。
_Avoid_: 图 id、模型名

**Graph id**:
assistant 记录指向的图名，值等于它建立时所用的 graph key。key 从配置里删掉后，这条记录的 graph_id 就指向不存在的图。
_Avoid_: 把 graph_id 当 graph key 的别名（一个是记录字段，一个是配置里的键）

**Assistant id**:
接口里标识 assistant 的字符串：可以是 assistant 的 uuid，也可以是当前配置里的 graph key。两者都不匹配即 404。
_Avoid_: 认为任意字符串都能当 assistant_id 用

**File logging（文件日志）**:
把同一份结构化日志同时写到 stdout 与本地文件的出口；文件恒为 JSON，带大小轮转与按天清理。
_Avoid_: 把文件日志当成独立于 stdout 的另一套日志
