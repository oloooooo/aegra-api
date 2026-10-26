"""`/custom/spaces` 这两条端点的目录遍历辅助函数。

它们从 async 端点里搬出来，改成经 `asyncio.to_thread` 调用（同步 rglob 在事件循环里
跑会卡住整个进程）。这里锁住两件事：计数口径不变、文件列表的字段/顺序不变。
"""

from custom.api.spaces import _count_files, _list_files


def test_count_files_counts_nested_files_only(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b.csv").write_text("yy", encoding="utf-8")
    assert _count_files(tmp_path) == 2


def test_count_files_missing_dir_is_zero(tmp_path):
    assert _count_files(tmp_path / "nope") == 0


def test_list_files_reports_relative_paths_sorted(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "X.XLSX").write_text("data", encoding="utf-8")
    (tmp_path / "a.md").write_text("hi", encoding="utf-8")

    out = _list_files(tmp_path)

    assert [o["path"] for o in out] == ["/a.md", "/sub/X.XLSX"]
    assert [o["ext"] for o in out] == [".md", ".xlsx"]
    assert out[1]["size"] == 4
    assert out[0]["modified"].endswith("+00:00")


def test_list_files_skips_unreadable_entry(tmp_path, monkeypatch):
    """遍历期间文件被删：stat 抛 OSError，应当跳过这一条而不是让整个列表 500。"""
    (tmp_path / "keep.md").write_text("hi", encoding="utf-8")
    gone = tmp_path / "gone.md"
    gone.write_text("bye", encoding="utf-8")

    real_stat = type(gone).stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "gone.md":
            raise OSError("vanished")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(gone), "stat", flaky_stat)

    assert [o["path"] for o in _list_files(tmp_path)] == ["/keep.md"]
