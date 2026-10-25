"""把 `graphs/okf/` 一键传到对象存储的 `okf/` 前缀（ADR-0016）。

幂等：对象已存在且字节数相同就跳过；`--prune` 顺带删掉 S3 上本地已没有的对象。

用法::

    PYTHONPATH=. .venv/Scripts/python.exe scripts/okf_push_s3.py --dry-run
    PYTHONPATH=. .venv/Scripts/python.exe scripts/okf_push_s3.py
    PYTHONPATH=. .venv/Scripts/python.exe scripts/okf_push_s3.py --prune

需要 `.env` 里的 `S3_ENDPOINT` / `S3_BUCKET`（见 `.env_example`）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import dotenv  # noqa: E402

dotenv.load_dotenv()

from custom import blobs  # noqa: E402
from graphs import okf_mirror  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="上传 OKF 到对象存储")
    ap.add_argument("--source", default=None, help="源目录，缺省 graphs/okf")
    ap.add_argument("--dry-run", action="store_true", help="只打印要做什么")
    ap.add_argument("--prune", action="store_true", help="删掉 S3 上本地已不存在的对象")
    args = ap.parse_args()

    src = Path(args.source).resolve() if args.source else okf_mirror.local_root()
    if not src.is_dir():
        print(f"源目录不存在：{src}")
        return 1

    if args.dry_run:
        files = [p for p in sorted(src.rglob("*")) if p.is_file() and not p.name.endswith(".tmp")]
        print(f"[dry-run] {len(files)} 个文件 → s3://{blobs.bucket()}/okf/")
        for p in files:
            print("  ", p.relative_to(src).as_posix())
        return 0

    try:
        blobs.ensure_bucket()
        pushed, pruned = okf_mirror.push(src, prune=args.prune)
    except Exception as e:  # noqa: BLE001 —— CLI：把话说清楚比 traceback 有用
        print(f"失败：{type(e).__name__}: {e}")
        return 1

    print(f"完成：上传 {pushed} 个，删除 {pruned} 个 → s3://{blobs.bucket()}/okf/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
