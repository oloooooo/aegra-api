"""会话文件上传的 e2e（ADR-0015 / ADR-0017）：真服务器 + 真 Postgres + 真对象存储。

不用图跑一轮（那要 LLM，慢且不稳）：断言分两层——接口层面的行为，以及真实落库
（PG `session_object` 行 + 对象存储里的字节）。

**需要对象存储**：md 与 xlsx 现在同一条链，没有 `s3.endpoint` 时上传接口整体 503，
所以涉及字节的断言在未配置对象存储时会 skip（接口层的 415/422/404 仍可跑）。
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import psycopg
import pytest

from aegra_api.settings import settings
from shared.session import blobs

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


def _ledger(identity: str, thread_id: str) -> list[tuple[str, Any, str, int]]:
    """按台账读这个会话的文件：(name, space, object_key, size_bytes)。"""
    with psycopg.connect(settings.db.database_url_sync) as conn:
        return conn.execute(
            "SELECT name, space, object_key, size_bytes FROM session_object"
            " WHERE owner_identity = %s AND thread_id = %s ORDER BY name",
            (identity, thread_id),
        ).fetchall()


def _object(key: str) -> bytes | None:
    got = blobs.get_object(key)
    return None if got is None else got[0]


@pytest.fixture(autouse=True)
def _require_object_storage():
    if not blobs.configured():
        pytest.skip("对象存储未配置（s3.endpoint 为空）：上传接口本来就不可用")


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


async def test_upload_lands_in_ledger_and_object_storage(http: httpx.AsyncClient) -> None:
    who = await _register_and_login(http)
    thread_id = await _create_thread(http, who["token"])

    content = "# 结论\n用 A 方案\n".encode()
    r = await _upload(http, who["token"], thread_id, "会议纪要.md", content)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "/uploads/会议纪要.md"
    assert body["name"] == "会议纪要.md"
    assert body["bytes"] == len(content)
    assert "ttl_minutes" not in body, "md 不再有 TTL（ADR-0017 决定 10）"

    rows = _ledger(who["identity"], thread_id)
    assert [row[0] for row in rows] == ["会议纪要.md"]
    name, space, key, size = rows[0]
    assert space is None, "md 不属于任何空间"
    assert key.startswith("uploads/") and key.endswith("/会议纪要.md")
    assert size == len(content) and _object(key) == content


async def test_reupload_same_name_overwrites(http: httpx.AsyncClient) -> None:
    who = await _register_and_login(http)
    thread_id = await _create_thread(http, who["token"])

    await _upload(http, who["token"], thread_id, "a.md", b"v1")
    first = _ledger(who["identity"], thread_id)[0][2]
    await _upload(http, who["token"], thread_id, "a.md", b"v2")

    rows = _ledger(who["identity"], thread_id)
    assert len(rows) == 1, "同名覆盖（PUT 语义），不做版本链"
    second = rows[0][2]
    assert second != first, "每次上传新 key"
    assert _object(second) == b"v2"
    assert _object(first) is None, "旧对象在提交成功后删掉"


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
    assert _ledger(other["identity"], thread_id) == []


async def test_delete_thread_clears_uploads(http: httpx.AsyncClient) -> None:
    who = await _register_and_login(http)
    thread_id = await _create_thread(http, who["token"])
    await _upload(http, who["token"], thread_id, "a.md", b"hello")
    key = _ledger(who["identity"], thread_id)[0][2]
    assert _object(key) == b"hello"

    r = await http.delete(f"/threads/{thread_id}", headers=_auth(who["token"]))
    assert r.status_code == 200, r.text
    assert _ledger(who["identity"], thread_id) == [], "先删行"
    assert _object(key) is None, "再删对象"


async def test_non_ascii_identity_can_upload(http: httpx.AsyncClient) -> None:
    """身份允许中文（对象 key 里没有身份，所以不可能撞上 ASCII 限制）。"""
    identity = f"张三-{uuid.uuid4().hex[:6]}"
    password = _password()
    r = await http.post(
        "/custom/register",
        json={"identity": identity, "display_name": "中文", "password": password},
    )
    assert r.status_code == 200, r.text
    token = (await http.post("/custom/login", json={"identity": identity, "password": password})).json()[
        "access_token"
    ]

    thread_id = await _create_thread(http, token)
    r = await _upload(http, token, thread_id, "报告.md", b"x")
    assert r.status_code == 200, r.text
    assert len(_ledger(identity, thread_id)) == 1


async def test_upload_is_readable_through_uploads_backend(http: httpx.AsyncClient) -> None:
    """真台账 + 真对象存储 + 真 backend：证明写进去的东西 agent 下得出来。

    这条不经过模型：台账里没有这个会话的「作用域」就查不到 key，key 写错就读不到字节。
    """
    import graphs.access.s3_backend as sb

    who = await _register_and_login(http)
    thread_id = await _create_thread(http, who["token"])
    await _upload(http, who["token"], thread_id, "readme.md", b"# T body")

    # 图外没有 run config：直接把「我这次 run 是谁」喂给 backend 的取作用域入口
    original = sb.session_scope.current_config
    sb.session_scope.current_config = lambda: {
        "configurable": {"thread_id": thread_id, "langgraph_auth_user": {"identity": who["identity"]}}
    }
    try:
        res = await sb.UploadsBackend().adownload_files(["/readme.md"])
    finally:
        sb.session_scope.current_config = original

    assert res[0].error is None, res[0].error
    assert res[0].content == b"# T body"
