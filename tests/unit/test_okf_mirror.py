"""知识空间根：local 直读盘、s3 读本地镜像（ADR-0016）。

锁的是「镜像怎么算新鲜」「push 幂等 / prune 只删本地没有的」——这两条错了会表现为
「某个副本读到的空间内容跟别人不一样」，最难查的那类问题。
"""

from pathlib import Path

import pytest

from graphs import okf_mirror


class FakeS3:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.listed = 0
        self.uploaded: list[str] = []

    def list_objects_v2(self, **_kw):
        self.listed += 1
        return {
            "Contents": [{"Key": k, "Size": len(v)} for k, v in self.objects.items()],
            "IsTruncated": False,
        }

    def upload_file(self, src, _bucket, key):
        self.uploaded.append(key)
        self.objects[key] = b"x" * 1  # 内容不重要，大小由后续断言控制


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(okf_mirror, "_last_pull", 0.0)
    monkeypatch.setenv("OKF_MIRROR_DIR", str(tmp_path / "mirror"))
    monkeypatch.delenv("OKF_MIRROR_TTL_SECONDS", raising=False)
    return tmp_path


def test_local_mode_uses_graphs_okf(monkeypatch):
    monkeypatch.setenv("OKF_BACKEND", "local")
    assert okf_mirror.knowledge_root() == okf_mirror.local_root()


def test_s3_mode_pulls_once_then_reuses(env, monkeypatch):
    monkeypatch.setenv("OKF_BACKEND", "s3")
    fake = FakeS3({"okf/sp/a.md": b"hi"})
    downloaded: list[str] = []
    monkeypatch.setattr(okf_mirror.blobs, "client", lambda: fake)
    monkeypatch.setattr(okf_mirror.blobs, "bucket", lambda: "b")

    def fake_download(key, dest):
        downloaded.append(key)
        Path(dest).write_bytes(b"hi")

    monkeypatch.setattr(okf_mirror.blobs, "download", fake_download)

    root = okf_mirror.knowledge_root()
    assert root == env / "mirror"
    assert downloaded == ["okf/sp/a.md"]

    okf_mirror.knowledge_root()  # 镜像已在 → 不再 list
    assert fake.listed == 1


def test_ttl_expiry_triggers_repull(env, monkeypatch):
    monkeypatch.setenv("OKF_BACKEND", "s3")
    monkeypatch.setenv("OKF_MIRROR_TTL_SECONDS", "0.001")
    fake = FakeS3({"okf/sp/a.md": b"hi"})
    monkeypatch.setattr(okf_mirror.blobs, "client", lambda: fake)
    monkeypatch.setattr(okf_mirror.blobs, "bucket", lambda: "b")
    monkeypatch.setattr(okf_mirror.blobs, "download", lambda key, dest: Path(dest).write_bytes(b"hi"))

    okf_mirror.knowledge_root()
    okf_mirror._last_pull = 0.0  # 等价于过期
    okf_mirror.knowledge_root()
    assert fake.listed == 2


def test_pull_skips_same_size_and_redownloads_changed(env, monkeypatch):
    monkeypatch.setattr(okf_mirror, "mirror_root", lambda: env / "mirror")
    (env / "mirror" / "sp").mkdir(parents=True)
    (env / "mirror" / "sp" / "same.md").write_text("hi", encoding="utf-8")  # 2 字节
    (env / "mirror" / "sp" / "grown.md").write_text("h", encoding="utf-8")  # 1 字节

    fake = FakeS3({"okf/sp/same.md": b"hi", "okf/sp/grown.md": b"hi"})
    downloaded: list[str] = []
    monkeypatch.setattr(okf_mirror.blobs, "client", lambda: fake)
    monkeypatch.setattr(okf_mirror.blobs, "bucket", lambda: "b")

    def fake_download(key, dest):
        downloaded.append(key)
        with open(dest, "wb") as fh:
            fh.write(b"hi")

    monkeypatch.setattr(okf_mirror.blobs, "download", fake_download)

    written = okf_mirror.pull(env / "mirror")

    assert downloaded == ["okf/sp/grown.md"]
    assert written == 1


def test_push_is_idempotent_and_prunes(env, monkeypatch):
    src = env / "src"
    (src / "sp").mkdir(parents=True)
    (src / "sp" / "a.md").write_text("hi", encoding="utf-8")
    fake = FakeS3({"okf/sp/a.md": b"hi", "okf/sp/gone.md": b"zz"})
    monkeypatch.setattr(okf_mirror.blobs, "client", lambda: fake)
    monkeypatch.setattr(okf_mirror.blobs, "bucket", lambda: "b")
    deleted: list[str] = []
    monkeypatch.setattr(okf_mirror.blobs, "delete", lambda key: deleted.append(key))

    pushed, pruned = okf_mirror.push(src)
    assert (pushed, pruned) == (0, 0)  # 大小相同 → 不重传
    assert deleted == []

    pushed, pruned = okf_mirror.push(src, prune=True)
    assert (pushed, pruned) == (0, 1)
    assert deleted == ["okf/sp/gone.md"]


def test_push_uploads_new_files(env, monkeypatch):
    src = env / "src"
    src.mkdir()
    (src / "new.md").write_text("hi", encoding="utf-8")
    fake = FakeS3()
    monkeypatch.setattr(okf_mirror.blobs, "client", lambda: fake)
    monkeypatch.setattr(okf_mirror.blobs, "bucket", lambda: "b")

    pushed, pruned = okf_mirror.push(src)

    assert (pushed, pruned) == (1, 0)
    assert fake.uploaded == ["okf/new.md"]
