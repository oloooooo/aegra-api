# ADR-0018 只基于本地 filesystem + Store 的文件读路径

- 状态：已接受（`feat/unified-files-local` 分支；与 ADR-0017 是同一件事的两种部署形态）
- 日期：2026-02-13
- 关联：[ADR-0005](./0005-space-isolation-in-backend.md)（空间判定在 backend）、
  [ADR-0013](./0013-session-files-live-in-store.md)（md 存 Store）、
  [ADR-0017](./0017-files-read-through-one-backend.md)（对象存储版）、
  [ADR-0015](./0015-large-tables-in-object-storage.md) / [ADR-0016](./0016-spaces-in-object-storage.md)（本分支归档）

## 背景

同一套图代码要能跑两种部署：一种只有本机磁盘与 PostgreSQL（单机、演示、离线），
一种要多副本共享对象存储。两条路线用**同一个读文件入口**，差别只在字节从哪来。
本分支（`feat/unified-files-local`）实现前者；对象存储版见 `feat/unified-files-s3` + ADR-0017。

## 决定

1. **读文件只有一个入口**：`graphs/access/factory.py::session_backend()` 返回
   `CompositeBackend`，默认 `SpaceBackend(root_dir=knowledge_root())`，`/uploads/` 路由到
   `UploadsBackend()`。图（`supervisor.py`、`space_agent.py`）与读文件的工具都从这里拿。
2. **空间内容就是仓库根的 `knowledge/`**：`knowledge_root()` 只有一个来源，
   不再有 `okf.backend` 开关、不再有本地镜像（ADR-0016 已归档）。
3. **会话上传的字节落本地盘**（`shared/session/disk.py`，根 `uploads.dir`，默认
   `.data/uploads`）。key 是 `<uuid4hex>/<文件名>`：**归属不写进 key**，一个 key 属于谁、
   哪个会话，只记 PG `session_object` 行。
4. **写入原子**：一律 `<name>.part-<rand>` → `os.replace`（同目录），任何时刻读到的都是完整文件；
   `put_stream` 边读边算 sha256 并强制上限（`TooLarge`）。
5. **清单真相 = 台账**：`custom/service/session_files.py::as_dicts()`；
   `graphs/middleware/session_files.py::_load()` 在盘模式下从台账取，md（Store）与
   xlsx（盘）在系统提示里是同一张清单。
6. **xlsx 工具不再自己找字节**：`graphs/tools/xlsx/__init__.py::_fetch` 走
   `runtime.backend.download_files([path])`，工具层的空间判定副本（旧 `_check_scope`）删除，
   判定单点回到 backend。缓存按**内容 sha16**名下，索引键同 sha16。
7. **删掉对象存储**：`shared/session/blobs.py`、`scripts/okf_{push,pull}_s3.py`、
   `scripts/reap_multipart.py`、`s3.*` / `okf.backend` / `okf.mirror_*` 配置项、
   `boto3` 依赖、`docs/S3.md`。
8. **`UploadsBackend` 叠在 `StoreBackend` 上，只覆写取字节与写家族**：md 的
   `ls/read/grep/glob` 语义继续用 Store 那份（文本、分页、TTL 续期），二进制只有一件不同——
   `download_files` 要返回真字节，而字节在盘上、路径只在台账里，所以覆写 `adownload_files`：
   先查台账（async PG）命中就读盘，未命中交回 Store（md）。
9. **agent 不能写会话文件，也不能改表**：`write`/`edit`/`delete`/`upload_files`（含异步版，
   `StoreBackend` 两套都有）全部返回「会话上传目录只读」；唯一的写入方是上传接口。
   xlsx 四件套保持只读——写表需要先解决「openpyxl 会抹掉公式缓存值」（后文缺陷 14）。

## 取舍

| 取舍 | 选了什么 | 放弃了什么 |
| --- | --- | --- |
| 归属 | key 用 uuid，归属只记 PG 台账 | 用 key 前缀（`<owner>/<thread>/…`）能按目录批量删/对账；现在只能先读行再逐个删 |
| 去重 | 每次上传都新写一份字节，不再按 sha 去重复用 | 相同文件重复占盘；换来的是「删一个会话不会删掉别人还在用的字节」 |
| 副本 | 单机足够，多副本需挂共享卷 | 对象存储那种天然的多副本共享 |
| 删除 | 先读台账 key → 删行 → 逐对象删；任一步失败只记日志 | 「行与字节严格一致」需要事务，这里选择「宁可留孤儿，不要留悬空行」 |
| 一致性 | 本地盘 + `os.replace`，单机强一致 | 无 CAS：两个副本同时写同一 key 是 last-write-wins（key 是 uuid，正常路径不会撞） |
| 写入能力 | agent 只读：唯一写入方是上传接口 | 「让 agent 改表」这个需求——它得先过「openpyxl 抹公式缓存」那关（缺陷 14），不是存储能解决的 |
| 依赖 | 删掉 boto3/botocore | 想切对象存储要换分支 |
| 空间判定 | 复用 `SpaceBackend`（ADR-0005），`..` 由 `FilesystemBackend` 直接拒（抛 `ValueError`） | 旧 `_check_scope` 返回的是错误字符串；现在上升为异常，调用方必须能接住 |

