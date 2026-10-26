"""清掉上传留下的垃圾：超时未完成的分片 + `tmp/` 中间对象（ADR-0015）。

两样东西都不会被对象存储自动回收：
- **分片**：`upload_fileobj` 那条路拿不到 `upload_id`，进程被杀就永远占着空间；
- **`tmp/` 对象**：上传写序是 `tmp/` → copy 到正式 key → PG 事务 → 删 `tmp/`，
  中途崩或事务失败就留在那里（实测见过残留）。

用法::

    PYTHONPATH=. .venv/Scripts/python.exe scripts/reap_multipart.py --dry-run
    PYTHONPATH=. .venv/Scripts/python.exe scripts/reap_multipart.py --older-than-hours 24

建议 cron / Windows 任务计划每几小时跑一次；不想跑脚本的话，用对象存储的生命周期规则
在 `tmp/` 前缀上兜底（abort 未完成分片是 S3 自己的 `AbortIncompleteMultipartUpload`）。

注意这是**运维脚本**，不是 crons API：aegra 的定时任务只触发图 run。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import dotenv  # noqa: E402

dotenv.load_dotenv()

from shared.session import blobs  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="清理未完成的上传（分片 + tmp/ 对象）")
    ap.add_argument("--older-than-hours", type=float, default=24.0, help="超过多久算垃圾，缺省 24 小时")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不删也不 abort")
    ap.add_argument("--skip-multipart", action="store_true", help="不处理未完成分片")
    ap.add_argument("--skip-tmp", action="store_true", help="不处理 tmp/ 对象")
    args = ap.parse_args()

    if not blobs.configured():
        print("对象存储未配置（config.yaml 的 s3.endpoint 为空），无事可做")
        return 0

    seconds = int(args.older_than_hours * 3600)
    suffix = "（dry-run，未改动任何东西）" if args.dry_run else ""

    if not args.skip_tmp:
        n = blobs.reap_tmp(seconds, dry_run=args.dry_run)
        print(f"tmp/ 对象：{n} 个超过 {args.older_than_hours:g} 小时{suffix}")
    if not args.skip_multipart:
        n = blobs.abort_stale_multiparts(seconds, dry_run=args.dry_run)
        print(f"未完成分片：{n} 个超过 {args.older_than_hours:g} 小时{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
