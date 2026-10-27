# 所有文件读法收敛到一个 backend：对象存储做真相，台账做归属

> 状态：**已实现**（决定 2026-02-12，分支 `feat/unified-files-s3`）。
> 取代 [ADR-0016](./archive/0016-spaces-in-object-storage.md) 的「本地镜像」方案（镜像代码已随本文退役）；
> 订正 [ADR-0013](./0013-session-files-live-in-store.md)（md 不再在 Store）与
> [ADR-0015](./0015-large-tables-in-object-storage.md) 的对象 key 布局。
> 落点：`graphs/access/{factory,s3_backend}.py`、`shared/session/blobs.py`、
> `shared/knowledge/{root,spec}.py`、`graphs/middleware/session_files.py`、
> `graphs/tools/xlsx/`、`custom/api/{uploads,spaces}.py`。

## 背景

同一次 run 里「读一个文件」有三条互不相干的路径，各自的隔离判定也不同：

| 来源 | 谁读 | 隔离判定在哪 |
|---|---|---|
| 空间知识（`/space/...`） | 8 个 fs 工具 → `SpaceBackend`（`FilesystemBackend`） | `SpaceGuard`（ADR-0005 的单点） |
| 上传的 xlsx | xlsx 四件套直连 S3（`graphs/tools/xlsx/store.py`） | **工具层自带一份** `_check_scope` |
| 上传的 md | `/uploads/*` → `StoreBackend`（LangGraph Store） | Store 命名空间 |

触发这次合并的两个事实：**写入方以后会变多**（第三方同步、外部系统直写对象存储），
多副本部署下本地盘上的东西每台看到的不一样；而两套隔离判定意味着**改一处必然漏另一处**。

## 决定

### 1. 读文件只有一个入口：`factory.session_backend()`

图和工具都从 `graphs/access/factory.py::session_backend()` 拿 backend（经 `Runtime` 注入），
不再有「工具自己按来源分支」。xlsx 四件套改成 `runtime.backend.download_files([path])` 取字节，
`_check_scope` / `store.Remote` / `_remote_of` 的「查台账拿 key」全部删掉——**净减代码**。

### 2. 两个 backend 共用一套读家族

`graphs/access/s3_backend.py` 的 `_ObjectTree` 把 `ls/read/glob/grep/download` 写成
「给一份 `{虚拟路径: 对象}` 就一定能算出来」的纯函数，子类只提供三件事：这份 key 表、
可见性判定、写判定。

- `S3Backend`：知识空间。前缀 `okf/`，虚拟路径 `/空间名/文件`；空间隔离复用 `SpaceGuard`
  （与本地盘 `SpaceBackend` 是**同一份规则**，只换存储）；写一律拒绝。
- `UploadsBackend`：`/uploads/` 会话上传，只读。key 不可从文件名推导，只能查台账。

### 3. 上传对象 key 是 `uploads/<uuid>/<name>`，归属只记 PG

不再把 `(owner, thread)` 编码进 key，也不从 key 反推归属：`session_object` 一行
（`object_key` / `owner` / `thread_id` / `name` / `sha16` / `size_bytes`）是唯一路由。
同会话同名重传 = 新 uuid + 新行（旧对象由首次写入的 sha16 相同的行引用，天然去重）。

### 4. 清单真相也是台账

`graphs/middleware/session_files.py::_load()` 在配了对象存储时读
`custom/service/session_files.py::as_dicts()`（一次 `WHERE owner, thread_id` 查询），
不再读 LangGraph Store。Store 路径只剩「本地没配对象存储」的 demo 模式。

### 5. 模式开关 `okf.backend`

`local`（缺省，直读仓库 `knowledge/`）| `s3fs`（直读对象存储 `okf/`，无需本地副本）。
旧镜像模式 `s3` 与 `pull`/`push`、`scripts/okf_{push,pull}_s3.py` 已**删除**；配置里写 `s3`
现在会明确报错，而不是静默降级。`s3fs` 下 `knowledge_root()` 同样直接抛错——**没有本地目录**，
任何还想要 `Path` 的调用点必须在接线时改掉（`spec.py` 已全改，见下）。

### 6. 为什么仍然自研（复用 ADR-0016 的实测结论）

`deepagents-backends` 0.2.0 与锁定的 deepagents 0.7.23 协议不兼容（方法名 `ls_info/grep_raw`、
缺 `delete`、返回 `str`）；`langchain-aws` 的 S3 backend PR 长期未合入。
自己写只有 478 行，因为 `BackendProtocol` 的 9 个 `a*` 方法**全有默认实现**，
细语义（分页切片、替换校验、glob 匹配、`FileData` 组装）都在 `deepagents.backends.utils`，直接复用。

### 7. 表格的 `read` 不给字节给指路提示

`_ObjectTree._read` 遇到 `.xlsx/.xlsm/.xls` 且内容是二进制时，返回「用
`xlsx_info / xlsx_query`」的错误文本，而不是 base64。base64 表格对模型只烧 token、读不懂；
字节的唯一正当出口是 `download_files`（xlsx 四件套走这条）。

### 8. 框架的驱逐文件写 `artifacts/`，不写 `okf/`

