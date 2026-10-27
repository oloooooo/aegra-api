"""`/uploads/` 路由：md 读 Store，xlsx 读本地盘（key 由 PG 台账点出）。

为什么叠在 `StoreBackend` 上而不是另写一个：md 的 ls/read/grep/glob 语义（文本、分页、
TTL 续期）都在 Store 那份实现里，重写一遍只会引入偏差。二进制只有一件不同的事——
`download_files` 要返回**真字节**，而字节在盘上、路径只在台账里。

于是只覆盖两处：取字节（`adownload_files`）与写家族（一律拒绝）。
- `adownload_files` 先查台账（async PG），命中就读盘；
- 未命中交回 Store（md）；两边都没有就是 `file_not_found`。
- 写家族（`write`/`edit`/`delete`/`upload_files` 及异步版）返回只读文案：唯一的写入入口是
  上传接口（`POST /custom/uploads[/tables]`），agent 不能改会话文件。

`ls` / `read` / `grep` 仍只看 Store（md）：xlsx 是 zip，这些工具对它本来也没意义
（`grep` 会跳过二进制、`read` 只能给 base64）。agent 知道有哪个 xlsx 靠系统提示里的
清单（`graphs/middleware/session_files.py`），用哪个靠 xlsx 四件套。
"""

from __future__ import annotations

import asyncio

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
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
        ledger = await _ledger_keys()
        out: list[FileDownloadResponse] = []
        for raw in paths:
            key = ledger.get("/" + raw.lstrip("/"))
            if key is None:
                out.extend(await super().adownload_files([raw]))  # md 在 Store
                continue
            try:
                out.append(FileDownloadResponse(path=raw, content=await asyncio.to_thread(disk.read, key)))
            except FileNotFoundError:
                # 行还在、字节没了：当不存在，别让 agent 以为文件可用
                out.append(FileDownloadResponse(path=raw, content=None, error="file_not_found"))
        return out


async def _ledger_keys() -> dict[str, str]:
    """本会话会话文件的 `{"/<名>": 盘上 key}`。拿不到作用域就是空表。

    懒导入：图模块不该把 ORM 拖进启动路径，`shared/` 也不能被 `custom/` 反向依赖。
    """
    scope = session_scope.resolve(session_scope.current_config())
    if scope is None:
        return {}
    from aegra_api.core.orm import get_session_maker
    from custom.service import session_files

    async with get_session_maker()() as session:
        rows = await session_files.list_for_thread(session, *scope)
    return {f"/{row.name}": row.object_key for row in rows}
