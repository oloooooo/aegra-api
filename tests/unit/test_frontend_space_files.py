"""`/custom/spaces` 文件树/文件数的实现（`shared.knowledge.spec.space_files / space_file_count`）。

`okf.backend: local` 读盘、`s3fs` 读对象存储，两种模式必须给出**同一种形状**：
path 以 `/` 开头、按路径排序、字段一致。HTTP 层只负责 `asyncio.to_thread` 包一层。

这两条错了的表现是「文件列表跟 agent 能读到的内容不一样」，很难从现象反推，
所以按字段锁死。
"""

import pytest

from shared.knowledge import spec


@pytest.fixture
def space(tmp_path, monkeypatch):
    """造一个本地空间 `sp`，把 `space_dir` 指向它（不碰真知识空间根）。"""
    folder = tmp_path / "sp"
    folder.mkdir()
    monkeypatch.setattr(spec, "_remote", lambda: False)
    monkeypatch.setattr(spec, "space_dir", lambda name: tmp_path / name)
    return folder


def test_counts_nested_files_only(space):
    (space / "sub").mkdir()
    (space / "sub" / "a.txt").write_text("x", encoding="utf-8")
    (space / "b.csv").write_text("yy", encoding="utf-8")
    assert spec.space_file_count("sp") == 2


def test_missing_space_is_zero(space):
    assert spec.space_file_count("nope") == 0
    assert spec.space_files("nope") == []


def test_files_report_relative_paths_sorted(space):
    (space / "sub").mkdir()
    (space / "sub" / "X.XLSX").write_text("data", encoding="utf-8")
    (space / "a.md").write_text("hi", encoding="utf-8")

    out = spec.space_files("sp")

    assert [o["path"] for o in out] == ["/a.md", "/sub/X.XLSX"]
    assert [o["ext"] for o in out] == [".md", ".xlsx"]
    assert out[1]["size"] == 4
    assert out[0]["modified"].endswith("+00:00")


def test_files_skip_unreadable_entry(space, monkeypatch):
    """遍历期间文件被删：stat 抛 OSError，应当跳过这一条而不是让整个列表 500。"""
    (space / "keep.md").write_text("hi", encoding="utf-8")
    gone = space / "gone.md"
    gone.write_text("bye", encoding="utf-8")

    real_stat = type(gone).stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "gone.md":
            raise OSError("vanished")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(gone), "stat", flaky_stat)

    assert [o["path"] for o in spec.space_files("sp")] == ["/keep.md"]


def test_remote_mode_strips_prefix_and_keeps_shape(monkeypatch):
    """s3fs 模式：key 去掉 `okf/<space>/` 前缀后与磁盘版同形。"""
    tree = {
        "okf/sp/a.md": {"size": 2, "etag": '"e1"', "modified": None},
        "okf/sp/sub/b.xlsx": {"size": 9, "etag": '"e2"', "modified": None},
        "okf/other/c.md": {"size": 1, "etag": '"e3"', "modified": None},
    }
    monkeypatch.setattr(spec, "_remote", lambda: True)
    monkeypatch.setattr(spec.blobs, "list_tree", lambda prefix: {k: v for k, v in tree.items() if k.startswith(prefix)})

    assert spec.space_file_count("sp") == 2
    assert [o["path"] for o in spec.space_files("sp")] == ["/a.md", "/sub/b.xlsx"]


def test_agent_md_read_from_remote(monkeypatch):
    """AGENT.md 也是经对象存储读的：s3fs 模式没有本地目录可读。"""
    monkeypatch.setattr(spec, "_remote", lambda: True)
    monkeypatch.setattr(
        spec.blobs,
        "get_object",
        lambda key: (b"---\ndescription: \xe8\xbf\x9c\xe7\xa8\x8b\xe6\xa1\xa3\xe6\xa1\x88\n---\n\xe4\xbd\xa0\xe6\x98\xaf\xe8\xa1\xa8\xe6\xa0\xbc agent", '"e"'),
    )

    description, prompt = spec.load_agent_spec("sp")

    assert description == "远程档案"
    assert prompt == "你是表格 agent"
