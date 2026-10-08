"""Literal text ingress and continuation; only the Telegram boundary is fake."""
from __future__ import annotations

import base64
import hashlib
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.document_page_reader import read_document_page
from telegram_search_mcp.document_reader import read_document


class TextProvider:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.file_name = "fixture.tsv"
        self.mime_type = "application/octet-stream"
        self.downloads = 0

    def ensure_ready(self): pass
    def get_account_id(self): return 17
    def request_budget(self, deadline): return nullcontext()
    def close(self): pass
    def resolve_target(self, chat_id):
        return {"id": chat_id, "type": {"@type": "chatTypePrivate", "user_id": 17}}

    def get_message(self, chat_id, message_id):
        return {"@type": "message", "chat_id": chat_id, "id": message_id,
                "content": {"@type": "messageDocument", "document": {
                    "file_name": self.file_name, "mime_type": self.mime_type,
                    "document": {"@type": "file", "id": 12,
                                 "size": self.source.stat().st_size}}}}

    def download_file(self, file_id, **options):
        if file_id != 12 or options != {"max_bytes": 64 * 1024 * 1024, "timeout": 300}:
            raise AssertionError("unexpected synthetic download request")
        self.downloads += 1
        return self.source


class TextContinuationFormatsTests(unittest.TestCase):
    # Independent literal expectations, not imported reader admission constants.
    TEXT_FORMATS = (
        ("txt", "text/plain"), ("text", "text/plain"), ("md", "text/markdown"),
        ("csv", "text/csv"), ("tsv", "text/tab-separated-values"),
        ("json", "application/json"), ("xml", "application/xml"),
        ("yaml", "application/yaml"), ("yml", "application/yaml"),
        ("py", "application/x-python"), ("js", "application/javascript"),
        ("ts", "text/plain"), ("tsx", "text/plain"), ("jsx", "text/plain"),
        ("html", "text/html"), ("css", "text/css"), ("sh", "text/plain"),
        ("sql", "text/plain"), ("log", "text/plain"), ("toml", "text/plain"),
    )

    def setUp(self) -> None:
        sandbox = tempfile.TemporaryDirectory(prefix="text-continuation-tests-")
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.source = self.downloads / "source"
        self.source.write_bytes(b"A\xc3\xa9\xf0\x9f\x99\x82\tB\r\n")
        self.provider = TextProvider(self.source)
        self.store = ArtifactStore(cache_dir=self.root / "artifacts")
        self.broker = Broker(
            socket_path=self.root / "unused.sock", artifact_store=self.store,
            client_factory=lambda: self.provider, download_source_root=self.downloads,
            policy=RuntimePolicy(enabled_capabilities=("artifacts", "attachment_pages")),
        )
        self.addCleanup(self.broker._executor.shutdown)

    def dispatch(self, operation, payload):
        return self.broker._dispatch({
            "operation": operation, "payload": payload, "client_id": "client_" + "a" * 24,
            "deadline": time.monotonic() + 30, "broker_generation": self.broker._generation,
        })

    def create(self, name, text, *, encoded=False):
        payload = {"file_name": name}
        if encoded:
            data = text if isinstance(text, bytes) else text.encode("utf-8")
            payload["content_base64"] = base64.b64encode(data).decode("ascii")
        else:
            payload["content"] = text
        return self.dispatch("create_local_artifact", payload)

    def page(self, artifact_id, **options):
        return self.dispatch("read_attachment_page", {
            "artifact_id": artifact_id, "render_pages": False, **options,
        })

    def read_path(self, path, **options):
        data = path.read_bytes()
        return read_document_page(
            path, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
            name=path.name, mime_type=None, render_pages=False, **options,
        )

    def test_local_text_and_base64_admit_every_lexical_suffix_with_readable_metadata(self):
        for suffix, mime_type in self.TEXT_FORMATS:
            for encoded in (False, True):
                with self.subTest(suffix=suffix, encoded=encoded):
                    made = self.create(f"local.{suffix}", "Aé🙂\tB\r\n", encoded=encoded)
                    self.assertEqual(made["status"], "complete")
                    self.assertEqual(Path(made["artifact_path"]).read_bytes(), b"A\xc3\xa9\xf0\x9f\x99\x82\tB\r\n")
                    self.assertEqual(self.broker._attachment_metadata(made["artifact_id"]),
                                     (None, f"local.{suffix}", mime_type, "document"))
                    result = self.page(made["artifact_id"])
                    self.assertEqual((result["status"], result["text"]), ("complete", "Aé🙂\tB\n"))

    def test_transfer_admits_every_text_suffix_even_without_text_mime(self):
        for suffix, _ in self.TEXT_FORMATS:
            with self.subTest(suffix=suffix):
                self.provider.file_name = f"transfer.{suffix}"
                transferred = self.dispatch("get_attachment", {"anchor": {"chat_id": 17, "message_id": 1024}})
                self.assertEqual(transferred["status"], "complete")
                self.assertEqual(Path(transferred["artifact_path"]).read_bytes(), self.source.read_bytes())
                result = self.page(transferred["artifact_id"])
                self.assertEqual((result["status"], result["text"]), ("complete", "Aé🙂\tB\n"))
                self.assertEqual(result["scope"]["source_anchor"], {"chat_id": 17, "message_id": 1024})

    def test_uppercase_suffixes_work_through_both_ingress_routes(self):
        for suffix in ("TSV", "TEXT", "TSX", "SH"):
            with self.subTest(suffix=suffix):
                made = self.create(f"local.{suffix}", "upper\tcase")
                self.assertEqual(made["status"], "complete")
                self.assertEqual(self.page(made["artifact_id"])["text"], "upper\tcase")
                self.provider.file_name = f"transfer.{suffix}"
                transferred = self.dispatch("get_attachment", {"anchor": {"chat_id": 17, "message_id": 1024}})
                self.assertEqual(transferred["status"], "complete")
                self.assertEqual(self.page(transferred["artifact_id"])["text"], "Aé🙂\tB\n")

    def test_suffix_only_tsv_is_supported_by_isolated_reader(self):
        path = self.root / "literal.TSV"
        path.write_bytes(b"a\tb\r\nc\td\r")
        current = self.read_path(path)
        self.assertEqual((current.status, current.kind, current.text), ("complete", "text", "a\tb\nc\td\n"))

    def test_suffix_only_tsv_legacy_reader_remains_prefix_only(self):
        path = self.root / "literal.TSV"
        path.write_bytes(b"a\tb\r\nc\td\r")
        legacy = read_document(path, max_chars=3)
        self.assertEqual((legacy.status, legacy.text), ("partial", "a\tb"))

    def test_csv_quoted_multiline_fields_empty_columns_and_formulas_remain_literal(self):
        made = self.create("quoted.csv", 'a,,"line1\r\nline2",=1+2\r\n"é🙂","tab\tvalue",\r\n')
        self.assertEqual(made["status"], "complete")
        cursor = None
        fragments = []
        for expected in ('a,,"line1\n', 'line2",=1+', '2\n"é🙂","ta', 'b\tvalue",\n'):
            arguments = {"max_chars": 10}
            if cursor is not None:
                arguments["cursor"] = cursor
            result = self.page(made["artifact_id"], **arguments)
            self.assertEqual(result["text"], expected)
            self.assertEqual(result["text_start"], len("".join(fragments)))
            fragments.append(result["text"])
            self.assertEqual(result["text_end"], len("".join(fragments)))
            cursor = result["next_cursor"]
        self.assertEqual((result["status"], result["has_more"], cursor), ("complete", False, None))
        self.assertEqual("".join(fragments), 'a,,"line1\nline2",=1+2\n"é🙂","tab\tvalue",\n')

    def test_tsv_windows_count_unicode_codepoints_and_preserve_quotes_and_empty_cells(self):
        made = self.create("unicode.tsv", 'Aé🙂\t\t"e\u0301\r\n字"\t=1+2\rZ', encoded=True)
        self.assertEqual(made["status"], "complete")
        cursor = None
        fragments = []
        for start, end, expected in ((0, 3, 'Aé🙂'), (3, 6, '\t\t"'), (6, 9, 'e\u0301\n'),
                                     (9, 12, '字"\t'), (12, 15, '=1+'), (15, 18, '2\nZ')):
            arguments = {"max_chars": 3}
            if cursor is not None:
                arguments["cursor"] = cursor
            result = self.page(made["artifact_id"], **arguments)
            self.assertEqual((result["text_start"], result["text_end"], result["text"]), (start, end, expected))
            fragments.append(result["text"])
            cursor = result["next_cursor"]
        self.assertEqual((result["status"], result["has_more"], cursor), ("complete", False, None))
        self.assertEqual("".join(fragments), 'Aé🙂\t\t"e\u0301\n字"\t=1+2\nZ')

    def test_json_markdown_and_source_remain_literal_without_interpretation(self):
        cases = (("malformed.json", '{"a": [1,\r\n  unclosed'),
                 ("literal.md", '# Heading\r\n**bold** [link](javascript:void(0))\r\n```\t=1+2'),
                 ("literal.py", 'raise RuntimeError("must not execute")\r\n'))
        expected = ('{"a": [1,\n  unclosed', '# Heading\n**bold** [link](javascript:void(0))\n```\t=1+2',
                    'raise RuntimeError("must not execute")\n')
        for (name, text), literal in zip(cases, expected):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(text.encode("utf-8"))
                result = self.read_path(path)
                self.assertEqual((result.status, result.text), ("complete", literal))

    def test_invalid_utf8_never_returns_valid_prefix_or_cursor(self):
        for suffix in ("csv", "tsv", "json"):
            with self.subTest(suffix=suffix):
                made = self.create(f"invalid.{suffix}", b"valid prefix\r\n\xffprivate", encoded=True)
                self.assertEqual(made["status"], "complete")
                result = self.page(made["artifact_id"], max_chars=3)
                self.assertEqual((result["status"], result["text"], result["has_more"], result["next_cursor"]),
                                 ("error", "", False, None))
                self.assertNotIn("private", result["detail"])
                path = self.root / f"invalid.{suffix}"
                path.write_bytes(b"valid prefix\r\n\xffprivate")
                parser = self.read_path(path, offset=3, max_chars=3)
                self.assertEqual((parser.status, parser.text, parser.images, parser.has_more), ("error", "", (), False))

    def test_unsupported_suffixes_stay_closed_and_binary_content_stays_base64_only(self):
        for suffix in ("bin", "exe", "rtf", "zip"):
            for encoded in (False, True):
                with self.subTest(suffix=suffix, encoded=encoded):
                    made = self.create(f"unsupported.{suffix}", "plain", encoded=encoded)
                    self.assertEqual((made["status"], made["artifact_id"]), ("error", None))
            self.provider.file_name = f"unsupported.{suffix}"
            self.provider.mime_type = "text/plain"
            before = self.provider.downloads
            denied = self.dispatch("get_attachment", {"anchor": {"chat_id": 17, "message_id": 1024}})
            self.assertEqual((denied["status"], denied["artifact_id"], self.provider.downloads), ("unsupported", None, before))
        for suffix in ("pdf", "docx", "xlsx", "pptx", "png", "jpg", "jpeg", "webp", "wav", "mp3", "ogg", "mp4", "mov", "webm"):
            with self.subTest(binary=suffix):
                denied = self.create(f"binary.{suffix}", "plain")
                self.assertEqual((denied["status"], denied["artifact_id"]), ("error", None))
                allowed = self.create(f"binary.{suffix}", b"\x00\xff", encoded=True)
                self.assertEqual(allowed["status"], "complete")
                self.assertEqual(Path(allowed["artifact_path"]).read_bytes(), b"\x00\xff")


if __name__ == "__main__":
    unittest.main()
