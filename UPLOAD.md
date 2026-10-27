# 文件上传：基本逻辑

用户往一个会话里传文件，走两个接口、**一条存储链路**。这份文档讲**为什么这么设计**和**一次上传到底
发生了什么**；实现细节见 [`docs/large-files.md`](./docs/large-files.md)，对象存储运维见
[`docs/S3.md`](./docs/S3.md)，代码入口是 [`custom/api/uploads.py`](./custom/api/uploads.py)
（模块开头有同样的分区说明）。

## 0. 一张表看全

| | md（笔记/资料） | xlsx（大表） |
|---|---|---|
| 接口 | `POST /custom/uploads` | `POST /custom/uploads/tables` |
| 文件名 | 任意 `*.md` | 必须 `<空间名>_<表名>.xlsx` |
| 载体 | 字节进对象存储 + 元数据行进 PG | 同左 |
| agent 眼里 | `/uploads/<name>` | `/uploads/<name>` |
| 谁读它 | `/uploads/` 路由（ls / read_file / grep / glob） | `xlsx_info / query / search / profile`（下字节 → SQLite 索引） |
| 单文件上限 | `session_files.max_upload_mb` = 10 MB | `xlsx.max_blob_mb` = 10 MB |
| 每会话个数 | `session_files.max_files_per_session` = 50 | `xlsx.max_xlsx_files_per_session` = 20 |
| 每用户总量 | `xlsx.max_blob_mb_per_user` = 2048 MB（**md + xlsx 合算**） | 同左 |
| 同名再传 | 覆盖（PUT 语义），不占新名额 | 覆盖，不占新名额（旧对象在提交成功后删） |
| 落点 | 对象存储 `uploads/<uuid>/<名>`，归属记 `session_object` 行 | 同左 |
| 孤儿保留 | 无 TTL：靠「删会话」+ `scripts/reap_orphans.py` | 同左 |

两种载体只差**文件名规则、大小闸、`space` 字段**（md 为 NULL）。字节与行都是同一套代码写、
同一条路径读——这是 ADR-0017 换掉「md 存 LangGraph Store」的直接原因：两个后端就有两套可见性，
`/uploads/` 换一次路由，Store 里的 md 立刻谁都看不见（实测踩过，见 ADR-0017 §背景）。

## 1. 为什么上传必须挂在会话上

会话文件的一切语义都以 **thread_id** 为键：每轮 run 起始注入的文件清单、删会话即删、孤儿对账。
所以接口强制要求 `thread_id`，而且必须是**你自己的**会话——不是你的会话一律回 `404`（跟 aegra 的
ownership 过滤同一口径，不告诉你"存在但无权"）。

没有"先传后挂"：无主的文件既不会被任何 run 看到，也没有任何机制会去清它。

## 2. 一次 md 上传发生了什么

```bash
TOKEN=$(curl -s -X POST localhost:2026/custom/login \
  -H 'Content-Type: application/json' \
  -d '{"identity":"root","password":"1234"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

TID=$(curl -s -X POST localhost:2026/threads -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{}' | python -c 'import sys,json;print(json.load(sys.stdin)["thread_id"])')

curl -s -X POST localhost:2026/custom/uploads \
  -H "Authorization: Bearer $TOKEN" \
  -F "thread_id=$TID" -F "file=@notes.md"
```

```
1. 归属        thread 是你的？不是 → 404
2. 文件名      basename + 消毒（控制字符/超长/后缀），不合规 → 422 / 415
3. 读身体      分块读，超过 10MB → 413（不信 Content-Length，它会撒谎或缺失）
4. 编码        不是合法 UTF-8 → 422（排在大小闸之后，不为报编码错先把 10MB 读满）
5. 名额        同类已有 50 个且不是同名覆盖 → 413
6. 写字节      put_bytes(uploads/<uuid>/notes.md, data)
7. 写行        锁用户行算配额 → upsert session_object（space = NULL）→ commit → 删被替换的旧对象
```

