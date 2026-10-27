# 文件上传：基本逻辑

用户往一个会话里传文件，走两个接口、落两种载体。这份文档讲**为什么这么设计**和**一次上传到底发生了什么**；
实现细节见 [`docs/large-files.md`](./docs/large-files.md)，取字节的唯一入口见
[ADR-0018](./docs/adr/0018-local-only-file-reads.md)，代码入口是
[`custom/api/uploads.py`](./custom/api/uploads.py)（模块开头有同样的分区说明）。

## 0. 一张表看全

| | md（笔记/资料） | xlsx（大表） |
|---|---|---|
| 接口 | `POST /custom/uploads` | `POST /custom/uploads/tables` |
| 载体 | LangGraph Store（PG 里的 JSON 文本） | 字节落本地盘（`uploads.dir`）+ 归属行进 PG |
| agent 眼里 | `/uploads/<name>` | `/uploads/<name>` |
| 谁读它 | `SessionFilesMiddleware`（清单）+ `/uploads/` 路由的 fs 工具（ls/read/grep/glob，叠在 `StoreBackend` 上） | `xlsx_info / query / search / profile`（`backend.download_files` 取字节 → 落缓存 → SQLite 索引） |
| 单文件上限 | `session_files.max_upload_mb` = 10 MB | `xlsx.max_blob_mb` = 10 MB |
| 每会话个数 | `session_files.max_files_per_session` = 50 | `xlsx.max_xlsx_files_per_session` = 20 |
| 每用户总量 | — | `xlsx.max_blob_mb_per_user` = 2048 MB |
| 同名再传 | 覆盖（PUT 语义），不占新名额 | 覆盖，不占新名额（旧对象在提交成功后删） |
| 落点 | 命名空间 `(uploads, h(identity), thread)`，key `/<name>` | key `<uuid>/<name>`（盘上 `<uploads.dir>/<uuid>/<name>`），归属只在 PG 台账 |
| 孤儿保留 | 72h（`session_files.upload_ttl_minutes`，**读一次续期**） | 无 TTL：靠「删会话」+ 对账（孤儿 key 与 `.part-*` 不会被自动清） |

## 1. 为什么上传必须挂在会话上

会话文件的一切语义都以 **thread_id** 为键：每轮 run 起始注入的文件清单、删会话即删、
孤儿 TTL。所以接口强制要求 `thread_id`，而且必须是**你自己的**会话——不是你的会话一律
回 `404`（跟 aegra 的 ownership 过滤同一口径，不告诉你"存在但无权"）。

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
5. 名额        已有 50 个且不是同名覆盖 → 413
6. 写入        store.aput(ns, "/notes.md", {content,…}, ttl=72h)
```

返回：

```json
{"name":"notes.md","path":"/uploads/notes.md","bytes":92,
 "uploaded_at":"2026-10-26T13:37:06Z","ttl_minutes":4320}
```

**同名 = 覆盖**：`ls /uploads` 里永远只有一份，不做版本链。**上传与对话不互斥但也不互补**——
清单是"下一轮 run 起始时读到的"，所以刚传的文件本轮 run 看不到。

## 3. 一次 xlsx 上传发生了什么

文件名必须是 `<空间名>_<表名>.xlsx`。空间名前缀校验的是**你有没有那个 space**，它只决定
主管把活派给哪个子 agent，**不决定存哪**：每次上传都新分配一个 `<uuid>/<name>` key。

```
1. file.size 先挡一次            不读身体就能 413
2. 文件名 + 空间前缀             不合规 → 422 / 415
3. disk.put_stream(file, new_key)  边读边算 sha256，写 `<name>.part-<rand>` → os.replace（原子）
4. PG 事务                      session_files.upsert()：锁用户行算配额 → 写行 → commit
5. 提交成功后才删被替换掉的旧 key
```

返回 `201`：

```json
{"name":"cbam-reporting_cn_goods.xlsx","path":"/uploads/cbam-reporting_cn_goods.xlsx",
 "space":"cbam-reporting","bytes":7332,"sha256":"4359cdfc…","indexed":false}
