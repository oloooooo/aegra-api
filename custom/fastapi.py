from contextlib import asynccontextmanager

from fastapi import FastAPI

from custom.delivery_api import router as delivery_router
from custom.frontend_api import router as frontend_router
from custom.outbox_dispatcher import dispatcher as outbox_dispatcher
from custom.routers import router as userspace_router
from custom.task_control_api import router as task_control_router
from custom.threads_delete import (
    assert_shadows_core_delete,
)
from custom.threads_delete import (
    router as threads_delete_router,
)
from custom.uploads import router as uploads_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动自检 + 发件箱分发者（ADR-0010）。

    删会话的清理依赖路由注册顺序遮挡（ADR-0013）。
    分发者每个进程跑一个，多进程靠 SKIP LOCKED + 租约分工。
    """
    assert_shadows_core_delete(app)
    await outbox_dispatcher.start()
    try:
        yield
    finally:
        await outbox_dispatcher.stop()


app = FastAPI(lifespan=lifespan)

app.include_router(userspace_router)
app.include_router(frontend_router)
app.include_router(delivery_router)
app.include_router(task_control_router)
app.include_router(uploads_router)
# 必须在核心路由 include 之前注册，才盖得住原生 delete_thread
app.include_router(threads_delete_router)


def _openapi_with_bearer():
    """给 OpenAPI 补上 bearer securitySchemes，让 /docs 出现 Authorize 按钮。

    Aegra 的鉴权走中间件 + require_auth(Request)，FastAPI 生成 schema 时
    看不到它们，所以这里手动声明；全局 security 让每次 Try it out 都带上 token。
    """
    if not app.openapi_schema:
        schema = FastAPI.openapi(app)
        schema.setdefault("components", {})["securitySchemes"] = {
            "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        }
        schema["security"] = [{"bearerAuth": []}]
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _openapi_with_bearer