## 缺陷与已知风险

1. **多副本不共享盘**：各副本只看得见自己收的字节。要共享得挂同一卷（NFS/EFS），
   `os.replace` 在 NFS 上仍然原子但慢。
2. **`.`data/uploads` 不在 git、不在备份范围**：需自行纳入备份；DB 与盘不同步时台账行会指向
   不存在的字节（`disk.read` 抛 `FileNotFoundError` → 工具报错，不会静默出错数据）。
3. **孤儿对象无 GC**：正常替换/删除路径会删旧字节，但崩溃、提交失败、手工改库都会留下孤儿 key
   与 `.part-*`。对账方法见 `docs/large-files.md`。
4. **`UploadsBackend` 每次取字节都查一次 PG**：`_ledger_keys()` 无缓存，一次 run 里多次
   `download_files` 会重复查（xlsx 四件套每次调用都走这条）。
5. **同步路径拿不到 xlsx 字节**：`read`/`download_files` 的同步版继承 `StoreBackend`，
   只看得见 Store 里的 md——对 `/uploads/*.xlsx` 会返回 `file_not_found`。当前工具都是 async，
   但这是隐式契约。
6. **没有 TTL 清理**：`session_files.upload_ttl_minutes` 仍只作用于 Store 里的 md
   （`shared/session/store.py`），盘上字节不会过期。
7. **空间清单是实时扫盘**：`has_tables()` / `space_files()` 每次 run 递归扫 `knowledge/`，
   空间变大后是纯 I/O 开销（无缓存）。
8. **xlsx 每次工具调用都重新取字节**：换「不会读到过期数据」，代价是大表每个调用多一次读盘
   （见 `graphs/tools/xlsx/store.py` 的 `shortcut:` 注释）。
9. **磁盘写满**：`put_stream` 抛 `OSError` → 500，没有预留空间或降级策略。
10. **知识空间随发布走**：空间内容在仓库 `knowledge/` 里，改内容要重新发布；
    没有「运维直接往存储里放文件」的路径。
11. **删会话的字节清理是 best-effort**：盘删除失败被吞（只记日志），孤儿留下。
12. **未跑 e2e**：`tests/e2e` 需要真服务，本分支只跑单测（基线 28 failed 与改动无关）。
13. ~~**`/uploads/` 的写是幽灵写**~~（**已修**）：`UploadsBackend` 曾未覆写写家族，`write` /
    `edit` / `delete` 落在 LangGraph Store，而 xlsx 工具读的是盘上台账指向的字节。所以
    `write_file('/uploads/x.xlsx', …)` 返回成功、`ls` 能看到、`read_file` 读得到文本，
    但**盘上一个字节没变**，`xlsx_info` 读的仍是真表——同一路径两套内容。现在写家族（同步 +
    异步两套）一律拒绝，回归测试 `tests/unit/test_uploads_backend_readonly.py`。
14. **openpyxl 保存会抹掉全工作簿的公式缓存值（已实测）**：openpyxl 只写 `<f>` 不写 `<v>`，
    实测把带 `<v>3</v>` 的文件用一个无关单元格赋值后重新保存，`<v>` 变回空，
    `data_only`/pandas 读到 `None`/`NaN`。而索引由 `pd.read_excel` 建，所以任何“用
    openpyxl 改表”的实现都会让**所有公式列**在索引里变空（不只被改那一格）。本机无
    LibreOffice 也无 `formulas`，服务端重算这条路不存在。

## 验证

- `pytest tests/unit`：**2001 passed / 28 failed / 1 skipped**，28 项失败是 main 上原有的
  （observability、broker window、jsonb、lifespan）。
- 新增/改写：`tests/unit/test_backend_scope.py`（空间判定，13 条）、
  `tests/unit/test_threads_delete_blobs.py`（删会话清字节）、
  `tests/unit/test_upload_tables.py`（上传写序与失败清理）、`tests/unit/test_session_files.py`（台账清单 + 路由）。
- 冒烟（手工）：真 PG + 真服务，上传 xlsx → 主管 run 读到 → 工具改 → 再读；`git diff` 确认
  `knowledge/` 没被动过。

## 后果

- 读文件的语义只有一份：新增「写文件」来源时，只要它进得了 backend（或台账），
  图和工具都不用改。
- 想换回对象存储：切 `feat/unified-files-s3`（ADR-0017），本分支的 `factory.py`、`disk.py`
  与 `UploadsBackend` 是替换点。
