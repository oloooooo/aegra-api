"""会话作用域：一次 run 能读哪条会话的文件（ADR-0015 §4.1）。

两个来源，**不要混**：

- 主管 run：`langgraph_auth_user.identity` + 服务端强制覆写的 `configurable.thread_id`，
  两者都是服务端注入的，可信。
- 子 agent run：`configurable.session_scope`，由 `AsyncTaskGuard` 在派活时**签发**。
  必须验签：`create_run_config` 只强制覆写 `thread_id/run_id/langgraph_auth_user`，
  其余 `configurable` 键客户端能自己塞，不验签就能读别人的会话文件。

为什么不直接把主管的 `langgraph_auth_user` 转发给子 agent：`allowed_spaces()` 会因此走
普通用户分支去读 `auth_user.user_spaces`，把子 agent 的权限从「本任务 space」放大成
「用户全部 space」（ADR-0005 的收窄失效）。所以转发的是一个窄凭证，不是身份。
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from graphs.space_access import _field, current_identity

_SCOPE_KEY = "session_scope"
_MISSING: Any = object()


def _secret() -> bytes:
    # 缺省值只为本地开发；生产必须配 SESSION_SCOPE_SECRET，否则重启即失效（能接受，
    # 因为凭证只在一次派活内使用，且派活前会重新签发）。
    return (os.environ.get("SESSION_SCOPE_SECRET") or "dev-session-scope").encode("utf-8")


def _sign(owner: str, thread_id: str) -> str:
    msg = f"{owner}\x00{thread_id}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def mint(owner: str, thread_id: str) -> dict[str, str]:
    """派活时签发的窄凭证：只对 `(owner, thread_id)` 这一条会话有效。"""
    return {"owner": owner, "thread_id": thread_id, "sig": _sign(owner, thread_id)}


def verify(scope: Any) -> tuple[str, str] | None:
    """校验并取出 `(owner, thread_id)`；不合法返回 None（调用方按"无会话"处理）。"""
    if not isinstance(scope, dict):
        return None
    owner = str(scope.get("owner") or "")
    thread_id = str(scope.get("thread_id") or "")
    sig = str(scope.get("sig") or "")
    if not owner or not thread_id or not sig:
        return None
    if not hmac.compare_digest(sig, _sign(owner, thread_id)):
        return None
    return owner, thread_id


def resolve(config: Any) -> tuple[str, str] | None:
    """当前 run 的会话作用域；没有（本地直跑 demo / 无鉴权）返回 None。"""
    cfg = (config or {}).get("configurable") or {}
    signed = verify(cfg.get(_SCOPE_KEY, _MISSING))
    if signed is not None:
        return signed
    if _SCOPE_KEY in cfg:
        # 带了 scope 但验签不过 —— 不退回"用自身身份"，直接当无权
        return None
    auth_user = cfg.get("langgraph_auth_user")
    if auth_user is None:
        return None
    thread_id = str(cfg.get("thread_id") or "")
    if not thread_id:
        return None
    identity = current_identity(config)
    if not identity or identity == "local":
        return None
    return identity, thread_id


def field(scope: Any, key: str, default: Any = None) -> Any:
    """dict / pydantic 都能取，保持与 `space_access._field` 一致的行为。"""
    return _field(scope, key, default)


_CONFIG_UNSET: Any = object()


def current_config() -> Any:
    """当前 run 的 config；图外（测试/脚本）返回空 dict。"""
    try:
        from langgraph.config import get_config

        return get_config() or {}
    except RuntimeError:
        return {}


def uploads_namespace(_runtime: Any = None) -> tuple[str, ...]:
    """`/uploads/` 路由的 Store 命名空间。

    主管 run 用自身的 `(identity, thread)`；子 agent 用派活时签发的 `session_scope`。
    拿不到作用域时返回一个**死命名空间**——读不到任何会话文件，也不会撞上真会话。
    """
    from custom.session_store import NAMESPACE_PREFIX, session_namespace

    scope = resolve(current_config())
    if scope is None:
        return (NAMESPACE_PREFIX, "_none", "_none")
    return session_namespace(*scope)
