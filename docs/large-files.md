# 会话文件与大表（设计）

配套 ADR：[0015 会话文件](./adr/0015-large-tables-in-object-storage.md)、
[0016 空间进 S3](./adr/0016-spaces-in-object-storage.md)。
第 3 稿（2026-10-25）：归属从「挂 space」改为「上传者私有 + 随会话删」，命名与 Key 全部重写。
**尚未实现。**

## 0. 开工前必须先修的两处**既有**缺陷

### 0.1 跨空间越权（blocker，与本方案无关，现在就能打）

`_check_scope`（`graphs/tools/xlsx/__init__.py:68`）只判原始字符串第一段，`_resolve`（`:51`）
用 `.resolve()` 展开 `..`。实测 `/cbam-reporting/../secret/x.xlsx` 两关都过，
`graphs/okf/secret/机密.txt` 真实存在。修法：`PurePosixPath` 规范化后拒绝任何 `..`，
并校验规范化后仍是 `/<空间>/<文件>` 两段。**搬 store 之前必须修**，否则漏洞一起搬过去。

### 0.2 只扫本地盘的判定

- `graphs/space_agents.py:96::has_tables` 用 `glob("*.xlsx")` 决定装不装 xlsx 四件套；
  `graphs/domain_agents.py:51` 依赖它。字节进 S3 / 会话私有后，它必须换成
  ①空间前缀探测（`OKF_BACKEND=s3`）或 ②「本次 run 有 `session_scope` 就装」。
- `custom/frontend_api.py:158/164` 的 `_count_files` / `_list_files` 用 `rglob` 扫本地盘。

## 1. 分层

| 存什么 | 放哪 | 生命周期 | 是真相吗 |
|---|---|---|---|
| md 会话文件 | LangGraph Store `(uploads, h(identity), thread_id)` | 72h 孤儿 TTL + 删会话即删 | 是 |
| xlsx 字节 | S3 `sessions/{h}/{thread}/{sha16}/{name}` | 删会话即删 | 是 |
| 元数据 | PG `session_object` | 事务 | 是 |
| 表索引 | S3 `sessions/{h}/{thread}/idx/{name}.h{hdr}.v{build}.sqlite` + 本机缓存 | 可丢 | **否** |
| 空间知识 | S3 `okf/<space>/`（ADR-0016） | 随空间 | 是 |

**所有会话文件都在 `sessions/{h}/{thread}/` 一个前缀下**，所以「删会话」= list 该前缀 +
delete，一次覆盖字节与索引——比上一稿的独立 `index/` 前缀省一套删除逻辑。

一条规矩定死：**本地磁盘上的一切都是缓存**。

## 2. 命名、规则与表

```
tmp      tmp/{uuid4}/{name}
blob     sessions/{h(identity)}/{thread_id}/{sha16}/{name}
index    sessions/{h(identity)}/{thread_id}/idx/{name}.h{header}.v{BUILD}.sqlite
cache    {XLSX_CACHE_DIR}/{h}-{thread}-{sha16}-{header}-v{BUILD}.sqlite
```

- `{sha16}` = sha256 前 16 hex，进路径 → 并发同名不同内容各写各的 key，**不会出现
  「行说 A、字节是 B」**；同内容重传 = 同 key + 同索引键 → 不重建。
- `v{BUILD}`：改 schema 就 +1，否则永远读到旧结构。
- 本地缓存名包含 `h + thread + sha16 + header + BUILD`，少一个维度就会串表。

**表规则（硬逻辑，`CONTEXT.md`「Table rule」）**：文件名必须形如
`<space>_<名字>.xlsx`，且 `<space>` ∈ 该用户的 `user_spaces`；否则 422。
空间名**只做路由**（主管据此选子 agent），**不进存储前缀**——存储只按会话说。

```sql
CREATE TABLE session_object (
    owner_identity VARCHAR(50)  NOT NULL REFERENCES user_tables(identity) ON DELETE CASCADE,
    thread_id      VARCHAR(64)  NOT NULL,
    name           VARCHAR(200) NOT NULL,
    space          VARCHAR(100) NOT NULL,   -- 从文件名前缀解析，仅用于展示/审计
    object_key     VARCHAR(512) NOT NULL,
    sha256         CHAR(64)     NOT NULL,
    size_bytes     BIGINT       NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_identity, thread_id, name)
);
```

