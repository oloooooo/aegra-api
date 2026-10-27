"""会话文件存储层与清单注入的单元测试（ADR-0013）。"""

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


def test_key_keeps_leading_slash() -> None:
    """CompositeBackend 把 /uploads/x.md 剥成 /x.md 直接当 store key，丢了斜杠就读不到。"""
    assert ss.session_key("报告.md") == "/报告.md"
    assert ss.session_key("/报告.md") == "/报告.md"
    assert ss.virtual_path("报告.md") == "/uploads/报告.md"


# ---------- 读写 ----------


async def test_put_md_sets_ttl_and_file_data_shape() -> None:
    store = FakeStore()
    meta = await ss.put_md(store, "luoxin", "t-1", "a.md", "hello")

    ns, key, value, ttl = store.puts[0]
    assert ns == ss.session_namespace("luoxin", "t-1")
    assert key == "/a.md"
    assert ttl == ss.UPLOAD_TTL_MINUTES
    # deepagents StoreBackend 只认这几个键
    assert value["content"] == "hello" and value["encoding"] == "utf-8"
    assert value["created_at"] and value["modified_at"]
    assert meta["path"] == "/uploads/a.md" and meta["bytes"] == 5


async def test_list_files_reports_truncation() -> None:
    items = [FakeItem(f"/f{i}.md", {"content": "x", "created_at": "2026-01-01"}) for i in range(3)]
    store = FakeStore(items)
    rows, truncated = await ss.list_files(store, "u", "t", limit=2)

    assert len(rows) == 2 and truncated is True
    assert rows[0]["path"] == "/uploads/f0.md"
    # 活跃会话读清单即续期，孤儿才过期
    assert store.searches[0][1]["refresh_ttl"] is True
    assert store.searches[0][1]["limit"] == 3  # limit+1 只为判断截断


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


async def test_middleware_queries_once_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    store = FakeStore([FakeItem("/a.md", {"content": "hello", "created_at": "now"})])
    mw = sf.SessionFilesMiddleware(store=store)

    assert await mw.abefore_agent({}, None) is None  # 不写 state
    assert await mw.abefore_agent({}, None) is None
    assert len(store.searches) == 1


class FakeRequest:
    """`ModelRequest` 的最小替身：只实现中间件用到的两个接口。"""

    def __init__(self, system_message: SystemMessage | None = None) -> None:
        self.system_message = system_message

    def override(self, *, system_message: SystemMessage) -> FakeRequest:
        return FakeRequest(system_message)


async def test_middleware_injects_listing_as_untrusted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    store = FakeStore([FakeItem("/a.md", {"content": "hello", "created_at": "now"})])
    mw = sf.SessionFilesMiddleware(store=store)

    seen: list = []

    async def handler(request):
        seen.append(request)
        return "ok"

    request = FakeRequest(SystemMessage(content="原本的提示"))
    assert await mw.awrap_model_call(request, handler) == "ok"

    content = seen[0].system_message.content
    assert "原本的提示" in content
    assert "/uploads/a.md" in content
    assert "不可信" in content  # 文件内容是资料，不是指令


async def test_middleware_passes_through_without_files(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    mw = sf.SessionFilesMiddleware(store=FakeStore([]))

    request = FakeRequest(SystemMessage(content="原"))
    called: list = []

    async def handler(req):
        called.append(req)
        return "ok"

    assert await mw.awrap_model_call(request, handler) == "ok"
    assert called[0] is request  # 没有清单就不改请求


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


def test_supervisor_routes_uploads_to_store() -> None:
    """/uploads/x 交给 UploadsBackend（key 带前导斜杠），其余仍然按 space 判定。"""
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


async def test_upload_rejects_when_session_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个会话的文件数上限：防单会话无限堆（TTL 只管孤儿，thread 配额只管会话数）。"""
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    import custom.api.uploads as up

    session = AsyncMock()
    session.scalar = AsyncMock(return_value="t-1")  # 归属校验通过

    user = MagicMock()
    user.identity = "luoxin"

    monkeypatch.setattr(up, "MAX_FILES_PER_SESSION", 2)
    full = [{"path": f"/uploads/{i}.md", "bytes": 1, "created_at": "", "name": f"{i}.md"} for i in range(2)]
    monkeypatch.setattr(up, "list_files", AsyncMock(return_value=(full, False)))
    meta = {"name": "1.md", "path": "/uploads/1.md", "bytes": 1, "uploaded_at": "now", "ttl_minutes": 4320}
    monkeypatch.setattr(up, "put_md", AsyncMock(return_value=meta))
    monkeypatch.setattr(up.db_manager, "get_store", lambda: AsyncMock())

    class FakeUpload:
        def __init__(self, filename: str) -> None:
            self.filename = filename
            self._sent = False

        async def read(self, n: int) -> bytes:
            if self._sent:
                return b""
            self._sent = True
            return b"x"

    with pytest.raises(HTTPException) as exc:
        await up.upload_session_file(session, user, "t-1", FakeUpload("new.md"))
    assert exc.value.status_code == 413

    # 同名重传是覆盖，不受上限影响
    await up.upload_session_file(session, user, "t-1", FakeUpload("1.md"))
