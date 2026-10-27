"""对象存储（SeaweedFS，S3 协议）的最小封装：上传文件的字节与空间镜像。

设计见 `docs/adr/0017`、`docs/large-files.md` §2/§3。几条不能改的约定：

- **归属只在台账里**：上传写的 key 是 `uploads/<uuid>/<名>`，owner/thread/名字全在 PG
  行（`session_object`）里。删会话 = 按台账逐对象删（`object_dir` 连带派生物）。
  key 不再是「按会话说」的，因为路由唯一来源必须是台账。
- **uuid 进 key**：并发上传同名文件各写各的 key，谁最后写 PG 行谁赢，另一个变成孤儿
  对象（生命周期/巡检收尾）。永远不会出现「行说 A、字节是 B」。
- **`tmp/` 是中转**：先落 `tmp/` 算 sha256，再 copy 到正式 key，最后删 tmp。
- **自己管分片**：`upload_fileobj` 的 `upload_id` 藏在内部，超限时**拿不到它来 abort**，
  未完成分片会一直占空间。这里手写 create/upload_part/complete，出错时能 abort。
- 知识空间镜像用 `okf/` 前缀（ADR-0016，正在被 object backend 取代）。

同步库（boto3），调用方一律 `asyncio.to_thread`。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import uuid
from typing import Any

from shared import config

_PART_BYTES = 8 * 1024 * 1024  # S3 要求除最后一片外 ≥5MiB
_DELETE_BATCH = 1000  # S3 单次 delete_objects 上限


class BlobTooLarge(Exception):
    """上传超过字节上限。路由层映射成 413。"""


class BlobUnavailable(Exception):
    """对象存储未配置或不可用。路由层映射成 503。"""


def _env(name: str, default: str = "") -> str:
    """凭证仍只走环境变量（不进 config.yaml）。"""
    return (os.environ.get(name) or default).strip()


def bucket() -> str:
    return config.get("s3.bucket")


def configured() -> bool:
    """有没有配对象存储。开发环境只跑磁盘时这是 False：调用方据此**跳过**而不是报错。"""
    return bool(config.get("s3.endpoint"))


_client_cache: Any = None


def client() -> Any:
    """懒加载：`graphs/tools/xlsx` 在 import 期就被拉起，模块级别建 client 会让
    没配 `S3_*` 的环境（本地 demo、单测）直接 import 失败。
    """
    global _client_cache
    if _client_cache is not None:
        return _client_cache
    endpoint = config.get("s3.endpoint")
    if not endpoint:
        raise BlobUnavailable("对象存储未配置（config.yaml 的 s3.endpoint 为空）")
    try:
        import boto3
        from botocore.config import Config
    except ImportError as e:  # pragma: no cover - 部署缺依赖时给个明白话
        raise BlobUnavailable("缺少 boto3，请先安装") from e
    _client_cache = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=_env("S3_ACCESS_KEY", "dev"),
        aws_secret_access_key=_env("S3_SECRET_KEY", "dev"),
        region_name=config.get("s3.region"),
        # 给了 endpoint_url 之后 boto3 仍默认 virtual-host，bucket.127.0.0.1:8333 解析不了
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )
    return _client_cache


def reset_client() -> None:
    """测试用：丢掉缓存的 client（改 env 后要重新建）。"""
    global _client_cache
    _client_cache = None


# ---------- key 命名（单一来源） ----------


def upload_key(name: str) -> str:
    """上传文件的 key：``uploads/<uuid>/<名>``。

    uuid 保证唯一，`<名>` 只是给人看的尾巴；**owner/thread 不在 key 里**——文件归属
    唯一来源是 PG 台账（`session_object`），不从 key 反推（否则 kex 就成了第二本账，
    改名/迁移时得两边同时改对）。
    """
    return f"uploads/{uuid.uuid4().hex}/{name}"


def object_dir(key: str) -> str:
    """一个对象的目录前缀（删文件连带它的派生物，如已重建的索引）。"""
    return key.rsplit("/", 1)[0] + "/"


def tmp_key(name: str) -> str:
    return f"tmp/{uuid.uuid4().hex}/{name}"


# ---------- 读写 ----------


def stat(key: str) -> dict[str, Any] | None:
    """HEAD 一个对象；不存在返回 None（把 404 当正常情况，调用方不必包 try）。"""
    try:
        return client().head_object(Bucket=bucket(), Key=key)
    except Exception as e:  # noqa: BLE001 —— 只有 NoSuchKey/404 该被吞
        if _is_missing(e):
            return None
        raise


def get(key: str) -> bytes:
    return client().get_object(Bucket=bucket(), Key=key)["Body"].read()


# ---------- 通用对象原语（backend 也用：LIST / 连 ETag 读 / 条件写） ----------


def get_object(key: str) -> tuple[bytes, str] | None:
    """GET 一个对象，连 ETag 一起返回。不存在返回 None（不抛）。

    ETag 是 `edit` 做 CAS 的比对基准：读到的内容与写回时的 If-Match 必须同源，
    否则「读-改-写」之间别人改了文件会被静默覆盖。
    """
    try:
        r = client().get_object(Bucket=bucket(), Key=key)
    except Exception as e:  # noqa: BLE001 —— 只有 NoSuchKey/404 该被吞
        if _is_missing(e):
            return None
        raise
    # 带引号的原始形态喂给 If-Match 最安全（各实现去引号的方式不一致）
    return r["Body"].read(), r.get("ETag") or ""


def put_bytes(
    key: str,
    body: bytes,
    *,
    if_match: str | None = None,
    if_none_match: str | None = None,
) -> str | None:
    """PUT 原始字节，可带 S3 条件头；冲突时抛 `PreconditionFailed`（调用方判定）。

    返回新 ETag。条件写要求服务端在同一把锁里判定 + 写入（SeaweedFS 0.9x 起支持），
    比「先 HEAD 再 PUT」的 last-write-wins 强：并发写不会互相覆盖到不知情。
    """
    kw: dict[str, Any] = {"Bucket": bucket(), "Key": key, "Body": body}
    if if_match:
        kw["IfMatch"] = if_match
    if if_none_match:
        kw["IfNoneMatch"] = if_none_match
    r = client().put_object(**kw)
    return r.get("ETag") or None


def list_tree(prefix: str) -> dict[str, dict[str, Any]]:
    """LIST 前缀下的全部对象（自动翻页）：`{key: {size, etag, modified}}`。

    一次返回全部而不是按分隔符分页：知识空间与会话文件都是小量级，调用方
    （backend 的 ls/glob/grep）要的是一棵完整的树，分页反而要自己拼。
    """
    s3, b = client(), bucket()
    out: dict[str, dict[str, Any]] = {}
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"Bucket": b, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            out[o["Key"]] = {
                "size": int(o.get("Size") or 0),
                "etag": o.get("ETag") or "",
                "modified": o.get("LastModified"),
            }
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")


def exists_prefix(prefix: str) -> bool:
    """前缀下有没有对象（对象存储没有空目录，目录的「存在」就是有东西）。"""
    page = client().list_objects_v2(Bucket=bucket(), Prefix=prefix, MaxKeys=1)
    return bool(page.get("Contents"))


def delete_many(keys: list[str]) -> int:
    """批量删（每批 1000，S3 上限）；返回删掉的个数。"""
    if not keys:
        return 0
    s3, b = client(), bucket()
    for i in range(0, len(keys), _DELETE_BATCH):
        batch = [{"Key": k} for k in keys[i : i + _DELETE_BATCH]]
        s3.delete_objects(Bucket=b, Delete={"Objects": batch})
    return len(keys)


def download(key: str, dest: str) -> None:
    client().download_file(bucket(), key, dest)


def put_stream(src: Any, key: str, *, max_bytes: int) -> tuple[str, int]:
    """把文件对象写成对象，同时算 sha256 与字节数；超线立刻中止并清掉分片。

    返回 `(sha256_hex, bytes)`。只有一片时走单次 PUT（简单、ETag=md5，便于对账）；
    多片时自己管 upload_id，出任何错都能 abort。空文件逰单次 PUT（S3 不接受 0 片分片）。
    """
    s3 = client()
    b = bucket()
    digest = hashlib.sha256()
    limit = f"文件超过 {max_bytes // (1024 * 1024)}MB 上限"

    first = src.read(_PART_BYTES)
    total = len(first)
    if total > max_bytes:
        raise BlobTooLarge(limit)
    digest.update(first)
    if not first:
        s3.put_object(Bucket=b, Key=key, Body=b"")
        return digest.hexdigest(), 0

    rest = src.read(_PART_BYTES)
    if not rest:  # 只有一片
        s3.put_object(Bucket=b, Key=key, Body=first)
        return digest.hexdigest(), total

    upload_id = s3.create_multipart_upload(Bucket=b, Key=key)["UploadId"]
    parts = [_upload_part(s3, b, key, upload_id, 1, first)]
    chunk, number = rest, 2
    try:
        while chunk:
            total += len(chunk)
            if total > max_bytes:
                raise BlobTooLarge(limit)
            digest.update(chunk)
            parts.append(_upload_part(s3, b, key, upload_id, number, chunk))
            number += 1
            chunk = src.read(_PART_BYTES)
        s3.complete_multipart_upload(Bucket=b, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
        return digest.hexdigest(), total
    except BaseException:
        with contextlib.suppress(Exception):  # 清理失败不该掩盖原始异常
            s3.abort_multipart_upload(Bucket=b, Key=key, UploadId=upload_id)
        raise


def _upload_part(s3: Any, b: str, key: str, upload_id: str, number: int, body: bytes) -> dict[str, Any]:
    r = s3.upload_part(Bucket=b, Key=key, UploadId=upload_id, PartNumber=number, Body=body)
    return {"ETag": r["ETag"], "PartNumber": number}


def copy(src_key: str, dst_key: str) -> None:
    """服务端复制（SeaweedFS 复用 chunk，不搬字节）。目的对象一次性出现。"""
    client().copy_object(Bucket=bucket(), Key=dst_key, CopySource={"Bucket": bucket(), "Key": src_key})


def delete(key: str) -> None:
    """幂等：不存在也算成功。"""
    client().delete_object(Bucket=bucket(), Key=key)


def delete_prefix(prefix: str) -> int:
    """删一个前缀下的全部对象，返回删掉的个数（删会话时用）。"""
    s3 = client()
    b = bucket()
    removed = 0
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"Bucket": b, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        for i in range(0, len(keys), _DELETE_BATCH):
            s3.delete_objects(Bucket=b, Delete={"Objects": keys[i : i + _DELETE_BATCH]})
            removed += len(keys[i : i + _DELETE_BATCH])
        if not page.get("IsTruncated"):
            return removed
        token = page.get("NextContinuationToken")


def abort_stale_multiparts(older_than_seconds: int = 86400, *, dry_run: bool = False) -> int:
    """清掉超时未完成的分片上传（进程被杀 / 超限中断留下的）。

    `upload_fileobj` 那条路拿不到 upload_id，只能靠这里兜底；本模块自己管分片时已经
    abort 过了，这条是给其它客户端与崩溃场景的保险。`dry_run` 只数不 abort。
    """
    from datetime import UTC, datetime, timedelta

    s3 = client()
    b = bucket()
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    aborted = 0
    kw: dict[str, Any] = {"Bucket": b}
    while True:
        page = s3.list_multipart_uploads(**kw)
        for up in page.get("Uploads", []):
            initiated = up.get("Initiated")
            if initiated is not None and initiated < cutoff:
                if not dry_run:
                    s3.abort_multipart_upload(Bucket=b, Key=up["Key"], UploadId=up["UploadId"])
                aborted += 1
        if not page.get("IsTruncated"):
            return aborted
        kw["KeyMarker"] = page.get("NextKeyMarker", "")
        kw["UploadIdMarker"] = page.get("NextUploadIdMarker", "")


def reap_tmp(older_than_seconds: int = 86400, *, dry_run: bool = False) -> int:
    """删掉 `tmp/` 下超时的上传中间对象，返回处理掉的个数。

    上传的写序是 `tmp/<uuid>/<name>` → copy 到正式 key → PG 事务 → 删 tmp。进程被杀、
    事务失败或调用方放弃时 tmp 会留下，而对象存储不会自己回收（实测见过残留）。
    超时口径按对象的最后写入时间，`older_than_seconds` 要大于一次上传的最长耗时。
    """
    from datetime import UTC, datetime, timedelta

    s3 = client()
    b = bucket()
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    removed = 0
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"Bucket": b, "Prefix": "tmp/"}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        stale = [
            {"Key": o["Key"]}
            for o in page.get("Contents", [])
            # 没有 LastModified 的就不动它：宁可留垃圾也不误删正在上传的
            if o.get("LastModified") is not None and o["LastModified"] < cutoff
        ]
        for i in range(0, len(stale), _DELETE_BATCH):
            batch = stale[i : i + _DELETE_BATCH]
            if not dry_run:
                s3.delete_objects(Bucket=b, Delete={"Objects": batch})
            removed += len(batch)
        if not page.get("IsTruncated"):
            return removed
        token = page.get("NextContinuationToken")

# ---------- 内部 ----------


def _is_missing(e: Exception) -> bool:
    response = getattr(e, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def ensure_bucket() -> None:
    """启动时调一次：bucket 不存在就建（幂等）。"""
    s3 = client()
    b = bucket()
    try:
        s3.head_bucket(Bucket=b)
    except Exception:  # noqa: BLE001 —— 不存在与无权都是这一句，直接尝试创建
        s3.create_bucket(Bucket=b)


__all__ = [
    "BlobTooLarge",
    "BlobUnavailable",
    "abort_stale_multiparts",
    "bucket",
    "client",
    "configured",
    "copy",
    "delete",
    "delete_many",
    "delete_prefix",
    "download",
    "ensure_bucket",
    "exists_prefix",
    "get",
    "get_object",
    "list_tree",
    "object_dir",
    "put_bytes",
    "put_stream",
    "reap_tmp",
    "reset_client",
    "tmp_key",
    "upload_key",
]
