"""Trusted-local upload rejects unsafe sources and unverified broker outcomes."""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.broker_client import BrokerClient, BrokerUnavailable
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.schemas import ReadAttachmentRequest
from test_uploaded_artifact_read import running_broker


class BoundaryClient:
    """Mutate/lose only a real broker response, or change the source at a boundary."""
    def __init__(self, client, *, boundary, change=None, action=None):
        self.client = client
        self.boundary = boundary
        self.change = change
        self.action = action
        self.dispatched = []

    def _invoke(self, operation, request):
        self.dispatched.append(operation)
        response = getattr(self.client, operation)(request)
        if operation == self.boundary:
            if self.action:
                self.action()
            if self.change == "lost":
                raise BrokerUnavailable("sensitive untrusted transport detail")
            if self.change == "interrupt":
                raise KeyboardInterrupt
            if self.change:
                response = response.model_copy(update=self.change)
        return response

    def begin_local_upload(self, request):
        return self._invoke("begin_local_upload", request)

    def append_local_upload(self, request):
        return self._invoke("append_local_upload", request)

    def finish_local_upload(self, request):
        return self._invoke("finish_local_upload", request)


class TransferCliTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("telegram_search_mcp.transfer_cli"),
                             "trusted-local transfer helper is not implemented")
        self.module = importlib.import_module("telegram_search_mcp.transfer_cli")
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="f10a-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.txt"
        self.source.write_bytes(b"Verified upload evidence")

    def invoke(self, client, source=None, *, file_name=None, kind="document"):
        return self.module.upload_source(source=str(source or self.source), kind=kind,
                                         file_name=file_name, client=client)

    def test_streamed_two_pass_upload_returns_exact_receipt_and_readable_artifact(self):
        # Removing chunk bounds or sending bytes out of order breaks a genuine upload.
        with self.source.open("wb") as output:
            block = b"abcdef0123456789" * 4096
            for _ in range(17):
                output.write(block)
        digest = hashlib.sha256()
        with self.source.open("rb") as source:
            while data := source.read(65536):
                digest.update(data)
        with running_broker(self.root) as (_, client, store):
            receipt = self.invoke(client, file_name="verified.txt")
            self.assertEqual(receipt, {"status": "complete", "artifact_id": receipt["artifact_id"],
                                     "size_bytes": 1114112, "sha256": digest.hexdigest()})
            self.assertEqual(store.lookup(receipt["artifact_id"]).size_bytes, 1114112)
            read = client.read_attachment(ReadAttachmentRequest(artifact_id=receipt["artifact_id"], max_chars=40))
            self.assertIn("abcdef0123456789", read.text)

    def test_leaf_parent_symlink_traversal_and_relative_sources_fail_before_begin(self):
        link = self.root / "leaf.txt"
        link.symlink_to(self.source)
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "file.txt").write_bytes(b"safe bytes")
        parent_link = self.root / "parent-link"
        parent_link.symlink_to(directory, target_is_directory=True)
        bad = [str(link), str(parent_link / "file.txt"),
               str(self.root) + "/directory/../source.txt", "relative.txt"]
        with running_broker(self.root) as (broker, client, _):
            for source in bad:
                with self.subTest(source=source), self.assertRaises(self.module.TransferError):
                    self.invoke(client, source)
                self.assertEqual(broker._uploads._entries, {})

    def test_nonregular_multilink_nonowner_empty_and_oversize_sources_fail_before_begin(self):
        folder = self.root / "folder"
        folder.mkdir()
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        hardlink = self.root / "hardlink.txt"
        os.link(self.source, hardlink)
        empty = self.root / "empty.txt"
        empty.touch()
        oversized = self.root / "oversize.png"
        with oversized.open("wb") as output:
            output.truncate(10_000_001)  # below the normal-suite 10 MiB fixture ceiling
        with running_broker(self.root) as (broker, client, _):
            for source, kind in [(folder, "document"), (fifo, "document"),
                                 (self.source, "document"), (empty, "document"), (oversized, "photo")]:
                with self.subTest(source=source), self.assertRaises(self.module.TransferError):
                    self.invoke(client, source, kind=kind)
                self.assertEqual(broker._uploads._entries, {})
            hardlink.unlink()
            with patch.object(self.module.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaises(self.module.TransferError):
                    self.invoke(client)
            self.assertEqual(broker._uploads._entries, {})

    def test_unsafe_display_name_and_kind_are_sanitized_failures_before_begin(self):
        with running_broker(self.root) as (broker, client, _):
            for name in ["../secret.txt", "/secret.txt", "résumé.txt", ".hidden", "a" * 129]:
                with self.subTest(name=name), self.assertRaises(self.module.TransferError):
                    self.invoke(client, file_name=name)
            with self.assertRaises(self.module.TransferError):
                self.invoke(client, kind="send")
            self.assertEqual(broker._uploads._entries, {})

    def test_source_replacement_before_begin_is_detected_after_rewind(self):
        rewind = self.module.os.lseek
        def replace_then_rewind(fd, offset, whence):
            replacement = self.root / "replacement.txt"
            replacement.write_bytes(b"Verified upload evidence")
            os.replace(replacement, self.source)
            return rewind(fd, offset, whence)
        with running_broker(self.root) as (broker, client, store):
            with patch.object(self.module.os, "lseek", side_effect=replace_then_rewind):
                with self.assertRaises(self.module.TransferError):
                    self.invoke(client)
            self.assertEqual(broker._uploads._entries, {})
            self.assertEqual(list(store.cache_dir.glob("artifact_*")), [])

    def test_source_mutation_growth_leaf_and_parent_replacement_never_finish(self):
        for mutation in ("bytes", "growth", "leaf", "parent"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir="/private/tmp", prefix="f10a-") as directory:
                root = Path(directory)
                parent = root / "source-dir"
                parent.mkdir()
                source = parent / "report.txt"
                source.write_bytes(b"abcdef")
                def change_source():
                    if mutation == "bytes":
                        source.write_bytes(b"ghijkl")
                    elif mutation == "growth":
                        with source.open("ab") as output:
                            output.write(b"g")
                    elif mutation == "leaf":
                        other = parent / "replacement.txt"
                        other.write_bytes(b"abcdef")
                        os.replace(other, source)
                    else:
                        parent.rename(root / "old-dir")
                        parent.mkdir()
                        (parent / "report.txt").write_bytes(b"abcdef")
                with running_broker(root) as (broker, real_client, store):
                    boundary = "begin_local_upload" if mutation == "bytes" else "append_local_upload"
                    client = BoundaryClient(real_client, boundary=boundary, action=change_source)
                    with self.assertRaises(self.module.TransferError):
                        self.invoke(client, source)
                    self.assertNotIn("finish_local_upload", client.dispatched)
                    self.assertEqual(broker._artifact_metadata, {})
                    self.assertEqual(list(store.cache_dir.glob("artifact_*")), [])

    def test_wrong_begin_append_and_finish_receipts_are_never_success(self):
        faults = [
            ("begin_local_upload", {"status": "error"}),
            ("begin_local_upload", {"upload_id": "bad-upload"}),
            ("append_local_upload", {"status": "error"}),
            ("append_local_upload", {"upload_id": "upload_" + "0" * 32}),
            ("append_local_upload", {"next_index": 2}),
            ("append_local_upload", {"received_bytes": 1}),
            ("finish_local_upload", {"status": "error"}),
            ("finish_local_upload", {"sha256": "0" * 64}),
            ("finish_local_upload", {"size_bytes": 1}),
            ("finish_local_upload", {"artifact_id": "artifact_" + "0" * 32 + "_" + "0" * 64 + "_1"}),
            ("finish_local_upload", {"file_name": "other.txt"}),
        ]
        for index, (operation, change) in enumerate(faults):
            with self.subTest(operation=operation, change=change):
                with running_broker(self.root / ("case" + str(index))) as (_, real_client, _):
                    client = BoundaryClient(real_client, boundary=operation, change=change)
                    with self.assertRaises(self.module.TransferError):
                        self.invoke(client)
                    self.assertEqual(client.dispatched.count(operation), 1)

    def test_default_cli_rejects_raw_coercion_and_unknown_fields_from_real_transport(self):
        # The general proxy is permissive about numeric response coercion. The
        # local helper must reject malformed wire receipts before that coercion.
        self.source.write_bytes(b"a")
        faults = [("append_local_upload", {"next_index": True}),
                  ("append_local_upload", {"received_bytes": "1"}),
                  ("finish_local_upload", {"size_bytes": "1"}),
                  ("finish_local_upload", {"unexpected": "untrusted field"})]
        for index, (operation, change) in enumerate(faults):
            with self.subTest(operation=operation, change=change), running_broker(self.root / str(index)) as (broker, _, _):
                dispatch = broker._dispatch
                dispatched = []
                def corrupt_receipt(request):
                    result = dispatch(request)
                    if request["operation"] == operation:
                        dispatched.append(operation)
                        result.update(change)
                    return result
                output, error = io.StringIO(), io.StringIO()
                with patch.object(broker, "_dispatch", side_effect=corrupt_receipt), \
                     patch.object(self.module, "BROKER_SOCKET_PATH", broker._socket_path), \
                     patch.object(self.module, "load_runtime_policy", return_value=broker._policy), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                    result = self.module.main(["upload", "--source", str(self.source), "--kind", "document"])
                self.assertNotEqual(result, 0)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(json.loads(error.getvalue())["code"], "upload_unverified")
                self.assertEqual(dispatched, [operation])

    def test_lost_append_and_finish_ack_are_not_replayed(self):
        for operation in ("append_local_upload", "finish_local_upload"):
            with self.subTest(operation=operation), running_broker(self.root / operation) as (broker, real_client, store):
                client = BoundaryClient(real_client, boundary=operation, change="lost")
                with self.assertRaises(self.module.TransferError) as failure:
                    self.invoke(client)
                self.assertEqual(failure.exception.code, "upload_unverified")
                self.assertEqual(client.dispatched.count(operation), 1)
                if operation == "append_local_upload":
                    self.assertNotIn("finish_local_upload", client.dispatched)
                    self.assertEqual(len(broker._uploads._entries), 1)
                    self.assertEqual(next(iter(broker._uploads._entries.values())).received, 24)
                    self.assertEqual(list(store.cache_dir.glob("artifact_*")), [])
                else:
                    self.assertEqual(len(list(store.cache_dir.glob("artifact_*"))), 1)

    def test_interrupted_append_leaves_no_artifact_or_finish(self):
        with running_broker(self.root) as (broker, real_client, store):
            client = BoundaryClient(real_client, boundary="append_local_upload", change="interrupt")
            with self.assertRaises(KeyboardInterrupt):
                self.invoke(client)
            self.assertNotIn("finish_local_upload", client.dispatched)
            self.assertEqual(broker._artifact_metadata, {})
            self.assertEqual(list(store.cache_dir.glob("artifact_*")), [])

    def test_disabled_artifacts_and_contract_mismatch_do_not_begin_upload(self):
        for mode in ("disabled", "mismatch", "policy_changed"):
            with self.subTest(mode=mode), running_broker(self.root / mode) as (broker, _, store):
                policy = RuntimePolicy(enabled_capabilities=("read",)) if mode == "disabled" else RuntimePolicy(
                    expected_package_version="not-installed") if mode == "mismatch" else RuntimePolicy()
                client = BrokerClient(socket_path=broker._socket_path, policy=policy,
                                      restart_callback=lambda: None, retry_backoff_seconds=0)
                if mode == "policy_changed":
                    broker._policy = RuntimePolicy(enabled_capabilities=("read",))
                with self.assertRaises(self.module.TransferError):
                    self.invoke(client)
                self.assertEqual(broker._uploads._entries, {})
                self.assertEqual(list(store.cache_dir.glob("artifact_*")), [])

    def test_cli_success_one_bounded_json_line_and_errors_never_echo_inputs(self):
        with running_broker(self.root) as (_, client, _), patch.object(self.module, "_make_client", return_value=client):
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                result = self.module.main(["upload", "--source", str(self.source), "--kind", "document"])
            self.assertEqual(result, 0)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertLess(len(output.getvalue()), 350)
            self.assertEqual(json.loads(output.getvalue())["status"], "complete")
            self.assertNotIn(str(self.root), output.getvalue())
            self.assertEqual(error.getvalue(), "")
        for arguments in (["download", "--source", "/sensitive/path/private.secret"],
                          ["upload", "--source", "/sensitive/path/private.secret", "--kind", "PAYLOAD"],
                          ["upload", "--source", "/sensitive/path/private.secret", "--kind", "document"]):
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                result = self.module.main(arguments)
            self.assertNotEqual(result, 0)
            self.assertEqual(output.getvalue(), "")
            self.assertLess(len(error.getvalue()), 350)
            self.assertNotIn("sensitive", error.getvalue())
            self.assertNotIn("PAYLOAD", error.getvalue())
            self.assertNotIn("Traceback", error.getvalue())
            self.assertEqual(json.loads(error.getvalue())["status"], "error")

    def test_sigterm_and_keyboard_interrupt_stop_cli_without_traceback_or_finish(self):
        for interruption in ("keyboard", "sigterm"):
            with self.subTest(interruption=interruption), running_broker(self.root / interruption) as (broker, real_client, _):
                def interrupt():
                    if interruption == "sigterm":
                        signal.raise_signal(signal.SIGTERM)
                    else:
                        raise KeyboardInterrupt
                client = BoundaryClient(real_client, boundary="append_local_upload", action=interrupt)
                output, error = io.StringIO(), io.StringIO()
                prior = signal.getsignal(signal.SIGTERM)
                with patch.object(self.module, "_make_client", return_value=client), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                    result = self.module.main(["upload", "--source", str(self.source), "--kind", "document"])
                self.assertEqual(result, 130)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(json.loads(error.getvalue())["code"], "interrupted")
                self.assertNotIn("Traceback", error.getvalue())
                self.assertNotIn("finish_local_upload", client.dispatched)
                self.assertEqual(broker._artifact_metadata, {})
                self.assertEqual(signal.getsignal(signal.SIGTERM), prior)

    def test_default_client_uses_fixed_policy_socket_and_cannot_restart_services(self):
        policy = RuntimePolicy()
        with patch.object(self.module, "load_runtime_policy", return_value=policy), \
             patch.object(self.module, "BrokerClient", wraps=BrokerClient) as constructor:
            client = self.module._make_client()
        self.assertEqual(client._socket_path, self.module.BROKER_SOCKET_PATH)
        self.assertIs(client._policy, policy)
        with patch("telegram_search_mcp.launch_agent.restart_launch_agent", side_effect=AssertionError):
            client._restart_callback()
