# 大表与文件上传（设计）

配套 ADR：[`adr/0015-large-tables-in-object-storage.md`](./adr/0015-large-tables-in-object-storage.md)。
本文是实现级设计：key 命名、表结构、时序、失败模式、依赖清单、测试计划。
**尚未实现**。第 2 稿（2026-10-25）：按对抗性审阅 + 联网校准重写，修正了行号公式、
`xlsx_search` 的 SQL 映射、`where` 语义差异、索引生命周期与越权前置缺陷。

## 0. 开工前必须先修的两处**既有**缺陷

### 0.1 跨空间越权（blocker，与本方案无关，现在就能打）

`_check_scope`（`graphs/tools/xlsx/__init__.py:68`）只看原始字符串第一段，
`_resolve`（`:51`）用 `.resolve()` 展开 `..`。实测：

```python
root = Path('graphs/okf').resolve()
rel  = 'cbam-reporting/../secret/x.xlsx'      # first seg = cbam-reporting（在 spaces 里，放行）
(root/rel).resolve()                          # → graphs/okf/secret/x.xlsx（okf/secret/机密.txt 真实存在）
root in (root/rel).resolve().parents          # True → 越界检查也过
```

即：知道别人空间名就能读其 xlsx。修法：`_check_scope` 里先 `PurePosixPath(rel)` 规范化，
拒绝任何 `..`，并校验规范化后仍只有两段（`/空间/文件`）。**这条要在搬 store 之前做**，
否则新代码会把漏洞一起搬过去。顺带补一个回归测试。

### 0.2 `has_tables` / `_list_files` 只看本地盘

- `graphs/space_agents.py:96` 的 `has_tables()` 用 `space_dir(space).glob("*.xlsx")` 决定
  **装不装** xlsx 四件套（`graphs/domain_agents.py:51` 依赖它）。字节进对象存储后，
  新上传的表**永远装不上工具**——静默失效，最难查的那种。
- `custom/frontend_api.py:158/164` 的 `_count_files` / `_list_files` 用 `rglob` 扫本地盘，
  前端的空间文件列表会看不到新表。

修法：`has_tables` 改成"本地 `glob` **或** S3 `list_objects_v2(prefix=f"spaces/{space}/", MaxKeys=1)`"，
结果按 space 缓 60 s（它是同步函数、在构图期调用，不能查 async PG）。

## 1. 分层

| 存什么 | 放哪 | 生命周期 | 是真相吗 |
|---|---|---|---|
| xlsx 字节（27.9 MB/个） | `spaces/{space}/{sha16}/{name}` | 跟元数据行同生共死 | 是 |
| 元数据 | PG `space_object` | 事务 | 是 |
| `.sqlite` 索引（26.9 MB） | `index/{space}/{sha16}/{name}.h{header}.v{build}.sqlite` + 本机缓存 | 可丢 | **否** |
| 会话文件（md） | 现状 Store（ADR-0013 不变） | 72h 孤儿 TTL | 是 |

一条规矩定死：**本地磁盘上的一切都是缓存**。`graphs/okf/` 降级为"部署自带的只读样例"。

## 2. 命名与表

```
tmp    tmp/{uuid4}/{name}                                  # 上传中转，生命周期 7 天兜底
blob   spaces/{space}/{sha16}/{name}                       # sha16 = sha256 前 16 hex（64 位）
index  index/{space}/{sha16}/{name}.h{header}.v{BUILD}.sqlite
cache  {XLSX_CACHE_DIR}/{space}-{sha16}-{header}-v{BUILD}.sqlite   # 本机，可删
```

四个要点：

- **`{sha16}` 进路径**：并发上传同名不同内容时各写各的 key，谁最后写 PG 行谁赢，
  另一个是孤儿对象。**永远不会出现"行说 A、字节是 B"**。用 16 hex 而不是 8
  （32 位在"同一文件反复改"的长期累积下有实际碰撞风险，代价为零）。
- **索引单独前缀**：索引与被覆盖的旧版本 blob 都不在行的 `object_key` 里，
  若和 blob 同前缀就没有任何东西会删它们（`spaces/` 没有生命周期规则）。
  删除时按 `index/{space}/{sha16}/{name}.` 前缀 list + delete。
- **`v{BUILD}`**：代码改列名/类型后旧产物必须失效，否则永远读到旧结构。
  `BUILD` 是 `index.py` 里的整数常量，改 schema 就 +1。
