# 会话文件与大表（设计）

配套 ADR：[0015 会话文件](./adr/0015-large-tables-in-object-storage.md)、
[0018 只基于本地 filesystem + Store](./adr/0018-local-only-file-reads.md)。
第 3 稿（2026-10-25）：归属从「挂 space」改为「上传者私有 + 随会话删」，命名与 Key 全部重写。
**已实现**（真服务单进程/多进程实测通过）；本文描述设计与取舍，落地位置见 §9。

## 0. 开工前必须先修的两处**既有**缺陷

### 0.1 跨空间越权（blocker，与本方案无关，现在就能打）

`_check_scope`（`graphs/tools/xlsx/__init__.py:68`）只判原始字符串第一段，`_resolve`（`:51`）
用 `.resolve()` 展开 `..`。实测 `/cbam-reporting/../secret/x.xlsx` 两关都过，
`knowledge/secret/机密.txt` 真实存在。修法：`PurePosixPath` 规范化后拒绝任何 `..`，
并校验规范化后仍是 `/<空间>/<文件>` 两段。**搬 store 之前必须修**，否则漏洞一起搬过去。

### 0.2 只扫本地盘的判定

- `shared/knowledge/spec.py:96::has_tables` 用 `glob("*.xlsx")` 决定装不装 xlsx 四件套；
  `graphs/space_agent.py:51` 依赖它。字节会话私有后，它必须改成「本次 run 有
  `session_scope` 就装」。
- `custom/api/:158/164` 的 `_count_files` / `_list_files` 用 `rglob` 扫本地盘。

## 1. 分层

| 存什么 | 放哪 | 生命周期 | 是真相吗 |
|---|---|---|---|
| md 会话文件 | LangGraph Store `(uploads, h(identity), thread_id)` | 72h 孤儿 TTL + 删会话即删 | 是 |
| xlsx 字节 | 本地盘 `<uploads.dir>/<uuid>/<name>` | 删会话即删 | 是 |
| 归属 | PG `session_object`（谁、哪个会话、key、sha256、大小） | 事务 | 是 |
| 表索引 | 本机缓存 `{xlsx.cache_dir}/{sha16}-h{hdr}.v{build}.sqlite` | 可丢 | **否** |
| 空间知识 | 仓库根 `knowledge/<space>/` | 随发布 | 是 |

**key 里不含身份**，所以「删会话」不再是一次前缀删除：先读台账拿本会话的 key 列表，
再逐 key 删（`threads_delete.py`）。代价是清理多一次查询，换来的是「一份字节可以被多条台账
引用，改归属不用搬字节」。

一条规矩定死：**本地磁盘上的一切都是缓存**。

## 2. 命名、规则与表

```
blob     {uploads.dir}/{uuid4hex}/{name}
part     {uploads.dir}/{uuid4hex}/{name}.part-{rand}     写入中间态，os.replace 后消失
index    {xlsx.cache_dir}/{sha16}-h{header}.v{BUILD}.sqlite
```

- `{uuid4hex}` 每次上传新分配 → 并发同名不同内容各写各的 key，**不会出现「行说 A、字节是 B」**；
  同内容重传也是新 key（不去重，见 ADR-0018 取舍表）。
- `{sha16}` = 内容 sha256 前 16 hex，**只用在缓存与索引键**上：内容变了缓存键就变。
- key 只接受 `<32 位小写 hex>/<单层文件名>`（`shared/session/disk.py::_path`），不合规一律当不存在。
- 索引缓存名含 `sha16 + header + BUILD`，少一个维度就会串表；不再含 `h/thread`
  （同一份内容只存一份索引，跨会话复用）。

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

`name` 校验沿用 `custom/api/uploads.py::_safe_name`（basename、去控制字符、≤200、剥反斜杠），
扩展名白名单 `.xlsx`。

## 3. 上传

`POST /custom/uploads/tables`（multipart，单文件，form: `thread_id`, `file`）
路由注册在 `custom/app.py`（现有 uploads router 旁边）。

### 3.1 大小判断（实际生效顺序）

| 序 | 动作 | 失败 |
|---|---|---|
| 1 | `require_auth` + thread 归属（FastAPI 依赖，handler 前已跑完） | 404 |
| 2 | `Content-Length > xlsx.max_blob_mb` | 413，一个字节都不读 |
| 3 | 流式读 64 KB：计数 + `hashlib.update` | 超线 → 停止喂流 → 删 tmp → 413 |
| 4 | 解析文件名前缀 → `space ∈ user_spaces`？ | 422 |