`name` 校验沿用 `custom/uploads.py::_safe_name`（basename、去控制字符、≤200、剥反斜杠），
扩展名白名单 `.xlsx`。

## 3. 上传

`POST /custom/uploads/tables`（multipart，单文件，form: `thread_id`, `file`）
路由注册在 `custom/fastapi.py`（现有 uploads router 旁边）。

### 3.1 大小判断（实际生效顺序）

| 序 | 动作 | 失败 |
|---|---|---|
| 1 | `require_auth` + thread 归属（FastAPI 依赖，handler 前已跑完） | 404 |
| 2 | `Content-Length > MAX_BLOB_MB` | 413，一个字节都不读 |
| 3 | 流式读 64 KB：计数 + `hashlib.update` | 超线 → 停止喂流 → 删 tmp → 413 |
| 4 | 解析文件名前缀 → `space ∈ user_spaces`？ | 422 |

**不用** `b"".join(chunks)`（现在 `custom/uploads.py:69` 就是，27.9 MB 常驻两份）。
`MAX_UPLOAD_MB=10` 留给 md 不动；`MAX_BLOB_MB` 独立，缺省 10。

**用 `upload_fileobj` 直写 S3 `tmp/` key**（不落本地文件）：请求流长度在读完前不可信，
而 `put_object` 要求先给 `ContentLength`；`upload_fileobj` 按 8 MiB 自动分片、长度未知也能跑、
单片可重试。

**中断的 multipart 必须有人收尾**：`upload_id` 在 `upload_fileobj` 内部，超线抛异常时拿不到
它来 abort，未完成分片会一直占空间。`custom/blobs.py::abort_stale_multiparts(older_than)`
+ `scripts/reap_multipart.py`（crons API 只触发图 run，不是运维脚本调度器，交给系统计划任务）。

### 3.2 时序（写序：不可变 blob 先，可变元数据后）

```
0. 读 PG 现有行（若有），记 old_key
1. 流式 upload_fileobj → tmp/{uuid}/{name}          计数 + sha256
2. copy_object tmp → sessions/{h}/{thread}/{sha16}/{name}   单次原子出现
3. BEGIN
     SELECT 1 FROM user_tables WHERE identity=$1 FOR UPDATE     ← 配额串行化
     SELECT COALESCE(sum(bytes),0) FROM session_object WHERE owner_identity=$1
     文件数：md<10、xlsx<20（同名覆盖不占新名额）
     if 超预算 → 413
     INSERT … ON CONFLICT (owner_identity, thread_id, name) DO UPDATE
   COMMIT
4. DELETE tmp
```

**失败清理**：删 `tmp/` 永远安全；最终 key **只在 `new_key != old_key` 时删**。否则
「同名同内容重传恰好卡在配额」会删掉旧行仍在引用的字节，造出「有行无字节」。

**幂等**：`(owner_identity, thread_id, name)` 主键 + `ON CONFLICT DO UPDATE`。唯一约束在 DB 上，
不靠「先查后插」（并发下会同时查空、同时插入）。

### 3.3 错误码

404 无权限/thread 不存在 · 415 非 `.xlsx` · 413 超字节/超配额 · 422 文件名不合法或 space 前缀
不是该用户的空间 · 503 对象存储不可用。

响应 `{path: "/uploads/<name>", name, space, bytes, sha256, indexed: false}`；**不等 ingest**。

### 3.4 明确不做

秒传预检（只省一次上传、多一次 RTT、解决不了竞态）、前端直传/分片/断点续传（28 MB 用不上，
入口已抽成 `put_stream()`，`MAX_BLOB_MB > 100` 再换）、内容去重（同名覆盖才是正确语义）。

## 4. 读取：字节从哪来

`graphs/tools/xlsx/store.py::fetch(path, scope) -> Path`

```
/uploads/<name>.xlsx
  ├─ 本机缓存命中（h+thread+sha16）→ 用它
  ├─ 查 PG session_object 拿 object_key → get_object → 原子落到缓存
  └─ 若 path 以 /<space>/ 开头且 OKF_BACKEND=local → 存量本地盘（双读兼容）
```

- 缓存写必须 **`.tmp` + `os.replace`**，命中后 `PRAGMA quick_check`。`immutable=1` 关掉了
  SQLite 的锁，撕开写坏的索引会被当有效文件复用。
- 缓存目录**不能在 OKF 根下**（根散文件对所有登录用户只读共享）。
- 并发各下各的（28 MB，罕见），原子替换保证不会读到半个文件。

