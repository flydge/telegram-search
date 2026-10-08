from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.local_upload import LocalUploadRegistry, UploadError


class LocalUploadTests(unittest.TestCase):
    def test_large_completed_uploads_have_bounded_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ArtifactStore(cache_dir=root / "artifacts")
            registry = LocalUploadRegistry(store, root=root / "uploads",
                                           quarantine_limit_bytes=12 * 1024 * 1024)
            data = b"x" * (11 * 1024 * 1024)
            chunk = data[:512 * 1024]
            encoded = base64.b64encode(chunk).decode()
            for _ in range(2):
                upload = registry.begin(file_name="large.bin", kind="document", size_bytes=len(data),
                                        sha256=hashlib.sha256(data).hexdigest())
                for index in range(len(data) // len(chunk)):
                    registry.append(upload, index=index, content_base64=encoded,
                                    sha256=hashlib.sha256(chunk).hexdigest())
                self.assertEqual(registry.finish(upload).size_bytes, len(data))
            quarantine = root / "uploads-quarantine"
            self.assertLessEqual(sum(item.stat().st_size for item in quarantine.iterdir()), 12 * 1024 * 1024)

    def test_two_chunks_finish_to_exact_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ArtifactStore(cache_dir=root / "artifacts")
            registry = LocalUploadRegistry(store, root=root / "uploads")
            payload = b"alpha" + b"beta"
            upload = registry.begin(file_name="report.txt", kind="document", size_bytes=len(payload),
                                    sha256=hashlib.sha256(payload).hexdigest())
            for index, part in enumerate((b"alpha", b"beta")):
                result = registry.append(upload, index=index, content_base64=base64.b64encode(part).decode(),
                                         sha256=hashlib.sha256(part).hexdigest())
                self.assertEqual(result, (index + 1, 5 if index == 0 else 9))
            artifact = registry.finish(upload)
            self.assertEqual(artifact.path.read_bytes(), payload)
            with self.assertRaises(UploadError):
                registry.finish(upload)

    def test_bad_order_hash_and_declared_size_never_create_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ArtifactStore(cache_dir=root / "artifacts")
            registry = LocalUploadRegistry(store, root=root / "uploads")
            upload = registry.begin(file_name="photo.png", kind="photo", size_bytes=3,
                                    sha256=hashlib.sha256(b"abc").hexdigest())
            encoded = base64.b64encode(b"abc").decode()
            with self.assertRaises(UploadError):
                registry.append(upload, index=1, content_base64=encoded,
                                sha256=hashlib.sha256(b"abc").hexdigest())
            with self.assertRaises(UploadError):
                registry.append(upload, index=0, content_base64=encoded, sha256="0" * 64)
            self.assertEqual(registry.append(upload, index=0, content_base64=encoded,
                                             sha256=hashlib.sha256(b"abc").hexdigest()), (1, 3))
            with self.assertRaises(UploadError):
                registry.append(upload, index=1, content_base64=encoded,
                                sha256=hashlib.sha256(b"abc").hexdigest())
            self.assertEqual(registry.finish(upload).size_bytes, 3)

    def test_size_limit_and_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [100.0]
            registry = LocalUploadRegistry(ArtifactStore(cache_dir=root / "artifacts"),
                                           root=root / "uploads", clock=lambda: now[0])
            with self.assertRaises(UploadError):
                registry.begin(file_name="huge.png", kind="photo", size_bytes=10_000_001,
                               sha256="0" * 64)
            upload = registry.begin(file_name="small.txt", kind="document", size_bytes=1,
                                    sha256="0" * 64)
            now[0] += 901
            with self.assertRaises(UploadError):
                registry.finish(upload)
            self.assertEqual(list((root / "uploads").glob("upload_*")), [])