**不用** `b"".join(chunks)`（现在 `custom/api/uploads.py:69` 就是，27.9 MB 常驻两份）。
`session_files.max_upload_mb=10` 留给 md 不动；`xlsx.max_blob_mb` 独立，缺省 10。

**直接流式落盘**（`.part-<rand>` → `os.replace`）：不信 `Content-Length`，边读边计数并算
sha256，超限抛 `TooLarge`；崩在中途只留 `.part` 垃圾，不会出现半截文件。

### 3.2 时序（写序：不可变 blob 先，可变元数据后）

```
0. 读 PG 现有行（若有），记 old_key
1. disk.put_stream(file, new_key) → {uuid4hex}/{name}   计数 + sha256，.part → os.replace
2. BEGIN
     SELECT 1 FROM user_tables WHERE identity=$1 FOR UPDATE     ← 配额串行化
     SELECT COALESCE(sum(bytes),0) FROM session_object WHERE owner_identity=$1
     文件数：md<10、xlsx<20（同名覆盖不占新名额）
     if 超预算 → 413
     INSERT … ON CONFLICT (owner_identity, thread_id, name) DO UPDATE
   COMMIT
3. 提交成功后才删 old_key
```

**失败清理**：只删**本次新写的 key**。key 每次都是新的（uuid），所以物理上不可能删掉旧行
仍在引用的字节，造出「有行无字节」。

**幂等**：`(owner_identity, thread_id, name)` 主键 + `ON CONFLICT DO UPDATE`。唯一约束在 DB 上，
不靠「先查后插」（并发下会同时查空、同时插入）。

### 3.3 错误码

404 无权限/thread 不存在 · 415 非 `.xlsx` · 413 超字节/超配额 · 422 文件名不合法或 space 前缀
不是该用户的空间 · 500 盘写不进去（写满/权限）。

响应 `{path: "/uploads/<name>", name, space, bytes, sha256, indexed: false}`；**不等 ingest**。

### 3.4 明确不做

秒传预检（只省一次上传、多一次 RTT、解决不了竞态）、前端直传/分片/断点续传（28 MB 用不上，
入口已抽成 `put_stream()`，`xlsx.max_blob_mb > 100` 再换）、内容去重（同名覆盖才是正确语义）。

### 3.5 为什么现在没有写工具（两条实测）

**结论（已定）**：agent 只读。`graphs/tools/xlsx/__init__.py:208` 的 `XLSX_TOOLS` 四个全是只读，
`SpaceBackend._check_write` 只放行内部驱逐前缀，`UploadsBackend` 的写家族一律拒绝。
想改表只有一条路：人工改完重新上传。这不是「还没做」，是**有意不做**——理由是两个实测：

1. **openpyxl 保存会把全工作簿的公式缓存值抹掉**。openpyxl 只写 `<f>` 不写 `<v>`；用 openpyxl
   打开一个带 `<v>3</v>` 的文件、改一个**无关**单元格、再保存，`<v>3</v>` 变回空 `<v />`，
   `data_only` 与 `pd.read_excel` 读到 `None`/`NaN`。而索引就是 `pd.read_excel` 建的
   （`xlsx/index.py:69`），所以改一格 = **所有公式列在索引里变空**。没有重算引擎可用
   （本机无 LibreOffice，无可用的 `formulas` 包）。
2. **`/uploads/` 的写曾是幽灵写**（已修）。`UploadsBackend` 只覆写了 `adownload_files`，
   写家族继承 `StoreBackend` → 写进 Store（文本）；而 xlsx 工具读的是台账指向的盘上字节。
   于是 `write_file('/uploads/x.xlsx', …)` 返回成功、`ls` 看得见、`read_file` 读得到，
   但盘上文件没变。现在写家族（同步 + 异步两套）一律拒绝，回归测试
   `tests/unit/test_uploads_backend_readonly.py`。

两个附带约束（将来要做「生成新表」时仍成立）：行号是契约
（`Excel第{rowid + header + 1}行`，插/删行会让模型手里的旧行号静默错位）；
本地盘没有 CAS（两个会话同时改同一份 = 后写覆盖前写，不可检测）。详见 ADR-0018 缺陷 13/14。

## 4. 读取：字节从哪来