```

`indexed: false` 恒定——索引是**缓存**，第一次调 xlsx 工具时才建。

**写序不能换**（写字节 → PG 事务 → 删旧 key）：任何一步崩都只会留下孤儿文件（或 `.part-*`），
不会出现"行说 A、字节是 B"。`os.replace` 保证读到的永远是完整文件。

失败时只删**本次新写的 key**。key 每次都是新的，所以物理上不可能删掉旧行在用的那份字节
（这也是不按内容 sha 去重的原因之一）。清理动作自身吞异常，否则会用一个清理错盖掉真正的错。

## 4. 传完之后，agent 怎么读到

```
agent 视角                       真实落点
/uploads/notes.md          →   Store (uploads, h(user), thread) + "/notes.md"
                                ├─ SessionFilesMiddleware 每轮 run 起始注入清单（不可信内容标注）
                                └─ StoreBackend：ls / read_file / grep / glob 直接用

/uploads/cbam-reporting_x.xlsx →  盘上 <uploads.dir>/<uuid>/x.xlsx（归属在 PG 台账）
                                ├─ UploadsBackend：查台账（异步）→ backend.download_files 取字节
                                ├─ xlsx 工具：落 .cache/xlsx（按内容 sha16）→ 建 SQLite 索引
                                └─ ls 列得出（合并台账）、read_file 给一句「用 xlsx 工具」的提示
