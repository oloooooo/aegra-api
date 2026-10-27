"""`/uploads/` 路由：md 读 Store，xlsx 读本地盘（key 由 PG 台账点出）。

为什么叠在 `StoreBackend` 上而不是另写一个：md 的 ls/read/grep/glob 语义（文本、分页、
TTL 续期）都在 Store 那份实现里，重写一遍只会引入偏差。二进制只有一件不同的事——
`download_files` 要返回**真字节**，而字节在盘上、路径只在台账里。

于是只覆盖两处：取字节（`adownload_files`）与写家族（一律拒绝）。
- `adownload_files` 先查台账（async PG），命中就读盘；
- 未命中交回 Store（md）；两边都没有就是 `file_not_found`。
- 写家族（`write`/`edit`/`delete`/`upload_files` 及异步版）返回只读文案：唯一的写入入口是
  上传接口（`POST /custom/uploads[/tables]`），agent 不能改会话文件。

`ls` 要把两半合起来：md 在 Store，表在台账指向的盘上——它是**同一个目录**里的文件。`ls`
漏掉表的话，agent（尤其没装清单的子 agent）就只能等别人把路径告诉它。`grep` / `glob`
仍只看 Store（xlsx 是 zip，本来也没意义）；`read` 遇到表给一句指向 xlsx 工具的提示，
而不是一句会让人以为文件丢了的 `file_not_found`。
"""

from __future__ import annotations

import asyncio
from typing import Any

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    LsResult,
    ReadResult,
    WriteResult,
)

from graphs.access import session_scope
from shared.session import disk

_READ_ONLY = "会话上传目录只读：新增/替换请走上传接口（POST /custom/uploads[/tables]）"


class UploadsBackend(StoreBackend):
    """`/uploads/<名>`：md 走 Store，xlsx 走盘；**整体只读**。

    写家族必须显式覆写：`StoreBackend` 自带同步与异步两套写实现（`write`/`awrite` …），
    不覆写就是把 agent 的写静默落进 Store——`write_file('/uploads/x.xlsx')` 会返回成功、
    `ls` 看得见、`read_file` 读得到文本，而盘上的 xlsx 一个字节没变（ADR-0018 缺陷 13）。
    """

    def __init__(self) -> None:
        super().__init__(namespace=session_scope.uploads_namespace)

    # ---------- 读家族：Store 的 md + 台账里的表 ----------

    async def als(self, file_path: str) -> LsResult:
        """合并台账：上传的表也是 `/uploads/` 里的文件，不能只列 Store 里的 md。"""
        base = await super().als(file_path)
        if base.error or file_path.strip("/"):
            return base
        ledger = await _ledger()
        seen = {e["path"] for e in base.entries or []}
        extra: list[FileInfo] = [
            FileInfo(path=path, is_dir=False, size=row.size_bytes)
            for path, row in ledger.items()
            if path not in seen
        ]
        return LsResult(entries=sorted([*(base.entries or []), *extra], key=lambda e: e["path"]))

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """表就在这儿，只是 `read` 读不了：给指向 xlsx 工具的提示，而不是「没有这个文件」。"""
        if "/" + file_path.lstrip("/") in await _ledger():
            return ReadResult(
                error=f"{file_path} 是二进制表格，read_file 读不了；用 xlsx_info / xlsx_query 等工具，路径原样传"
            )
        return await super().aread(file_path, offset=offset, limit=limit)

    # ---------- 写家族：一律拒绝 ----------

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_READ_ONLY)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_READ_ONLY)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        return EditResult(error=_READ_ONLY)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        return EditResult(error=_READ_ONLY)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=_READ_ONLY)

    async def adelete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=_READ_ONLY)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=p, error=_READ_ONLY) for p, _ in files]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        ledger = await _ledger()
        out: list[FileDownloadResponse] = []
        for raw in paths:
            row = ledger.get("/" + raw.lstrip("/"))
            if row is None:
                out.extend(await super().adownload_files([raw]))  # md 在 Store
                continue
            try:
                out.append(
                    FileDownloadResponse(path=raw, content=await asyncio.to_thread(disk.read, row.object_key))
                )
            except FileNotFoundError:
                # 行还在、字节没了：当不存在，别让 agent 以为文件可用
                out.append(FileDownloadResponse(path=raw, content=None, error="file_not_found"))
        return out


async def _ledger() -> dict[str, Any]:
    """本会话会话文件的 `{"/<名>": 台账行}`。拿不到作用域就是空表。

    懒导入：图模块不该把 ORM 拖进启动路径，`shared/` 也不能被 `custom/` 反向依赖。
    """
    scope = session_scope.resolve(session_scope.current_config())
    if scope is None:
        return {}
    from aegra_api.core.orm import get_session_maker
    from custom.service import session_files

    async with get_session_maker()() as session:
        rows = await session_files.list_for_thread(session, *scope)
    return {f"/{row.name}": row for row in rows}
