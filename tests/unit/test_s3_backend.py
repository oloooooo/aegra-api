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
}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> sb.S3Backend:
    # 空间清单来自配置（默认只有一个空间），这里不需要它：判定规则本身由 test_space_guard 守
    # 两处都要换：`_check_read` 在 backend 模块里，`_shown`/探针在 s3_backend 里
    monkeypatch.setattr(backend_mod, "spaces_from_config_or_runtime", lambda: None)
    monkeypatch.setattr(sb, "spaces_from_config_or_runtime", lambda: None)
    monkeypatch.setattr(sb.blobs, "list_tree", lambda _prefix: dict(_FILES))
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
