"""把对象存储的 `okf/` 前缀拉到本地镜像（`okf.backend: s3` 用，ADR-0016）。

`knowledge_root()` 在镜像缺失时会自动调用它；手动跑用于「空间内容改了，想让各副本立刻生效」。

用法::

    PYTHONPATH=. .venv/Scripts/python.exe scripts/okf_pull_s3.py
    PYTHONPATH=. .venv/Scripts/python.exe scripts/okf_pull_s3.py --dest .cache/okf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import dotenv  # noqa: E402

dotenv.load_dotenv()

from shared.knowledge import root as okf_mirror  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="从对象存储拉取 OKF 镜像")
    ap.add_argument("--dest", default=None, help="镜像目录，缺省 config.yaml 的 okf.mirror_dir（或 .cache/okf）")
    args = ap.parse_args()

    dest = Path(args.dest).resolve() if args.dest else okf_mirror.mirror_root()
    try:
        written = okf_mirror.pull(dest)
    except Exception as e:  # noqa: BLE001
        print(f"失败：{type(e).__name__}: {e}")
        return 1

    print(f"完成：{dest} 更新 {written} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
