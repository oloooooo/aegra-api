"""会话文件上传（ADR-0013）：只收 md，落 LangGraph Store，按 (上传者, 主管 thread) 隔离。

为什么上传必须挂会话：会话文件的一切语义（清单注入、删会话即删、孤儿 TTL）都以
thread_id 为键。无主的文件既没法被任何 run 看到，也没有任何机制会去清它。

为什么上传与对话互斥：上传写的是"下次 run 起始时读到的清单"，不是往当前 run 里塞消息。
两者并行不会出错，但也没意义——本轮 run 看不到刚传的文件。

校验顺序故意固定：thread 归属 → 扩展名 → 大小 → 编码。前两步便宜，
大文件在第三步就被挡住，绝不为了报"编码错"先把 10MB 读满。
"""

from __future__ import annotations

import asyncio
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
from custom import blobs, session_objects
from custom.schemas import UploadFileResponse, UploadTableResponse
from custom.session_store import (
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
    """把一个 xlsx 挂到会话上（ADR-0015）：字节进对象存储，行进 `session_object`。

    文件名必须是 `<空间名>_<表名>.xlsx`；空间名校验的是**上传者有没有该空间**，
    它只决定主管派哪个子 agent，不决定存哪——存储前缀恒为 `sessions/{h}/{thread}/`。

    写序：tmp → copy 到正式 key → PG 事务 → 删 tmp。中途崩只会留孤儿对象，
    不会出现"有行没字节"；提交失败时只在 `新 key != 旧 key` 时删新对象
    （同名同内容重传恰好卡在配额，删了就把旧行唯一的那份字节删掉了）。
    """
    await _require_owned_thread(session, user, thread_id)

    # 1. 便宜的闸先开：不读身体就能拒
    if file.size is not None and file.size > session_objects.MAX_BLOB_BYTES:
        raise HTTPException(status_code=413, detail=f"文件超过 {session_objects.MAX_BLOB_MB}MB 上限")

    name = _safe_name(file.filename, suffix=".xlsx")
    try:
        space = session_objects.parse_space(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    spaces = getattr(user, "user_spaces", None)
    if spaces is not None and space not in spaces:
        raise HTTPException(
            status_code=422,
            detail=f"文件名前缀 {space!r} 不是你的空间（可用：{sorted(spaces)}）",
        )

    existing = await session_objects.get_one(session, user.identity, thread_id, name)
    old_key = existing.object_key if existing else None

    tmp = blobs.tmp_key(name)
    final: str | None = None
    try:
        sha256, size = await asyncio.to_thread(
            blobs.put_stream, file.file, tmp, max_bytes=session_objects.MAX_BLOB_BYTES
        )
        final = blobs.blob_key(user.identity, thread_id, sha256, name)
        await asyncio.to_thread(blobs.copy, tmp, final)
        await session_objects.upsert(
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
    except blobs.BlobTooLarge as e:
        await _drop_new(final, old_key)
        raise HTTPException(status_code=413, detail=str(e)) from None
    except blobs.BlobUnavailable:
        logger.exception("upload_table_without_store", thread_id=thread_id)
        await _drop_new(final, old_key)
        raise HTTPException(status_code=503, detail="存储未就绪，请稍后重试") from None
    except session_objects.QuotaExceeded as e:
        await _drop_new(final, old_key)
        raise HTTPException(status_code=413, detail=str(e)) from None
    except Exception:
        logger.exception("upload_table_failed", thread_id=thread_id, name=name)
        await _drop_new(final, old_key)
        raise
    finally:
        await asyncio.to_thread(blobs.delete, tmp)  # 幂等；删失败也不影响正确性

    # 提交成功后才删被替换掉的旧对象
    if old_key and old_key != final:
        await asyncio.to_thread(blobs.delete, old_key)

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
    """失败清理：只删本次新写的对象，且它不能是旧行正在用的那一个。"""
    if final and final != old_key:
        await asyncio.to_thread(blobs.delete, final)
