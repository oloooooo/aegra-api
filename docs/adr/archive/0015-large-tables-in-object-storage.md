# ~~会话文件：md 进 Store、xlsx 进对象存储，都私有且随会话删~~

> 状态：**主体已实现**（决定 2026-10-25，第 3 稿；2026-10-26 复核代码后订正此状态行——之前一直写着「实现未开始」）。
> 本文取代第 1 稿「大表挂 space」的全部内容（那版已作废，理由见文末「作废记录」）。
> 实现细节见 [`../large-files.md`](../large-files.md)；空间内容进 S3 见 [ADR-0016](./0016-spaces-in-object-storage.md)。
> 落点：`shared/session/blobs.py`（S3、自管分片可 abort、`delete_prefix`）、`custom/service/session_files.py`（配额与行）、
> `custom/db/models.py::SessionObject`、`custom/api/uploads.py::POST /custom/uploads/tables`、
> `custom/middleware/threads_delete.py`（两条删除链合流）、`graphs/tools/xlsx/index.py`（SQLite 索引）、
> `graphs/access/session_scope.py`（签名凭证）、`graphs/supervisor.py::supervisor_backend`（`/uploads/` 路由）。
> **仍未做**：预建索引要动 worker（见「后果」）。~~② 未完成分片与 `tmp/` 残留没有清理脚本~~ →
> 2026-10-26 已补 `blobs.reap_tmp()` + `scripts/reap_multipart.py`（实测清掉一个 24h+ 的
> `tmp/` 残留对象；用法见 `docs/S3.md` §8）。

## 背景：三个实测数字

目标规模 80k 行 × 10 列 × 3 sheet（240 万 cell），xlsx 27.9 MB。

| 事实 | 数字 |
|---|---|
| 现有四个 xlsx 工具的读法 | `pd.read_excel` 全表进内存（`graphs/tools/xlsx/__init__.py:91`） |
| `xlsx_info` 现在（3 sheet 全读） | **41.7 s**，峰值 129 MB |
| 单 sheet `read_excel` | **14.7 s**（无缓存，每次一样慢） |
| 一次 ingest 进 SQLite（df 已在内存） | 0.5 s，产物 26.9 MB |
| 之后 `WHERE … LIMIT 200` / `COUNT` / `GROUP BY` | 2 ms / 1 ms / 3 ms |
| `read_excel(nrows=5)` / `openpyxl read_only` 取行列数 | 25 ms / 27 ms |

结论：慢的不是查询，是"每次调用都重新解析整个 xlsx"。

## 关键决定

### 1. 归属：**上传者私有 + 随会话删**，不挂 space

文件只对上传者本人、只在这一条主管 thread 里可见；会话删除即删。
这与 md 会话文件（ADR-0013）是同一套会话语义，只是载体不同。

### 2. 两种载体：md → Store，xlsx → 对象存储

- md 继续留 LangGraph Store：`/uploads/*.md` 要靠 `StoreBackend` 才能被 `read_file` 读到，
  而 deepagents 没有 S3 backend（上游 PR #1913 / #6325 均 closed）；Store 的
  「最后读取后 N 小时过期」也正是 ADR-0013 选的孤儿语义。
- xlsx 必须进对象存储：base64 进 Store 约 37 MB/文件，每次 `get` 拉整块。
- 两边的删除链统一到「删会话即删」，所以只有一条 mental model。

### 3. 文件名前缀只是**路由**，不是隔离

上传文件名形如 `cbam-reporting_报价表.xlsx`，`_` 前那一段是 **space 名**：

- 必须 ∈ 该用户的 `user_spaces`，否则 422 拒收（这是「表规则」的一部分）；
- 主管据此决定派哪个 space 子 agent 分析（表不属于任何 space，但主题属于）；
- **存储前缀仍是 `sessions/{h(identity)}/{thread_id}/`**，与 space 无关。

这样"按空间分配"和"仅上传者可见 + 随会话删"同时成立。

### 4. 子 agent 靠签名凭证读同一份文件

子 agent 是静态服务身份（ADR-0004），算不出 `h(主管 identity)`。所以：

- `AsyncTaskGuard._config_for()` 额外签发 `configurable.session_scope = {owner, thread_id, sig}`，
  `sig = HMAC(服务端密钥, owner+thread_id)`；
