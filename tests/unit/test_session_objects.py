"""`custom/session_objects` 的纯逻辑：文件名规则与配额边界（不连库）。"""

import pytest

from custom.service import session_files as so


@pytest.mark.parametrize(
    ("name", "space"),
    [
        ("cbam-reporting_报价表.xlsx", "cbam-reporting"),
        ("cbam-reporting_报价表.XLSX", "cbam-reporting"),
        ("a_b_c.xlsx", "a"),  # 只按第一个下划线切
        ("space.with.dot_表.xlsx", "space.with.dot"),
    ],
)
def test_parse_space_ok(name, space):
    assert so.parse_space(name) == space


@pytest.mark.parametrize(
    "name",
    [
        "没有前缀.xlsx",  # 缺 <空间名>_
        "_只有表名.xlsx",  # 空空间名
        "有前缀_.xlsx",  # 空表名
        "cbam-reporting.xlsx",  # 只有空间名
    ],
)
def test_parse_space_rejects(name):
    with pytest.raises(ValueError, match="文件名必须是"):
        so.parse_space(name)


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(so, "MAX_BYTES_PER_USER", 1000)
    monkeypatch.setattr(so, "MAX_XLSX_FILES_PER_SESSION", 3)
    return so


def test_quota_allows_within_budget(limits):
    assert limits.quota_violation(used_bytes=0, old_bytes=0, new_bytes=1000, thread_count=0, is_overwrite=False) is None


def test_quota_rejects_over_bytes(limits):
    msg = limits.quota_violation(used_bytes=1, old_bytes=0, new_bytes=1000, thread_count=0, is_overwrite=False)
    assert msg and "总量超出上限" in msg


def test_quota_overwrite_does_not_double_count_bytes(limits):
    """同名覆盖：旧行的字节要先减掉，否则每次重传都会被自己挤爆。"""
    assert (
        limits.quota_violation(used_bytes=1000, old_bytes=1000, new_bytes=1000, thread_count=3, is_overwrite=True)
        is None
    )


def test_quota_overwrite_does_not_take_a_new_slot(limits):
    """文件数已满时，同名覆盖仍要放行。"""
    assert limits.quota_violation(used_bytes=0, old_bytes=0, new_bytes=10, thread_count=3, is_overwrite=True) is None


def test_quota_rejects_new_file_when_session_full(limits):
    """上限由调用方按类别传入（md 与 xlsx 各算各的）。"""
    msg = limits.quota_violation(
        used_bytes=0, old_bytes=0, new_bytes=10, thread_count=3, is_overwrite=False, thread_limit=3, kind="xlsx"
    )
    assert msg and "xlsx 文件数已达上限（3）" in msg


def test_quota_kind_appears_in_message(limits):
    """md 与 xlsx 走同一个函数，报错里得说清是哪一类满了。"""
    msg = limits.quota_violation(
        used_bytes=0, old_bytes=0, new_bytes=10, thread_count=9, is_overwrite=False, thread_limit=9, kind="md"
    )
    assert msg and "md 文件数已达上限（9）" in msg


def test_quota_counts_other_sessions_for_bytes(limits):
    """字节是**按用户**算的：另一个会话占用的量也要计入。"""
    msg = limits.quota_violation(used_bytes=999, old_bytes=0, new_bytes=10, thread_count=0, is_overwrite=False)
    assert msg and "总量超出上限" in msg
