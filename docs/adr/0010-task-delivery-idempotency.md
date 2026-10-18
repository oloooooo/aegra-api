# 任务结果投递的幂等与并发

结果投递（见 `docs/design.md §2.4`）有两处会并发：同一轮里多个后台任务同时完成、以及
多个子 agent 在几乎同一时刻收尾。本文记录判重依据、每种并发下的行为，以及已知边界。

## 权威判据：state 里的投递消息本身

投递消息统一为：

```
[Async subagent '<name>' has completed] Result: …
<空行>
task_id: <id>
run_id: <完成这次执行的 run id>
```

判重键是 **(task_id, run_id) 二元组**，且**只认以 `[Async subagent` 开头的消息**。
用二元组而不是 `task_id` 单键，是因为 `update_async_task` 会给同一个任务开新 run，
只认 `task_id` 会把更新后的第二次结果当成重复丢掉。派活工具返回的
`Launched async subagent. task_id: …` 同样带 `task_id:`，不排除会把"待投递"误判成"已投递"
（实现时踩过，自检里有回归用例）。

判重放在 state 而不是内存表，是因为：

- 进程重启、队列重投（Redis at-least-once）后再跑一次 `aafter_agent`，读 state 仍能判出来；
- 多进程部署时 state 是唯一共享的真相，内存表各算各的。

## 三种并发场景

| 场景 | 行为 | 幂等性 |
|---|---|---|
| 同一轮里多个任务同时转终态（主管正在跑） | `abefore_model` 一次 `_plan` 把**所有**待投递任务合成一条 state 更新（一个 `messages` 列表） | 天然幂等：一次事务、一次 checkpoint |
| 多个子 agent 同时收尾，但主管**正忙** | notifier 看到 `busy` 就不投递，只安排延迟复查（8s/20s/45s）；结果由 ① 在主管边界注入 | 不会并发生成 run |
| 多个子 agent 同时收尾，主管**空闲** | 各自的 notifier 都要开新 run。用进程内锁把"判重 + 创建"串行化，并记一条 `(父线程, task)` 的本地认领，补上"刚发出去、state 还没落 checkpoint"的空白期 | 单进程内确定唯一；跨进程见下 |

## 已知边界：多进程下的重复窗口

`_DELIVERY_LOCK` 与 `_DELIVERED_LOCAL` 都是**进程内**的。两个子 agent 跑在不同 worker 进程、
在几十毫秒内先后完成、且主管此刻空闲时，两次投递可能都通过判重，各开一个 run：

- 用户会看到两条「子 agent 返回」；
- 主管会因此多跑一轮（两次模型调用）。

概率低（要求同一父线程下两个任务几乎同时结束、且主管已空闲），后果是"多答一次"，不丢数据。
兜底有两层：投递消息里的 `task_id` 让模型能识别重复（主 prompt 要求"同一 task_id 只处理一次"），
前端按 `task_id` 也只会看到两段可辨识的返回。

## Considered Options

- **数据库认领表**（`task_id` 唯一键，投递前 `INSERT … ON CONFLICT DO NOTHING`）：跨进程严格
  一次，但 `graphs/` 侧没有 DB 通道，要么给 `custom/` 加接口、要么把投递整体搬进服务端。
  与 ADR-0008 里那张登记表是同一个待办，等有多进程部署需求时一起做。
- **用 `multitask_strategy="interrupt"` 投递**：能把"两个 run"压成"一个 run"，但语义是
  Supersede（杀掉前一个 run），前一个投递消息已经写进 state 撤不回来，只是少了一次并发写。
  收益不抵语义代价，不用。
- **进程内锁 + 本地认领（选中）**：零新依赖，单进程（当前部署）完全确定；多进程留一个已记录的窄窗口。

## Consequences

- 投递在单进程部署下是幂等的：同一 `(父线程, task)` 至多一条消息、至多一个额外 run。
- 多进程部署下允许"极端并发时多答一次"，不丢结果、不重复执行子 agent。
- 跨进程严格一次需要 DB 认领；届时应把 `_DELIVERED_LOCAL`、`_DELIVERY_LOCK` 与 ADR-0008 的
  并发上限账本一起收敛到同一张登记表。
- 投递的写操作不幂等（`runs.create` 没有幂等键）。重试前会重新读一次父线程状态与 state，
  因此"请求超时但其实已成功"的情况会被下一次判重拦住，不会重复开 run。
