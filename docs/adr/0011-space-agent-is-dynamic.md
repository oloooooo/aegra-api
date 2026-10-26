# 空间子 agent 动态化：一个图，名单每次 run 现算

一个 Space 就是一个领域子 agent。旧实现把这件事写死在五处：`aegra.json` 三个 graph key、
`graphs/supervisor.py`（原 `deepagents_nodes.py`）里的 `_DOMAIN_SPECS`、`graphs/space_agent.py`
（原 `domain_agents.py`）的三个 builder、`graphs/prompts/` 六个 md 与六个常量、`main.md` 里手写的一段名单。加一个 space 要改遍四处
（表、目录、prompt、代码），删一个同理。本文记录为什么改成"空间文件夹即子 agent"。

## 决定

| 关注点 | 决定 | 位置 |
|---|---|---|
| 子 agent 的 system prompt 与"何时派给我" | 都放 `<space>/AGENT.md`（frontmatter `description` + 正文 prompt） | 知识空间文件夹 |
| 子 agent 的图 | **只有一个** `domain_agent` 图，space 经 run config 绑定 | `graphs/space_agent.py` |
| 可用名单 | 每次 run 现算：主管自己的 spaces → 各空间 `AGENT.md` 的 description | `graphs/supervisor.py::domain_specs` |
| 子 agent 名 | **space 名原样**（`cbam-reporting`），不做 `-`→`_` 映射 | 同上 |
| 主管的提示词 | 不再列名单——`start_async_task` 的工具描述本来就带 `{available_agents}` | `graphs/prompts/main.md` |
| 建 space | 名字校验 + 目录由知识根派生 + 从模板生成 `AGENT.md` | `custom/api/spaces.py` → `shared/knowledge/spec.py` |

单一来源落在 `shared/knowledge/spec.py`：目录解析、`AGENT.md` 解析与兜底、脚手架、xlsx 探测。
图工厂与 HTTP 路由都从那里取，`custom` 与 `graphs` 两个包不再各存一份名单。

## 为什么内容跟着文件夹走，而不是进 `space` 表

- prompt 正文就是"这个空间有哪些文档、先读哪份"，它与文档**必须一起改**；放库里必然与
  文件夹漂移，而且改一次 prompt 要发一次 SQL。
- 短的那句 `description` 也放同一个文件（frontmatter），避免"一句话在库、正文在盘"的半拆状态。
- 兜底：`AGENT.md` 缺失/没写 description 时，description 退到正文首行，prompt 退到一句通用
  说明——空间刚建、还没写 prompt 时也能跑。

## 为什么是工厂图

- 名单依赖**本次 run 的**调用者（他自己的 spaces），静态图只能在 import 时定死。
- aegra 支持 1 参数 `config` 的工厂图，且**不缓存**、按 run 重建（`graph_factory.py:184-208`），
  正好承载"prompt 与名单取决于这次 run"。
- 主管自己的 spaces 已在 run config 里（`langgraph_auth_user.user_spaces`，服务端注入、
  客户端伪造不了），所以建图**不查库**：`allowed_spaces(config)` + 读 N 个 `AGENT.md`。

代价：`create_deep_agent` 每次 run 重建一次（带 fs 中间件的 supervisor）。实测量级是毫秒到
几十毫秒，相对于一次模型调用可忽略；换来的是加/删空间零代码改动。

## 为什么所有 space 共用一个图

`aegra.json` 是启动时读的配置，"每个 space 一个 graph key" 意味着**加 space 要重启服务**，
这正是本次要消灭的成本。共用 `domain_agent` 后：

- `start_async_task(subagent_type="<space>")` → 拦截层把 `user_spaces=[space]` 写进子 run 的
  `configurable` → 子图按这个 space 取 prompt、`SpaceBackend` 按它限可见目录；
- 前端/外部客户端不能再直接 `assistant_id=cbam_reporting` 调某个领域图——它们本来就只经
  `/custom/tasks` 代理（`docs/frontend-api.md`），不受影响。

## 删除 Space 的语义

**已经派出的任务保留当时授权的 space**：spaces 在派活那一刻写进子 run 的 config，之后不重新
鉴权，所以删 space 不影响在跑的任务。新任务自然选不到它（名单现算）。

投递侧也不需要特殊分支：guard 解析 spec 时若 `agent_name` 已不在当前名单里，就借本部署任意
一个 spec 拼 url/headers 去轮询子 run、取摘要（这些请求与 space 无关），只有"派新任务"才
要求 spec 存在。

## 权限与路径

- 建 space 原本就要求 `write` 权限（超管），本条不变。
- `space.path` 改为**由名字派生**（`graphs/okf/<space>`，POSIX 分隔符），请求体里的 `path`
  只为老客户端兼容而保留、不参与任何判定。目录名同时也是子 agent 名，所以只有一套校验
  （`valid_space_name`：字母/数字/下划线/点/短横线，1-64 字符）。

## 事实依据（已核实）

- `start_async_task` 的工具描述由 `ASYNC_TASK_TOOL_DESCRIPTION.format(available_agents=…)`
  生成，已包含每个子 agent 的 name + description（`deepagents/middleware/async_subagents.py:173`），
  所以主管提示词里那段名单是纯重复。
- `allowed_spaces(config)` 读的是服务端注入的 `langgraph_auth_user.user_spaces`
  （`graphs/access/spaces.py`），建图无需查库。
- aegra 的工厂图判定只看参数个数与注解（`graph_factory.py::_classify_factory`），1 参数即
  `config`，且静态图缓存不含工厂图。
- 子 agent 名在 deepagents 里只当字符串用（工具参数值、`_ClientCache` 的键），带短横线无碍。

## Considered Options

- **在 `space` 表加 `description` / `system_prompt` 两列**：能通过 API 编辑，但 prompt 与文档
  分离、必然漂移；弃。
- **每个 space 注册一个 graph**：`aegra.json` 启动时读，加 space 要重启；弃。
- **保持静态 supervisor，自己写五个派活工具**：能省掉"每 run 建图"，但要把
  start/check/update/cancel/list 全部重写，且丢掉 deepagents 的客户端缓存与工具描述生成；弃。
- **内容跟文件夹 + 单图 + 工厂图（选中）**：加/删 space 只动文件夹与一行表，代码零改动。

## Consequences

- 主管每 run 重建一次图；`AGENT.md` 的读取（每次 run N 个小文件）也在这条路径上。
- `AGENT.md` 由超管手写或建空间时从模板生成；目前没有"编辑 prompt"的 HTTP 接口，
  要改就改文件（与改知识文档同一套运维习惯）。
- 名单只在 run 开始时确定：**run 中途新增的 space 要等下一次 run 才可选**。
- 一个 space 只能有一个子 agent prompt；同一空间想要两套行为得拆成两个 space。