- **不替换 `langgraph_auth_user`**：一换，`allowed_spaces()` 会走普通用户分支去读
  `auth_user.user_spaces`，子 agent 的权限从「本任务 space」放大成「用户全部 space」；
- 必须签名：`create_run_config` 只强制覆写 `thread_id/run_id/langgraph_auth_user`，
  其余 `configurable` 键客户端能自己塞（ADR-0013 更正块踩过这条）；
- 子 agent 的 backend 从 `SpaceBackend` 换成
  `CompositeBackend(default=SpaceBackend, routes={"/uploads/": StoreBackend(ns=f(session_scope))})`，
  xlsx 工具用同一个 `session_scope` 算 S3 前缀（工具仍绕 backend 直读字节）。

### 5. 上限与规则（env 可改，改数不发版）

| 项 | 值 | 说明 |
|---|---|---|
| `MAX_BLOB_MB` | 10 | xlsx 单文件字节上限（`MAX_UPLOAD_MB` 留给 md） |
| `MAX_CELLS` | 2 000 000 | 结构性硬闸，超了直接拒，不静默截断 |
| `MAX_MD_FILES_PER_SESSION` | 10 | |
| `MAX_XLSX_FILES_PER_SESSION` | 20 | |
| `MAX_THREADS_PER_USER` | 50 | **含子线程**（现行口径） |
| `MAX_BLOB_BYTES_PER_USER` | 2 GB | 每用户 blob 总量 |

**不做**：秒传预检、前端直传、断点续传、按内容去重（同名覆盖就是正确语义）。
**总是**建表索引（一次几十秒，换此后毫秒级；两条路径比一条贵）。

## Considered Options

| 方案 | 否决理由 |
|---|---|
| 大表挂 space（第 1 稿） | 一张随手传的表变成永久共享知识，不可撤回；与「私有 + 随会话删」冲突 |
| xlsx 进 Store / PG | base64 约 37 MB/文件，每次调用拉整块 |
| 子 agent 直接替换 auth_user | 权限从「一个 space」放大成「用户全部 space」（ADR-0005 的收窄失效） |
| 不签名、直接信 `configurable.session_scope` | 客户端可伪造 owner，读别人的会话文件 |
| md 也搬 S3 | 要自己实现 `BackendProtocol`（~200 行）+ 重建「读后续期」TTL；md 用不上 S3 的容量 |
| 内容寻址主键 + 引用计数 | 删除要跨会话引用计数与 GC；主键用 `(owner, thread, name)` 后整类问题消失 |
| 进程内 LRU 缓存 DataFrame | 现在收益≈0；大表时又不如磁盘产物 |

## 后果

**要接受的**：

- 必须顺带修两处**既有**缺陷，否则本方案静默失效：
  ① `_check_scope` 的跨空间越权（`/空间A/../空间B/x.xlsx` 现在能读）；
  ② `shared/knowledge/spec.py::has_tables` 与 `custom/api/spaces.py::_list_files` 只扫本地盘。
- 首次访问未建索引的大表 **41.7 s**（一次，全集群一次）；预建索引需要动 worker，未做。
- 新增运行时依赖 `boto3` + 部署组件 SeaweedFS。
- 多一条运维逻辑：未完成的 multipart 上传 S3 不会自动清，需自己定时调
  `shared/session/blobs.py::abort_stale_multiparts`（仓库没带定时脚本；aegra 的 crons API 只触发
  **图 run**，不是运维脚本调度器）。
- 两套载体 = 两套删除链，统一在 `custom/middleware/threads_delete.py` 一处。

## 作废记录（2026-10-25）

第 1 稿决定「大表挂 space，元数据进 `space_object` 表，空间成员共享」。作废原因：
用户明确要求 **xlsx 仅上传者可见、随会话删除**。挂 space 与这两条都不兼容（space 内的东西
对所有空间成员可见、且没有任何会话语义）。文件名里的 space 前缀因此降级为「路由提示」。

## 怎么验证（本地）

```bash
weed server -s3 -dir=./.seaweedfs            # S3 网关 :8333
# ~~.env: S3_ENDPOINT=http://127.0.0.1:8333 / S3_BUCKET=aegra-blobs~~
.venv/Scripts/python.exe -m pytest tests/unit/test_xlsx_index.py -q
```

boto3 必须配 `s3={"addressing_style": "path"}`：给了 `endpoint_url` 后仍默认 virtual-host，
`bucket.127.0.0.1:8333` 解析不了。
