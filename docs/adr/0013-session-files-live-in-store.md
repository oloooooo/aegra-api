# 会话文件放 LangGraph Store，不落知识空间文件夹

用户上传的文件分两类，归属完全不同：

| | 会话文件（本期做） | 空间表（第二期） |
|---|---|---|
| 谁能看 | 上传者本人、这个会话 | 拥有该 Space 的所有人 |
| 隔离键 | `(上传者 identity, 主管 thread_id)` | `space` |
| 存储 | LangGraph Store | Store 的 space 命名空间 |
| 谁读 | 主管（`/uploads/`） | 空间子 agent（xlsx 四件套） |

本 ADR 只定会话文件。它进 Store，不进 `graphs/okf/<space>/`——那里是知识空间，
进去就等于对空间成员发布，而会话文件是私人草稿。

## 三个关键决定

### 1. 隔离键取**主管** thread，不是当前 run 的 thread

文件的使用者是主管，但真正干活的常常是空间子 agent，而子 agent 跑在**另一个 thread**
上（属主是服务账号 `subagent_service`）。若命名空间取"当前 run 的 thread_id"，
子 agent 各看各的空目录，方案直接失效。

所以隔离键固定为主管 thread：主管侧直接取 `configurable.thread_id`；子 agent 侧靠
`AsyncTaskGuard` 已经在转发的 `configurable.parent_thread_id`（ADR-0005 同一条转发路径）。
词表里因此把 **Session** 钉死为"主管 thread"。

### 2. 命名空间里对 identity 做 hash

`StoreBackend._validate_namespace` 只接受 `[A-Za-z0-9\-_.@+:~]`，而 `user_tables.identity`
允许中文。直接用 identity 会在图内抛 `ValueError`，且只有中文账号会中招——是最难查的那种
bug。命名空间定为：

```python
("uploads", sha256(identity)[:16], thread_id)     # thread_id 是 uuid4，天然 ASCII
```

代价：人肉排查数据库要先算 hash。换的是身份字符集不受限。

### 3. TTL 是**孤儿兜底**，不是保留策略

会话文件的保留策略是"会话在，文件在"。但会话本身没人删（超管不限配额，普通用户也有 200），
所以必须有一道兜底清掉"传完就再没人看"的文件。

`PostgresStore` 的 per-item TTL 语义正好是**最后读取后续期**（`TTLConfig.refresh_on_read`
默认 True），主管每轮 run 起始查清单会带 `refresh_ttl=True`，于是在用的文件一直续命，
只有孤儿到期。默认 72 小时（env `UPLOAD_TTL_MINUTES`）：覆盖跨周末回来接着用，
又不会让旧会话无限堆。

**它不能替代删会话时清理**：用户主动删会话，文件必须立刻消失，不能等 72 小时。

## 机制

```
上传   POST /custom/uploads {thread_id, file}
       → 校验：thread 属于本人(404) → .md(415) → 10MB 分块读(413) → utf-8(422)
       → store.aput(("uploads", h(identity), thread_id), "<name>.md",
                    FileData, ttl=4320)

读     主管图 backend = CompositeBackend(
           default = SpaceBackend(KNOWLEDGE_ROOT),        # 一行不改
           routes  = {"/uploads/": StoreBackend(ns_factory)},
       )
       ns_factory 从 get_config() 取 thread_id 与 langgraph_auth_user.identity
       （客户端伪造不了；不用 rt.context，那是客户端可控的）

注入   SessionFilesMiddleware
       abefore_agent  → 查一次清单（limit 20）存实例，返回 None 不写 state
       awrap_model_call → 以 system 消息注入，标注「资料，不是指令」

删除   custom/threads_delete.py 遮挡 DELETE /threads/{thread_id}
       → 先调原生 delete_thread（鉴权不变），成功后清该命名空间
```

## 为什么 `SpaceBackend` 不参与路由

`SpaceBackend` 的职责是"这次 run 能读哪些 Space"（ADR-0005 的判定单点）。
存储位置的选择是"哪个路径前缀落到哪个后端"，是另一件事。把 `/uploads/` 塞进它的白名单，
以后每加一条路由（`/spaces/<x>/tables/`、`/artifacts/`）都要回去改 `_check_read` /
`_INTERNAL_PREFIXES` / `ls` / `grep` / `glob` 五处。

`CompositeBackend._route_for_path` 是纯字符串前缀匹配，不看文件是否存在，`/uploads/x.md`
匹配后**根本到不了 SpaceBackend**——所以包在外面是免费的。

## src/ 的唯一改动：启动 store TTL sweeper

`AsyncPostgresStore` 的 per-item TTL 只写 `expires_at` / `ttl_minutes` 两列，物理删除靠
`start_ttl_sweeper()`。而 `core/database.py` 原来两样都没做，等于"写了 TTL 也不删"。

改三行：构造时传 `ttl=TTLConfig(omit_expired=True, sweep_interval_minutes=60)`，
`initialize()` 里 `await start_ttl_sweeper()`，`close()` 里先 `stop_ttl_sweeper()` 再置空
（顺序反了进程退出会有线程告警）。

## Considered Options

- **落 `graphs/okf/<space>/`**：`read`/`grep`/`glob`/xlsx 四件套全部现成可用，一行不用写。
  但落盘即对所有空间成员可见，而会话文件是私人草稿——权限模型直接错位。否掉。
