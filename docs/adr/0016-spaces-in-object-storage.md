# 空间内容进对象存储，backend 由 env 二选一

> 状态：**决定已定，实现未开始**（2026-10-25）。
> 与 [ADR-0015](./0015-large-tables-in-object-storage.md) 配套：那边是会话文件，这边是空间知识。

## 背景

空间知识现在在 `graphs/okf/<space>/`（本地目录，总计 145 K），由 `SpaceBackend` 继承
deepagents 的 `FilesystemBackend` 读取，8 个 fs 工具全部经它。两个问题：

1. **多副本不一致**：本地盘挂载在多实例部署下每台看到的不是同一份内容。
2. **上传的 xlsx 无处安放**：ADR-0015 把字节放进了对象存储，空间这边还是盘，两套来源。

## 决定

### 1. 空间内容进 S3，前缀 `okf/<space>/...`

`graphs/okf/` 保留为**开发副本**与**上传源**，不再是生产真相。

### 2. `OKF_BACKEND` env 二选一：`local` | `s3`

- `local`（缺省，本地开发）：`SpaceBackend(FilesystemBackend)`，与今天完全一致；
- `s3`：只读的 `SpaceObjectBackend`，`BackendProtocol` 的 S3 实现。

两条实现**共用同一套路径判定**：`_check_read` / `_check_write` / 内部驱逐前缀
（`graphs/space_backend.py` 现在是单点，搬走后必须还是单点）。做法是把判定抽成 mixin，
两个类只差存储层。

### 3. 只读（Q6a 不变）

写全部 deny。`SpaceObjectBackend` 的 `write_file` / `edit_file` / `delete` 一律返回拒绝，
与现在的 `permissions=[FilesystemPermission(operations=["write"], paths=["/"], mode="deny")]` 一致。

### 4. 一键上传脚本 `scripts/okf_push_s3.py`

- 遍历 `graphs/okf/`，逐个 PUT 到 `okf/<相对路径>`；
- 幂等：先比本地 md5 与 S3 ETag（单次 PUT 的 ETag 就是 md5），相同则跳过；
- `--dry-run` 只打印；`--prune` 删掉 S3 上本地已不存在的对象；
- 退出码：0 成功、1 有失败（供 CI 用）。

### 5. 为什么自己写

deepagents 0.7.23 的 `backends/` 里没有 S3；PyPI 0.7.32 的依赖里也没有 s3/boto。
上游 PR #1913（remote backend）、#6325（BlobStorageMiddleware）均 closed 未合入。
社区包 `deepagents-backends` 有 S3 实现，但它按「逐次 GET」设计，且我们要复用
`SpaceBackend` 的路径判定（那才是访问控制的单点），所以自己实现一个薄类更省。

## Considered Options

| 方案 | 否决理由 |
|---|---|
| 保留 FilesystemBackend，生产靠同步盘 | 多副本不一致；多一个部署依赖 |
| 引入社区 `deepagents-backends` | 判定单点会裂成两处；依赖面大（`aioboto3`） |
| FUSE 挂载（`rclone mount` / `seaweedfs mount`） | Windows 无 FUSE（需 WinFsp），且把一个可靠的 HTTP 层换成不可靠的内核层 |
| 双实现改成一个实现（彻底删 local） | 本地开发要跑 SeaweedFS 才能看文档，开发体验与测试成本都变差 |

## 后果

**要接受的**：

- 多约 200 行 `BackendProtocol` 实现 + 一组单测；**`grep`/`glob` 在 S3 上没有索引**，
  只能 list + 逐个 GET，空间大了会慢（现在总计 145 K，暂时无妨）。
- 双实现意味着以后改路径判定要两边一起测。
- 部署多一个组件（SeaweedFS），但它在 ADR-0015 里已经要了，不重复计。
- `has_tables()`（决定装不装 xlsx 四件套）在 `s3` 模式下要改成查前缀，
  不能再用 `space_dir(space).glob("*.xlsx")`。

## 怎么验证（本地）

```bash
.venv/Scripts/python.exe scripts/okf_push_s3.py --dry-run
.venv/Scripts/python.exe scripts/okf_push_s3.py
OKF_BACKEND=s3 .venv/Scripts/python.exe -m pytest tests/unit/test_space_backend.py -q
```
