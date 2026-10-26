"""JWT 的铸造：用户 access token 与服务身份长期 token 的唯一实现。

两侧都要：`custom/`（登录时签发、代理调用时铸服务 token）与 `graphs/`（派活前铸服务
token）。放 `shared/` 而不是 `custom/`：`graphs/supervisor.py` 在**模块加载期**就要
`service_token()`，让它 import 整个 HTTP 包会把 FastAPI 侧拖进图的启动路径。

这里只放「签发」，不放校验：校验要按调用方映射成不同的异常（Auth handler 抛
`AuthHTTPException`，FastAPI 依赖抛 `HTTPException`），所以两种包装各自留在
`custom/middleware/auth.py` 与 `custom/service/account.py`。
"""

from __future__ import annotations

import os
import time

import jwt

from shared import config

# access token 有效期（秒）；非密钥，配在 config.yaml 的 auth.access_token_ttl
TOKEN_TTL = int(config.get("auth.access_token_ttl"))

# 内部服务账号（async subagent 的运行身份）：非密钥，配在 config.yaml。
# 只有它能在服务端读子线程 / 订阅子 run；custom 侧的 Auth handler 对它免查库放行。
SERVICE_IDENTITY = config.get("auth.service_identity")

_service_token_cache: str | None = None


def create_access_token(identity: str) -> str:
    now = int(time.time())
    payload = {"sub": identity, "iat": now, "exp": now + TOKEN_TTL}
    return jwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")


def service_token() -> str:
    """服务身份的长期 token（进程级缓存）。

    custom 的 Auth handler 对 SERVICE_IDENTITY 免 DB 直接放行；子 agent 的 thread 属主
    就是它，所以只有它能读子线程/订阅子 run。shortcut: 10 年有效、与服务同生共死；
    要轮换时改 env 或改成启动时铸造。
    """
    global _service_token_cache
    if _service_token_cache is None:
        now = int(time.time())
        _service_token_cache = jwt.encode(
            {"sub": SERVICE_IDENTITY, "iat": now, "exp": now + 10 * 365 * 24 * 3600},
            os.environ["JWT_SECRET"],
            algorithm="HS256",
        )
    return _service_token_cache


__all__ = ["SERVICE_IDENTITY", "TOKEN_TTL", "create_access_token", "service_token"]
