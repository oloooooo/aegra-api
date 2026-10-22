"""会话文件上传（ADR-0013）：只收 md，落 LangGraph Store，按 (上传者, 主管 thread) 隔离。

为什么上传必须挂会话：会话文件的一切语义（清单注入、删会话即删、孤儿 TTL）都以
thread_id 为键。无主的文件既没法被任何 run 看到，也没有任何机制会去清它。

为什么上传与对话互斥：上传写的是"下次 run 起始时读到的清单"，不是往当前 run 里塞消息。
两者并行不会出错，但也没意义——本轮 run 看不到刚传的文件。

校验顺序故意固定：thread 归属 → 扩展名 → 大小 → 编码。前两步便宜，
大文件在第三步就被挡住，绝不为了报"编码错"先把 10MB 读满。
"""

from __future__ import annotations

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
from custom.schemas import UploadFileResponse
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


def _safe_name(raw: str | None) -> str:
    """取 basename 并消毒；不合规即 422（不做静默改名，改名会与用户的引用对不上）。"""
    # 浏览器可能传 Windows 反斜杠路径，PureWindowsPath 一并剥掉
    name = PureWindowsPath((raw or "").replace("\\", "/")).name.strip()
    if not name or name in {".", ".."} or _BAD_NAME.search(name):
        raise HTTPException(status_code=422, detail="文件名非法")
    if len(name) > 200:
        raise HTTPException(status_code=422, detail="文件名过长（上限 200 字符）")
    if not name.lower().endswith(".md"):
        raise HTTPException(status_code=415, detail="本期只支持 .md 文件")
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
    owned = await session.scalar(
        select(ThreadORM.thread_id).where(
            ThreadORM.thread_id == thread_id,
            ThreadORM.user_id == user.identity,
        )
    )
    if owned is None:
        # 与 aegra 的 ownership 过滤一致：别人的会话一律 404，不给"存在但无权"的信号
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")

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
