"""xlsx 工具取字节的接缝：经 backend 拿字节 + 按内容落缓存。

以前这里有一份自己的空间判定（`_check_scope`）；改成走 backend 后，隔离只在
`SpaceGuard` 一份，工具这边只剩「拿到字节 → 缓存键由内容决定」。
"""

import asyncio

import pytest
from deepagents.backends.protocol import FileDownloadResponse

from graphs.tools import xlsx as xlsx_mod
from graphs.tools.xlsx import store


class _Runtime:
    """`_fetch` 只用到 backend，其余字段不参与。"""


class _FakeBackend:
    def __init__(self, responses: dict[str, FileDownloadResponse]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        self.calls.append(paths)
        return [self.responses[p] for p in paths]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path / "cache")
    return tmp_path / "cache"


def _use(monkeypatch, backend: _FakeBackend) -> None:
    monkeypatch.setattr(xlsx_mod, "session_backend", lambda: backend)


def test_fetch_reads_bytes_through_backend(cache, monkeypatch):
    _use(monkeypatch, _FakeBackend({"/sp/t.xlsx": FileDownloadResponse(path="/sp/t.xlsx", content=b"xlsx")}))
    path, key = asyncio.run(xlsx_mod._fetch("/sp/t.xlsx", _Runtime()))
    assert path.read_bytes() == b"xlsx"
    assert path.name == f"{key}.xlsx"


def test_fetch_reports_backend_error_without_touching_cache(cache, monkeypatch):
    backend = _FakeBackend({"/sp/nope.xlsx": FileDownloadResponse(path="/sp/nope.xlsx", error="permission_denied")})
    _use(monkeypatch, backend)
    with pytest.raises(FileNotFoundError, match="permission_denied"):
        asyncio.run(xlsx_mod._fetch("/sp/nope.xlsx", _Runtime()))
    assert not cache.exists()
    assert backend.calls == [["/sp/nope.xlsx"]]


def test_same_content_from_two_paths_shares_one_cache_file(cache):
    """同一个文件从不同虚拟路径读到（空间里一份、会话上传一份）：缓存只存一份、索引键相同。"""
    first, key1 = store.cache_bytes("/sp/t.xlsx", b"same")
    second, key2 = store.cache_bytes("/uploads/t.xlsx", b"same")
    assert key1 == key2
    assert first == second
    assert sorted(p.name for p in cache.iterdir()) == [f"{key1}.xlsx"]


def test_changed_content_gets_a_new_key(cache):
    _, key1 = store.cache_bytes("/sp/t.xlsx", b"v1")
    _, key2 = store.cache_bytes("/sp/t.xlsx", b"v2")
    assert key1 != key2
    assert len(list(cache.iterdir())) == 2