- **本地缓存名带全部维度**：`space`+`sha16`+`header`+`build`，少一个就会串表。

```sql
CREATE TABLE space_object (
    space        VARCHAR(100) NOT NULL REFERENCES space(name) ON DELETE CASCADE,
    name         VARCHAR(200) NOT NULL,
    object_key   VARCHAR(512) NOT NULL,
    sha256       CHAR(64)     NOT NULL,
    bytes        BIGINT       NOT NULL,
    content_type VARCHAR(100) NOT NULL,
    uploaded_by  VARCHAR(50)  NOT NULL REFERENCES user_tables(identity),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (space, name)
);
```

`name` 校验沿用 `custom/uploads.py::_safe_name`（basename、去控制字符、≤200 字符、
`PureWindowsPath` 剥反斜杠），扩展名白名单 `.xlsx`。

## 3. 上传

`POST /custom/spaces/{space}/files`（multipart，单文件，字段 `file`）
路由注册在 `custom/fastapi.py:40` 之后（不是 `custom/main`）。

### 3.1 大小判断（实际生效顺序）

| 序 | 动作 | 失败 |
|---|---|---|
| 1 | `require_auth` + `space ∈ user_spaces`（FastAPI 依赖，进入 handler 前已跑完） | 404 |
| 2 | `Content-Length > MAX_BLOB_MB` | 413，一个字节都不读 |
| 3 | 前端 `File.size` 预检 | 纯 UX，不可信 |
| 4 | 流式读 64 KB：计数 + `hashlib.update` | 超线 → 停止喂流 → 删 tmp → 413 |

**不用** `b"".join(chunks)`（现在 `custom/uploads.py:69` 就是这样，27.9 MB 常驻两份）。
`MAX_UPLOAD_MB=10` 只留给 md，不动；新增 `MAX_BLOB_MB=50`（目标表的 1.8 倍）。

**用 `upload_fileobj` 直写 S3 的 `tmp/` key**（不落本地临时文件）：请求流长度在读完前不可信，
而 `put_object` 要求先给 `ContentLength`；`upload_fileobj` 按 8 MiB 自动分片、长度未知也能跑、
单片失败可单独重试。S3 官方建议 ≥100 MiB 才上 multipart，27.9 MB 单 PUT 也对，但分片对
"长度未知"更省心。

**中断的 multipart 必须有人收尾**：`upload_fileobj` 的 `upload_id` 在内部，超线抛异常时
**拿不到它来 abort**，S3/SeaweedFS 的未完成分片会一直占空间。所以
`custom/blobs.py::abort_stale_multiparts(older_than)`：`list_multipart_uploads` + 超 1 天的全部
abort。**没有现成的挂载点**——aegra 的 crons API（`src/aegra_api/api/crons.py:88`）只按 schedule
触发**图 run**，不是运维脚本调度器。落成 `scripts/reap_multipart.py`，交给系统计划任务/侧车。

### 3.2 时序（写序：不可变 blob 先，可变元数据后）

```
0. 读 PG 现有行（若有），记 old_key / old_bytes        ← 只为失败时判定，不加锁
1. 流式 upload_fileobj → tmp/{uuid}/{name}            计数 + sha256；进程崩溃只留 tmp 孤儿
2. copy_object tmp → spaces/{space}/{sha16}/{name}    单次原子出现
3. BEGIN
     SELECT 1 FROM space WHERE name=$1 FOR UPDATE      ← 配额串行化在这行
     SELECT COALESCE(sum(bytes),0) FROM space_object WHERE space=$1
     if sum - old_bytes + new_bytes > SPACE_QUOTA_MB → 413
     INSERT … ON CONFLICT (space,name) DO UPDATE
   COMMIT
4. DELETE tmp
```

**失败清理**（第 3 步失败）：删 `tmp/` 永远安全；最终 key **只在 `new_key != old_key` 时删**。
否则"同名同内容重传恰好卡在配额"会把**旧行仍在引用的字节**删掉，造出"有行无字节"。

配额为什么不用 `list → 判 → put`：现有会话上传那条路（`custom/uploads.py:110-118`）是 TOCTOU，
md 时代代价小，二进制时代是 GB 级。锁 `space` 行即可，`space` 表本来就一行一个空间，
不需要新增计数表（也就不会有计数漂移）。

