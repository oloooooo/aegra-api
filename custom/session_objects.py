"""会话文件的元数据与配额（ADR-0015 §3.2）。

字节在对象存储（`custom/blobs.py`），行在这里。三条约定：

- **主键 `(owner_identity, thread_id, name)`**：同名覆盖是 PUT 语义，不是版本链；
  唯一约束落在 DB 上，不靠"先查后插"（并发下两个请求会同时查空、同时插入）。
- **配额锁用户行，不锁会话**：字节上限是按用户算的，锁会话的话换个会话就绕过去了。
- **同名覆盖不吃双份**：算字节要减掉旧行；算文件数不占新名额。

配额不足抛 `QuotaExceeded`，由路由层映射成 413。
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from custom.models import SessionObject, UserTables


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


# 每会话文件数：md 与 xlsx 分开计（文件名规则、载体、读法都不同）
MAX_MD_FILES_PER_SESSION = _int_env("MAX_MD_FILES_PER_SESSION", 10)
MAX_XLSX_FILES_PER_SESSION = _int_env("MAX_XLSX_FILES_PER_SESSION", 20)
# 每用户二进制总量：会话数 × 每会话文件数相乘会失控，必须有这道闸
MAX_BYTES_PER_USER = _int_env("MAX_BLOB_MB_PER_USER", 2048) * 1024 * 1024
# 单个 xlsx 的 HTTP 层上限（与 md 的 MAX_UPLOAD_MB 分开）
MAX_BLOB_MB = _int_env("MAX_BLOB_MB", 10)
MAX_BLOB_BYTES = MAX_BLOB_MB * 1024 * 1024

_XLSX_SUFFIX = ".xlsx"


class QuotaExceeded(Exception):
    """配额不足；消息直接回给前端做 detail。"""


def parse_space(name: str) -> str:
    """文件名 → 空间名。规则见 `CONTEXT.md`「Table rule」：`<空间名>_<表名>.xlsx`。

    只解析、不判权限——`space ∈ user_spaces` 由调用方校验（它才有 config）。
    """
    stem = name[: -len(_XLSX_SUFFIX)] if name.lower().endswith(_XLSX_SUFFIX) else name
    space, sep, rest = stem.partition("_")
    if not sep or not space or not rest:
        raise ValueError("文件名必须是 <空间名>_<表名>.xlsx")
    return space


def quota_violation(
    *,
    used_bytes: int,
    old_bytes: int,
    new_bytes: int,
    thread_count: int,
    is_overwrite: bool,
) -> str | None:
    """返回拒绝原因，或 None 表示放行。纯函数：不碰 DB，便于覆盖边界。"""
    total = used_bytes - old_bytes + new_bytes
    if total > MAX_BYTES_PER_USER:
        return (
            f"二进制文件总量超出上限（{MAX_BYTES_PER_USER // (1024 * 1024)}MB），"
            f"本次上传后约 {total // (1024 * 1024)}MB，请删掉不用的文件"
        )
    if not is_overwrite and thread_count >= MAX_XLSX_FILES_PER_SESSION:
        return f"本会话的 xlsx 文件数已达上限（{MAX_XLSX_FILES_PER_SESSION}），同名覆盖不受限"
    return None


async def upsert(
    session: AsyncSession,
    *,
    owner: str,
    thread_id: str,
    name: str,
    space: str,
    object_key: str,
    sha256: str,
    size_bytes: int,
) -> str | None:
    """写一行（同名覆盖），返回**被替换掉的旧 object_key**（没有则 None）。

    调用方在事务提交后再决定是否删旧对象：只有 `旧 key != 新 key` 时才该删，
    否则"同名同内容重传恰好卡在配额"会把旧行仍在引用的字节删掉。
    """
    # 锁用户行：串行化同一个人的并发上传；行不存在（数据异常）也照样往下走
    await session.execute(select(UserTables.identity).where(UserTables.identity == owner).with_for_update())

    used = await session.scalar(
        select(func.coalesce(func.sum(SessionObject.size_bytes), 0)).where(SessionObject.owner_identity == owner)
    )
    old = await session.get(SessionObject, (owner, thread_id, name))
    count = await session.scalar(
        select(func.count())
        .select_from(SessionObject)
        .where(SessionObject.owner_identity == owner, SessionObject.thread_id == thread_id)
    )

    message = quota_violation(
        used_bytes=int(used or 0),
        old_bytes=old.size_bytes if old else 0,
        new_bytes=size_bytes,
        thread_count=int(count or 0),
        is_overwrite=old is not None,
    )
    if message:
        raise QuotaExceeded(message)

    if old is None:
        session.add(
            SessionObject(
                owner_identity=owner,
                thread_id=thread_id,
                name=name,
                space=space,
                object_key=object_key,
                sha256=sha256,
                size_bytes=size_bytes,
            )
        )
        old_key = None
    else:
        old_key = old.object_key
        old.space = space
        old.object_key = object_key
        old.sha256 = sha256
        old.size_bytes = size_bytes
    await session.flush()
    return old_key


async def get_one(session: AsyncSession, owner: str, thread_id: str, name: str) -> SessionObject | None:
    return await session.get(SessionObject, (owner, thread_id, name))


async def list_for_thread(session: AsyncSession, owner: str, thread_id: str) -> list[SessionObject]:
    rows = await session.scalars(
        select(SessionObject)
        .where(SessionObject.owner_identity == owner, SessionObject.thread_id == thread_id)
        .order_by(SessionObject.name)
    )
    return list(rows)


async def as_dicts(session: AsyncSession, owner: str, thread_id: str) -> list[dict[str, Any]]:
    """给前端/中间件用的清单形状（与会话文件清单同一套字段风格）。"""
    return [
        {
            "name": r.name,
            "path": f"/uploads/{r.name}",
            "space": r.space,
            "bytes": r.size_bytes,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in await list_for_thread(session, owner, thread_id)
    ]


async def delete_for_thread(session: AsyncSession, owner: str, thread_id: str) -> int:
    """删掉这个会话的全部行（删会话时调用）。对象由调用方按前缀清。"""
    rows = await list_for_thread(session, owner, thread_id)
    for row in rows:
        await session.delete(row)
    await session.flush()
    return len(rows)
