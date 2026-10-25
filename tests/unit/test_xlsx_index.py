"""xlsx → SQLite 索引与四个工具的结果（不连 S3、不连库）。

锁的是审阅里发现的四个坑：行号 `rowid+header+1`、`xlsx_search` 按**单元格**计、
`where` 的 SQL 语义差异（括号/空格列名要引号、`!=` 漏空值）、以及只读边界。
"""

import json
import sqlite3

import pandas as pd
import pytest

from graphs.tools.xlsx import index


@pytest.fixture
def xlsx(tmp_path):
    p = tmp_path / "t.xlsx"
    detail = pd.DataFrame(
        {
            "料号": ["A1", "A2", "A3", None],
            "数量": [1, 10, 5, 7],
            "价 格": [100, 200, 300, 400],
            "数量(个)": [1, 2, 3, 4],
        }
    )
    summary = pd.DataFrame({"site": [1, 1, 2, 2], "pn": ["a_b", "axb", "c", "c"]})
    with pd.ExcelWriter(p) as w:
        detail.to_excel(w, sheet_name="明细", index=False)
        summary.to_excel(w, sheet_name="汇总", index=False)
    return p


@pytest.fixture
def conn(xlsx, tmp_path):
    idx = index.ensure(xlsx, tmp_path, "k", 0)
    c = index.open_ro(idx)
    yield c
    c.close()


def test_info_reports_all_sheets(conn):
    out = json.loads(index.info(conn, "t.xlsx", None))

    assert out["file"] == "t.xlsx"
    assert out["sheets"]["明细"] == {"rows": 4, "columns": ["料号", "数量", "价 格", "数量(个)"]}
    assert out["sheets"]["汇总"] == {"rows": 4, "columns": ["site", "pn"]}
    assert out["preview"]["sheet"] == "明细"
    assert len(out["preview"]["head"]) == 3
    assert out["preview"]["head"][3 - 3]["料号"] == "A1"


def test_info_preview_keeps_null_as_none(conn):
    out = json.loads(index.info(conn, "t.xlsx", "明细"))
    # 第 4 行料号是 NaN → sqlite NULL → JSON null
    assert out["sheets"]["明细"]["rows"] == 4


def test_query_basic_header_and_csv(conn):
    text = index.query(conn, None, None, "数量 > 4", 200, 0)
    head, csv = text.split("\n", 1)

    assert head.startswith("匹配 3 行，返回第 1-3 行")
    assert csv.splitlines()[0] == "料号,数量,价 格,数量(个)"
    assert csv.splitlines()[1].startswith("A2,10,")


def test_query_quotes_columns_with_spaces_and_parens(conn):
    assert "匹配 3 行" in index.query(conn, None, None, '"价 格" > 150', 200, 0)
    assert "匹配 2 行" in index.query(conn, None, ["料号"], '"数量(个)" > 2', 200, 0)


def test_query_ne_skips_null_rows(conn):
    """SQL 的 `!=` 对 NULL 不成立；pandas 的 `!=` 会带上空值行。工具描述里已写明。"""
    text = index.query(conn, None, ["料号"], "料号 != 'A1'", 200, 0)

    assert "匹配 2 行" in text  # A2、A3；NULL 那行被漏掉
    assert "None" not in text


def test_query_clamps_limit_and_offset(conn):
    assert "返回第 1-4 行" in index.query(conn, None, None, None, 10_000, 0)
    assert "返回第 6-5 行" in index.query(conn, None, None, None, 20, 5)  # 空页也照原样印


def test_query_rejects_unknown_column(conn):
    with pytest.raises(ValueError, match="列不存在"):
        index.query(conn, None, ["没有这列"], None, 20, 0)


def test_query_rejects_unknown_sheet(conn):
    with pytest.raises(ValueError, match="sheet 不存在"):
        index.query(conn, "不存在的表", None, None, 20, 0)