**幂等**：`(space, name)` 主键 + `ON CONFLICT DO UPDATE`。唯一约束落在 DB 上，不是"先查后插"
（并发下两个请求会同时查空、同时插入）。重传同内容 → sha 相同 → 索引 key 相同 → 不重建。

### 3.3 错误码

| 情况 | 码 |
|---|---|
| 无权限 / space 不存在 | 404（不泄漏存在性） |
| 扩展名不是 `.xlsx` | 415 |
| 超 `MAX_BLOB_MB` | 413 |
| 超 space 配额 | 413（detail 带已用量/上限） |
| 文件名非法 | 422 |
| 对象存储不可用 | 503 |

响应 `{path: "/space/x.xlsx", name, bytes, sha256, indexed: false}`；接口**不等 ingest**。

### 3.4 明确不做

| 不做 | 理由 | 触发升级 |
|---|---|---|
| 前端分片 + 预签名直传 | 1 GB 实测中转 58.7 s/CPU 72% vs 直传 29.3 s/5%，但我们是 28 MB；入口已抽成 `put_stream()` | `MAX_BLOB_MB > 100` 或出口带宽被打满 |
| 断点续传 / 前端进度 | 一次性传一个表，收益近零 | 同上 |
| "秒传"预检（`POST /check?sha256=`） | 只省一次上传；多一次 RTT；解决不了"检查完、上传前"的竞态——权威只能是 DB 唯一约束 | 永不（我们不是内容去重产品） |

## 4. 读取：字节从哪来

`graphs/tools/xlsx/store.py::fetch(path, space) -> Path`：本地盘 → 本机缓存 → `get_object`。

- 缓存写必须是 **`.tmp` + `os.replace`**，命中后再 `PRAGMA quick_check`。
  `immutable=1` 关掉了 SQLite 自己的锁，撕开写坏的索引会被当有效文件复用。
- 清理：启动 + 每 N 小时扫一次，删 mtime > `XLSX_CACHE_TTL_DAYS`。
- 缓存目录**不能在 OKF 根下**（根散文件对所有登录用户只读共享）。
- 并发下同一文件可能各下各的（28 MB，罕见），用原子替换保证不会读到半个文件。

## 5. ingest → SQLite

### 5.1 触发

`index.py::ensure(space, sha16, name, header) -> Path`：本机缓存 → `HEAD` 索引 key →
build 到 `tempfile` → 单次 `put_object` → 原子落到缓存。键见 §2。并发 build 幂等，无锁。

### 5.2 build

```python
sheets = pd.read_excel(real_path, sheet_name=None, header=header)   # 一次 41.7 s
for i, (name, df) in enumerate(sheets.items()):
    conn.execute("INSERT INTO meta VALUES (?,?)", (i, name, len(df)))
    df.to_sql(f"s{i}", conn, index=False)
# columns 表：(sheet_idx, col_idx, col_name, dtype)
```

`PRAGMA journal_mode=OFF, synchronous=OFF` 只用于 build（临时文件，崩了重来）。

### 5.3 四个工具的 SQL 映射（返回值必须逐字不变）

| 工具 | 现在 | 之后 |
|---|---|---|
| `xlsx_info` | 全读 41.7 s | `SELECT` from `meta` + 前 3 行 `limit 3` |
| `xlsx_query` | `read_excel` + `df.query` | `COUNT(*)` 同 WHERE 取 total + `LIMIT/OFFSET` 取页 |
| `xlsx_search` | 逐**单元格**命中 | **每列一条 `UNION ALL`**，`ORDER BY rowid, col_idx` |
| `xlsx_profile` | pandas 逐列 | 逐列 `COUNT(*) / COUNT(col) / COUNT(DISTINCT) / min / max / avg` |

**两个必须写准的地方**（审阅实测出来的）：

1. **行号是 `rowid + header + 1`，不是 `+2`**。`to_sql` 的 rowid 从 1 开始，而现在
   `_search` 的 `r` 从 0 开始（`graphs/tools/xlsx/__init__.py:158` 的 `r + header + 2`）。
   对不上就整体错一行，且"看起来能跑"。写进测试。
2. **`xlsx_search` 不能折叠成"每行一次"**。现在是 `mask.to_numpy().nonzero()`（`:155-160`），
   一行里两列命中会出**两条**结果、且带列名。一条 `WHERE c0 LIKE … OR c9 LIKE …`
   只能给出每行一次、拿不到列。所以按列拆：

