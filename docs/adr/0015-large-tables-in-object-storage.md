# 大表（xlsx）放对象存储，派生索引用 SQLite

> 状态：**决定已定，实现未开始**（2026-10-25）。
> 本文取代 ADR-0013 那张对比表里「空间表（第二期）→ Store 的 space 命名空间」一行；
> ADR-0013 关于会话文件（md、Store、TTL）的全部内容**不变**。
> 实现细节见 [`../large-files.md`](../large-files.md)。

## 背景：三个实测数字

目标规模是 80k 行 × 10 列 × 3 sheet（240 万 cell），xlsx 文件 27.9 MB。

| 事实 | 数字 |
|---|---|
| 现有四个 xlsx 工具的读法 | `pd.read_excel` 全表进内存（`graphs/tools/xlsx/__init__.py:91`） |
| `xlsx_info` 现在（3 sheet 全读） | **41.7 s**，峰值 129 MB |
| 单 sheet `read_excel` | **14.7 s**（无缓存，每次一样慢） |
| 一次 ingest 进 SQLite（df 已在内存） | 0.5 s，产物 26.9 MB |
| 之后 `WHERE … LIMIT 200` / `COUNT` / `GROUP BY` | 2 ms / 1 ms / 3 ms，峰值 <1 MB |
| `read_excel(nrows=5)` | 25 ms |
| `openpyxl load_workbook(read_only=True)` 取行列数 | 27 ms |

结论：**慢的不是查询，是"每次调用都重新解析整个 xlsx"**。所以问题不是"怎么查得快"，
而是"解析一次、之后别再解析"。

## 关键决定

### 1. 字节进对象存储，不进 Store、不进 PG

- Store：值必须是 JSON 文本，xlsx 得 base64，约 37 MB/文件，且每次 `get` 拉整块，
  还会被 store sweeper / TTL 语义牵连。
- PG：同理，`bytea` 37 MB 走 WAL 与备份，代价不可接受。
- 对象存储（SeaweedFS，S3 协议）：客户端用 `boto3` 直连。

**不需要把 xlsx 接进 deepagents 的 backend**。四个 xlsx 工具本来就不走 backend
（自读盘），它们需要的只是"字节从哪来"——把 `Path` 换成 `(bucket, key)` 即可。
S3Backend / BlobStorageMiddleware / 社区 `deepagents-backends` 一律不需要：
deepagents 本地 0.7.23 无 S3 backend，上游 PR #1913 与 #6325 均 closed 未合入。

### 2. 大表挂 **space**，不挂会话

这是本 ADR 最重要的一个取舍。会话文件（ADR-0013）的命名空间是
`(uploads, sha256(上传者 identity), 主管 thread_id)`，而真正干活的子 agent 用静态身份
`subagent_service`（ADR-0004），拿不到主管 identity，算不出那个命名空间；要靠
`AsyncTaskGuard._config_for()` 做受信转发才行——而那是一次 ADR 级改动，且转发当前
**并不存在**（`AsyncTaskGuard._config_for()` 只回传 `user_spaces`；ADR-0013 §1 的描述
与实现不符，是文档 bug）。

而 **space 已经在 `configurable.user_spaces` 里转发给子 agent**（ADR-0005，
`graphs/space_access.py:43`）。ADR-0013 在 2026-10-25 的更正块（`docs/adr/0013:29-45`）里
已经自己写明了“`parent_thread_id` 转发不存在”，所以给 `/uploads` 开子 agent 需要同时改
转发与 backend 路由两处。挂 space 之后：

- 鉴权、隔离、`_check_scope`（`graphs/tools/xlsx/__init__.py:68`）、四个工具、
  子 agent 可见性**全部现成**（依赖的正是同一个 `_config_for()` 的 `user_spaces`，
  区别只是这条转发已经存在）；
- ADR-0013 关于「会话文件只对主管可见、子 agent 的 backend 没有 `/uploads/` route」
  的决定**一个字都不用改**。

**但“现成”不等于“正确”**：`_check_scope` 今天有跨空间越权（第一段判权后任 `..` 都能穿，
实测 `/空间A/../空间B/x.xlsx` 能读到 `空间B` 的文件）。**搬 store 之前必须先修它**，
不然新代码会把同一个漏洞一并搬过去。见 `../large-files.md` §0.1。

代价：大表对空间成员可见（而非仅上传者）。这与 space 本身的语义一致，可接受。

### 3. 派生索引 = SQLite，键由内容哈希决定

- 每 sheet 一张表 `s0/s1/s2`，另加 `meta` / `columns` 两张小表。
- 索引键 = `sha256(字节) + header + BUILD`（`BUILD` 是代码里的 schema 版本号，改列就 +1）。
  **相同内容 → 相同键 → 相同产物**，于是“谁建都一样”：不需要分布式锁、不需要 leader