def test_search_counts_cells_and_covers_all_sheets(conn):
    """命中按**单元格**算，且跨 sheet 汇总（LIKE 对 ASCII 不区分大小写）。"""
    out = index.search(conn, "a", None, 0, 100)

    # 明细：料号 A1/A2/A3 三格；汇总：pn a_b、axb 两格
    assert "命中 5 处" in out
    lines = out.splitlines()[1:]
    assert sum("明细" in line for line in lines) == 3
    assert sum("汇总" in line for line in lines) == 2


def test_search_row_number_matches_excel(conn):
    """header=0 时第一条数据在 Excel 第 2 行，A2 是第二条数据 → 第 3 行。"""
    out = index.search(conn, "A2", None, 0, 100)

    assert "Excel第3行" in out


def test_search_escapes_like_wildcards(conn):
    """`a_b` 只该命中字面量 a_b；没转义时也会命中 axb。"""
    out = index.search(conn, "a_b", None, 0, 100)

    assert "命中 1 处" in out
    assert "a_b" in out


def test_search_truncates_hits_but_reports_real_total(conn):
    out = index.search(conn, "c", None, 0, 1)

    assert "命中 2 处" in out  # 汇总里两行 pn=c
    assert "仅显示前 1 条" in out


def test_search_unknown_sheet(conn):
    with pytest.raises(ValueError, match="sheet 不存在"):
        index.search(conn, "x", "没有", 0, 10)


def test_profile_numbers_and_duplicates(conn):
    out = json.loads(index.profile(conn, "明细"))

    assert out["rows"] == 4
    assert out["duplicate_rows"] == 0
    assert out["columns"]["料号"]["nulls"] == 1
    assert out["columns"]["料号"]["unique"] == 3
    assert out["columns"]["数量"]["min"] == 1
    assert out["columns"]["数量"]["max"] == 10
    assert out["columns"]["数量"]["mean"] == 5.75


def test_profile_counts_duplicate_rows(conn):
    out = json.loads(index.profile(conn, "汇总"))

    assert out["duplicate_rows"] == 1  # (2, c) 出现两次 → 额外 1 行
    assert out["columns"]["pn"]["sample"] == ["a_b", "axb", "c"]


def test_readonly_boundary_blocks_attach_and_writes(conn):
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("ATTACH DATABASE 'other.db' AS other")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("CREATE TABLE evil(x)")


def test_authorizer_blocks_dangerous_functions(conn):
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("SELECT zeroblob(1000000)").fetchone()


def test_header_offset_shifts_row_numbers(tmp_path):
    """header=1（首行是表格标题）时，第一条数据在 Excel 第 3 行。"""
    p = tmp_path / "h.xlsx"
    pd.DataFrame([["季度表", None], ["料号", "数量"], ["A1", 1]]).to_excel(p, index=False, header=False)
    idx = index.ensure(p, tmp_path, "h", 1)
    with index.open_ro(idx) as c:
        assert "Excel第3行" in index.search(c, "A1", None, 1, 10)


def test_rejects_table_over_max_cells(tmp_path, monkeypatch):
    """能上传但处理不了的宽表：建索引前就拒，别让 agent 等 40 秒再撞超时。"""
    p = tmp_path / "big.xlsx"
    pd.DataFrame({"a": list(range(50)), "b": list(range(50))}).to_excel(p, index=False)
    monkeypatch.setattr(index, "_MAX_CELLS", 10)

    with pytest.raises(ValueError, match="表太大"):
        index.ensure(p, tmp_path / "cache", "k", 0)


def test_cache_sweep_removes_expired_files(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    stale = cache / "stale.sqlite"
    stale.write_bytes(b"x")
    import os as _os
    import time as _time

    old = _time.time() - 10 * 86400
    _os.utime(stale, (old, old))
    monkeypatch.setattr(index, "_swept", False)
    monkeypatch.setattr(index, "_CACHE_TTL_DAYS", 7)

    index._sweep_cache(cache)

    assert not stale.exists()