```sql
SELECT rowid, 0 AS col, CAST(c0 AS TEXT) AS v FROM s0 WHERE c0 LIKE ? ESCAPE '\'
UNION ALL SELECT rowid, 1, CAST(c1 AS TEXT) FROM s0 WHERE c1 LIKE ? ESCAPE '\'
… ORDER BY rowid, col
```

`total` 仍需扫完全表（现在就是为了拿真实总数才不早停），命中列表再截到 `_MAX_HITS`。
`_MAX_VALUE_LEN=80` / `_MAX_ROWS=200` / `_MAX_HITS=100` 三个钳位不变。
`xlsx_query` 的头 `匹配 N 行，返回第 a-b 行（列：[…]）` 用同一公式可逐字复现，
空页也会照样印 `第 201-200 行`（pandas 的行为，别"修"它）。

### 5.4 `where` 的语义差异（全部实测，SQLite 3.53.1）

| 表达式 | pandas `df.query` | SQLite 直拼 | 结论 |
|---|---|---|---|
| `数量 > 0` | ✅ | ✅ | 裸中文列名可用 |
| `数量(个) > 0` | ✅ | ❌ `no such column` | **括号列名必须加引号** |
| `价 格 > 0` | ✅ | ❌ `syntax error` | **空格列名必须加引号** |
| `"价 格" > 0` | — | ✅ | 双引号/反引号都行 |
| `pn == 'a'` | ✅ | ✅ | `==` == `=` |
| `pn != 'a'`（含 NULL 行） | 返回 1 行 | **返回 0 行** | **语义不同，必须写进描述** |
| `~col.isna()` / `@var` / `col.str.xx` | ✅ | ❌ | 不支持，写 `col IS NULL` |

所以工具描述必须写：**列名只要含字母数字下划线之外的字符（括号、空格、点、横线）就用
双引号包起来；空值判断写 `IS NULL`；`!=` 在 SQL 里会漏掉空值行**。
`LIKE` 也不是字面匹配：keyword 含 `_` 或 `%` 会当通配符，必须 `ESCAPE`，
且只有 ASCII 忽略大小写（中文不分大小写无影响）。

### 5.5 安全边界

- `sqlite3.connect("file:…?mode=ro&immutable=1", uri=True)`；
- `set_authorizer`：只放 `SQLITE_SELECT / SQLITE_READ / SQLITE_RECURSIVE`，
  **`SQLITE_FUNCTION` 按函数名白名单**（`like/glob/lower/upper/length/abs/round/coalesce/ifnull/substr/trim`），
  其余一律 DENY。放开整个 `SQLITE_FUNCTION` 等于放开 `zeroblob(100000000)`，一个 where 就能吃到
  上百 MB；
- **不要用 `connect(timeout=)` 当语句超时**：那是等锁超时，对
  `1=1 AND (SELECT count(*) FROM s0 a,s0 b,s0 c)>0` 无效。用 `set_progress_handler` 计时间，
  超 `QUERY_BUDGET_MS` 就 `interrupt()`；
- `LIMIT` 仍由 `_MAX_ROWS` 钳死。

## 6. 一致性

| 场景 | 结果 | 机制 |
|---|---|---|
| 两实例同时 ingest | 都成功，语义相同 | 键 = sha256 + header + BUILD；PUT 原子；无锁 |
| 上传中途崩溃 | `tmp/` 孤儿 | 生命周期 7 天 |
| 第 2 步完、第 3 步崩 | 孤儿 blob | 生命周期 / 手工清；**不会有行没字节** |
| 第 3 步失败（配额） | 旧行完好 | 只在 `new_key != old_key` 时删最终 key |
| 上传超限被打断 | 未完成分片滞留 | `scripts/reap_multipart.py`（§3.1） |
| 删文件 | 行先删 → blob key + `index/{…}/{name}.` 前缀全删（404 当成功） | 前缀 list，避免索引永留 |
| 并发同名不同内容 | 行指向最后提交者；另一份孤儿 | `{sha16}` 进 key |
| 配额并发 | 严格 | `SELECT … FROM space FOR UPDATE` |
| 旧缓存 | 自然失效 | 文件名含 sha16 + BUILD |
| 多副本 Worker | 无差异 | 无进程内状态 |

