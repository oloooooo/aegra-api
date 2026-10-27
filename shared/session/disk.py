"""会话上传字节的本地盘存储（本分支不接对象存储）。

key 布局：`<uuid4hex>/<文件名>`，落在 `uploads.dir`（默认 `.data/uploads`）下。
**归属不写在 key 里**：一个 key 属于哪个用户、哪个会话，只记 PG 的 `session_object` 行。
这样同一份字节可以出现在任何会话里，而换归属不需要搬字节；代价是没有行就没有归属，
孤儿对象只能靠对账脚本清（见 `docs/large-files.md`）。

写入一律 `<name>.part-<rand>` → `os.replace`（同一目录内，原子）。所以：
- 任何时刻都不会有「半截文件」被读到；
- 崩在中间只留 `.part` 垃圾，不会出现「行说 A、字节是 B」。

key 只接受 `<32 位小写 hex>/<单层文件名>`。台账里的值来自本模块，这里只是最后一道闸：
不合规的 key 一律当「不存在」处理，绝不拼出 `..` 去删别的目录。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import uuid
from pathlib import Path

from shared import config

_ROOT = Path(config.ROOT)
_KEY = re.compile(r"^[0-9a-f]{32}/[^/\\\x00-\x1f]{1,200}$")
_CHUNK = 64 * 1024


class TooLarge(Exception):
    """超过调用方给的上限（异常文案直接给 HTTP 层用）。"""


def root() -> Path:
    return _ROOT / config.get("uploads.dir")


def new_key(name: str) -> str:
    """给一次新上传分配 key。不再按 sha 去重：复用字节会让删除与配额互相踩。"""
    return f"{uuid.uuid4().hex}/{name}"


def _path(key: str) -> Path | None:
    """key → 绝对路径；不合规返回 None（调用方当不存在处理）。"""
    if not _KEY.match(key):
        return None
    return root() / key


def exists(key: str) -> bool:
    path = _path(key)
    return bool(path and path.is_file())


def read(key: str) -> bytes:
    path = _path(key)
    if path is None:
        raise FileNotFoundError(key)
    return path.read_bytes()


def delete(key: str) -> bool:
    """删一个对象；不存在返回 False（幂等）。"""
    path = _path(key)
    if path is None or not path.is_file():
        return False
    path.unlink(missing_ok=True)
    _prune(path.parent)
    return True


def delete_many(keys: list[str]) -> int:
    return sum(1 for key in keys if delete(key))


def put_stream(fileobj, key: str, *, max_bytes: int) -> tuple[int, str]:
    """边读边算 sha256 落盘；超限抛 `TooLarge`。返回 (字节数, sha256 全 64 位 hex)。

    先写 `.part-*` 再 `os.replace`：读到的永远是完整文件（原子写）。
    """
    path = _path(key)
    if path is None:
        raise ValueError(f"非法 key: {key!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.part-{uuid.uuid4().hex[:8]}")
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as out:
            while chunk := fileobj.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise TooLarge(f"文件超过 {max_bytes // 1024 // 1024}MB 上限")
                digest.update(chunk)
                out.write(chunk)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return size, digest.hexdigest()


def put_bytes(key: str, body: bytes) -> str:
    """写一小块字节（缓存/测试用）。返回 sha256 全 64 位 hex。"""
    path = _path(key)
    if path is None:
        raise ValueError(f"非法 key: {key!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.part-{uuid.uuid4().hex[:8]}")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    return hashlib.sha256(body).hexdigest()


def _prune(directory: Path) -> None:
    """删空掉的 uuid 目录（失败无所谓，留个空目录不影响正确性）。"""
    with contextlib.suppress(OSError):
        directory.rmdir()
