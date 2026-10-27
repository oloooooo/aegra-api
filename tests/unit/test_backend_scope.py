"""空间隔离判定（`SpaceGuard`，经 `SpaceBackend` 落地）。

回归重点：`/空间A/../空间B/x.xlsx` 的首段属于 A（有权限），但真正被打开的是 B 的文件。
只比对原始字符串就等于不设防（2026-10-25 实测可通过，`knowledge/secret/` 真实存在）。

这些用例以前测的是 xlsx 工具里的 `_check_scope` 副本；xlsx 工具改走 backend 后只剩
这一份判定，所以测试也搬到这里（`graphs/access/backend.py`）。
"""

import pytest

from graphs.access import backend as backend_mod
from graphs.access.backend import SpaceBackend


@pytest.fixture
def okf(tmp_path, monkeypatch):
    """root 下两个空间 + 一个根散文件 + 一个"别人的"目录。"""
    for name in ("space-a", "space-b", "secret"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "t.md").write_text("x", encoding="utf-8")
    (tmp_path / "process.md").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(backend_mod, "spaces_from_config_or_runtime", lambda: frozenset({"space-a"}))
    return SpaceBackend(root_dir=tmp_path)


def test_allows_own_space(okf):
    assert okf.read("/space-a/t.md").error is None


def test_allows_root_file(okf):
    result = okf.read("/process.md")
    assert result.error is None
    assert result.file_data["content"] == "hi"


def test_allows_dot_segment_inside_own_space(okf):
    assert okf.read("/space-a/./t.md").error is None


def test_denies_other_space(okf):
    assert "无权访问" in (okf.read("/space-b/t.md").error or "")


def test_denies_missing_root_file(okf):
    assert "无权访问" in (okf.read("/nope.md").error or "")


@pytest.mark.parametrize(
    "path",
    [
        "/space-a/../secret/t.md",  # 换到没权限的目录
        "/space-a/../space-b/t.md",  # 换到另一个空间（同样是别人的）
        "/space-a//../secret/t.md",  # 多斜杠变体
        "/space-a/./../secret/t.md",
    ],
)
def test_denies_traversal_into_other_dir(okf, path):
    """`..` 在 `FilesystemBackend` 解析阶段就被拒（比判定层更早，抛而不是返回错误）。"""
    with pytest.raises(ValueError, match="Path traversal not allowed"):
        okf.read(path)


def test_denies_escape_above_root(okf):
    with pytest.raises(ValueError, match="Path traversal not allowed"):
        okf.read("/space-a/../../pyproject.toml")


def test_denies_symlink_escape(okf, tmp_path):
    """空间里的符号链接指到根外：`FilesystemBackend` 解析后自己在根外抛错（存储层兜住，
    判定层看不到符号链接）。Windows 上建不了就跳过。"""
    link = tmp_path / "space-a" / "out.md"
    try:
        link.symlink_to(tmp_path.parent / "outside.md")
    except (OSError, NotImplementedError):
        pytest.skip("该平台不支持创建符号链接")
    with pytest.raises(ValueError, match="outside root directory"):
        okf.read("/space-a/out.md")


def test_no_auth_context_allows(okf, monkeypatch):
    """本地 demo（无 langgraph_auth_user）不设限，与改动前一致。"""
    monkeypatch.setattr(backend_mod, "spaces_from_config_or_runtime", lambda: None)
    assert okf.read("/secret/t.md").error is None


def test_service_identity_without_forwarded_spaces_denies(okf, monkeypatch):
    """服务身份但没转发名单：fail-closed（空集），一个空间都不放行。"""
    monkeypatch.setattr(backend_mod, "spaces_from_config_or_runtime", frozenset)
    assert "无权访问" in (okf.read("/space-a/t.md").error or "")