返回：

```json
{"name":"notes.md","path":"/uploads/notes.md","bytes":92}
```

**同名 = 覆盖**：`ls /uploads` 里永远只有一份，不做版本链。**上传与对话不互斥但也不互补**——
清单是"下一轮 run 起始时读到的"，所以刚传的文件本轮 run 看不到。

## 3. 一次 xlsx 上传发生了什么

文件名必须是 `<空间名>_<表名>.xlsx`。空间名前缀校验的是**你有没有那个 space**，它只决定
主管把活派给哪个子 agent，**不决定存哪**：对象 key 与 md 同一条规则 `uploads/<uuid>/<名>`，
`space` 只是台账里的一列。

```
1. file.size 先挡一次            不读身体就能 413
2. 文件名 + 空间前缀             不合规 → 422 / 415
3. put_stream(file, tmp/…)      手写分片：边读边算 sha256，超限自己 abort
4. copy(tmp, uploads/<uuid>/<名>)   服务端复制，不搬字节
5. PG 事务                      session_files.upsert()：锁用户行算配额 → 写行 → commit
6. 删 tmp；提交成功后再删被替换掉的旧对象
```

返回 `201`：

```json
{"name":"cbam-reporting_cn_goods.xlsx","path":"/uploads/cbam-reporting_cn_goods.xlsx",
 "space":"cbam-reporting","bytes":7332,"sha256":"4359cdfc…","indexed":false}
```

`indexed: false` 恒定——索引是**缓存**，第一次调 xlsx 工具时才建。

**写序不能换**（`tmp` → 正式 key → PG 事务 → 删 `tmp`）：任何一步崩都只会留下垃圾对象，
不会出现"行说 A、字节是 B"。每次上传都分配新 key（uuid），所以同名同内容的并发上传不会
互相删字节；台账里后提交的行赢。

失败时只删「本次新写、且不是旧行正在用的」对象——同名同内容重传恰好卡在配额时，删了新的
就等于删掉旧行唯一的那份字节。清理动作自身吞异常，否则「存储不可用 → 503」会变成「未知错误 → 500」。

## 4. 传完之后，agent 怎么读到

```
agent 视角                       真实落点
/uploads/notes.md          →   对象存储 uploads/<uuid>/notes.md（key 查 session_object 行）
                                ├─ SessionFilesMiddleware 每轮 run 起始注入清单（不可信内容标注）
                                └─ /uploads/ 路由：ls / read_file / grep / glob / download_files

/uploads/cbam-reporting_x.xlsx → 同一条路由，同一份台账
                                ├─ read_file 只回一句「用 xlsx 工具」的提示（zip 二进制）
                                └─ xlsx 工具：查台账拿 key → 下到 .cache/xlsx → 建 SQLite 索引
```

**主管**每轮 run 起始拿到清单（含 md 与 xlsx），**子 agent 没有清单注入**——它靠 `ls /uploads/`
自己发现，或者由主管在派活文本里点名。

**子 agent** 读同一条会话的文件靠派活时签发的 `session_scope`（HMAC 签名，
`graphs/access/session_scope.py`）：它读得到本会话的 `/uploads/*`，读不到别的主管的会话。
两条载体都走 `graphs/access/factory.py::session_backend()` —— 读文件只有一个入口（ADR-0017）。

### 可见性边界

| 谁 | 看到清单 | 看到内容 | 靠什么 |
|---|---|---|---|
| 主管 agent | 是（md + xlsx，最多 20 条：表保底 10 个名额，剩下给 md） | 是 | 清单由 `SessionFilesMiddleware` 每轮 run 起始查一次台账；内容走 `/uploads/` 路由 |
| 子 agent（被派活） | **否** | 是（仅本会话） | 清单要自己 `ls /uploads/` 或由主管在派活文本里点名；内容靠派活签发的 `session_scope` |
| 同用户的另一个会话 | 否 | 否 | 台账按 `(identity, thread_id)` 查，别的会话连 key 都拿不到 |
| 另一个用户 | 否 | 否 | 上传接口强制 thread ownership（不是你的 → 404）；空间内容另有 `SpaceGuard` |

