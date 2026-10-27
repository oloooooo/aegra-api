"""会话文件上传的总入口：两个接口、同一个存储链路、一套会话隔离。

===============================================================================
一、两个接口 / 一个载体
===============================================================================

    POST /custom/uploads          md   → 对象存储（字节）+ PG `session_object`（行）
    POST /custom/uploads/tables   xlsx → 对象存储（字节）+ PG `session_object`（行）

两者只差三件事：文件名规则（xlsx 必须 `<空间名>_<表名>.xlsx`，md 随便）、大小闸
（`max_upload_mb` vs `max_blob_mb`）、以及 md 的 `space` 为 NULL。字节与行都是同一套
代码写、同一条读路径读：agent 读文件靠 backend（`/uploads/` 路由到 `UploadsBackend`，
key 查台账，ADR-0017），xlsx 的二进制额外由 xlsx 四件套解析。见 ADR-0015 / ADR-0017。

为什么 md 曾经在 LangGraph Store：那是 ADR-0013 的方案（`StoreBackend` 直接当 md 的
backend）。换掉的原因见 ADR-0017——两个后端就意味着两套可见性，`/uploads/` 一旦换路由，
Store 里的 md 立刻变得谁都看不见。

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
      │ 4. list_for_thread()  数一遍现有**同类**文件；**同名覆盖不占名额**
      │ 5. put_bytes()        字节进对象存储 `uploads/<uuid>/<名>`
      │ 6. upsert()           锁用户行算配额 → 写/覆盖 `session_object` 行（space = NULL）→ commit
      ▼
    {"name": …, "path": "/uploads/<name>", "bytes": …}

同名 = 覆盖（PUT 语义）：`ls /uploads` 里永远只有一份，不做版本链。md 与 xlsx 走同一条存储
链路，只是 md 不属于任何空间（台账里 `space` 为 NULL）。

===============================================================================
四、xlsx 分支
===============================================================================

    POST /custom/uploads/tables (file=<空间名>_<表名>.xlsx, thread_id)
      │ 1. file.size 先挡一次           不读身体就能 413
      │ 2. _safe_name(suffix=".xlsx") + parse_space()
      │      文件名前缀必须是上传者拥有的 space —— 但它**只决定主管派哪个子 agent**，
      │      不决定存哪（key 恒为 uploads/<uuid>/<名>，归属只在台账）
      │ 3. put_stream(file, tmp/…)       手写分片：边读边算 sha256；超限自己 abort
      │ 4. copy(tmp, uploads/<uuid>/<name>)   服务端复制，不搬字节
      │ 5. session_files.upsert()        锁用户行算配额 → 写/覆盖行 → commit
      │ 6. 删 tmp；**提交成功后**再删被替换掉的旧对象
      ▼
    201 {"name", "path": "/uploads/<name>", "space", "bytes", "sha256", "indexed": false}

（`indexed` 恒为 false：索引是缓存，第一次调工具时才建，见 graphs/tools/xlsx/index.py。）

**写序不能换**：tmp → 正式 key → PG 事务 → 删 tmp。任何一步崩都只会留垃圾对象，
不会出现「行说 A、字节是 B」。正式 key 是 `uploads/<uuid>/<名>`（`blobs.upload_key`）：
每次上传一个新对象，提交成功后删被替掉的旧对象；行与字节的对应关系只在
`session_object` 台账里。

**失败清理只删「本次新写、且不是旧行正在用的」对象**（`_drop_new`）。清理自身必须
吞异常——它一抛，就把「存储不可用 → 503」变成「未知错误 → 500」（实测踩过）。

===============================================================================
五、落到哪里 / 谁能读到
===============================================================================

    md / xlsx  对象存储 `uploads/<uuid>/<名>`（`blobs.upload_key`，uuid 每次新分配）
               归属只在 PG `session_object`：主键 (owner, thread, name)
               读：SessionFilesMiddleware（清单注入）+ UploadsBackend（ls/read/grep/glob/
                   download_files，key 每次查台账）；xlsx 的二进制再交给 xlsx 四件套解析

子 agent 读**同一条会话**的文件靠派活时签发的 `session_scope`（graphs/access/session_scope.py），
它读不到别的主管的会话。

key 里**没有** owner/thread：归属由台账唯一决定，所以 key 不能反推身份（否则台账与 key
就成了两本账，改名/迁移得同时改对两处）。

===============================================================================
六、删除与垃圾（各自的清理责任，别在这里加「顺手删」）
===============================================================================

    删会话      custom/middleware/threads_delete.py：先删行 → 再删对象 → 清 Store（历史遗留）
    上传残留    tmp/ 对象与未完成分片 → scripts/reap_multipart.py（定期跑）
    容量对账    scripts/reap_orphans.py：孤儿对象 / 悬空行（缺省只报告）

===============================================================================
七、代码分区（改代码前先看这张表）
===============================================================================

  A. 闸与工具    _MAX_BYTES, _CHUNK, _BAD_NAME, _safe_name(), _read_limited(),
                 _require_owned_thread()
  B. md 分支     upload_session_file()
  C. xlsx 分支   upload_session_table(), _drop_new()

限额、S3 形态、对账与排错见根目录 `UPLOAD.md`；实现细节见 `docs/large-files.md`。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from pathlib import PureWindowsPath
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import get_session
from aegra_api.models.auth import User
from custom.api.schemas import UploadFileResponse, UploadTableResponse
from custom.service import session_files
from custom.service.session_files import MAX_FILES_PER_SESSION, MAX_UPLOAD_MB
from shared.session import blobs

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

    与 xlsx 同一条链（ADR-0017）：字节进对象存储 `uploads/<uuid>/<名>`，行进 `session_object`
    （`space` 为 NULL——md 不属于任何空间）。同名覆盖（PUT 语义），不做版本链。
    """
    await _require_owned_thread(session, user, thread_id)

    name = _safe_name(file.filename)
    data = await _read_limited(file)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=422,
            detail=f"文件不是合法 UTF-8 文本（第 {e.start} 字节处解码失败）",
        ) from None

    # 同类文件数上限：同名重传是覆盖，不占新名额（字节配额在 upsert 里锁用户行统一算）
    rows = await session_files.list_for_thread(session, user.identity, thread_id)
    if len(rows) >= MAX_FILES_PER_SESSION and name not in {r.name for r in rows}:
        raise HTTPException(
            status_code=413,
            detail=f"本会话文件数已达上限（{MAX_FILES_PER_SESSION}），同名覆盖不受限",
        )

    existing = await session_files.get_one(session, user.identity, thread_id, name)
    old_key = existing.object_key if existing else None
    final = blobs.upload_key(name)
    try:
        await asyncio.to_thread(blobs.put_bytes, final, data)
        await session_files.upsert(
            session,
            owner=user.identity,
            thread_id=thread_id,
            name=name,
            space=None,
            object_key=final,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
        await session.commit()
    except blobs.BlobUnavailable:
        logger.exception("upload_md_without_store", thread_id=thread_id)
        await _drop_new(final, old_key)
        raise HTTPException(status_code=503, detail="存储未就绪，请稍后重试") from None
    except session_files.QuotaExceeded as e:
        await _drop_new(final, old_key)
        raise HTTPException(status_code=413, detail=str(e)) from None
    except Exception:
        logger.exception("upload_md_failed", thread_id=thread_id, name=name)
        await _drop_new(final, old_key)
        raise

    # 提交成功后才删被替换掉的旧对象
    if old_key and old_key != final:
        await asyncio.to_thread(blobs.delete, old_key)

    logger.info(
        "session_file_uploaded",
        identity=user.identity,
        thread_id=thread_id,
        name=name,
        bytes=len(data),
    )
    return UploadFileResponse(name=name, path=f"/uploads/{name}", bytes=len(data))


@router.post("/uploads/tables", response_model=UploadTableResponse, status_code=201)
async def upload_session_table(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
    thread_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> UploadTableResponse:
    """把一个 xlsx 挂到会话上（ADR-0015）：字节进对象存储，行进 `session_object`。

    文件名必须是 `<空间名>_<表名>.xlsx`；空间名校验的是**上传者有没有该空间**，
    它只决定主管派哪个子 agent，不决定存哪——key 恒为 `uploads/<uuid>/<名>`，`space` 只是台账一列。

    写序：tmp → copy 到正式 key → PG 事务 → 删 tmp。中途崩只会留孤儿对象，
    不会出现"有行没字节"；提交失败时只在 `新 key != 旧 key` 时删新对象
    （同名同内容重传恰好卡在配额，删了就把旧行唯一的那份字节删掉了）。
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

    tmp = blobs.tmp_key(name)
    final: str | None = None
    try:
        sha256, size = await asyncio.to_thread(
            blobs.put_stream, file.file, tmp, max_bytes=session_files.MAX_BLOB_BYTES
        )
        final = blobs.upload_key(name)
        await asyncio.to_thread(blobs.copy, tmp, final)
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
    except blobs.BlobTooLarge as e:
        await _drop_new(final, old_key)
        raise HTTPException(status_code=413, detail=str(e)) from None
    except blobs.BlobUnavailable:
        logger.exception("upload_table_without_store", thread_id=thread_id)
        await _drop_new(final, old_key)
        raise HTTPException(status_code=503, detail="存储未就绪，请稍后重试") from None
    except session_files.QuotaExceeded as e:
        await _drop_new(final, old_key)
        raise HTTPException(status_code=413, detail=str(e)) from None
    except Exception:
        logger.exception("upload_table_failed", thread_id=thread_id, name=name)
        await _drop_new(final, old_key)
        raise
    finally:
        # 清理不能抛：它一旦抛出去，就把「上传成不成功」的结论盖掉了（曾经把 503 变成 500）
        with contextlib.suppress(Exception):
            await asyncio.to_thread(blobs.delete, tmp)

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
    """失败清理：只删本次新写的对象，且它不能是旧行正在用的那一个。

    自身必须吞异常：存储已经坏了（这往往就是进到这里的原因），再抛一次只会把
    「存储不可用 → 503」变成「未知错误 → 500」。
    """
    if final and final != old_key:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(blobs.delete, final)
