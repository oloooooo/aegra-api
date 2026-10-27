"""对象存储 backend：知识空间与会话上传的**同一套读法**（ADR-0017）。

为什么自研而不是拿现成的（都实测过，见 ADR-0017）：

- `DiTo97/deepagents-backends` 0.2.0 与锁定的 deepagents 0.7.23 协议不兼容（方法叫
  `ls_info/glob_info/grep_raw`、缺 `delete`、返回 `str` 而非 `LsResult/GrepResult`）；
- `langchain-aws` 的 S3 backend PR 长期未合入；
- deepagents 0.7.23 的 `BackendProtocol` 9 个 `a*` 方法**全有默认实现**（`asyncio.to_thread`
  包同步版），所以只需实现 9 个同步方法，细语义（切片/行号/替换校验/glob 匹配）全在
  `deepagents.backends.utils` 里，直接复用。

两个 backend 共用一套读家族（`_ObjectTree`）：

- ``S3Backend``        知识空间。对象前缀 `okf/`，虚拟路径 ``/空间名/文件``。空间隔离复用
                      `SpaceGuard`——与本地盘 `SpaceBackend` 是同一份规则，只换存储。
- ``UploadsBackend``   ``/uploads/`` 会话上传文件，**只读**。对象 key 是
                      `uploads/<uuid>/<name>`（不可从名字推导），唯一路由是 PG 台账
                      `session_object`，所以它只能异步查库：框架调用的正是
                      `aread/als/aglob/agrep/adownload_files`。同步版**不实现**：
                      继承 `BackendProtocol` 的默认（`NotImplementedError`），因为同步读要
                      把 async PG 会话跑在另一个事件循环里——本地/单副本都不成立。

shortcut: `UploadsBackend` 每个动作查一次台账（一次主键索引查询），不按 run 缓存——
缓存会让「同一 run 内新上传的文件看不见」这种一致性坑变得难以排查，等真有量再谈。
"""

from __future__ import annotations

import asyncio
import base64
import posixpath
from dataclasses import dataclass
from typing import Any

import structlog
from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileData,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import (
    InvalidGlobPatternError,
    _get_backend_read_file_type,
    _glob_search_files,
    check_empty_content,
    create_file_data,
    grep_matches_from_files,
    perform_string_replacement,
    slice_read_response,
)

from graphs.access import session_scope
from graphs.access.backend import SpaceGuard
from graphs.access.spaces import spaces_from_config_or_runtime
from shared.session import blobs

logger = structlog.getLogger(__name__)

_NOT_FOUND = "file_not_found"
_READ_ONLY = "会话上传目录只读：新增/替换请走上传接口（POST /custom/uploads[/tables]）"

# `read` 对表格给指路提示而不是 base64（表格只该用 xlsx 四件套读）
_TABLE_SUFFIXES = (".xlsx", ".xlsm", ".xls")

# 框架把 ooxml/zip/parquet 都归为 text（`_EXTENSION_TO_FILE_TYPE` 只细分图片/音视频），
# 直接解码会把二进制当乱码交给模型；这几类一律走 base64。
_FORCE_BINARY = frozenset({".xlsx", ".xlsm", ".docx", ".pptx", ".zip", ".parquet"})


def _is_binary(vpath: str, data: bytes) -> bool:
    return (
        _get_backend_read_file_type(vpath) != "text"
        or posixpath.splitext(vpath)[1].lower() in _FORCE_BINARY
        or b"\x00" in data
    )


def _vpath(raw: str | None) -> str:
    """规范化虚拟路径：`/a/../b` → `/b`，越界（`/../x`）直接拒。

    判定与读取必须落在**同一个**规范化结果上：原始字符串比对挡不住 `..` 穿越
    （`/空间A/../空间B/x` 首段是 A 却打开了 B 的文件）。
    """
    p = posixpath.normpath("/" + str(raw or "/").replace("\\", "/").strip().strip("/"))
    if p == "/.." or p.startswith("/../"):
        raise ValueError(f"路径越界：{raw}")
    return p


