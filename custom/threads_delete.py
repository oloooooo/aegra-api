"""删会话时连带清掉它的会话文件（ADR-0013）。

为什么用「注册一条同名路由遮挡核心路由」：aegra 的 `delete_thread` 只删 checkpoint 与
thread 行，不认识 store。`src/` 默认不动，所以把清理放在这一层。可行是因为
`custom/fastapi.py` 的 app 是**基础 app**，核心路由是在 `_include_core_routers()` 里
之后才 `include_router` 的，Starlette 按注册顺序匹配——先注册的先命中。

这是**隐式顺序依赖**，aegra 升级改了装载顺序就会静默失效（回到"删会话留垃圾"）。
所以 `assert_shadows_core_delete` 在启动时核对首个匹配确实是我们这条，不对就启动失败。

业务逻辑不重写：直接调原生 `delete_thread`，它内部的鉴权（ownership + `@auth.on.threads.delete`
级联删子线程）照旧跑。它 404 就代表不是这个用户的会话，我们也就不会去碰 store。
"""

from __future__ import annotations

from typing import Any, Annotated

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.api.threads import delete_thread as core_delete_thread
# 必须用 require_auth：get_current_user 读 scope["user"]，只有 AuthenticationMiddleware
# 或先跑过 require_auth 才有值，而 aegra 用的是自己的 auth 中间件（copy 自 custom/routers.py）
from aegra_api.core.auth_deps import require_auth
from aegra_api.core.database import db_manager
from aegra_api.core.orm import get_session
from aegra_api.models.auth import User
from custom.session_store import clear

logger = structlog.getLogger(__name__)

router = APIRouter(tags=["/threads"])

_SHADOW_PATH = "/threads/{thread_id}"


@router.delete(_SHADOW_PATH, include_in_schema=False)
async def delete_thread(
    thread_id: str,
    user: Annotated[User, Depends(require_auth)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """核心删除 + 会话文件清理。"""
    result = await core_delete_thread(thread_id, user, session)
    try:
        store = db_manager.get_store()
    except RuntimeError:  # 初始化未完成：文件留着，靠孤儿保留期兜底（ADR-0013）
        logger.warning("session_files_clear_skipped_no_store", thread_id=thread_id)
        return result
    try:
        removed = await clear(store, user.identity, thread_id)
        if removed:
            logger.info(
                "session_files_cleared",
                identity=user.identity,
                thread_id=thread_id,
                removed=removed,
            )
    except Exception:  # noqa: BLE001 — thread 行已经删了，这里 500 只会让客户端重试成 404
        # 残留文件由孤儿保留期清理；比"删成功却报错"更可取
        logger.exception("session_files_clear_failed", identity=user.identity, thread_id=thread_id)
    return result


# ---------- 启动断言 ----------


def _iter_routes(router: Any, prefix: str = "") -> Any:
    """递归遍历路由（FastAPI >=0.137 用 `_IncludedRouter` 嵌套，没有 .path）。"""
    for route in getattr(router, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = getattr(route, "include_context", None)
            yield from _iter_routes(inner, prefix + (getattr(ctx, "prefix", "") or ""))
        elif getattr(route, "path", None) is not None:
            yield prefix + route.path, (getattr(route, "methods", None) or set()), route


def assert_shadows_core_delete(app: Any) -> None:
    """核对 `DELETE /threads/{thread_id}` 的首个匹配是本模块的路由。

    首个匹配不是我们 → 清理逻辑不会执行，而且**不会报错**，只会静默留垃圾。
    宁可启动失败。
    """
    for path, methods, route in _iter_routes(app.router):
        if path == _SHADOW_PATH and "DELETE" in methods:
            if getattr(route, "endpoint", None) is not delete_thread:
                raise RuntimeError(
                    "DELETE /threads/{thread_id} 首次匹配不是 custom 的清理路由，"
                    "会话文件将不会被删除（ADR-0013）。检查 custom app 与核心路由的装载顺序。"
                )
            return
    raise RuntimeError("没有找到 DELETE /threads/{thread_id}：路由未注册，会话文件不会被清理")