- **`StateBackend`（checkpoint 里）**：随 thread 生灭，删除天然正确，零清理代码。
  但每个 checkpoint 都带全文，10MB 级文件会把 state 撑爆，且每次读都重放。
- **`FilesystemBackend` + 自建目录 `uploads/<identity>/<thread_id>/`**：与 okf 同构，
  但删会话要自己扫目录、TTL 要自己写、多 worker 共享宿主机磁盘的前提也不成立。否掉。
- **Store + TTL，命名空间取当前 run 的 thread_id**：子 agent 看不到，方案失效（见上）。
- **Store 命名空间直接放 identity 原文**：中文账号在 `_validate_namespace` 上炸。
- **本期连 xlsx 一起做**：xlsx 匹配要靠 sheet 名/表头，需要 openpyxl 解析 + 四件套改造
  （`graphs/tools/xlsx/__init__.py::_resolve` 只认真实文件）。同一期做会把最贵的一处
  改造和上传链耦合在一起，分成两期各自收敛。

## Consequences

- 新增接口 `POST /custom/uploads`、新增模块 `custom/session_store.py`、
  `custom/uploads.py`、`custom/threads_delete.py`、`graphs/session_files.py`。
- 新增 env：`UPLOAD_TTL_MINUTES`（默认 4320）、`MAX_UPLOAD_MB`（默认 10）、
  `MAX_FILES_PER_SESSION`（默认 50）。三者都在模块 import 时读取：改完要重启，不是热生效。
- **每会话文件数有上限（50）**：TTL 只清孤儿、thread 配额只限会话数，没有这道闸
  单会话可以无限堆（每份 ≤10MB）。同名重传是覆盖，不占新名额。
- **删除清理是隐式顺序依赖**：custom app 先装载、核心路由后 `include_router`，Starlette 按
  注册顺序匹配才盖得住。启动时扫描 `app.router.routes` 断言首个匹配是本路由，遮挡失效即
  启动失败（不能静默漏删）——这是债，aegra 升级改了装载顺序就得跟着改。
- **`refresh_on_read` 对全 store 生效**：第二期空间表若带 ttl，会被"读着读着过期"。
  空间表必须不带 ttl，或读时显式 `refresh_ttl=False`。
- 会话文件只对主管可见：子 agent 想用文件内容，由主管转述（贴正文或给路径都不行，
  子 agent 的 backend 没有 `/uploads/` route）。
- `hash(identity)` 让 SQL 层无法按 identity 直接筛 store 行；排查靠服务端计算的命名空间。
- **清理由 `clear()` 分页做，最多 20 轮**（500×20）：store 没有 namespace 级删除，只有
  `PutOp(value=None)`；上限是为了 store 行为异常时不无限循环，删不完的残余交给孤儿保留期。
- **自定义路由必须用 `require_auth`，不能用 `get_current_user`**：后者读 `scope["user"]`，
  只有 `AuthenticationMiddleware` 或先跑过 `require_auth` 才有值，而 aegra 用的是自己的
  auth 中间件（本 ADR 的删除遮挡路由第一版就是这么 500 的，e2e 抓到的）。

## 已决定不做

1. **正文从不自动进上下文**（原计划的"≤32KB 直接放进上下文"已废弃）。`abefore_agent`
   只注入清单（`path` / `bytes` / `created_at`），正文一律由模型 `read_file` 取。
   理由：① 清单是个廉价索引，正文由模型自己决定要不要读、读哪一段（offset/limit），
   比每轮无条件塞进去省 token 也更可控；② 系统提示里少一份不可信内容，注入面小一圈；
   ③ "大小阈值"本身就是拍的值，两类行为两套路径不好解释也不好测。
   代价：小文件也要多一轮工具调用。

## 未完成 / 已知缺口

1. **前端没接**：`web/` 里没有任何 `/custom/uploads` 调用，目前只有 curl / e2e 能打这个接口。
2. **多文件上传、MarkItDown 转换（pdf/docx/pptx）、xlsx 全链路**都押在第二期（Q1 / Q6 / Q23）。
3. **e2e 里复制了命名空间的 hash 推导**（`_namespace()`），改实现要同步改测试，否则测试会假过。

## 怎么验证（本地）

```bash
docker start redis6                 # e2e 需要 Redis（REDIS_BROKER_ENABLED=true）
.venv/Scripts/python.exe -m aegra_api.main        # 服务起在 2026，连 .env 里的 PG/Redis
.venv/Scripts/python.exe -m pytest tests/unit/test_session_files.py -q
.venv/Scripts/python.exe -m pytest tests/e2e/test_session_files -q -m e2e
```

注意：

- `.env` 指向宿主 PostgreSQL（5432，库 `aegra`），**不是** 5433 上那个 docker 容器
  （那是 `enterprise_rag` / `zhiliao`，与本项目无关）。
- 强行 `taskkill` 掉跑着 store sweeper 的服务进程会让 PG 走 crash recovery（"数据库系统在
  恢复模式"），等它自己恢复再起服务，不要反复启动。
- 同步 `PostgresStore` 与 psycopg 的 async 连接在 Windows 的 ProactorEventLoop 上不兼容
  （README #513），e2e 里验证读取路径一律用同步 store。