**清单的 20 个名额按载体各留一份**（`_cap()`）：表（xlsx）保底 10 个，md 用剩下的；表少于 10 张时
空出的名额全给 md，表更多时也吃 md 没用完的那部分。所以 md 再多也不会把表挤出清单——否则
文件名排序一压，agent 连「这次会话有几张表」都不知道。被截断时末尾提示
「只列了前 20 个；用 `ls /uploads/` 看全部」（`ls` 也列台账里的表）。

两件事容易搞混：

- **清单注入 ≠ 后端能力**。清单是「告诉 agent 有什么」，后端是「按路径取字节」。主管两样都有，
  子 agent 只有后者。所以子 agent 不知道有哪些文件，除非它 `ls`——这不是 bug，是权限收窄。
- **看不见 ≠ 不存在**。`ls /uploads/` 只列台账里的行；对象存储里可能有孤儿对象（删会话失败、
  上传中途失败留下的），对 agent 一律不可见，靠 `scripts/reap_orphans.py` 对账。

**写入恒被拒**：`/uploads/` 路由对 `write_file` / `edit_file` / `delete` 一律回一句
「会话上传目录只读：请走上传接口」。新增与替换只有 HTTP 上传一条路（ADR-0017 决定 8）。

## 5. agent 怎么读 xlsx

`read_file('/uploads/x.xlsx')` 只回一句「这是二进制表格，用 xlsx 工具」——zip 里的字节对模型没用。
真正的路是四个工具：`xlsx_info` / `xlsx_query` / `xlsx_search` / `xlsx_profile`（`graphs/tools/xlsx/`）。

```
工具调用
  └─ _fetch(path, runtime)                每次调用都重新取字节：文件换了也不会答旧表
       ├─ backend.adownload_files([path])   /uploads/ 路由 → 查台账拿 key → 取原始字节
       ├─ store.cache_bytes(path, data)     落 {xlsx.cache_dir}/{sha16}{后缀}（tmp + 原子替换）
       └─ index.ensure(xlsx, cache_dir, sha16, header)
            ├─ 先探单元格数（openpyxl read_only，不把表读进内存）> xlsx.max_cells → 直接报错
            ├─ openpyxl 逐 sheet → pandas → sqlite：meta / col_meta / s0..sN
            └─ 落 {cache_dir}/{sha16}-h{header}-v{BUILD}.sqlite（建临时文件再原子换名）
  └─ 同步实现丢线程池（sqlite 阻塞）；异常转成错误文本返回，不炸图
```

- **索引键 = 内容 sha16 + header 行 + BUILD**。内容变 → sha 变 → 新索引，永远不会拿旧表回答；
  `header` 指「表头在第几行」，不同 header 是不同索引，互不覆盖。
- **建索引很贵**（实测 129 MB / 41.7 s），所以 `ensure` 先探单元格数把「能上传但处理不了」的表
  提前拒掉。建好的索引留在盘上，`xlsx.cache_ttl_days` 到期后每进程启动扫一次删掉，用到再建。
- **查询侧只读三件套**：`mode=ro&immutable=1` + `set_authorizer` 白名单（只放 SELECT 与白名单函数）
  + 每 20k 条指令查一次 `xlsx.query_budget_ms` 预算，跑飞的长查询会被掐断。
- **四个工具的分工**：`info` 看结构（sheet / 列 / 类型 / 行数），`query` 按结构化条件取行
  （`columns` / `where` / `limit` / `offset`），`search` 做文本包含检索，`profile` 对每列出统计。
  四个都只读，都走同一份索引。
- **空间里的表**（`has_tables(space)` 为真）走 `CodeInterpreterMiddleware` 的 ptc 模式；只有本会话
  上传的表时装工具本身（不额外起 JS 解释器）。两条路的取字节方式完全相同。