`/large_tool_results/`、`/conversation_history/`、`/blobs/` 这类内部前缀是
`FilesystemMiddleware` 自己的缓存（工具结果太大就落文件、老消息压缩成文件留档），不是空间知识。
`S3Backend._key_of` 对它们走 `artifacts/` 前缀，`_keys()` 同时 LIST 两个前缀——所以框架驱逐后
仍能按路径 `read` 回来，但 `okf/` 里查不到它们。

### 9. `read`/`ls`/`download_files` 不做全量 LIST

只有 `glob`/`grep` 需要「整个前缀的 key 表」。`read_file`、`ls`、`download_files` 的 key 都能
由虚拟路径算出来，所以：

- `read`/`download_files` 只把被问的那一个 key 交给 `_ObjectTree`（存在与否由 GET 回答）
- `ls` 走单次**分隔** LIST（`blobs.list_dir`），只取这一层

实测（本地 SeaweedFS，2000 个对象 / 100 目录 × 20 文件）：`read` 122 ms → **1.8 ms**，
`ls` 132 ms → **2.9 ms**。这是纯浪费的账：一次 `read_file` 不值得先付一遍全空间的 LIST。

## 取舍

| 取舍点 | 选了什么 | 为什么不选另一个 |
|---|---|---|
| 归属编码在 key 里 vs 台账 | 台账 | key 编码归属时，「把文件转给别人」= 搬对象（对象存储没有 rename，只能 GET+PUT+DELETE）；台账改一行 |
| 一次动作一次台账查询 vs 按 run 缓存 | 每次查 | 缓存会让「同一 run 内新上传的文件看不见」这种坑只在生产偶发；一次主键查询换确定性 |
| `write` 用条件写 vs 覆盖 | 覆盖（last-write-wins） | 协议要求 write 是覆盖语义；把 CAS 加在 write 上会让「第二次写」失败，违反契约。CAS 加在真正有读-改-写窗口的 `edit` 上 |
| `UploadsBackend` 实现同步版 vs 只异步 | 只异步 | 同步读要把 async PG 会话跑在另一个事件循环里（`asyncio.run` 嵌套），本地和单副本都不成立；框架调用的正是异步方法 |
| 写能力（2026-02-12 用户拍板） | 先不开写：`write`/`edit` 的实现与 `edit` 的 CAS 留着，但不做启动探测 | 写入方还没定下来；加探测会让“写能用”这件事看起来已就绪。真要开写时再议探测与降级 |
| `spec.py` 模式分支 vs 抽象一层「虚拟文件系统」 | 分支 | 抽象层要同时包住 `Path` 和对象存储，两个调用面（`rglob`/`read_text`/`mkdir`）语义对不齐；分支只有 5 个函数 |

## 缺陷与已知风险

按「会不会静默出错」排序，都是**这次没做**的部分：

1. **`write` 不是 CAS**（见上）。两个副本同时 `write` 同一路径 → 后写的赢，无告警。
2. **`edit` 的 CAS 只在支持条件写的对象存储上成立**。SeaweedFS 自 PR #7154 起支持
   `If-Match`/`If-None-Match`（filer 同一把锁内判定+写入）；**别的 S3 实现若忽略该头**，
   `edit` 静默退化成 last-write-wins（哪些实现、哪种拓扑不成立见缺陷 10）。`edit` 冲突只重试
   1 次，之后把「文件已被改过」交给模型重读。
3. **`glob`/`grep` 仍付一次全空间 LIST**（`read`/`ls`/`download_files` 已不再——见决定 9）。
   一次 `list_tree("okf/")` 在 2000 个对象上是 115 ms 且随对象数线性增长；`glob`/`grep` 本身
   就是「扫一批」，暂时接受。升级触发：单空间对象数 > ~1 万，或 LIST > 500 ms 时改成按子路径
   分段 LIST + etag 缓存。
4. **`grep` 是 N+1 GET**：文本候选逐个 GET（本地 SeaweedFS 单次 GET ~1.8 ms）。
   已按 path 圈定候选（原来 `grep /空间/子目录/` 会把**整个前缀**的字节都 GET 一遍，
   2000 个对象实测 3.95 s → 圈定后 172 ms）。`max_count` 只省掉「已满足后不再取」。
   大文件量下的下一步是 per-run 的 ETag→内容缓存。
5. **`has_tables()` 已统一为递归**（原缺陷：两种形态都只看 `<space>/` 一层，表放子目录就不装
   xlsx 四件套——agent 的失败表现是“我没有读表工具”，不报错、最难排查）。现在两模式都递归
   扫整棵子树；代价是每次 run 多一次 LIST（`s3fs`）/目录遍历（本地）。
6. **对象存储没有空目录**：`_space_dir_exists` 用 `list_objects_v2(MaxKeys=1)` 探「有没有以此
   为前缀的对象」，所以「刚建好但还没写文件的空空间」在 `s3fs` 下不存在。
