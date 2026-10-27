"""会话文件上传的总入口：两个接口、两条载体、一套会话隔离。

===============================================================================
一、两个接口 / 两种载体
===============================================================================

    POST /custom/uploads          md   → LangGraph Store（JSON 文本）
    POST /custom/uploads/tables   xlsx → 本地盘（字节）+ PG `session_object`（行）

为什么 md 不进盘、xlsx 不进 Store：agent 读文件靠 deepagents 的 fs 工具
（ls/read/grep/glob），它们只认 backend。md 文本放 Store 白拿这四件工具；xlsx 是 zip
二进制，read/grep 读不了，只能走 xlsx 四件套（字节经 backend 取到，再建 SQLite 索引）。
两条路在 `/uploads/` 合成一个 backend（`graphs/access/uploads_backend.py`）。

===============================================================================
二、共同前置：会话归属
===============================================================================

    _require_owned_thread()   别人的会话一律 404（与 aegra 的 ownership 过滤同一口径，
                              不给「存在但无权」的信号）

**thread 是会话文件的唯一隔离键**：清单注入（每轮 run 起始）、删会话即删、孤儿 TTL
全以它为准。无主文件没有任何 run 能看到，也没有任何机制会去清它——所以两个接口都
强制 `thread_id`，不接受「先传后挂」。

===============================================================================
三、md 分支
===============================================================================

    POST /custom/uploads (file=<name>.md, thread_id)
      │ 1. _safe_name()        basename + 消毒（控制字符 / 超长 / 后缀），不合规 422
      │ 2. _read_limited()     分块读并卡 MAX_UPLOAD_MB（不信 Content-Length，它会撒谎或缺失）
      │ 3. decode("utf-8")     不是文本就 422 —— 排在大小闸之后，别为大文件先读满再看编码
      │ 4. list_files()        数一遍现有文件；**同名覆盖不占名额**
      │ 5. put_md()            store.aput(ns, "/<name>", {content, encoding, 时间戳}, ttl=72h)
      ▼
    {"name": …, "path": "/uploads/<name>", "bytes": …, "ttl_minutes": 4320}

同名 = 覆盖（PUT 语义）：`ls /uploads` 里永远只有一份，不做版本链。

===============================================================================
四、xlsx 分支
===============================================================================

    POST /custom/uploads/tables (file=<空间名>_<表名>.xlsx, thread_id)
      │ 1. file.size 先挡一次           不读身体就能 413
      │ 2. _safe_name(suffix=".xlsx") + parse_space()
      │      文件名前缀必须是上传者拥有的 space —— 但它**只决定主管派哪个子 agent**，
      │      不决定存哪（key 恒为 `<uuid>/<名>`，归属只记台账行）
      │ 3. disk.put_stream(file, key)   边读边算 sha256 写 .part-*，再原子 rename；超限自己收
      │ 4. session_files.upsert()        锁用户行算配额 → 写/覆盖行 → commit
      │ 5. **提交成功后**再删被替换掉的旧对象
      ▼
    201 {"name", "path": "/uploads/<name>", "space", "bytes", "sha256", "indexed": false}

（`indexed` 恒为 false：索引是缓存，第一次调工具时才建，见 graphs/tools/xlsx/index.py。）

**写序不能换**：key → PG 事务 → 删旧对象。`.part` → rename 是原子的，所以任一步崩
都只留垃圾对象，不会出现「行说 A、字节是 B」。同名上传各写一个 uuid key，谁最后提交
谁赢，另一个是孤儿对象——不破坏一致性。

**失败清理只删「本次新写、且不是旧行正在用的」对象**（`_drop_new`）：同名同内容重传
恰好卡在配额时，删了新对象就等于删掉旧行唯一的那份字节。清理自身必须吞异常——
它一抛，就把「存储不可用 → 503」变成「未知错误 → 500」（实测踩过）。

===============================================================================
五、落到哪里 / 谁能读到
===============================================================================

     md    Store 命名空间 (uploads, h(identity), thread) + key "/<name>"
          读：SessionFilesMiddleware（每轮 run 起始的清单注入）、
              StoreBackend（agent 对 /uploads/<name> 的 ls/read/grep/glob）
    xlsx  本地盘 `<uploads.dir>/<uuid>/<名>`，行在 PG `session_object`
          读：清单注入（台账）+ xlsx 四件套（`adownload_files` → 落缓存 → 建 SQLite 索引）

子 agent 读**同一条会话**的文件靠派活时签发的 `session_scope`（graphs/access/session_scope.py），
它读不到别的主管的会话。

——两种载体的归属只认两个键：Store 命名空间用 `h()`（`identity_digest`）；
盘上 key 不带归属，只靠台账行。两处判定若不一致，同一份内容会出现在两种视角下（排查极难）。

===============================================================================
六、删除与垃圾（各自的清理责任，别在这里加「顺手删」）
===============================================================================

    删会话      custom/middleware/threads_delete.py：先读出 key → 删行 → 逐对象删 → 清 Store
    孤儿 md     Store 自带 TTL（aegra 启动时 start_ttl_sweeper），读一次续期
    孤儿 xlsx   没有行就没有归属，只能靠对账清（docs/large-files.md §10）
    容量对账    PG 的 sum(size_bytes) 不含索引与被覆盖的旧对象

===============================================================================
七、代码分区（改代码前先看这张表）
===============================================================================

  A. 闸与工具    _MAX_BYTES, _CHUNK, _BAD_NAME, _safe_name(), _read_limited(),
                 _require_owned_thread()
  B. md 分支     upload_session_file()
  C. xlsx 分支   upload_session_table(), _drop_new()

限额、对账与排错见根目录 `UPLOAD.md`；实现细节见 `docs/large-files.md`。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import PureWindowsPath
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_deps import require_auth
from aegra_api.core.database import db_manager
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import get_session
from aegra_api.models.auth import User
from custom.api.schemas import UploadFileResponse, UploadTableResponse
from custom.service import session_files
from shared.session import disk
from shared.session.store import (
    MAX_FILES_PER_SESSION,
    MAX_UPLOAD_MB,
    list_files,
    put_md,
    virtual_path,
)

router = APIRouter(prefix="/custom", tags=["/custom"])
logger = structlog.getLogger(__name__)

_MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024
_CHUNK = 64 * 1024
# 控制字符（含 NUL）会让日志与 HTML 渲染出问题，直接拒
_BAD_NAME = re.compile(r"[\x00-\x1f\x7f]")


def _safe_name(raw: str | None, suffix: str = ".md") -> str:
    """取 basename 并消毒；不合规即 422（不做静默改名，改名会与用户的引用对不上）。"""
    # 浏览器可能传 Windows 反斜杠路径，PureWindowsPath 一并剥掉
    name = PureWindowsPath((raw or "").replace("\\", "/")).name.strip()
    if not name or name in {".", ".."} or _BAD_NAME.search(name):
        raise HTTPException(status_code=422, detail="文件名非法")
    if len(name) > 200:
        raise HTTPException(status_code=422, detail="文件名过长（上限 200 字符）")
    if not name.lower().endswith(suffix):
        raise HTTPException(status_code=415, detail=f"本期只支持 {suffix} 文件")
    return name


async def _read_limited(upload: UploadFile) -> bytes:
    """分块读并卡上限：不信 Content-Length（客户端可撒谎，也可缺省）。"""
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_CHUNK):
        total += len(chunk)
        if total > _MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"文件超过 {MAX_UPLOAD_MB}MB 上限")
        chunks.append(chunk)
    return b"".join(chunks)


async def _require_owned_thread(session: AsyncSession, user: User, thread_id: str) -> None:
    """别人的会话一律 404，不给“存在但无权”的信号（与 aegra 的 ownership 过滤一致）。"""
    owned = await session.scalar(
        select(ThreadORM.thread_id).where(
            ThreadORM.thread_id == thread_id,
            ThreadORM.user_id == user.identity,
        )
    )
    if owned is None:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")


@router.post("/uploads", response_model=UploadFileResponse)
async def upload_session_file(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
    thread_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> UploadFileResponse:
    """把一个 md 文件挂到某个会话上，返回它在 agent 眼里的路径。

    同名文件覆盖（PUT 语义）：`ls /uploads` 里永远只有一份，不做版本链。
    """
    await _require_owned_thread(session, user, thread_id)

    name = _safe_name(file.filename)
    data = await _read_limited(file)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=422,
            detail=f"文件不是合法 UTF-8 文本（第 {e.start} 字节处解码失败）",
        ) from None

    try:
        store = db_manager.get_store()
    except RuntimeError:
        logger.exception("upload_without_store", thread_id=thread_id)
        raise HTTPException(status_code=503, detail="存储未就绪，请稍后重试") from None

    # 同名重传是覆盖，不占新名额；只有真新增才判配额（不续期：判额度不该延长 TTL）
    rows, _ = await list_files(
        store, user.identity, thread_id, limit=MAX_FILES_PER_SESSION, refresh_ttl=False
    )
    if len(rows) >= MAX_FILES_PER_SESSION and virtual_path(name) not in {r["path"] for r in rows}:
        raise HTTPException(
            status_code=413,
            detail=f"本会话文件数已达上限（{MAX_FILES_PER_SESSION}），同名覆盖不受限",
        )

    meta = await put_md(store, user.identity, thread_id, name, text)
    logger.info(
        "session_file_uploaded",
        identity=user.identity,
        thread_id=thread_id,
        name=name,
        bytes=meta["bytes"],
    )
    return UploadFileResponse(**meta)


@router.post("/uploads/tables", response_model=UploadTableResponse, status_code=201)
async def upload_session_table(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
    thread_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> UploadTableResponse:
    """把一个 xlsx 挂到会话上（ADR-0015）：字节进本地盘，行进 `session_object`。

    文件名必须是 `<空间名>_<表名>.xlsx`；空间名校验的是**上传者有没有该空间**，
    它只决定主管派哪个子 agent，不决定存哪——key 恒为 `<uuid>/<名>`。

    写序：写 key（原子 rename）→ PG 事务 → 删旧对象。中途崩只会留孤儿对象，
    不会出现"有行没字节"；提交失败时只在 `新 key != 旧 key` 时删新对象。
    """
    await _require_owned_thread(session, user, thread_id)

    # 1. 便宜的闸先开：不读身体就能拒
    if file.size is not None and file.size > session_files.MAX_BLOB_BYTES:
        raise HTTPException(status_code=413, detail=f"文件超过 {session_files.MAX_BLOB_MB}MB 上限")

    name = _safe_name(file.filename, suffix=".xlsx")
    try:
        space = session_files.parse_space(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    spaces = getattr(user, "user_spaces", None)
    if spaces is not None and space not in spaces:
        raise HTTPException(
            status_code=422,
            detail=f"文件名前缀 {space!r} 不是你的空间（可用：{sorted(spaces)}）",
        )

    existing = await session_files.get_one(session, user.identity, thread_id, name)
    old_key = existing.object_key if existing else None

    final = disk.new_key(name)
    sha256: str | None = None
    size = 0
    try:
        sha256, size = await asyncio.to_thread(
            disk.put_stream, file.file, final, max_bytes=session_files.MAX_BLOB_BYTES
        )
        await session_files.upsert(
            session,
            owner=user.identity,
            thread_id=thread_id,
            name=name,
            space=space,
            object_key=final,
            sha256=sha256,
            size_bytes=size,
        )
        await session.commit()
    except disk.TooLarge as e:
        await _drop_new(final, old_key)
        raise HTTPException(status_code=413, detail=str(e)) from None
    except session_files.QuotaExceeded as e:
        await _drop_new(final, old_key)
        raise HTTPException(status_code=413, detail=str(e)) from None
    except Exception:
        logger.exception("upload_table_failed", thread_id=thread_id, name=name)
        await _drop_new(final, old_key)
        raise

    # 提交成功后才删被替换掉的旧对象
    if old_key and old_key != final:
        await asyncio.to_thread(disk.delete, old_key)

    logger.info(
        "session_table_uploaded",
        identity=user.identity,
        thread_id=thread_id,
        name=name,
        space=space,
        bytes=size,
    )
    return UploadTableResponse(
        name=name,
        path=f"/uploads/{name}",
        space=space,
        bytes=size,
        sha256=sha256,
        indexed=False,
    )


async def _drop_new(final: str | None, old_key: str | None) -> None:
    """失败清理：只删本次新写的对象，且它不能是旧行正在用的那一个。

    自身必须吞异常：出错了再抛一次只会把「上传失败」的具体原因盖成未知错误。
    """
    if final and final != old_key:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(disk.delete, final)