`graphs/tools/xlsx/__init__.py::_fetch(path)` → `runtime.backend.download_files([path])`；
`graphs/tools/xlsx/store.py::cache_bytes` 只负责落缓存。

```
/uploads/<name>.xlsx                     → UploadsBackend：查台账拿 key → disk.read
/<space>/<name>.xlsx                     → SpaceBackend：knowledge/<space>/<name>.xlsx
  └─ 两条路都返回字节 → store.cache_bytes 按 sha16 落盘 → 建/复用索引
```

**工具不自己找字节**：旧的 `_check_scope()` 与「查 PG 拿 key」都删了，判定单点回到 backend
（ADR-0018）。所以未来多一个写入方，只要它进得了 backend 或台账，工具不用改。
（注：现在唯一的写入方是上传接口；agent 对 `/uploads/` 只读，见 §3.5。）

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

子 agent 的 backend 由 `graphs/access/factory.py::session_backend()` 装配：
`CompositeBackend(default=SpaceBackend, routes={"/uploads/": UploadsBackend()})`，
`UploadsBackend` 每次读都按经 `session_scope` 验签后的（上传者, 主管 thread）过滤台账行——
所以它读得到本会话的 `/uploads/*`，读不到别的会话的。

## 5. ingest → SQLite

`index.py::ensure(xlsx, cache_dir, cache_key, header)`：本机缓存命中则直接用；否则先用
`openpyxl read_only` 探 cell 数（**超 `xlsx.max_cells` 直接拒，不建索引**），再 build 到临时文件、
原子替换。键见 §2，并发 build 幂等无锁。每次调用开头顺带扫一遍过期缓存（每进程一次，
`xlsx.cache_ttl_days`）。

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
| 两实例同时 ingest | 都成功，语义相同 | 缓存键含 sha256+header+BUILD，`os.replace` 原子，无锁 |
| 上传中途崩溃 | `.part-*` 垃圾 | 没有自动清理，手工对账（§10） |
| 第 1 步完、第 2 步崩 | 孤儿 key | 无悬空行（行还没写）；孤儿 key 手工对账 |
| 第 2 步失败（配额） | 旧行完好 | 只删本次新写的 key |
| 删会话 | 台账行 + 字节都清 | 读 key 列表 → 删行 → 逐 key 删盘 |
| 并发同名不同内容 | 行指向最后提交者，另一份成孤儿 | key 含 uuid4 |
| 配额并发 | 严格 | `SELECT … FROM user_tables FOR UPDATE` |
| 旧缓存 | 自然失效 | 缓存名含 sha16 + BUILD |

**不需要**：Redis 广播、分布式锁、`If-None-Match`、引用计数、leader election。

## 7. 依赖与配置

| 包 | 用途 |
|---|---|
| （无新增） | 字节落盘用 stdlib `pathlib`/`hashlib`；`pandas`/`openpyxl` 已有 |

本分支删掉了 `boto3` 依赖（S3 客户端只在 `feat/unified-files-s3` 分支存在）。

```yaml
# config.yaml
uploads:
  dir: .data/uploads        # 会话上传字节的根目录（相对仓库根）
xlsx:
  max_blob_mb: 10
  max_cells: 2000000
  max_md_files_per_session: 10   # 未生效：md 个数上限走 session_files.max_files_per_session
  max_xlsx_files_per_session: 20
  max_blob_mb_per_user: 2048
  cache_dir: .cache/xlsx
  cache_ttl_days: 7
quota:
  max_threads_per_user: 50
```

```ini
# .env —— 只有密钥
SESSION_SCOPE_SECRET=<hmac key>
```

## 8. 改动清单（已落地）