7. **孤儿对象没有自动 GC，但有对账脚本**：`threads_delete` 先删行再删对象，PG 删成功而 S3 删
   失败就留下孤儿（另有并发同名上传、覆盖后旧对象删除失败两个来源）。`scripts/reap_orphans.py`
   双向比对 `uploads/` 与台账，缺省只报告，`--delete-orphans` / `--delete-rows` 才动手。
   它是**人工运维脚本**，不是定时任务；要自动就得挂 cron（同 `reap_multipart.py`）。
8. **md 的 TTL 续期在台账模式下消失了**：Store 路径有 `refresh_ttl=True`，台账模式没有。
   结果是孤儿 md 到期不再被续命——这其实是想要的，但它不再是「靠 TTL 兜底」而是「靠删会话」。
9. **驱逐文件已挪出 `okf/`**（原缺陷：`/large_tool_results`、`/conversation_history`、`/blobs`
   在 `s3fs` 下写成 `okf/large_tool_results/…`，与空间知识同前缀 → 运维对账/备份会把垃圾算进去、
   将来按前缀授权会连缓存目录一起暴露、也没法按会话整批清）。现在内部前缀映射到 `artifacts/`，
   `_keys()` 合并两个前缀的 LIST（多一次 LIST，换 `okf/` 语义干净）。存量对象没迁移：
   上线前 `okf/large_tool_results/` 之类若有内容，需手工搬到 `artifacts/`。
10. **条件写的边界：同集群多网关成立；跨集群、以及不实现条件写的存储都不成立。** 原子性要求
    「检查 ETag」和「写入」被同一个东西串行化，所以判断只看这一点：
    - **同一 SeaweedFS 集群内的多个 S3 网关 / 多个 filer：成立**。对象按 key 路由到 hash ring
      上的 owner filer，由它的 `ObjectTransaction` 在同一把锁内判定+写入（PR #9629 route-by-key、
      #8801 修 multipart 期间 TOCTOU），官方措辞是 cluster-wide atomic，与客户端连哪个网关无关。
    - **两个独立集群 + `filer.sync`：不成立**。两边各有各的 hash ring，没有共享的串行化点：
      同一个 ETag 在两个集群各自 `If-Match` 写会**都返回 200**，异步复制后一方编辑静默消失；
      同一个复制窗口里 `ls`/`get` 也只能看到最终一致的结果。
    - **不实现 PUT 条件头的 S3 实现：不成立，而且失败是静默的**。Ceph RGW 的 objectops 里 PUT 只认
      `content-md5` / `content-type` / `x-amz-meta-*` / `x-amz-acl`（条件头只写在 GET/HEAD 下），
      tracker 里 2014 年挂的 conditional PUT 至今没落地；MinIO 的客户端库确实发 `If-Match`，但服务端
      是否真的强制执行没有公开证据（minio-go 自注释为 “MinIO specific extension”），要拿真机验。
      **S3 协议没有「不支持该参数」的错误码**：头被忽略时响应仍是 200，`put_bytes` 里的
      `_is_precondition_failed` 根本走不到，`edit` 就此悄悄变成 last-write-wins。
      要支持这类存储，只能启动时做能力探测（用故意错的 `If-Match` 写探针对象，看是否 412），
      探不过就 fail-closed——当前选择是「先不开写」，所以探测也没做。
11. **`UploadsBackend` 的同步协议方法不实现**（继承 `BackendProtocol` 默认 → `NotImplementedError`）。
    框架实际只用异步版本（`aread/als/aglob/agrep/adownload_files`）；任何同步调用路径都会抛错。
12. **e2e 是一份手动脚本，没接 CI**：`scripts/e2e_s3fs.py` 对着真 SeaweedFS 跑（2026-02-12
    全绿 13 项）：纯对象存储下的空间 spec、跨空间拒绝、`read` 对表给指路提示、`download_files`
    真字节、`_fetch` → 缓存 → sqlite 索引 → `xlsx_info` 读到列名且索引里有 2 行数据、用完清理。
    仍未覆盖：真 agent 跑一轮对话（要 LLM key 与起服务）、SeaweedFS 集群/多副本形态。

## 怎么验证（本地）

```bash
# Git Bash 必须加 MSYS_NO_PATHCONV=1，否则 -dir=/data 会被改写成 Windows 路径
MSYS_NO_PATHCONV=1 docker run -d --name swtest -p 8333:8333 chrislusf/seaweedfs:latest server -s3 -dir=/data
.venv/Scripts/python.exe -m pytest tests/unit -q
```

`config.yaml` 里配 `s3.endpoint: http://127.0.0.1:8333`、`okf.backend: s3fs` 后，
`pytest tests/unit/test_frontend_space_files.py tests/unit/test_session_files.py` 覆盖模式分支。

## 后果

- 镜像已退役（2026-02-12）：`pull`/`push`、`scripts/okf_{push,pull}_s3.py`、
  `tests/unit/test_okf_mirror.py`、`okf.mirror_dir`/`okf.mirror_ttl_seconds` 全部删除，
  ADR-0016 移入 `archive/`。往对象存储传空间内容现在用标准 S3 客户端（`aws s3 sync`）。
- 「只基于本地 filesystem + store」的形态另开长期分支（`feat/unified-files-local`），
  main 只留对象存储形态。
