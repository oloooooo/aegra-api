"""会话文件上传的 e2e（ADR-0013）：真服务器 + 真 Postgres + 真 Redis。

不用图跑一轮（那要 LLM，慢且不稳）：断言分两层——
接口层面的行为，以及 store 表里的真实落库（prefix / key / ttl_minutes / 过期时间）。
"""

from __future__ import annotations

import hashlib

import uuid
from typing import Any

import httpx
import psycopg
import pytest

from aegra_api.settings import settings

pytestmark = pytest.mark.e2e

_BASE = settings.app.SERVER_URL


def _password() -> str:
    # 测试用户随手建，密码只要过 bcrypt；不与真实账号冲突
    return f"e2e-{uuid.uuid4().hex[:12]}"


async def _register_and_login(http: httpx.AsyncClient) -> dict[str, str]:
    identity = f"e2e-upload-{uuid.uuid4().hex[:10]}"
    password = _password()
    r = await http.post(
        "/custom/register",
        json={"identity": identity, "display_name": "E2E", "password": password},
    )
    assert r.status_code == 200, r.text
    r = await http.post("/custom/login", json={"identity": identity, "password": password})
    assert r.status_code == 200, r.text
    return {"identity": identity, "token": r.json()["access_token"]}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_thread(http: httpx.AsyncClient, token: str) -> str:
    r = await http.post("/threads", json={}, headers=_auth(token))
    assert r.status_code in (200, 201), r.text
    return r.json()["thread_id"]


def _namespace(identity: str, thread_id: str) -> str:
    """与 custom/session_store.py 同一套推导：点点拼成 store 的 prefix 列。"""
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"uploads.{digest}.{thread_id}"


def _store_rows(prefix: str) -> list[tuple[str, Any, Any, Any]]:
    with psycopg.connect(settings.db.database_url_sync) as conn:
        return conn.execute(
            "SELECT key, ttl_minutes, expires_at, updated_at FROM store WHERE prefix = %s ORDER BY key",
            (prefix,),
        ).fetchall()


async def _upload(
    http: httpx.AsyncClient, token: str, thread_id: str, name: str, content: bytes
) -> httpx.Response:
    return await http.post(
        "/custom/uploads",
        headers=_auth(token),
        data={"thread_id": thread_id},
        files={"file": (name, content, "text/markdown")},
    )


@pytest.fixture
async def http():
    async with httpx.AsyncClient(base_url=_BASE, timeout=30.0) as client:
        try:
            await client.get("/health")
        except httpx.HTTPError:  # 服务器没起：e2e 本来就该跳过
            pytest.skip(f"server not reachable at {_BASE}")
        yield client


async def test_upload_lands_in_store_with_ttl(http: httpx.AsyncClient) -> None:
    who = await _register_and_login(http)
    thread_id = await _create_thread(http, who["token"])

    r = await _upload(http, who["token"], thread_id, "会议纪要.md", "# 结论\n用 A 方案\n".encode())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "/uploads/会议纪要.md"
    assert body["name"] == "会议纪要.md"
    assert body["bytes"] > 0
    assert body["ttl_minutes"] >= 1

    rows = _store_rows(_namespace(who["identity"], thread_id))
    assert [row[0] for row in rows] == ["/会议纪要.md"], "key 必须带前导斜杠，否则 /uploads/x.md 读不到"
    _, ttl_minutes, expires_at, _ = rows[0]
    assert ttl_minutes == body["ttl_minutes"]
    assert expires_at is not None, "必须写 expires_at，否则 sweeper 无从清理（ADR-0013）"


async def test_reupload_same_name_overwrites(http: httpx.AsyncClient) -> None:
    who = await _register_and_login(http)
    thread_id = await _create_thread(http, who["token"])

    await _upload(http, who["token"], thread_id, "a.md", b"v1")
    await _upload(http, who["token"], thread_id, "a.md", b"v2")
    rows = _store_rows(_namespace(who["identity"], thread_id))
    assert len(rows) == 1, "同名覆盖（PUT 语义），不做版本链"


async def test_rejects_non_md(http: httpx.AsyncClient) -> None:
    who = await _register_and_login(http)
    thread_id = await _create_thread(http, who["token"])
    r = await _upload(http, who["token"], thread_id, "a.xlsx", b"binary")
    assert r.status_code == 415


async def test_rejects_non_utf8(http: httpx.AsyncClient) -> None:
    who = await _register_and_login(http)
    thread_id = await _create_thread(http, who["token"])
    r = await _upload(http, who["token"], thread_id, "a.md", b"\xff\xfe\x00bad")
    assert r.status_code == 422