| 文件 | 内容 |
|---|---|
| `shared/session/disk.py` | 本地盘对象：key 命名 `<uuid>/<名>`、`put_stream`（`.part` → `os.replace`，算 sha256）、`read`/`delete`/`delete_many` |
| `custom/service/session_files.py` | `parse_space`、`quota_violation`、`upsert`（锁用户行） |
| `custom/db/models.py` + alembic `c1d2e3f4a5b6` | `session_object` 表 |
| `custom/api/uploads.py` | `POST /custom/uploads/tables` |
| `graphs/access/session_scope.py` | 签发/验签/解析（主管自身 config，子 agent 用签名凭证） |
| `graphs/access/factory.py` | 读文件唯一装配点：`CompositeBackend(default=SpaceBackend, routes={"/uploads/": UploadsBackend()})` |
| `graphs/access/uploads_backend.py` | `/uploads/` 只读 backend：查台账 → 读盘，只实现异步协议方法 |
| `graphs/middleware/async_task_guard.py` | 派活时签发 `session_scope` |
| `graphs/space_agent.py` / `graphs/supervisor.py` | CompositeBackend + `/uploads` 路由；主管也装 xlsx 四件套 |
| `graphs/tools/xlsx/{store,index}.py` + `__init__.py` | 字节解析、ingest → SQLite、四个工具改跑 SQL |
| `custom/middleware/threads_delete.py` | 删会话连带清台账行 + 盘上字节 |
| `shared/knowledge/root.py` | 只有一条路：`knowledge_root()` = 仓库根 `knowledge/`（无开关、无镜像） |
| `tests/unit/test_{backend_scope,session_objects,upload_tables,session_scope,xlsx_index,threads_delete_blobs,session_files}.py` | 约 70 条单测 |

## 9. 测试策（已落地）

单测共约 70 条，位于 `tests/unit/`（盘层 monkeypatch `shared.session.disk`，不碰真盘）：
`test_backend_scope.py`、`test_session_objects.py`、`test_upload_tables.py`、
`test_session_scope.py`、`test_xlsx_index.py`、`test_threads_delete_blobs.py`、
`test_session_files.py`。
重点覆盖：越权回归（含 `..` 与符号链接逃逸）、文件名/空间规则、流式超限与失败清理、
配额两闸、删会话的「先读 key 再删」、行号公式 `rowid + header + 1`、`xlsx_search` 的单元格
语义、`where` 语义矩阵、authorizer 边界、`session_scope` 验签。

端到端没写成自动化用例，是**手跑**的：真 PG + 真服务，
上传 → 主管 run 里 `xlsx_info('/uploads/…')` 拿到正确 JSON → 删会话后台账行与盘上文件都清空。

## 10. 未做 / 已知限制

- 首次访问建索引几十秒（预建索引需动 worker）。
- 前端直传、断点续传、秒传预检：见 ADR-0018「有意的非目标」。
- 存量 `knowledge/<space>/*.xlsx` **仍是空间文件**，没有「把空间表迁成会话表」的脚本；
  两套来源共存是有意的。
- 配额不严格：PG 的 `sum(size_bytes)` 不含索引与被覆盖的旧 key，物理用量可能到 2 倍。
- **孤儿无 GC**：崩溃留的 `.part-*`、提交失败留下的 key 没有自动清理。对账办法（只读）：

  ```bash
  # 盘上有、台账里没有（看着像孤儿）：左差集
  comm -23 <(cd .data/uploads && find . -type f -not -name '*.part-*' | sed 's|^\./||' | sort) \
           <(psql -Atc 'select object_key from session_object order by 1')
  # 台账里有、盘上没了（悬空行：读的时候会 FileNotFoundError）：右差集
  comm -13 <(cd .data/uploads && find . -type f -not -name '*.part-*' | sed 's|^\./||' | sort) \
           <(psql -Atc 'select object_key from session_object order by 1')
  ```

  删孤儿之前先确认它不在任何台账里（`session_object` 是唯一真相）。
- 只支持 `.xlsx`；`csv` / `xls` / 多文件联查（duckdb）未做。
- 多副本不共享盘：字节只在本机。要共享得挂同一卷（见 ADR-0018 缺陷 1）。

## 11. 参考

- [S3 multipart limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html)：≥100 MiB 考虑分片；
  part 5 MiB–5 GiB；未完成上传必须显式 abort。
- 预签名直传 vs 服务端中转（1 GB：58.7 s/72% CPU 对 29.3 s/5% CPU）——本分支不做前端直传。
- 幂等上传：SHA-256 + DB 唯一索引 + 409，禁止只靠业务层先查后插。
- 同步哈希阻塞请求线程的线上复盘（2 GB 数分钟）→ 我们 28 MB 约 100 ms，阈值内。
- Databricks Apps 双身份模型（app identity vs user authorization）→ 对应本方案的「不替换
  `langgraph_auth_user`，只签窄凭证」。
- 对象存储侧的同类设计见 `feat/unified-files-s3` 分支 + [ADR-0017](./adr/0017-files-read-through-one-backend.md)。