**不需要**：Redis 广播、分布式锁、`If-None-Match`、引用计数、leader election。

## 7. 外部设计原则校准

查过业界通行做法后逐条对照（来源见 §12）。除一条外都是有意偏离：

| 通行原则 | 本方案 | 判定 |
|---|---|---|
| 大文件走前端分片 + 预签名直传 | 服务端流式中转，入口抽成 `put_stream()` | 偏离，见 §3.4 的升级触发 |
| 分片上传 + 断点续传 + 进度条 | 无 | 偏离，同上 |
| 幂等靠 DB 唯一约束，不靠"先查后插" | `(space,name)` 主键 | ✅ |
| SHA-256 流式计算，不整份进内存 | 边读边 `hashlib.update` | ✅ |
| 重复文件返回 409 + 已有 id | 同名同内容直接 200，复用索引 | 偏离：覆盖（PUT）才是正确语义 |
| 哈希不要在请求线程同步算（2 GB 要几分钟） | 27.9 MB 约 100 ms | 阈值内；>500 MB 改分片并行或挪后台 |
| multipart 必须显式 complete/abort | 靠 `abort_stale_multiparts` | ✅ 已补 |
| 内容寻址 + 引用计数 | 主键 `(space,name)`，哈希只进 key | 偏离，换零 GC；代价是同表多人传存两份 |

SeaweedFS 侧：S3 网关是 filer 的前端（不是独立存储）；bucket versioning 的支持按版本而异
（有报告称 3.85 不支持 `PutBucketVersioning`），所以**不依赖 versioning**——`{sha16}` 进 key
已经解决覆盖问题。多实例可各起一个 gateway 挂在同一组 filer 后做 HA。

## 8. 依赖与配置

| 包 | 用途 | 备注 |
|---|---|---|
| `boto3>=1.35`（+ `botocore`） | S3 客户端 | 唯一新运行时依赖；同步 API 走 `asyncio.to_thread` |
| SeaweedFS `weed.exe`（非 pip） | 对象存储本体 | release zip / docker `chrislusf/seaweedfs` |

不新增：`sqlite3`（stdlib）、`pandas`/`openpyxl`（已有）、`aioboto3`（多一份 session 生命周期）、
`moto`（S3 层用真 SeaweedFS 的 e2e 覆盖，单测用 monkeypatch，见 §10）。

**boto3 client 必须懒加载**：`graphs/tools/xlsx/__init__.py` 在 import 期就被
`graphs/tools/__init__.py:13`、`graphs/xlsx_tools.py`、`graphs/xlsx_process copy.py:12` 拉起来，
模块级建 client 会让没配 `S3_*` 的环境（本地 demo、单测）直接 import 失败。

```bash
S3_ENDPOINT=http://127.0.0.1:8333
S3_ACCESS_KEY=... S3_SECRET_KEY=... S3_BUCKET=aegra-blobs S3_REGION=us-east-1
MAX_BLOB_MB=50            # xlsx 单文件上限；MAX_UPLOAD_MB=10 留给 md
SPACE_QUOTA_MB=2048
XLSX_CACHE_DIR=.cache/xlsx
XLSX_CACHE_TTL_DAYS=7
```

`Config(s3={"addressing_style": "path"})` 必须有：给了 `endpoint_url` 后 boto3 仍默认
virtual-host，`bucket.127.0.0.1:8333` 解析不了。bucket 启动时 `head_bucket` 失败则
`create_bucket`（幂等）。

## 9. 改动清单

| 文件 | 内容 | 估行 |
|---|---|---|
| `graphs/tools/xlsx/__init__.py` | **先修 0.1 越权**；四个 tool 改为调 `index` | 改 ~120 |
| `custom/blobs.py` | 新增：懒加载 client、key 命名、`put_stream`（计数+sha256）、`get`、`copy`、`delete`、`head`、`abort_stale_multiparts` | ~110 |
| `custom/space_files_api.py` | 新增：上传路由 + 鉴权 + 配额事务 | ~80 |
| `custom/models.py` + alembic | 新增 `SpaceObject` | ~50 |
| `graphs/tools/xlsx/store.py` | 新增：本地盘/缓存/S3 解析，原子写缓存 | ~60 |
| `graphs/tools/xlsx/index.py` | 新增：`BUILD`、build、只读 SQL 查询 | ~170 |
| `graphs/tools/xlsx/schema.py` | `where` 描述改成 SQL 口径（引号/IS NULL/!= 漏空值） | ~6 |
| `graphs/space_agents.py` | `has_tables` 增加 S3 探测 + 60 s 缓存 | ~20 |
| `custom/frontend_api.py` | `_list_files` / `_count_files` 合并 `space_object` | ~20 |
| `custom/fastapi.py` | 注册新 router（`:40` 后） | ~2 |
| `scripts/reap_multipart.py` | 新增：清未完成分片 | ~30 |
| `pyproject.toml` / `.env.example` | `boto3` + 新 env | ~10 |

