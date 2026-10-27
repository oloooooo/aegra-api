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
| 谁读它 | `SessionFilesMiddleware`（清单）、Store 的 fs 工具（ls/read/grep/glob） | `xlsx_info / query / search / profile`（`backend.download_files` 取字节 → 落缓存 → SQLite 索引） |
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
                                ├─ UploadsBackend：查台账 → backend.download_files 取字节
                                ├─ xlsx 工具：落 .cache/xlsx（按内容 sha16）→ 建 SQLite 索引
                                └─ 不能 read_file / grep（zip 二进制）
```

**子 agent** 读同一条会话的文件靠派活时签发的 `session_scope`（HMAC 签名，
`graphs/access/session_scope.py`）：它读得到本会话的 `/uploads/*`，读不到别的主管的会话。

`h(identity)` = sha256 前 16 位（`shared/session/store.py::identity_digest`），只用于 md 的
Store 命名空间；xlsx 的 key 不含身份，隔离靠台账行 + 每次读都按当前会话过滤。

## 5. 删除与垃圾：三个责任人

| 什么 | 谁负责 |
|---|---|
| 删会话 | `custom/middleware/threads_delete.py`：先读出台账里本会话的 key → 删 `session_object` 行 → 逐 key 删盘 → 清 Store。任何一步失败只记日志，不把删除变成 500 |
| 孤儿 md | Store 自带 TTL（aegra 启动时 `start_ttl_sweeper()`），**读一次续期**，所以"在用的会话永不失效" |
| 上传残留 | `.part-*` 半截文件与孤儿 key：没有自动清理，手工对账（见 `docs/large-files.md`「对账」） |

## 6. 配置

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

## 7. 常见问题

| 现象 | 原因 |
|---|---|
| `404 Thread '…' not found` | 不是这个用户的会话（含：thread_id 拼错） |
| `415` | md 接口只收 `.md`，xlsx 接口只收 `.xlsx` |
| `413` | 单文件超 10MB、会话内文件数满、或每用户二进制总量满 |
| `422 文件名前缀 'x' 不是你的空间` | xlsx 文件名必须以自己的 space 名开头 |
| `422 不是合法 UTF-8 文本` | md 必须是 UTF-8 文本（GBK 文件先转码） |
| `500` | 盘写不进去（写满/权限）：`put_stream` 抛 `OSError`，日志里有 `upload_table_failed` |
| 传了但这一轮看不到 | 清单在**下一轮** run 起始才读；先发一条消息再问 |

## 8. 有意的非目标

秒传预检、前端直传、断点续传、按内容去重、md 落盘——都不做，理由见
[ADR-0018](./docs/adr/0018-local-only-file-reads.md)（md 留 Store 是为了白拿 fs 工具与 TTL；
xlsx 落盘是因为 read/grep 读不了 zip，而工具要的是字节而不是文本）。
