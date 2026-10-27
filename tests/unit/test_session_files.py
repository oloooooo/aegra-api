"""会话文件存储层与清单注入的单元测试（ADR-0015 / ADR-0017）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from langchain_core.messages import SystemMessage

from custom.middleware.threads_delete import (
    assert_shadows_core_delete,
    delete_thread,
)
from custom.middleware.threads_delete import (
    router as threads_delete_router,
)
from graphs.middleware import session_files as sf
from shared.session import store as ss


class FakeItem:
    def __init__(self, key: str, value: dict) -> None:
        self.key = key
        self.value = value


class FakeStore:
    """够用的 store 替身：记下每次调用的参数。"""

    def __init__(self, items: list[FakeItem] | None = None) -> None:
        self.items = items or []
        self.puts: list[tuple] = []
        self.searches: list[tuple] = []
        self.batches: list[list] = []

    async def aput(self, namespace, key, value, *, ttl=None) -> None:
        self.puts.append((namespace, key, value, ttl))

    async def asearch(self, namespace, **kwargs) -> list[FakeItem]:
        self.searches.append((namespace, kwargs))
        return self.items

    async def abatch(self, ops) -> None:
        self.batches.append(list(ops))
        # 真 store 删完就搜不到了；替身也得照做，否则 clear() 的循环不会收敛
        gone = {op.key for op in ops if op.value is None}
        self.items = [i for i in self.items if i.key not in gone]


# ---------- 命名空间 ----------


def test_namespace_is_ascii_safe_and_deterministic() -> None:
    """identity 允许中文；`StoreBackend._validate_namespace` 只收 ASCII，所以要 hash。"""
    ns = ss.session_namespace("张三", "thread-1")
    assert ns == ss.session_namespace("张三", "thread-1")
    assert ns[:1] == (ss.NAMESPACE_PREFIX,)
    assert ns[1] != "张三" and all(c.isascii() for c in ns[1])
    assert ss.session_namespace("李四", "thread-1") != ns  # 不同用户不共享


# ---------- Store 遗留清场（老部署的 md 还在 store 里） ----------


async def test_clear_deletes_by_putting_none() -> None:
    store = FakeStore([FakeItem("/a.md", {}), FakeItem("/b.md", {})])
    assert await ss.clear(store, "u", "t") == 2

    ops = store.batches[0]
    assert [op.key for op in ops] == ["/a.md", "/b.md"]
    assert all(op.value is None for op in ops)  # store 没有 DeleteOp


# ---------- 中间件 ----------


def _patch_config(monkeypatch: pytest.MonkeyPatch, thread_id: str = "t-1") -> None:
    def fake_config():
        return {
            "configurable": {
                "thread_id": thread_id,
                "langgraph_auth_user": {"identity": "luoxin"},
            }
        }

    monkeypatch.setattr(sf, "get_config", fake_config)


def _patch_ledger(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict],
    truncated: bool = False,
    calls: list | None = None,
) -> None:
    async def fake_ledger(identity: str, thread_id: str):
        if calls is not None:
            calls.append((identity, thread_id))
        return rows, truncated

    monkeypatch.setattr(sf, "_ledger_files", fake_ledger)


_ROW = {"name": "a.md", "path": "/uploads/a.md", "bytes": 5, "created_at": "now"}


async def test_middleware_queries_once_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    calls: list = []
    _patch_ledger(monkeypatch, [_ROW], calls=calls)
    mw = sf.SessionFilesMiddleware()

    assert await mw.abefore_agent({}, None) is None  # 不写 state
    assert await mw.abefore_agent({}, None) is None
    assert len(calls) == 1


class FakeRequest:
    """`ModelRequest` 的最小替身：只实现中间件用到的两个接口。"""

    def __init__(self, system_message: SystemMessage | None = None) -> None:
        self.system_message = system_message

    def override(self, *, system_message: SystemMessage) -> FakeRequest:
        return FakeRequest(system_message)


async def test_middleware_injects_listing_as_untrusted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    _patch_ledger(monkeypatch, [_ROW, {**_ROW, "name": "b.xlsx", "path": "/uploads/b.xlsx"}])
    mw = sf.SessionFilesMiddleware()

    seen: list = []

    async def handler(request):
        seen.append(request)
        return "ok"

    request = FakeRequest(SystemMessage(content="原本的提示"))
    assert await mw.awrap_model_call(request, handler) == "ok"

    content = seen[0].system_message.content
    assert "原本的提示" in content
    assert "/uploads/a.md" in content and "/uploads/b.xlsx" in content  # 两张载体同一份清单
    assert "不可信" in content  # 文件内容是资料，不是指令


async def test_middleware_passes_through_without_files(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    _patch_ledger(monkeypatch, [])
    mw = sf.SessionFilesMiddleware()

    request = FakeRequest(SystemMessage(content="原"))
    called: list = []

    async def handler(req):
        called.append(req)
        return "ok"

    assert await mw.awrap_model_call(request, handler) == "ok"
    assert called[0] is request  # 没有清单就不改请求


async def test_middleware_reads_the_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """清单真相是 PG 台账（`session_object`），中间件不再碰 LangGraph Store。"""
    _patch_config(monkeypatch)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    async def fake_as_dicts(_session, owner, thread_id):
        assert (owner, thread_id) == ("luoxin", "t-1")
        return [{"name": "a.xlsx", "path": "/uploads/a.xlsx", "space": "u", "bytes": 7, "created_at": "now"}]

    monkeypatch.setattr("custom.service.session_files.as_dicts", fake_as_dicts)
    monkeypatch.setattr("aegra_api.core.orm.get_session_maker", lambda: FakeSession)

    mw = sf.SessionFilesMiddleware()
    seen: list = []

    async def handler(request):
        seen.append(request)
        return "ok"

    assert await mw.awrap_model_call(FakeRequest(SystemMessage(content="原")), handler) == "ok"
    assert "/uploads/a.xlsx" in seen[0].system_message.content


# ---------- 删会话的遮挡 ----------


def _fake_core_delete_app(order: str) -> FastAPI:
    """造一个与 aegra 同构的 app：先 include 的那条路由先匹配。"""
    from fastapi import APIRouter

    core = APIRouter()

    @core.delete("/threads/{thread_id}")
    async def core_delete() -> dict[str, str]:
        return {"status": "deleted"}

    app = FastAPI()
    if order == "custom_first":
        app.include_router(threads_delete_router)
        app.include_router(core)
    else:
        app.include_router(core)
        app.include_router(threads_delete_router)
    return app


def test_shadow_assertion_passes_when_custom_registered_first() -> None:
    assert_shadows_core_delete(_fake_core_delete_app("custom_first"))


def test_shadow_assertion_fails_when_core_wins() -> None:
    with pytest.raises(RuntimeError, match="首次匹配不是"):
        assert_shadows_core_delete(_fake_core_delete_app("core_first"))


async def test_delete_clears_session_files(monkeypatch: pytest.MonkeyPatch) -> None:
    import custom.middleware.threads_delete as td

    cleared: list = []

    async def fake_core(thread_id, user, session):
        return {"status": "deleted"}

    async def fake_clear(store, identity, thread_id):
        cleared.append((identity, thread_id))
        return 2

    monkeypatch.setattr(td, "core_delete_thread", fake_core)
    monkeypatch.setattr(td, "clear", fake_clear)
    monkeypatch.setattr(td.db_manager, "get_store", lambda: AsyncMock())

    user = MagicMock()
    user.identity = "luoxin"
    assert await delete_thread("t-1", user, AsyncMock()) == {"status": "deleted"}
    assert cleared == [("luoxin", "t-1")]


async def test_delete_survives_clear_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """thread 行已经删了，清理失败只能记日志；500 会让客户端重试成 404。"""
    import custom.middleware.threads_delete as td

    async def fake_core(thread_id, user, session):
        return {"status": "deleted"}

    async def boom(*_a, **_kw):
        raise RuntimeError("store down")

    monkeypatch.setattr(td, "core_delete_thread", fake_core)
    monkeypatch.setattr(td, "clear", boom)
    monkeypatch.setattr(td.db_manager, "get_store", lambda: AsyncMock())

    user = MagicMock()
    user.identity = "luoxin"
    assert await delete_thread("t-1", user, AsyncMock()) == {"status": "deleted"}


# ---------- 上传接口的校验 ----------


def test_safe_name_strips_paths() -> None:
    from custom.api.uploads import _safe_name

    assert _safe_name("a/b/report.md") == "report.md"
    assert _safe_name(r"C:\tmp\x.md") == "x.md"
    # 穿越被 basename 就地吃掉，不需要额外判定
    assert _safe_name("../../etc/passwd.md") == "passwd.md"
    assert _safe_name("REPORT.MD") == "REPORT.MD"  # 扩展名大小写不敏感


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        ("x.txt", 415),  # 本期只收 md
        ("", 422),
        ("..", 422),
        ("a\x00b.md", 422),  # 控制字符
        ("a" * 250 + ".md", 422),
    ],
)
def test_safe_name_rejects(raw: str, status: int) -> None:
    from fastapi import HTTPException

    from custom.api.uploads import _safe_name

    with pytest.raises(HTTPException) as exc:
        _safe_name(raw)
    assert exc.value.status_code == status


async def test_read_limited_caps_size(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException, UploadFile

    import custom.api.uploads as up

    monkeypatch.setattr(up, "_MAX_BYTES", 8)

    class FakeUpload:
        def __init__(self, data: bytes) -> None:
            self._buf = data

        async def read(self, n: int) -> bytes:
            chunk, self._buf = self._buf[:n], self._buf[n:]
            return chunk

    assert await up._read_limited(FakeUpload(b"12345678")) == b"12345678"
    with pytest.raises(HTTPException) as exc:
        await up._read_limited(FakeUpload(b"123456789"))
    assert exc.value.status_code == 413
    assert UploadFile is not None  # 只是确认导入路径没变


# ---------- 主管的存储路由 ----------


def test_supervisor_routes_uploads_to_uploads_backend() -> None:
    """/uploads/x 交给 UploadsBackend（key 由台账查），其余仍然按 space 判定。"""
    from deepagents.backends.composite import _route_for_path

    from graphs.supervisor import supervisor_backend

    backend = supervisor_backend()
    route = lambda p: _route_for_path(  # noqa: E731
        default=backend.default, sorted_routes=backend.sorted_routes, path=p
    )

    uploads_be, key, prefix = route("/uploads/report.md")
    assert type(uploads_be).__name__ == "UploadsBackend"
    assert (key, prefix) == ("/report.md", "/uploads/")

    space_be, _, prefix2 = route("/cbam-reporting/index.md")
    assert type(space_be).__name__ == "SpaceBackend"
    assert prefix2 is None


def test_backend_mode_switches_default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """`okf.backend: s3fs` 只切默认 backend；`/uploads/` 路由不变（md 也在对象存储里）。"""
    from deepagents.backends.composite import _route_for_path

    from graphs.access import factory

    monkeypatch.setattr(factory, "backend_mode", lambda: "s3fs")
    backend = factory.session_backend()
    assert type(backend.default).__name__ == "S3Backend"
    assert list(backend.routes) == ["/uploads/"]
    routed, key, prefix = _route_for_path(
        default=backend.default, sorted_routes=backend.sorted_routes, path="/uploads/a.md"
    )
    assert (type(routed).__name__, key, prefix) == ("UploadsBackend", "/a.md", "/uploads/")


async def test_clear_stops_on_misbehaving_store() -> None:
    """store 删完还返回同一批东西（行为异常）时不能死循环。"""

    class StuckStore(FakeStore):
        async def asearch(self, namespace, **kwargs):
            return [FakeItem("/loop.md", {})]

    stuck = StuckStore()
    removed = await ss.clear(stuck, "u", "t")
    # 每轮返回同一个 key：删了也不消失 -> 跑满轮数就停，不能无限循环
    assert removed == ss._MAX_CLEAR_ROUNDS
    assert len(stuck.batches) == ss._MAX_CLEAR_ROUNDS


class _FakeUpload:
    """`UploadFile` 的最小替身：只实现 `filename` 与分块 `read`。"""

    def __init__(self, filename: str, body: bytes = b"x") -> None:
        self.filename = filename
        self._buf = body

    async def read(self, n: int) -> bytes:
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


def _patch_upload_session(monkeypatch: pytest.MonkeyPatch, rows: list | None = None):
    """把端点依赖的 DB 与对象存储都换掉，返回 (module, session, user, put_bytes)。"""
    from unittest.mock import AsyncMock, MagicMock

    import custom.api.uploads as up
    from shared.session import blobs as blobs_mod

    session = AsyncMock()
    session.scalar = AsyncMock(return_value="t-1")  # 归属校验通过
    user = MagicMock()
    user.identity = "luoxin"

    monkeypatch.setattr(up.session_files, "list_for_thread", AsyncMock(return_value=rows or []))
    monkeypatch.setattr(up.session_files, "get_one", AsyncMock(return_value=None))
    monkeypatch.setattr(up.session_files, "upsert", AsyncMock(return_value=None))
    put = MagicMock()
    monkeypatch.setattr(blobs_mod, "put_bytes", put)
    monkeypatch.setattr(blobs_mod, "delete", MagicMock())
    return up, session, user, put


async def test_upload_stores_bytes_and_ledger_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """md 与 xlsx 同一条链路：字节进 `uploads/<uuid>/<名>`，行写台账，`space` 为 NULL。"""
    import hashlib

    up, session, user, put = _patch_upload_session(monkeypatch)
    body = b"# hi"
    resp = await up.upload_session_file(session, user, "t-1", _FakeUpload("notes.md", body))

    assert resp.path == "/uploads/notes.md" and resp.bytes == len(body)
    key, sent = put.call_args.args
    assert key.startswith("uploads/") and key.endswith("/notes.md")
    assert sent == body

    # upsert 必须带上「无空间」与内容指纹
    kwargs = up.session_files.upsert.call_args.kwargs
    assert kwargs["space"] is None
    assert kwargs["object_key"] == key
    assert kwargs["sha256"] == hashlib.sha256(body).hexdigest()
    assert kwargs["size_bytes"] == len(body)
    session.commit.assert_awaited()


async def test_upload_rejects_when_session_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """同类文件数上限；同名重传是覆盖，不受上限影响。"""
    from types import SimpleNamespace

    from fastapi import HTTPException

    rows = [SimpleNamespace(name=f"{i}.md") for i in range(2)]
    up, session, user, _ = _patch_upload_session(monkeypatch, rows)
    monkeypatch.setattr(up, "MAX_FILES_PER_SESSION", 2)

    with pytest.raises(HTTPException) as exc:
        await up.upload_session_file(session, user, "t-1", _FakeUpload("new.md"))
    assert exc.value.status_code == 413

    assert await up.upload_session_file(session, user, "t-1", _FakeUpload("1.md")) is not None
