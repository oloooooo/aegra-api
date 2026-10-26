"""前端图清单：只列 aegra.json 里注册的图。

为什么不复用 LangGraph 原生 ``GET /assistants``（见 docs/streaming.md §7）：它会把所有
历史 assistant 都吐出来（含已删除的图和示例图），前端需要的只是当前配置里的那几个。
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends

from aegra_api.core.auth_deps import require_auth
from aegra_api.models.auth import User
from shared import config

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["/custom"])

_PROJECT_ROOT = config.ROOT
_GRAPH_IDS_CACHE: dict[str, tuple[int, list[str]]] = {}


def _graph_ids() -> list[str]:
    """aegra.json 里注册的图（启动时读的那份配置）。

    按 mtime 缓存：配置改了（通常需重启）不必每请求重读；配置坏了要留痕，
    不能静默变成空列表——前端会以为「没有可用助手」。
    """
    cfg = _PROJECT_ROOT / os.environ.get("AEGRA_CONFIG", "aegra.json")
    try:
        mtime = cfg.stat().st_mtime_ns
    except OSError:
        logger.warning("aegra_config_missing", path=str(cfg))
        return []
    cached = _GRAPH_IDS_CACHE.get("v")
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("aegra_config_unreadable", path=str(cfg), error=str(e))
        return []
    graphs = data.get("graphs")
    if not isinstance(graphs, dict) or not graphs:
        logger.warning("aegra_config_no_graphs", path=str(cfg), keys=sorted(data))
        return []
    ids = sorted(graphs)
    _GRAPH_IDS_CACHE["v"] = (mtime, ids)
    return ids


@router.get("/graphs")
async def list_graphs(user: Annotated[User, Depends(require_auth)]) -> dict[str, Any]:
    """前端可选的助手：只列 aegra.json 注册的图，不含历史/示例 assistant。"""
    return {"graphs": _graph_ids(), "identity": user.identity, "permissions": user.permissions}