def _is_precondition_failed(exc: Exception) -> bool:
    """条件写冲突（412）：If-Match 不成立时 S3/SeaweedFS 返回它。"""
    response = getattr(exc, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    return code in {"PreconditionFailed", "412"} or response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412


@dataclass(frozen=True)
class _Obj:
    """一个对象的身份：key 不可从虚拟路径推导，只能带着走。"""

    key: str
    size: int = 0


def _get_bytes(obj: _Obj) -> bytes | None:
    got = blobs.get_object(obj.key)
    return None if got is None else got[0]


class _ObjectTree:
    """「一堆对象 = 一棵虚拟目录树」的读家族，只写一遍。

    子类提供 ``_keys()``（本次可见的 ``{vpath: _Obj}``）与 ``_shown()``（搜索结果过滤），
    读字节统一走 `_get_bytes`（key 就在 `_Obj` 里，两种存储取法一样）。

    异步子类自己 `await` 出 keys 再把整段计算丢进线程（``asyncio.to_thread``）：
    boto3 是同步的，跑在事件循环里会卡住整台服务。
    """

    def _keys(self) -> dict[str, _Obj]:
        raise NotImplementedError

    def _shown(self, vpath: str) -> bool:  # noqa: ARG002 —— 默认全可见
        return True

    def _gate(self, vpath: str) -> str | None:  # noqa: ARG002 —— 默认不设限
        return None

    def _gate_write(self, vpath: str) -> str | None:  # noqa: ARG002
        raise NotImplementedError

    # ---------- ls ----------

    def _ls(self, keys: dict[str, _Obj], path: str) -> LsResult:
        vpath = _vpath(path)
        if err := self._gate(vpath):
            return LsResult(error=err)
        base = "/" if vpath == "/" else vpath + "/"
        dirs: set[str] = set()
        files: list[FileInfo] = []
        for p, obj in keys.items():
            if not p.startswith(base):
                continue
            rest = p[len(base) :]
            if "/" in rest:
                dirs.add(rest.split("/", 1)[0])
            elif self._shown(p):
                files.append(FileInfo(path=p, is_dir=False, size=obj.size))
        entries: list[FileInfo] = [
            FileInfo(path=f"{base}{d}/", is_dir=True, size=0) for d in sorted(dirs) if self._shown(f"{base}{d}")
        ]
        entries.extend(files)
        entries.sort(key=lambda e: e["path"])  # 与 FilesystemBackend 同序（纯按虚拟路径排）
        if not entries and vpath != "/":
            return LsResult(error=f"Path '{vpath}' not found")
        return LsResult(entries=entries)

    # ---------- read ----------

    def _read(self, keys: dict[str, _Obj], file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        vpath = _vpath(file_path)
        if err := self._gate(vpath):
            return ReadResult(error=err)
        obj = keys.get(vpath)
        if obj is None:
            return ReadResult(error=f"File '{vpath}' not found")
        data = _get_bytes(obj)
        if data is None:
            return ReadResult(error=f"File '{vpath}' not found")
        if _is_binary(vpath, data):
            if vpath.lower().endswith(_TABLE_SUFFIXES):
                # base64 表格对模型只烧 token，一句指路比一堆乱码有用
                return ReadResult(
                    error=f"{vpath} 是二进制表格，read_file 读不了；用 xlsx_info / xlsx_query 等工具，路径原样传"
                )
            return ReadResult(file_data=FileData(content=base64.b64encode(data).decode("ascii"), encoding="base64"))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:  # 后缀像文本、内容不是：base64 而不是报错
            return ReadResult(file_data=FileData(content=base64.b64encode(data).decode("ascii"), encoding="base64"))
        if msg := check_empty_content(text):
            return ReadResult(file_data=FileData(content=msg, encoding="utf-8"))
        return slice_read_response(create_file_data(text), offset, limit)

    # ---------- glob / grep ----------

    def _glob(self, keys: dict[str, _Obj], pattern: str, path: str | None = None) -> GlobResult:
        vpath = _vpath(path)
        if err := self._gate(vpath):
            return GlobResult(error=err, matches=[])
        found = self._match(keys, pattern, vpath)
        if isinstance(found, GlobResult):  # 非法 pattern
            return found
        return GlobResult(matches=[{"path": p} for p in found])

    def _match(self, keys: dict[str, _Obj], pattern: str, vpath: str) -> list[str] | GlobResult:
        """复用框架的 glob 语义：无 `/` 匹配任意层的 basename，有 `/` 相对 path 匹配。"""
        empty = {p: create_file_data("") for p in keys if self._shown(p)}
        try:
            found = _glob_search_files(empty, pattern, vpath)
        except InvalidGlobPatternError as e:
            return GlobResult(error=str(e), matches=[])
        return [] if found == "No files found" else found.split("\n")

    def _grep(
        self,
        keys: dict[str, _Obj],
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        vpath = _vpath(path)
        if err := self._gate(vpath):
            return GrepResult(error=err, matches=[])
        # 只下文本候选：xlsx/图片/视频进 grep 既慢又只会解码失败
        cand = {p: o for p, o in keys.items() if o.size and self._shown(p) and not _is_binary(p, b"")}
        if glob:
            found = self._match(cand, glob, vpath)
            if isinstance(found, GlobResult):
                return GrepResult(error=found.error, matches=[])
            cand = {p: o for p, o in cand.items() if p in set(found)}
        files: dict[str, Any] = {}
        for p, obj in cand.items():
            data = _get_bytes(obj)
            if data is None:
                continue
            try:
                files[p] = create_file_data(data.decode("utf-8"))
            except UnicodeDecodeError:
                continue  # 后缀像文本、其实不是：跳过，而不是让整次搜索失败
        return grep_matches_from_files(files, pattern, vpath, None, max_count=max_count)

    # ---------- 上下行 ----------

    def _download(self, keys: dict[str, _Obj], paths: list[str]) -> list[FileDownloadResponse]:
        out: list[FileDownloadResponse] = []
        for path in paths:
            vpath = _vpath(path)
            err = self._gate(vpath)
            obj = keys.get(vpath)
            data = None if err or obj is None else _get_bytes(obj)
            if err is None and data is None:
                err = _NOT_FOUND
            out.append(FileDownloadResponse(path=path, content=data, error=err))
        return out

    def _upload(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        out: list[FileUploadResponse] = []
        for path, content in files:
            vpath = _vpath(path)
            err = self._gate_write(vpath)
            if err is None:
                try:
                    blobs.put_bytes(self._key_of(vpath), content)
                except Exception:  # noqa: BLE001 —— 单个文件失败不该让整批炸掉
                    logger.exception("s3_backend_upload_failed", path=vpath)
                    err = "permission_denied"
            out.append(FileUploadResponse(path=path, error=err))
        return out

    def _key_of(self, vpath: str) -> str:
        raise NotImplementedError


class S3Backend(SpaceGuard, _ObjectTree, BackendProtocol):
    """知识空间：对象前缀默认 `okf/`，虚拟路径 ``/空间名/文件``。

    空间隔离复用 `SpaceGuard`（`_check_read`/`_check_write`/`_visible`），这里只回答
    「这个空间文件夹在存储里存在吗」与「字节怎么读写」。
    """

    def __init__(self, prefix: str = "okf/") -> None:
        self._prefix = prefix if prefix.endswith("/") else prefix + "/"

    # ---------- 路径 ↔ 对象 key ----------

    def _key_of(self, vpath: str) -> str:
        return f"{self._prefix}{vpath.lstrip('/')}"

    def _keys(self) -> dict[str, _Obj]:
        tree = blobs.list_tree(self._prefix)
        return {
            "/" + k[len(self._prefix) :]: _Obj(k, int(v.get("size") or 0))
            for k, v in tree.items()
            if len(k) > len(self._prefix) and not k.endswith("/")
        }

    def _shown(self, vpath: str) -> bool:
        spaces = spaces_from_config_or_runtime()
        return True if spaces is None else self._visible(vpath, spaces)

    def _gate(self, vpath: str) -> str | None:
        return self._check_read(vpath)

    def _gate_write(self, vpath: str) -> str | None:
        return self._check_write(vpath)

    # ---------- SpaceGuard 探针（一次 LIST/HEAD，命中即够） ----------

    def _space_dir_exists(self, name: str) -> bool:
        return blobs.exists_prefix(f"{self._prefix}{name}/")

    def _root_file_exists(self, name: str) -> bool:
        return blobs.stat(self._key_of(f"/{name}")) is not None

    # ---------- 协议：读家族 ----------

    def ls(self, path: str) -> LsResult:
        return self._ls(self._keys(), path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._read(self._keys(), file_path, offset, limit)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self._glob(self._keys(), pattern, path)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        return self._grep(self._keys(), pattern, path, glob, max_count=max_count)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._download(self._keys(), paths)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._upload(files)

    # ---------- 协议：写家族 ----------

    def write(self, file_path: str, content: str) -> WriteResult:
        vpath = _vpath(file_path)
        if err := self._gate_write(vpath):
            return WriteResult(error=err)
        try:
            blobs.put_bytes(self._key_of(vpath), content.encode("utf-8"))
        except Exception as e:  # noqa: BLE001 —— 存储不可用，回给模型而不是抛穿
            logger.exception("s3_backend_write_failed", path=vpath)
            return WriteResult(error=f"Error writing file '{vpath}': {e}")
        return WriteResult(path=vpath)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002 —— 协议签名
    ) -> EditResult:
        """读-改-写走 CAS：PUT 带 If-Match（读到的 ETag），冲突时重读重放。

        没有 CAS，两次并发编辑会互相覆盖到「谁都不知道」；条件写把冲突降级成一次
        重读重放，而不是静默丢掉其中一份改动。5 次仍抢不到就报错让调用方重读——
        无退避睡眠：每次重试自带一次 GET + PUT，本身就是间隔。
        """
        vpath = _vpath(file_path)
        if err := self._gate_write(vpath):
            return EditResult(error=err)
        key = self._key_of(vpath)
        for attempt in (1, 2, 3, 4, 5):
            got = blobs.get_object(key)
            if got is None:
                return EditResult(error=f"File '{vpath}' not found")
            data, etag = got
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as e:
                return EditResult(error=f"Error editing file '{vpath}': {e}")
            replaced = perform_string_replacement(content, old_string, new_string, replace_all)
            if isinstance(replaced, str):
                return EditResult(error=replaced)
            new_content, occurrences = replaced
            try:
                blobs.put_bytes(key, new_content.encode("utf-8"), if_match=etag or None)
            except Exception as e:  # noqa: BLE001
                if _is_precondition_failed(e) and attempt < 5:
                    logger.info("s3_backend_edit_conflict_retry", path=vpath)
                    continue
                logger.exception("s3_backend_edit_failed", path=vpath)
                return EditResult(error=f"Error editing file '{vpath}': {e}")
            return EditResult(path=vpath, occurrences=occurrences)
        return EditResult(error=f"Error editing file '{vpath}': 文件被并发修改，请重新读取后再改")

    def delete(self, file_path: str) -> DeleteResult:
        vpath = _vpath(file_path)
        if err := self._gate_write(vpath):
            return DeleteResult(error=err)
        key = self._key_of(vpath)
        if blobs.stat(key) is not None:
            blobs.delete(key)
            return DeleteResult(path=vpath)
        if blobs.exists_prefix(key + "/"):  # 目录：对象存储没有空目录，「有东西」就是存在
            blobs.delete_prefix(key + "/")
            return DeleteResult(path=vpath)
        return DeleteResult(error=f"File '{vpath}' not found")


class UploadsBackend(_ObjectTree, BackendProtocol):
    """``/uploads/``：本会话上传的文件，只读视图，字节直连对象存储。

    虚拟路径是 ``/名字``（`CompositeBackend` 已把 ``/uploads/`` 前缀剥掉，回程再拼回）。
    key 由台账给：``uploads/<uuid>/<名>``，**不可从虚拟路径推导**，所以每次动作查一次
    `session_object`（异步）。框架调用的正是 ``a*`` 方法；同步方法拿不到 async PG 会话，
    直接抛错而不是给出「看不见文件」的假答案。
    """

    def _scope(self) -> tuple[str, str] | None:
        try:
            config = session_scope.current_config()
        except RuntimeError:  # 图外（测试/脚本）
            return None
        return session_scope.resolve(config)

    async def _rows(self) -> dict[str, _Obj]:
        scope = self._scope()
        if scope is None:
            return {}
        owner, thread_id = scope
        # 懒导入：这两个模块会把 ORM 与 custom 拖进 import 路径，backend 在启动期就被拉起
        from aegra_api.core.orm import get_session_maker
        from custom.service import session_files

        async with get_session_maker()() as session:
            rows = await session_files.list_for_thread(session, owner, thread_id)
        return {f"/{r.name}": _Obj(r.object_key, r.size_bytes) for r in rows}

    def _gate_write(self, vpath: str) -> str | None:  # noqa: ARG002
        return _READ_ONLY

    # ---------- 协议：异步（框架走的就是这些） ----------

    async def als(self, path: str) -> LsResult:
        return self._ls(await self._rows(), path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        keys = await self._rows()
        return await asyncio.to_thread(self._read, keys, file_path, offset, limit)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self._glob(await self._rows(), pattern, path)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        keys = await self._rows()
        return await asyncio.to_thread(self._grep, keys, pattern, path, glob, max_count=max_count)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        keys = await self._rows()
        return await asyncio.to_thread(self._download, keys, paths)

    # ---------- 协议：写（同步版就是继承来的 async 默认实现要调的那个） ----------

    def write(self, file_path: str, content: str) -> WriteResult:  # noqa: ARG002
        return WriteResult(error=_READ_ONLY)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:  # noqa: ARG002
        return EditResult(error=_READ_ONLY)

    def delete(self, file_path: str) -> DeleteResult:  # noqa: ARG002
        return DeleteResult(error=_READ_ONLY)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:  # noqa: ARG002
        return [FileUploadResponse(path=path, error=_READ_ONLY) for path, _ in files]


__all__ = ["S3Backend", "UploadsBackend"]
