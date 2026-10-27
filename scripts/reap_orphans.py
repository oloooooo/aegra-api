"""对账：对象存储的 `uploads/` 与台账 `session_object` 两边对不对得上（ADR-0017 缺陷 7）。

偏差有两个方向，**都只报不猜**：

- **孤儿对象**：对象在、台账里没有行。来源：删会话时对象删失败被吞、同名覆盖后旧对象没删掉、
  并发上传同名（后到的那次读到的 `existing` 还是空的）。
- **悬空行**：台账有行、对象没了。**正常写序不会产生**（先写对象再提交行，失败会删掉新对象；
  删会话是先删行再删对象），所以出现就说明有外部动作：手工清桶、生命周期规则、从旧备份恢复库。

用法::

    PYTHONPATH=. .venv/Scripts/python.exe scripts/reap_orphans.py            # 只报告（缺省）
    PYTHONPATH=. .venv/Scripts/python.exe scripts/reap_orphans.py --delete-orphans
    PYTHONPATH=. .venv/Scripts/python.exe scripts/reap_orphans.py --delete-rows

`--delete-rows` 不可逆（配额会跟着降），先看报告再动手。`tmp/` 与未完成分片不在这里管，
那是 `scripts/reap_multipart.py`。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import dotenv  # noqa: E402

dotenv.load_dotenv()

from shared.session import blobs  # noqa: E402

_UPLOADS = "uploads/"
_SAMPLES = 10


async def _ledger() -> dict[str, tuple[str, str, str]]:
    """`{object_key: (owner, thread_id, name)}`，一次全量读台账。"""
    from sqlalchemy import select

    from aegra_api.core.orm import get_session_maker
    from custom.db.models import SessionObject

    async with get_session_maker()() as session:
        rows = (
            await session.execute(
                select(
                    SessionObject.object_key,
                    SessionObject.owner_identity,
                    SessionObject.thread_id,
                    SessionObject.name,
                )
            )
        ).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def _delete_rows(keys: list[str]) -> int:
    from sqlalchemy import delete

    from aegra_api.core.orm import get_session_maker
    from custom.db.models import SessionObject

    if not keys:
        return 0
    async with get_session_maker()() as session:
        result = await session.execute(delete(SessionObject).where(SessionObject.object_key.in_(keys)))
        await session.commit()
    return int(result.rowcount or 0)


def _show(title: str, items: list[str]) -> None:
    print(f"\n{title}: {len(items)}")
    for item in items[:_SAMPLES]:
        print(f"  {item}")
    if len(items) > _SAMPLES:
        print(f"  … 还有 {len(items) - _SAMPLES} 条")


async def main() -> int:
    ap = argparse.ArgumentParser(description="对账 uploads/ 对象与 session_object 台账")
    ap.add_argument("--delete-orphans", action="store_true", help="删掉台账里没有的对象（不可逆）")
    ap.add_argument("--delete-rows", action="store_true", help="删掉对象已不存在的台账行（不可逆）")
    args = ap.parse_args()

    if not blobs.configured():
        print("对象存储未配置（config.yaml 的 s3.endpoint 为空）", file=sys.stderr)
        return 2

    try:
        ledger = await _ledger()
    except Exception as e:  # noqa: BLE001 —— 连不上库就该停，不要拿空台账去删对象
        print(f"读台账失败，已中止（不做任何删除）：{e}", file=sys.stderr)
        return 2

    objects = blobs.list_tree(_UPLOADS)
    orphans = sorted(set(objects) - set(ledger))
    dangling = sorted(set(ledger) - set(objects))

    print(f"对象 {len(objects)} 个 / 台账 {len(ledger)} 行")
    _show("孤儿对象（有对象、无台账行）", orphans)
    _show("悬空行（有台账行、无对象）", dangling)
    if not orphans and not dangling:
        print("\n两边一致。")
        return 0

    if args.delete_orphans and orphans:
        deleted = await asyncio.to_thread(blobs.delete_many, orphans)
        print(f"\n已删孤儿对象 {deleted} 个")
    if args.delete_rows and dangling:
        removed = await _delete_rows(dangling)
        print(f"\n已删悬空台账行 {removed} 行")
    if not args.delete_orphans and not args.delete_rows:
        print("\n只是报告：加 --delete-orphans / --delete-rows 才会真删。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
