# 文件上传：基本逻辑

用户往一个会话里传文件，走两个接口、落两种载体。这份文档讲**为什么这么设计**和**一次上传到底发生了什么**；
实现细节见 [`docs/large-files.md`](./docs/large-files.md)，对象存储运维见 [`docs/S3.md`](./docs/S3.md)，
代码入口是 [`custom/api/uploads.py`](./custom/api/uploads.py)（模块开头有同样的分区说明）。

## 0. 一张表看全

| | md（笔记/资料） | xlsx（大表） |
|---|---|---|
| 接口 | `POST /custom/uploads` | `POST /custom/uploads/tables` |
| 载体 | LangGraph Store（PG 里的 JSON 文本） | 字节进对象存储（S3）+ 元数据行进 PG |
| agent 眼里 | `/uploads/<name>` | `/uploads/<name>` |
| 谁读它 | `SessionFilesMiddleware`（清单）、Store 的 fs 工具（ls/read/grep/glob） | `xlsx_info / query / search / profile`（先查 PG → 下字节 → SQLite 索引） |
| 单文件上限 | `session_files.max_upload_mb` = 10 MB | `xlsx.max_blob_mb` = 10 MB |
| 每会话个数 | `session_files.max_files_per_session` = 50 | `xlsx.max_xlsx_files_per_session` = 20 |
| 每用户总量 | — | `xlsx.max_blob_mb_per_user` = 2048 MB |
| 同名再传 | 覆盖（PUT 语义），不占新名额 | 覆盖，不占新名额（旧对象在提交成功后删） |
| 落点 | 命名空间 `(uploads, h(identity), thread)`，key `/<name>` | `sessions/{h(identity)}/{thread}/{sha16}/<name>` |
| 孤儿保留 | 72h（`session_files.upload_ttl_minutes`，**读一次续期**） | 无 TTL：靠「删会话」+ 上传残留清理 |

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
主管把活派给哪个子 agent，**不决定存哪**：存储前缀永远是 `sessions/{h}/{thread}/`。

```
1. file.size 先挡一次            不读身体就能 413
2. 文件名 + 空间前缀             不合规 → 422 / 415
3. put_stream(file, tmp/…)      手写分片：边读边算 sha256，超限自己 abort
4. copy(tmp, sessions/{h}/{thread}/{sha16}/<name>)    服务端复制，不搬字节
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
不会出现"行说 A、字节是 B"。同名同内容的并发上传写同一个 key，谁最后提交谁赢。

失败时只删「本次新写、且不是旧行正在用的」对象——同名同内容重传恰好卡在配额时，删了新的
就等于删掉旧行唯一的那份字节。清理动作自身吞异常，否则「存储不可用 → 503」会变成「未知错误 → 500」。

## 4. 传完之后，agent 怎么读到

```
agent 视角                       真实落点
/uploads/notes.md          →   Store (uploads, h(user), thread) + "/notes.md"
                                ├─ SessionFilesMiddleware 每轮 run 起始注入清单（不可信内容标注）
                                └─ StoreBackend：ls / read_file / grep / glob 直接用

/uploads/cbam-reporting_x.xlsx →  对象存储 sessions/{h}/{thread}/{sha16}/x.xlsx
                                ├─ xlsx 工具：查 PG 拿 object_key → 下到 .cache/xlsx → 建 SQLite 索引
                                └─ 不能 read_file / grep（zip 二进制）
```

**子 agent** 读同一条会话的文件靠派活时签发的 `session_scope`（HMAC 签名，
`graphs/access/session_scope.py`）：它读得到本会话的 `/uploads/*`，读不到别的主管的会话。

`h(identity)` = sha256 前 16 位，Store 命名空间与对象存储前缀**共用同一个函数**
（`shared/session/store.py::identity_digest`）——两处若不一致，同一份内容会落在两套前缀下。

## 5. 删除与垃圾：三个责任人

| 什么 | 谁负责 |
|---|---|
| 删会话 | `custom/middleware/threads_delete.py`：先删 `session_object` 行 → 删 S3 前缀 → 清 Store。任何一步失败只记日志，不把删除变成 500 |
| 孤儿 md | Store 自带 TTL（aegra 启动时 `start_ttl_sweeper()`），**读一次续期**，所以"在用的会话永不失效" |
| 上传残留 | `tmp/` 中间对象与未完成分片：`PYTHONPATH=. python scripts/reap_multipart.py`（见 `docs/S3.md` §8） |

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
s3:
  endpoint: ""            # 空 = 不启用对象存储：xlsx 上传 503，md 不受影响
  bucket: aegra-blobs
```

两套部署形态：`s3.endpoint` 为空 → 只有 md 可用（本地开发）；填上 → 两种都能用，
所有副本读同一份对象（生产）。

## 7. 常见问题

| 现象 | 原因 |
|---|---|
| `404 Thread '…' not found` | 不是这个用户的会话（含：thread_id 拼错） |
| `415` | md 接口只收 `.md`，xlsx 接口只收 `.xlsx` |
| `413` | 单文件超 10MB、会话内文件数满、或每用户二进制总量满 |
| `422 文件名前缀 'x' 不是你的空间` | xlsx 文件名必须以自己的 space 名开头 |
| `422 不是合法 UTF-8 文本` | md 必须是 UTF-8 文本（GBK 文件先转码） |
| `503 存储未就绪` | 对象存储没配（`s3.endpoint` 为空）或连不上 |
| 传了但这一轮看不到 | 清单在**下一轮** run 起始才读；先发一条消息再问 |

## 8. 有意的非目标

秒传预检、前端直传、断点续传、按内容去重、md 进对象存储——都不做，理由见
[ADR-0015](./docs/adr/0015-large-tables-in-object-storage.md)（md 留 Store 是为了白拿 fs 工具与 TTL；
xlsx 自己直连 S3 是因为 read/grep 读不了 zip）。