## 10. 测试计划

`tests/unit/`（S3 层 monkeypatch `blobs`，不引 moto）：

1. **越权回归**：`/空间A/../空间B/x.xlsx` 必须被拒（对应 §0.1）；
2. `_safe_name` 对 `.xlsx` 的接受/拒绝矩阵；
3. 流式计数超 `MAX_BLOB_MB` → 413 且 tmp 被删；
4. 配额：`sum - old + new > 上限` → 413；同名覆盖不吃双份；**失败时不删旧 key**；
5. 幂等：同内容重传 → 索引 key 不变、`ensure()` 不 build；
6. 索引：`BUILD` 变化 → 新 key；本地缓存写入是原子替换（并发下不会读到半个）；
7. **行号公式**：`rowid + header + 1`，`header=0/1` 各一条；
8. **`xlsx_search` 单元格语义**：一行两列命中 → 两条结果、列名正确；
9. `where` 语义矩阵（§5.4 六条，含括号列名、`!=` 空值、`LIKE` 通配转义）；
10. authorizer：`zeroblob` / `ATTACH` / `PRAGMA` 被拒；`progress_handler` 能拦住笛卡尔积炸弹。

`tests/e2e/test_space_files/`（真 SeaweedFS）：上传 27.9 MB → 首次 `xlsx_query` 41 s →
第二次 <100 ms → 删行后查不到 → 索引前缀也已清空。

## 11. 未做 / 已知限制

- 首次访问 41.7 s（预建索引未做，需动 worker）。
- 前端直传、断点续传、秒传预检：见 §3.4。
- 存量 `graphs/okf/<space>/*.xlsx` 迁移脚本未写，先双读单写。
- 存储层配额不严格：PG 的 `sum(bytes)` 只数 xlsx，索引与被覆盖的旧 blob 不计入，
  所以"配额"只在元数据层面成立，物理用量可能到 2 倍。接受（可按 prefix 定期核账）。
- `abort_stale_multiparts` 依赖 SeaweedFS 实现 `ListMultipartUploads`：社区资料说支持，
  **没有实测**。若不支持，退化到手工清。
- 只支持 `.xlsx`；`csv` / `xls` / 多文件联查（duckdb）未做。
- 没查 `deepagents` 的 `FilesystemMiddleware` 是否对 `/space/*.xlsx` 做额外处理
  （8 个 fs 工具读不了 xlsx，预期没有），开工前 5 分钟确认。

## 12. 参考

- [S3 multipart upload limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html)：
  ≥100 MiB 考虑分片；part 5 MiB–5 GiB；最多 10 000 片；未完成上传必须显式 abort。
- [Uploading objects](https://docs.aws.amazon.com/AmazonS3/latest/dev/UploadingObjects.html)：单 PUT 5 GB / 分片 5 TB。
- 预签名直传 vs 服务端中转的量化对比（1 GB：58.7 s/72% CPU 对 29.3 s/5% CPU）：CSDN《前端分片直传 MinIO》。
- 幂等上传：SHA-256 + DB 唯一索引 + 409，禁止只靠业务层先查后插：php中文网《C# 文件上传的幂等令牌》。
- 同步哈希阻塞请求线程的线上复盘（2 GB 数分钟）：CSDN《大文件上传 SHA-256 哈希阻塞请求线程》。
- SeaweedFS：S3 网关 = filer 前端、多 gateway 做 HA；`completeMultipartUpload` 相关提交见
  [seaweedfs/seaweedfs](https://github.com/seaweedfs/seaweedfs)。
- SeaweedFS bucket versioning 的版本差异：CSDN《seaweedfs 桶版本控制》。