### 4.1 `session_scope`（谁算得出来）

| run | 来源 |
|---|---|
| 主管 | 自己的 `langgraph_auth_user.identity` + 服务端覆写的 `configurable.thread_id` |
| 子 agent | `configurable.session_scope`，**必须验签** |

`AsyncTaskGuard._config_for()` 签发：`{owner, thread_id, sig=HMAC(secret, owner+thread_id)}`。
**不替换 `langgraph_auth_user`**，否则 `allowed_spaces()` 会走普通用户分支读
`auth_user.user_spaces`，把子 agent 从「本任务 space」放大成「用户全部 space」。

子 agent 的 backend：`CompositeBackend(default=SpaceBackend, routes={"/uploads/": StoreBackend(ns=f(scope))})`，
xlsx 工具用 `scope` 算 S3 前缀（工具仍绕 backend 直读字节）。主管的
`_uploads_namespace()` 改成「有 `session_scope` 且验签通过就用它，否则用自身 config」。

## 5. ingest → SQLite

`index.py::ensure(scope, sha16, name, header)`：本机缓存 → `HEAD` 索引 key → build 到
`tempfile` → 单次 `put_object` → 原子落缓存。键见 §2，并发 build 幂等无锁。

build：`read_excel(sheet_name=None, header=header)` 一次全读（41.7 s），每 sheet 一张
`s{i}` 表 + `meta` / `columns` 两张小表；`journal_mode=OFF, synchronous=OFF` 只用于构建。

### 5.1 四个工具的 SQL 映射（返回值必须逐字不变）

| 工具 | 现在 | 之后 |
|---|---|---|
| `xlsx_info` | 全读 41.7 s | `meta` + 前 3 行 `limit 3` |
| `xlsx_query` | `read_excel` + `df.query` | 同 WHERE 的 `COUNT(*)` + `LIMIT/OFFSET` |
| `xlsx_search` | 逐**单元格** | 每列一条 `UNION ALL`，`ORDER BY rowid, col_idx` |
| `xlsx_profile` | pandas 逐列 | 逐列 `COUNT/COUNT(DISTINCT)/min/max/avg` |

两个必须写准的（审阅实测）：

1. **行号 `rowid + header + 1`**（不是 `+2`）：`to_sql` 的 rowid 从 1 开始，现在 `_search`
   的 `r` 从 0 开始（`:158` 的 `r + header + 2`）。错一行且「看起来能跑」→ 写进测试。
2. **`xlsx_search` 不能折叠成「每行一次」**：现在 `mask.to_numpy().nonzero()`（`:155-160`）
   一行两列命中出两条、带列名；一条 `WHERE c0 LIKE … OR c9 LIKE …` 给不出列。按列拆：

```sql
SELECT rowid, 0 AS col, CAST(c0 AS TEXT) AS v FROM s0 WHERE c0 LIKE ? ESCAPE '\'
UNION ALL SELECT rowid, 1, CAST(c1 AS TEXT) FROM s0 WHERE c1 LIKE ? ESCAPE '\'
… ORDER BY rowid, col
```

`total` 仍扫全表（现在就是为了拿真实总数）。`_MAX_ROWS=200` / `_MAX_HITS=100` /
`_MAX_VALUE_LEN=80` 不变。`xlsx_query` 的头 `匹配 N 行，返回第 a-b 行（列：[…]）` 可逐字复现，
空页照样印 `第 201-200 行`（pandas 的行为，别「修」它）。

### 5.2 `where` 的语义差异（实测 SQLite 3.53.1）

| 表达式 | pandas | SQLite 直拼 | 结论 |
|---|---|---|---|
| `数量 > 0` | ✅ | ✅ | 裸中文列名可用 |
| `数量(个) > 0` | ✅ | ❌ `no such column` | 括号列名必须加引号 |
| `价 格 > 0` | ✅ | ❌ `syntax error` | 空格列名必须加引号 |
| `"价 格" > 0` | — | ✅ | 双引号/反引号都行 |
| `pn != 'a'`（含 NULL） | 1 行 | **0 行** | 语义不同，必须写进描述 |
| `~col.isna()` / `@var` / `.str` | ✅ | ❌ | 不支持，改 `IS NULL` |

工具描述必须写清这四条。`LIKE` 也不是字面匹配：`_`/`%` 是通配符，要 `ESCAPE`；
只有 ASCII 忽略大小写。

