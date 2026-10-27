"""手动 e2e（不在 CI 里）：真 SeaweedFS + 真 backend + 真 xlsx 工具链（ADR-0017 缺陷 12）。

跑法：``PYTHONPATH=. PYTHONUTF8=1 .venv/Scripts/python.exe scripts/e2e_s3fs.py``
（需要本机 ``docker run -d -p 8333:8333 chrislusf/seaweedfs server -s3``，见 docs/S3.md）

守单测proof不了的那条链路：对象存储 -> ``download_files`` -> 磁盘缓存 -> sqlite 索引 -> 工具答案，
外加 ``has_tables()`` 递归（表放子目录也要装上工具）、跨空间拒绝、驱逐文件不落 ``okf/``。
"""

import asyncio
import io
import sys

from openpyxl import Workbook

import shared.config as config

_CFG = {
    "okf.backend": "s3fs",
    "s3.endpoint": "http://127.0.0.1:8333",
    "s3.bucket": "aegra-blobs",
    "s3.region": "us-east-1",
}
_orig_get = config.get
config.get = lambda key: _CFG.get(key, _orig_get(key))  # noqa: ARG005

SPACE = "e2e-space"
DOC_SPACE = "e2e-doc"
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAILED.append(name)


def make_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "sales"
    ws.append(["region", "units"])
    ws.append(["east", 12])
    ws.append(["south", 7])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def main() -> None:
    import graphs.access.spaces as spaces_mod
    from graphs.access import factory
    from graphs.tools.xlsx import _fetch, _info, index
    from graphs.tools.xlsx import store as xstore
    from shared.knowledge import spec
    from shared.session import blobs

    # Simulate the real run context: an auth user who owns exactly these spaces.
    # (Outside a graph run `get_config()` has no auth_user, so the ACL is not exercised.)
    spaces_mod.get_config = lambda: {
        "configurable": {"langgraph_auth_user": {"identity": "e2e-user", "user_spaces": [SPACE, DOC_SPACE]}}
    }

    try:
        blobs.put_bytes(f"okf/{SPACE}/AGENT.md", f"---\nname: {SPACE}\n---\nspace agent\n".encode())
        blobs.put_bytes(f"okf/{SPACE}/index.md", b"# e2e\n")
        blobs.put_bytes(f"okf/{SPACE}/data/t.xlsx", make_xlsx())
        blobs.put_bytes(f"okf/{DOC_SPACE}/AGENT.md", f"---\nname: {DOC_SPACE}\n---\ndocs only\n".encode())

        # --- space spec: pure object storage, no local directory ---
        spaces = spec.list_spaces()
        check("list_spaces sees both spaces", spaces == sorted([SPACE, DOC_SPACE]), str(spaces))
        check("load_agent_spec reads body", "space agent" in spec.load_agent_spec(SPACE)[1])
        check("has_tables recurses into data/", spec.has_tables(SPACE) is True)
        check("has_tables false for docs-only space", spec.has_tables(DOC_SPACE) is False)

        # --- backend ---
        backend = factory.session_backend()
        check("default backend is S3Backend", type(backend.default).__name__ == "S3Backend", type(backend.default).__name__)
        check("read md", backend.read(f"/{SPACE}/index.md").error is None)
        ls_paths = [e["path"] for e in (backend.ls(f"/{SPACE}/").entries or [])]
        check(
            "ls lists file + subdir",
            ls_paths == [f"/{SPACE}/AGENT.md", f"/{SPACE}/data/", f"/{SPACE}/index.md"],
            str(ls_paths),
        )
        table_read = backend.read(f"/{SPACE}/data/t.xlsx")
        check(
            "read on table points at xlsx tools",
            bool(table_read.error) and "xlsx_info" in str(table_read.error),
            str(table_read.error),
        )
        denied = backend.read("/e2e-other/AGENT.md")
        check("other space denied", bool(denied.error) and "AGENT.md" not in str(denied.file_data), str(denied.error)[:80])
        resp = (await backend.adownload_files([f"/{SPACE}/data/t.xlsx"]))[0]
        check("download_files returns real bytes", resp.content is not None and resp.content[:2] == b"PK", str(resp.error))

        # --- eviction: framework scratch files must not pollute okf/ ---
        backend.write("/large_tool_results/e2e-1.txt", "scratch")
        spilled = blobs.list_tree("artifacts/")
        check("evicted file lands in artifacts/", any(k.endswith("e2e-1.txt") for k in spilled), str(list(spilled))[:100])
        check("okf/ stays clean", not any("large_tool_results" in k for k in blobs.list_tree("okf/")))
        check("evicted file still readable", backend.read("/large_tool_results/e2e-1.txt").file_data is not None)

        # --- toolchain: bytes -> cache -> index -> answer ---
        xlsx, cache_key = await _fetch(f"/{SPACE}/data/t.xlsx", None)
        info = await asyncio.to_thread(_info, xlsx, cache_key, "t.xlsx", None, 0)
        check("xlsx_info sees sheet + columns", "sales" in info and "region" in info, info[:300])

        import sqlite3

        db = index.ensure(xlsx, xstore.cache_dir(), cache_key, 0)
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM s0").fetchone()[0]
        check("index holds the 2 data rows", rows == 2, str(rows))
    finally:
        blobs.delete_prefix(f"okf/{SPACE}/")
        blobs.delete_prefix(f"okf/{DOC_SPACE}/")
        blobs.delete_prefix("okf/e2e-other/")
        blobs.delete_prefix("artifacts/")
        left = blobs.list_tree("okf/e2e")
        check("cleaned up", not left, str(list(left)))


try:
    asyncio.run(main())
finally:
    print("\nFAILED:", FAILED or "none")
    sys.exit(1 if FAILED else 0)
