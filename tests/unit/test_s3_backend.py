"""`S3Backend` 的读家族：只测「key 表 → 协议结果」这段纯逻辑，不碰网络。

`_keys()` 的 LIST 与 `_get_bytes()` 的 GET 在这里都被换掉——真正要守的是路径映射、
空间可见性判定与协议返回形状（这些错了不会抛异常，只会让 agent 看到错的东西）。
"""

from __future__ import annotations

import pytest

from graphs.access import backend as backend_mod
from graphs.access import s3_backend as sb

_FILES = {
    "okf/sp/a.md": {"size": 5},
    "okf/sp/t.xlsx": {"size": 9},
    "okf/sp/sub/deep.md": {"size": 3},
    # 框架的驱逐文件：在 artifacts/ 而不是 okf/（它们不是空间知识）
    "artifacts/large_tool_results/r1.txt": {"size": 5},
}


def _dir_listing(prefix: str) -> tuple[list[str], dict[str, dict[str, int]]]:
    """模拟分隔 LIST：只回这一层的子目录与文件。"""
    dirs = sorted({k[: k.index("/", len(prefix)) + 1] for k in _FILES if k.startswith(prefix) and "/" in k[len(prefix) :]})
    files = {k: v for k, v in _FILES.items() if k.startswith(prefix) and "/" not in k[len(prefix) :]}
    return dirs, files


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> sb.S3Backend:
    # 空间清单来自配置（默认只有一个空间），这里不需要它：判定规则本身由 test_space_guard 守
    # 两处都要换：`_check_read` 在 backend 模块里，`_shown`/探针在 s3_backend 里
    monkeypatch.setattr(backend_mod, "spaces_from_config_or_runtime", lambda: None)
    monkeypatch.setattr(sb, "spaces_from_config_or_runtime", lambda: None)
    monkeypatch.setattr(sb.blobs, "list_tree", lambda prefix: {k: v for k, v in _FILES.items() if k.startswith(prefix)})
    monkeypatch.setattr(sb.blobs, "list_dir", _dir_listing)
    monkeypatch.setattr(sb, "_get_bytes", lambda obj: b"PK\x03\x04zipish" if obj.key.endswith(".xlsx") else b"hello")
    return sb.S3Backend()


def test_ls_maps_keys_to_virtual_paths(backend: sb.S3Backend) -> None:
    result = backend.ls("/sp/")
    assert result.error is None
    got = [(e["path"], e.get("is_dir"), e.get("size")) for e in result.entries or []]
    assert got == [("/sp/a.md", False, 5), ("/sp/sub/", True, 0), ("/sp/t.xlsx", False, 9)]


def test_read_text_has_no_line_numbers(backend: sb.S3Backend) -> None:
    """行号由中间件加（`slice_read_response` 只管切片），backend 不能自己加。"""
    result = backend.read("/sp/a.md")
    assert result.file_data is not None
    assert result.file_data["content"] == "hello"


def test_read_on_table_points_at_xlsx_tools(backend: sb.S3Backend) -> None:
    """表格给指路提示，而不是几 KB base64（模型看不懂，还烧 token）。"""
    result = backend.read("/sp/t.xlsx")
    assert result.file_data is None
    assert result.error and "xlsx_info" in result.error


def test_evicted_files_live_outside_the_space_prefix(backend: sb.S3Backend) -> None:
    """驱逐文件写 artifacts/：`okf/` 只放空间知识，垃圾能按前缀整批清。"""
    assert backend._key_of("/large_tool_results/r1.txt") == "artifacts/large_tool_results/r1.txt"
    assert backend._key_of("/sp/a.md") == "okf/sp/a.md"
    # 合并 key 表之后仍然读得回来（框架驱逐后会按路径 read 回来）
    assert backend.read("/large_tool_results/r1.txt").file_data is not None
    # 空间可见性判定对内部前缀单独放行（`_is_internal`），与 okf/ 前缀无关
    assert backend._gate("/large_tool_results/r1.txt") is None


def test_read_and_download_do_not_list(backend: sb.S3Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    """`read`/`download_files` 不能付全量 LIST 的账（2000 个对象实测 115ms，单 GET 2ms）。"""
    monkeypatch.setattr(sb.blobs, "list_tree", lambda _p: pytest.fail("read/download 不该 LIST"))
    assert backend.read("/sp/a.md").file_data is not None
    assert backend.download_files(["/sp/a.md"])[0].content == b"hello"


def test_grep_fetches_only_files_under_the_given_path(backend: sb.S3Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    """grep 一个子目录只能下去这个子目录的字节（原来会把整个前缀 GET 一遍）。"""
    fetched: list[str] = []

    def _fake(obj: sb._Obj) -> bytes:
        fetched.append(obj.key)
        return b"hit"

    monkeypatch.setattr(sb, "_get_bytes", _fake)
    result = backend.grep("hit", "/sp/sub/")
    assert fetched == ["okf/sp/sub/deep.md"]
    assert [m["path"] for m in result.matches or []] == ["/sp/sub/deep.md"]