### 5.3 安全边界

- `sqlite3.connect("file:…?mode=ro&immutable=1", uri=True)`；
- `set_authorizer`：放 `SQLITE_SELECT / SQLITE_READ / SQLITE_RECURSIVE`，
  `SQLITE_FUNCTION` **按函数名白名单**（`like/glob/lower/upper/length/abs/round/coalesce/ifnull/substr/trim`）。
  放开整个 FUNCTION 等于放开 `zeroblob(100000000)`，一个 where 吃满内存；
- **不要用 `connect(timeout=)` 当语句超时**（那是等锁）。用 `set_progress_handler` 计时，
  超预算 `interrupt()`；
- `LIMIT` 由 `_MAX_ROWS` 钳死。

## 6. 一致性

| 场景 | 结果 | 机制 |
|---|---|---|
| 两实例同时 ingest | 都成功，语义相同 | 键含 sha256+header+BUILD，PUT 原子，无锁 |
| 上传中途崩溃 | `tmp/` 孤儿 | 生命周期 7 天 |
| 第 2 步完、第 3 步崩 | 孤儿 blob | 生命周期；**不会有行没字节** |
| 第 3 步失败（配额） | 旧行完好 | 只在 `new_key != old_key` 时删最终 key |
| 上传超限被打断 | 未完成分片 | `scripts/reap_multipart.py` |
| 删会话 | 前缀全清 | list `sessions/{h}/{thread}/` → delete（覆盖字节+索引） |
| 并发同名不同内容 | 行指向最后提交者，另一份孤儿 | `{sha16}` 进 key |
| 配额并发 | 严格 | `SELECT … FROM user_tables FOR UPDATE` |
| 旧缓存 | 自然失效 | 文件名含 sha16 + BUILD |

**不需要**：Redis 广播、分布式锁、`If-None-Match`、引用计数、leader election。

## 7. 依赖与配置

| 包 | 用途 |
|---|---|
| `boto3>=1.35`（+ `botocore`） | S3 客户端；同步 API 走 `asyncio.to_thread` |
| SeaweedFS `weed.exe`（非 pip） | 对象存储本体 |

不新增：`sqlite3`（stdlib）、`pandas`/`openpyxl`（已有）、`aioboto3`、`moto`。

**boto3 client 必须懒加载**：`graphs/tools/xlsx/__init__.py` 在 import 期就被
`graphs/tools/__init__.py:13`、`graphs/xlsx_tools.py` 拉起，模块级建 client 会让没配 `S3_*`
的环境（本地 demo、单测）直接 import 失败。

```bash
S3_ENDPOINT=http://127.0.0.1:8333
S3_ACCESS_KEY=... S3_SECRET_KEY=... S3_BUCKET=aegra-blobs S3_REGION=us-east-1
OKF_BACKEND=local|s3            # ADR-0016
MAX_BLOB_MB=10  MAX_CELLS=2000000
MAX_MD_FILES_PER_SESSION=10  MAX_XLSX_FILES_PER_SESSION=20
MAX_THREADS_PER_USER=50  MAX_BLOB_BYTES_PER_USER=2147483648
XLSX_CACHE_DIR=.cache/xlsx  XLSX_CACHE_TTL_DAYS=7
SESSION_SCOPE_SECRET=<hmac key>
```

`Config(s3={"addressing_style": "path"})` 必须有：给了 `endpoint_url` 后 boto3 仍默认
virtual-host，`bucket.127.0.0.1:8333` 解析不了。bucket 启动时 `head_bucket` 失败则
`create_bucket`（幂等）。

## 8. 改动清单

