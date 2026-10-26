"""LangGraph SDK 客户端缓存：同一 (base_url, token) 复用连接池。

服务 token 只在服务端内部使用，不下发给浏览器（见 docs/streaming.md §7）。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from langgraph_sdk import get_client

from shared.token import service_token

_CLIENTS: dict[tuple[str, str], Any] = {}
# shortcut: 超过上限时丢弃最旧条目但不显式 aclose()，连接由 GC 回收；
#           并发用户数远大于 8 时才需要改成带关闭的 LRU。
_CLIENT_CACHE_MAX = 8


def _client(request: Request, token: str) -> Any:
    """按 (base_url, token) 复用 SDK 客户端；token 相同即同一身份，可安全共享。"""
    key = (str(request.base_url).rstrip("/"), token)
    client = _CLIENTS.get(key)
    if client is None:
        if len(_CLIENTS) >= _CLIENT_CACHE_MAX:
            _CLIENTS.pop(next(iter(_CLIENTS)))
        client = get_client(url=key[0], headers={"Authorization": f"Bearer {token}"})
        _CLIENTS[key] = client
    return client


def _service_client(request: Request) -> Any:
    """服务身份客户端（能读子 agent 的线程）。"""
    return _client(request, service_token())


def _user_client(request: Request) -> Any:
    """用户身份客户端：借它走原生接口，顺带享受 ownership 校验。"""
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="缺少令牌")
    return _client(request, token)