- 上传响应里的 `indexed: false` 恒定：上传只写字节与台账，索引是第一次用工具时才建的缓存。

## 6. 删会话：谁清什么

`DELETE /threads/{id}` 被 `custom/middleware/threads_delete.py` 截下来（注册一条同名路由**遮挡**
核心路由；启动时 `assert_shadows_core_delete` 核对首个匹配确实是它，aegra 升级改了装载顺序就
启动失败，而不是静默退化成「删会话留垃圾」）。顺序固定：

```
1. 原生 delete_thread   鉴权与 404 语义照旧（不是你的会话 → 404，之后什么都不碰）
2. 查台账              session_object 里这个 (identity, thread_id) 的所有 object_key
3. 删行                先删行：行留着会让配额虚高
4. 删对象              再删对象：删不掉只是垃圾，有对账脚本兜底
5. 清 Store 命名空间   只清老部署（ADR-0013 时代）留在 Store 里的 md
```

第 3、4、5 步各自兜底——任何一步失败只记日志，不把删除变成 500（线程行已经删了，报错只会让
客户端重试到 404）。所以删除是幂等的，残留交给下一轮对账：

| 残留 | 谁清 |
|---|---|
| 孤儿对象（对象在、行没了） | `PYTHONPATH=. python scripts/reap_orphans.py`：缺省只报告，`--delete-orphans` 才真删 |
| 悬空行（行在、对象没了） | 同上，`--delete-rows` |
| `tmp/` 中间对象与未完成分片 | `PYTHONPATH=. python scripts/reap_multipart.py`（`docs/S3.md` §8） |
| 本机 xlsx 索引缓存 | `xlsx.cache_ttl_days` 到期后进程启动自清；不删也只是一份可再生缓存 |
| 老 Store 里的 md | 只在删会话时清；不删会话就一直留着（没有 TTL 了） |

## 7. 配置

```yaml
# config.yaml
session_files:            # md
  max_upload_mb: 10
  max_files_per_session: 50
xlsx:                     # xlsx（字节配额对两种载体合算）
  max_blob_mb: 10
  max_xlsx_files_per_session: 20
  max_blob_mb_per_user: 2048
  cache_dir: .cache/xlsx
s3:
  endpoint: ""            # 空 = 不启用对象存储：**两种上传都 503**
  bucket: aegra-blobs
```

两套部署形态：`s3.endpoint` 为空 → 上传接口整体不可用（503），知识空间走本地目录；填上 →
两种文件都能用，所有副本读同一份对象（生产）。

## 8. 常见问题

| 现象 | 原因 |
|---|---|
| `404 Thread '…' not found` | 不是这个用户的会话（含：thread_id 拼错） |
| `415` | md 接口只收 `.md`，xlsx 接口只收 `.xlsx` |
| `413` | 单文件超 10MB、会话内同类文件数满、或每用户字节总量满 |
| `422 文件名前缀 'x' 不是你的空间` | xlsx 文件名必须以自己的 space 名开头 |
| `422 不是合法 UTF-8 文本` | md 必须是 UTF-8 文本（GBK 文件先转码） |
| `503 存储未就绪` | 对象存储没配（`s3.endpoint` 为空）或连不上 |
| 传了但这一轮看不到 | 清单在**下一轮** run 起始才读；先发一条消息再问 |
| `read_file` 读 xlsx 只有一句提示 | 那是二进制表格，用 `xlsx_info` / `xlsx_query` 等工具 |

## 9. 有意的非目标

秒传预检、前端直传、断点续传、按内容去重、**agent 写回 xlsx**——都不做，理由见
[ADR-0017](./docs/adr/0017-files-read-through-one-backend.md)：写表要重算公式缓存、重建索引，
且会与本分支的只读权限模型冲突；需要改表时由人改完重传（同名覆盖）。