| 文件 | 内容 | 估行 |
|---|---|---|
| `graphs/tools/xlsx/__init__.py` | **先修 §0.1 越权**；四个 tool 改调 `index` | 改 ~120 |
| `custom/blobs.py` | 懒加载 client、key 命名、`put_stream`、`get`、`copy`、`delete`、`head`、`abort_stale_multiparts` | ~110 |
| `custom/session_objects.py` | 新增：PG 读写 + 配额事务 + 文件名→space 解析 | ~100 |
| `custom/uploads.py` | 新增 `/uploads/tables` 路由（或独立 `table_uploads.py`） | ~90 |
| `custom/models.py` + alembic | `SessionObject` | ~50 |
| `custom/threads_delete.py` | 删会话时删 `sessions/{h}/{thread}/` 前缀 + PG 行 | ~15 |
| `graphs/tools/xlsx/store.py` | 新增：scope 解析、缓存、S3 取字节 | ~70 |
| `graphs/tools/xlsx/index.py` | 新增：`BUILD`、build、只读 SQL 查询 | ~170 |
| `graphs/tools/xlsx/schema.py` | `where` 描述改成 SQL 口径 | ~6 |
| `graphs/async_task_guard.py` | `_config_for()` 签发签名 `session_scope` | ~20 |
| `graphs/domain_agents.py` | `SpaceBackend` → `CompositeBackend`（含 `/uploads` 路由） | ~15 |
| `graphs/deepagents_nodes.py` | `_uploads_namespace()` 优先用 `session_scope` | ~15 |
| `graphs/space_agents.py` | `has_tables` 认 `session_scope` / S3 前缀 | ~20 |
| `graphs/space_backend.py` + 新 `space_object_backend.py` | 路径判定抽 mixin + 只读 S3 实现（ADR-0016） | ~220 |
| `custom/frontend_api.py` | `_list_files` / `_count_files` 合并 `session_object` | ~20 |
| `scripts/okf_push_s3.py` | 一键上传 OKF → S3（`--dry-run` / `--prune`） | ~60 |
| `scripts/reap_multipart.py` | 清未完成分片 | ~30 |
| `pyproject.toml` / `.env.example` | `boto3` + 新 env | ~12 |

## 9. 测试计划

`tests/unit/`（S3 层 monkeypatch `blobs`，不引 moto）：

1. **越权回归**：`/空间A/../空间B/x.xlsx` 必须被拒（§0.1）；
2. 文件名规则：`cbam_报价.xlsx` ✅ / `无前缀.xlsx` ❌ / `别的空间_x.xlsx`（无权限）❌；
3. 流式计数超限 → 413 且 tmp 被删；配额失败 → **不删旧 key**；
4. 配额：字节与文件数两闸；同名覆盖不占新名额；
5. 幂等：同内容重传 → 索引键不变、`ensure()` 不 build；
6. `BUILD` 变化 → 新 key；缓存写入是原子替换；
7. **行号公式** `rowid + header + 1`（header=0/1 各一条）；
8. **`xlsx_search` 单元格语义**：一行两列命中 → 两条结果、列名正确；
9. `where` 语义矩阵（§5.2 六条，含括号列名、`!=` 空值、`LIKE` 转义）；
10. authorizer：`zeroblob` / `ATTACH` / `PRAGMA` 被拒；`progress_handler` 拦住笛卡尔积炸弹；
11. `session_scope` 验签：篡改 owner → 拒；无 `sig` → 拒。

`tests/e2e/`（真 SeaweedFS）：上传 10 MB 表 → 首次查询建索引 → 第二次 <100 ms →
删会话后查不到且前缀已清空。

## 10. 未做 / 已知限制

- 首次访问建索引几十秒（预建索引需动 worker）。
- 前端直传、断点续传、秒传预检：见 ADR-0015 §3.4。
- 存量 `graphs/okf/<space>/*.xlsx` 迁移脚本未写，先双读单写。
- 存储层配额不严格：PG 的 `sum(bytes)` 不含索引与被覆盖的旧 blob，物理用量可能到 2 倍。
- `abort_stale_multiparts` 依赖 SeaweedFS 实现 `ListMultipartUploads`：社区资料说支持，**未实测**。
- 只支持 `.xlsx`；`csv` / `xls` / 多文件联查（duckdb）未做。
- `grep`/`glob` 在 S3 backend 上没有索引，只能 list + 逐个 GET（ADR-0016）。

## 11. 参考

- [S3 multipart limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html)：≥100 MiB 考虑分片；
  part 5 MiB–5 GiB；未完成上传必须显式 abort。
- 预签名直传 vs 服务端中转（1 GB：58.7 s/72% CPU 对 29.3 s/5% CPU）。
- 幂等上传：SHA-256 + DB 唯一索引 + 409，禁止只靠业务层先查后插。
- 同步哈希阻塞请求线程的线上复盘（2 GB 数分钟）→ 我们 28 MB 约 100 ms，阈值内。
- Databricks Apps 双身份模型（app identity vs user authorization）→ 对应本方案的「不替换
  `langgraph_auth_user`，只签窄凭证」。
- SeaweedFS：S3 网关是 filer 的前端；bucket versioning 支持按版本而异，**不依赖**。