```

**子 agent** 读同一条会话的文件靠派活时签发的 `session_scope`（HMAC 签名，
`graphs/access/session_scope.py`）：它读得到本会话的 `/uploads/*`，读不到别的主管的会话。
它**没有清单注入**（`SessionFilesMiddleware` 只装在主管图），靠 `ls /uploads/` 自己发现文件。

`h(identity)` = sha256 前 16 位（`shared/session/store.py::identity_digest`），只用于 md 的
Store 命名空间；xlsx 的 key 不含身份，隔离靠台账行 + 每次读都按当前会话过滤。

### 可见性边界

| 谁 | 看到清单 | 看到内容 | 靠什么 |
|---|---|---|---|
| 主管 agent | 是（md 与 xlsx 合并，最多 20 条） | 是 | 清单由 `SessionFilesMiddleware` 每轮 run 起始算一次（Store 列举 + 台账行合并） |
| 子 agent（被派活） | **否** | 是（仅本会话） | 靠 `ls /uploads/` 自己发现（`UploadsBackend.als` 会把台账里的表合进来），或主管在派活文本里点名 |
| 同用户的另一个会话 | 否 | 否 | md 按 Store 命名空间、xlsx 按台账行，两者都以 `(identity, thread_id)` 为键 |
| 另一个用户 | 否 | 否 | 上传接口强制 thread ownership（不是你的 → 404）；空间内容另有 `SpaceGuard` |

**清单的 20 条是 md 与 xlsx 合算的**（`merged = sorted(md + xlsx, key=文件名)[:20]`）：会话里 md 超过
20 个时，xlsx 按文件名排序会被挤到 20 条之外——清单里连一条表都不出现，只在末尾给一句
「只列了前 20 个；用 `ls /uploads/` 看全部」。所以文件多的会话里，「知道有哪些表」完全依赖 `ls`
（`UploadsBackend.als` 会把台账里的表合进来）。这不是可遇见的罕见情况：50 个 md 的会话很平常。

两件事容易搞混：

- **清单注入 ≠ 后端能力**。清单是「告诉 agent 有什么」，后端是「按路径取字节」。主管两样都有，
  子 agent 只有后者。子 agent 靠 `ls` 发现文件不是巧合：`als` 专门合并了台账，否则它只看得见 Store 里的 md。
- **看不见 ≠ 不存在**。`ls /uploads/` 只列台账行与 Store 项；盘上可能有孤儿文件（删会话失败、上传中途
  失败留下的 `.part-*` 与旧 key），对 agent 一律不可见，得手工对账。

**写入恒被拒**：`/uploads/` 路由对 `write_file` / `edit_file` / `delete` 一律回一句
「会话上传目录只读：请走上传接口」。新增与替换只有 HTTP 上传一条路。

## 5. agent 怎么读 xlsx

`read_file('/uploads/x.xlsx')` 只回一句「这是二进制表格，用 xlsx 工具」。
真正的路是四个工具：`xlsx_info` / `xlsx_query` / `xlsx_search` / `xlsx_profile`（`graphs/tools/xlsx/`）。

```
工具调用
  └─ _fetch(path, runtime)                每次调用都重新取字节：文件换了也不会答旧表
       ├─ backend.adownload_files([path])   /uploads/ 路由 → 查台账拿 key → 读盘上字节
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
核心路由）。顺序固定：

```
1. 原生 delete_thread   鉴权与 404 语义照旧（不是你的会话 → 404，之后什么都不碰）
2. 读台账               session_object 里这个 (identity, thread_id) 的所有 key
3. 删行                 先删行：行留着会让配额虚高
4. 删盘                 逐 key 删 <uploads.dir> 下的文件，顺手删空目录
5. 清 Store 命名空间    md 实体在 Store 里，清完才算真删
```

第 4、5 步各自兜底——任何一步失败只记日志，不把删除变成 500（线程行已经删了，报错只会让
客户端重试到 404）。所以删除幂等，残留靠下一轮对账/过期：

| 残留 | 谁清 |
|---|---|
| `.part-*` 半截文件 | 没有自动清理，手工对账（`docs/large-files.md`「对账」） |
| 孤儿 key（文件在、行没了） | 同上，按台账外扫 `<uploads.dir>` 对比 |
| Store 里的 md | Store 自带 TTL（`session_files.upload_ttl_minutes`），**读一次续期** |
| 本机 xlsx 索引缓存 | `xlsx.cache_ttl_days` 到期后进程启动自清；不删也只是一份可再生缓存 |

## 7. 配置

```yaml
# config.yaml
session_files:            # md
  max_upload_mb: 10
  max_files_per_session: 50
  upload_ttl_minutes: 4320
xlsx:                     # xlsx
  max_blob_mb: 10
  max_xlsx_files_per_session: 20
  max_md_files_per_session: 10   # ⚠ 当前代码没读这个键（md 的个数上限走上一段），改之前先对齐
  max_blob_mb_per_user: 2048
  cache_dir: .cache/xlsx
uploads:                  # xlsx 字节落盘的位置（相对仓库根）
  dir: .data/uploads
```

多副本：字节在**本机盘**上，副本之间不共享。要共享就挂同一卷（NFS/EFS）；
不想共享就只跑单副本（默认形态）。对象存储版见 `feat/unified-files-s3` 分支。

## 8. 常见问题

| 现象 | 原因 |
|---|---|
| `404 Thread '…' not found` | 不是这个用户的会话（含：thread_id 拼错） |
| `415` | md 接口只收 `.md`，xlsx 接口只收 `.xlsx` |
| `413` | 单文件超 10MB、会话内文件数满、或每用户二进制总量满 |
| `422 文件名前缀 'x' 不是你的空间` | xlsx 文件名必须以自己的 space 名开头 |
| `422 不是合法 UTF-8 文本` | md 必须是 UTF-8 文本（GBK 文件先转码） |
| `500` | 盘写不进去（写满/权限）：`put_stream` 抛 `OSError`，日志里有 `upload_table_failed` |
| 传了但这一轮看不到 | 清单在**下一轮** run 起始才读；先发一条消息再问 |

## 9. 有意的非目标

秒传预检、前端直传、断点续传、按内容去重、md 落盘——都不做，理由见
[ADR-0018](./docs/adr/0018-local-only-file-reads.md)（md 留 Store 是为了白拿 fs 工具与 TTL；
xlsx 落盘是因为 read/grep 读不了 zip，而工具要的是字节而不是文本）。

**agent 只读**：唯一写入方是上传接口。`/uploads/` 的写家族（`write`/`edit`/`delete`/
`upload_files`，同步与异步两套）一律返回「会话上传目录只读」，空间路径更早就只能读。
“让 agent 改表”需要先解决两件事：openpyxl 保存会抹掉全工作簿的公式缓存值（索引里公式列
全变空）、插删行会让模型手里的行号契约错位。四条实测与取舍见 [ADR-0018](./docs/adr/0018-local-only-file-reads.md)
缺陷 13/14 与 `docs/large-files.md` §3.5。
