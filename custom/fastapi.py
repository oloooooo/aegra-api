from fastapi import FastAPI
from custom.routers import router as userspace_router
from custom.frontend_api import router as frontend_router
app = FastAPI()

app.include_router(userspace_router)
app.include_router(frontend_router)


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