election、不需要跨实例广播。
- 索引是**可丢弃的派生数据**：建失败不影响上传成功，删了下次重建。索引放独立
  `index/` 前缀（不能跟 blob 同前缀：它不在 PG 行的 `object_key` 里，同前缀就没人删它）。
- 打开方式 `mode=ro&immutable=1` + `set_authorizer` 只放 SELECT/READ + 函数名白名单。
  `where` 直拼 SQL 在引擎层被限死，比现在 `df.query(engine="python")` 能跑任意 Python 严格。
- `where` 的语法与现在**不完全兼容**（已实测）：`数量 > 0` 裸中文名可用，但 `数量(个)`、
  `价 格` 必须加引号；`!=` 在 SQL 里会漏掉空值行；`~col.isna()` / `@var` 不支持。
  这些必须写进工具描述，否则模型会大面积报错或静默少行。

顺带一个"免费"的兼容性：SQLite 的 unquoted identifier 允许任意 ≥0x80 字符，反引号也
支持，`==` 等价于 `=`。现有 `where` 写法（`数量 > 0`、`料号 == 'ABC'`、
`` `col a` > 1 ``）几乎能原样跑，不需要写表达式翻译器。

### 4. 字节入口先经服务端流式中转，直传留到 >100 MB

`presigned PUT` 让浏览器直传才是大文件的正解，但需要额外的「确认上传」接口、临时前缀、
过期清理。当前 27.9 MB 一次中转完全够用，且 sha256 本来就要过一次字节。
把入口抽成 `custom/blobs.py::put_stream()` 一个函数，换直传时只改它。

## Considered Options

| 方案 | 否决理由 |
|---|---|
| xlsx 进 LangGraph Store | base64 约 37 MB/文件，每次调用拉整块 |
| xlsx 进 PG `bytea` | WAL / 备份 / 连接内存都不划算 |
| `pyarrow` + `duckdb` + parquet | 两个新依赖，百万行 / 多文件联查才值得 |
| 进程内 LRU 缓存 DataFrame | 现在收益≈0；大表时又不如磁盘产物（`memory_usage()` 还低估 object 列） |
| 给子 agent 挂 `/uploads` | 推翻 ADR-0013，需先改身份转发 |
| S3Backend / BlobStorageMiddleware | 工具本来不走 backend，属多余 |
| 内容寻址主键（`blobs/{sha256}`）+ 引用计数 | 删除要跨会话引用计数与 GC；改用 `(space, name)` 主键后整类问题消失 |
| 浏览器直传（presigned） | 第一期成本不划算，留到 >100 MB |

## 后果

**要接受的**：

- **必须顺带修两处既有缺陷，否则本方案会静默失效**：
  ① `_check_scope` 的跨空间越权；② `graphs/space_agents.py::has_tables` 与
  `custom/frontend_api.py` 的 `_list_files` 都只扫本地盘 → 新上传的表永远装不上 xlsx 工具、
  前端也看不到。详见 `../large-files.md` §0。
- 首次访问一个未建索引的大表会 **41.7 s**（一次，全集群一次）。缓解（上传后丢后台 job
  预建）需要动 worker，本期不做，代码里记 `shortcut:`。
- 新增一个运行时依赖 `boto3`，以及一个部署组件 SeaweedFS。
- 多一条运维逻辑：未完成的 multipart 上传 S3 不会自动清（必须显式 abort），
  需要 `scripts/reap_multipart.py` 定期跑一次。aegra 的 crons API 只触发**图 run**，
  不是运维脚本调度器，所以这个脚本得交给系统计划任务/侧车（`docs/large-files.md` §3.1）。
- 存量 `graphs/okf/<space>/*.xlsx` 需要迁移；迁移脚本本期未写，先做**双读单写**兼容。
- `graphs/okf` 从"真相"退化为"部署自带的只读样例"。这顺手修掉一个已有的多机缺陷：
  本地盘挂载在多副本下本来就不一致。

**不需要再做的**：不再讨论「xlsx 放 PG / Store」，不再试图给 deepagents 写 S3 backend。

## 怎么验证（本地）

```bash
weed server -s3 -dir=./.seaweedfs            # S3 网关在 :8333
# .env: S3_ENDPOINT=http://127.0.0.1:8333 / S3_BUCKET=aegra-blobs
.venv/Scripts/python.exe -m aegra_api.main
.venv/Scripts/python.exe -m pytest tests/unit/test_xlsx_index.py -q
```

`boto3` 必须配 `s3={"addressing_style": "path"}`：给了 `endpoint_url` 之后 boto3 仍默认
virtual-host，`bucket.127.0.0.1:8333` 解析不了。