async def test_rejects_foreign_thread(http: httpx.AsyncClient) -> None:
    owner = await _register_and_login(http)
    other = await _register_and_login(http)
    thread_id = await _create_thread(http, owner["token"])

    # 别人的会话一律 404：不给"存在但无权"的信号
    r = await _upload(http, other["token"], thread_id, "a.md", b"x")
    assert r.status_code == 404
    assert _store_rows(_namespace(other["identity"], thread_id)) == []


async def test_delete_thread_clears_uploads(http: httpx.AsyncClient) -> None:
    who = await _register_and_login(http)
    thread_id = await _create_thread(http, who["token"])
    await _upload(http, who["token"], thread_id, "a.md", b"hello")
    prefix = _namespace(who["identity"], thread_id)
    assert len(_store_rows(prefix)) == 1

    r = await http.delete(f"/threads/{thread_id}", headers=_auth(who["token"]))
    assert r.status_code == 200, r.text
    # 遮挡路由生效才会清：只靠孤儿保留期的话这里还有行
    assert _store_rows(prefix) == []


async def test_store_prefix_is_hashed_identity(http: httpx.AsyncClient) -> None:
    """身份在命名空间里是 hash：中文账号名也过得了 StoreBackend 的 ASCII 校验。"""
    identity = f"张三-{uuid.uuid4().hex[:6]}"
    password = _password()
    r = await http.post(
        "/custom/register",
        json={"identity": identity, "display_name": "中文", "password": password},
    )
    assert r.status_code == 200, r.text
    token = (
        await http.post("/custom/login", json={"identity": identity, "password": password})
    ).json()["access_token"]

    thread_id = await _create_thread(http, token)
    r = await _upload(http, token, thread_id, "报告.md", b"x")
    assert r.status_code == 200, r.text

    prefix = _namespace(identity, thread_id)
    assert identity not in prefix and prefix.startswith("uploads.")
    assert len(_store_rows(prefix)) == 1


def test_store_sweeps_expired_items() -> None:
    """落库带 expires_at 只是半步；物理删除靠 sweeper（ADR-0013 / Q21）。

    这里不依赖 60 分钟的 tick：手写一行已过期的数据，再直接调 sweep_ttl()，
    验证 sweep 在当前 schema 上真的删得掉（也是 ttl_config 形状的验证）。
    """
    import uuid as _uuid

    from langgraph.store.base import TTLConfig
    from langgraph.store.postgres import PostgresStore
    from psycopg.types.json import Jsonb

    prefix = f"uploads.sweeptest.{_uuid.uuid4().hex[:8]}"
    with psycopg.connect(settings.db.database_url_sync) as conn:
        conn.execute(
            "INSERT INTO store (prefix, key, value, expires_at)" " VALUES (%s, %s, %s, now() - interval '1 hour')",
            (prefix, "/old.md", Jsonb({"content": "x", "encoding": "utf-8"})),
        )
        conn.commit()

    with PostgresStore.from_conn_string(
        settings.db.database_url_sync,
        ttl=TTLConfig(omit_expired=True, sweep_interval_minutes=60),
    ) as store:
        assert store.sweep_ttl() >= 1

    with psycopg.connect(settings.db.database_url_sync) as conn:
        left = conn.execute("SELECT count(*) FROM store WHERE prefix = %s", (prefix,)).fetchone()[0]
    assert left == 0


async def test_upload_is_readable_through_supervisor_backend(http: httpx.AsyncClient) -> None:
    """真 store + 真 CompositeBackend：证明写进去的东西 agent 用 read_file 读得出来。

    这条不经过模型：key 少一个前导斜杠、value 不符合 FileData 形状，都会在这里挂。
    """
    from deepagents.backends import CompositeBackend, StoreBackend
    from langgraph.store.postgres import PostgresStore

    from custom.session_store import session_namespace
    from graphs.deepagents_nodes import supervisor_backend

    who = await _register_and_login(http)
    thread_id = await _create_thread(http, who["token"])
    await _upload(http, who["token"], thread_id, "readme.md", b"# T body")

    # 用同步 store：Windows 的 ProactorEventLoop 跑不了 psycopg 的 async 连接（见 README #513）
    with PostgresStore.from_conn_string(settings.db.database_url_sync) as store:
        real = supervisor_backend()
        # 只替换 uploads 路由的 store（真进程里由 runtime 注入），namespace 推导保持一致
        backend = CompositeBackend(
            default=real.default,
            routes={
                "/uploads/": StoreBackend(
                    namespace=lambda _rt: session_namespace(who["identity"], thread_id),
                    store=store,
                )
            },
        )
        result = backend.read("/uploads/readme.md")
        assert result.error is None, result.error
        assert result.file_data is not None
        assert "body" in result.file_data["content"]

        # ls /uploads 也要看得见（清单注入靠 store.search，这条靠 backend.ls）
        listing = backend.ls("/uploads")
        # CompositeBackend 会把 route 前缀拼回路径
        assert any(e["path"] == "/uploads/readme.md" for e in (listing.entries or [])), listing
