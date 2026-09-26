from __future__ import annotations

import asyncio
import contextlib
import io
import importlib
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mcp import Client

from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.broker_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    receive_frame,
    send_frame,
)
from telegram_search_mcp.schemas import (
    DiscoverTargetsRequest,
    ResolveTargetRequest,
    SearchRequest,
)
from telegram_search_mcp.server import build_server
from telegram_search_mcp.tdjson import GlobalMessagePage, TDLibError


class SerializedFakeTDLib:
    def __init__(self) -> None:
        self._raw_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self.active_raw_calls = 0
        self.peak_raw_calls = 0
        self.serialization_wait_count = 0
        self.closed = 0

    def _raw_call(self) -> None:
        acquired = self._raw_lock.acquire(blocking=False)
        if not acquired:
            with self._counter_lock:
                self.serialization_wait_count += 1
            self._raw_lock.acquire()
        try:
            with self._counter_lock:
                self.active_raw_calls += 1
                self.peak_raw_calls = max(self.peak_raw_calls, self.active_raw_calls)
            time.sleep(0.03)
            with self._counter_lock:
                self.active_raw_calls -= 1
        finally:
            self._raw_lock.release()

    def ensure_ready(self) -> None:
        self._raw_call()

    def resolve_target(self, target: str | int) -> dict[str, Any]:
        self._raw_call()
        return {
            "@type": "chat",
            "id": -1001,
            "title": f"Known {target}",
            "type": {"@type": "chatTypeSupergroup", "is_channel": False},
        }

    def get_chat_list_prefix(self, chat_list: str) -> list[int]:
        del chat_list
        self._raw_call()
        return []

    def load_more_chats(self, chat_list: str, on_positions: Any) -> bool:
        del chat_list
        self._raw_call()
        on_positions([])
        return True

    def search_global_messages(
        self, chat_list: str, query: str, *, offset: str
    ) -> GlobalMessagePage:
        del chat_list, query, offset
        self._raw_call()
        return GlobalMessagePage(messages=[], next_offset="", integrity_partial=False)

    def search_chat_messages(
        self,
        chat_id: int,
        query: str,
        *,
        from_message_id: int,
        limit: int,
    ) -> dict[str, Any]:
        del chat_id, query, from_message_id, limit
        self._raw_call()
        return {
            "@type": "foundChatMessages",
            "total_count": 0,
            "messages": [],
            "next_from_message_id": 0,
        }

    def get_chat_link(self, chat: dict[str, Any]) -> None:
        del chat
        self._raw_call()
        return None

    def close(self) -> None:
        self.closed += 1


class BrokerWalkingSkeletonTests(unittest.IsolatedAsyncioTestCase):
    def test_subprocess_proxies_share_one_fake_tdlib_broker(self) -> None:
        broker_script = r'''
import signal
import sys
import time
from collections import deque
from pathlib import Path

from telegram_search_mcp.broker import Broker
from telegram_search_mcp.keychain import ApiCredentials
from telegram_search_mcp.tdjson import TDLibClient


class FakeRaw:
    def __init__(self):
        self.pending = deque()
        self.authorization_calls = 0

    def send(self, payload):
        request_type = payload["@type"]
        if request_type == "getAuthorizationState":
            self.authorization_calls += 1
            state = (
                "authorizationStateWaitTdlibParameters"
                if self.authorization_calls == 1
                else "authorizationStateReady"
            )
            response = {"@type": state}
        elif request_type == "setTdlibParameters":
            response = {"@type": "ok"}
        elif request_type == "searchPublicChat":
            response = {
                "@type": "chat",
                "id": -1001,
                "title": "Synthetic Chat",
                "type": {"@type": "chatTypeSupergroup", "is_channel": False},
            }
        else:
            response = {"@type": "error", "code": 500}
        response["@extra"] = payload["@extra"]
        self.pending.append(response)

    def receive(self, timeout):
        del timeout
        time.sleep(0.05)
        return self.pending.popleft() if self.pending else None

    def close(self):
        pass


root = Path(sys.argv[1])
client = TDLibClient(
    raw=FakeRaw(),
    session_directory=root / "fake-session",
    credential_loader=lambda: ApiCredentials(12345, "FAKE_CREDENTIAL_SENTINEL"),
)
broker = Broker(
    socket_path=root / "broker.sock",
    lock_path=root / "broker.lock",
    client_factory=lambda: client,
)
signal.signal(signal.SIGTERM, lambda *_args: broker.shutdown())
signal.signal(signal.SIGINT, lambda *_args: broker.shutdown())
broker.serve_forever()
'''
        proxy_script = r'''
import sys
from pathlib import Path
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.schemas import ResolveTargetRequest

client = BrokerClient(
    socket_path=Path(sys.argv[1]),
    restart_callback=lambda: None,
    retry_backoff_seconds=0,
)
response = client.resolve(ResolveTargetRequest(target="@known_chat"))
print(response.status)
client.close()
'''
        repository = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(repository / "src")
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            broker_process = subprocess.Popen(
                [sys.executable, "-c", broker_script, str(root)],
                cwd=repository,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not (root / "broker.sock").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((root / "broker.sock").exists())
                proxies = [
                    subprocess.Popen(
                        [sys.executable, "-c", proxy_script, str(root / "broker.sock")],
                        cwd=repository,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for _ in range(2)
                ]
                results = [process.communicate(timeout=10) for process in proxies]
                health = BrokerClient(
                    socket_path=root / "broker.sock",
                    restart_callback=lambda: None,
                    retry_backoff_seconds=0,
                ).health()
            finally:
                broker_process.terminate()
                try:
                    broker_stdout, broker_stderr = broker_process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    broker_process.kill()
                    broker_stdout, broker_stderr = broker_process.communicate(timeout=5)

        self.assertEqual([item[0] for item in results], ["resolved\n", "resolved\n"])
        self.assertEqual([item[1] for item in results], ["", ""])
        self.assertGreaterEqual(health["serialization_wait_count"], 1)
        self.assertNotIn(
            "FAKE_CREDENTIAL_SENTINEL",
            broker_stdout + broker_stderr + "".join(value for pair in results for value in pair),
        )

    async def test_two_mcp_clients_share_one_serialized_backend(self) -> None:
        try:
            broker_module = importlib.import_module("telegram_search_mcp.broker")
        except ModuleNotFoundError:
            self.fail("broker is not implemented")

        with tempfile.TemporaryDirectory() as parent:
            socket_path = Path(parent) / "broker.sock"
            created: list[SerializedFakeTDLib] = []

            def client_factory() -> SerializedFakeTDLib:
                backend = SerializedFakeTDLib()
                created.append(backend)
                return backend

            broker = broker_module.Broker(
                socket_path=socket_path,
                client_factory=client_factory,
                verify_peer_uid=False,
            )
            broker_thread = threading.Thread(target=broker.serve_forever)
            broker_thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=2))
            first_proxy = BrokerClient(socket_path=socket_path)
            second_proxy = BrokerClient(socket_path=socket_path)
            self.assertNotEqual(first_proxy.client_id, second_proxy.client_id)
            first_server = build_server(service_factory=lambda: first_proxy)
            second_server = build_server(service_factory=lambda: second_proxy)

            async with Client(first_server) as first, Client(second_server) as second:
                first_tools, second_tools = await asyncio.gather(
                    first.list_tools(), second.list_tools()
                )
                first_result, second_result = await asyncio.gather(
                    first.call_tool("resolve_target", {"target": "@known_chat"}),
                    second.call_tool("resolve_target", {"target": "@known_chat"}),
                )
                health = await asyncio.to_thread(first_proxy.health)

            first_proxy.close()
            second_proxy.close()
            broker.shutdown()
            broker_thread.join(timeout=2)

        expected_tools = [
            "_manifest",
            "resolve_target",
            "discover_targets",
            "search_correspondence",
        ]
        self.assertEqual([tool.name for tool in first_tools.tools], expected_tools)
        self.assertEqual([tool.name for tool in second_tools.tools], expected_tools)
        self.assertFalse(first_result.is_error)
        self.assertFalse(second_result.is_error)
        self.assertEqual(first_result.structured_content["status"], "resolved")
        self.assertEqual(second_result.structured_content["status"], "resolved")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].peak_raw_calls, 1)
        self.assertGreaterEqual(health["peak_overlap"], 2)
        self.assertGreaterEqual(health["serialization_wait_count"], 1)
        self.assertEqual(created[0].closed, 1)

    async def test_peer_uid_gate_accepts_owner_and_rejects_foreign_uid(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        for peer_uid, expected, expected_backends in (
            (os.getuid(), "resolved", 1),
            (os.getuid() + 1, "error", 0),
        ):
            with self.subTest(peer_uid=peer_uid), tempfile.TemporaryDirectory() as parent:
                socket_path = Path(parent) / "broker.sock"
                created: list[SerializedFakeTDLib] = []

                def client_factory() -> SerializedFakeTDLib:
                    backend = SerializedFakeTDLib()
                    created.append(backend)
                    return backend

                broker = broker_module.Broker(
                    socket_path=socket_path,
                    client_factory=client_factory,
                    peer_uid_loader=lambda _connection, value=peer_uid: value,
                )
                broker_thread = threading.Thread(target=broker.serve_forever)
                broker_thread.start()
                self.assertTrue(broker.wait_until_ready(timeout=2))
                proxy = BrokerClient(
                    socket_path=socket_path,
                    restart_callback=lambda: None,
                    retry_backoff_seconds=0,
                )

                result = await asyncio.to_thread(
                    proxy.resolve,
                    ResolveTargetRequest(target="@known_chat"),
                )

                proxy.close()
                broker.shutdown()
                broker_thread.join(timeout=2)

            self.assertEqual(result.status, expected)
            self.assertEqual(len(created), expected_backends)

    def test_nonblocking_owner_lock_rejects_a_second_broker(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            socket_path = root / "broker.sock"
            lock_path = root / "broker.lock"
            first = broker_module.Broker(
                socket_path=socket_path,
                lock_path=lock_path,
                client_factory=SerializedFakeTDLib,
                verify_peer_uid=False,
            )
            first_thread = threading.Thread(target=first.serve_forever)
            first_thread.start()
            self.assertTrue(first.wait_until_ready(timeout=2))
            second = broker_module.Broker(
                socket_path=socket_path,
                lock_path=lock_path,
                client_factory=SerializedFakeTDLib,
                verify_peer_uid=False,
            )

            with self.assertRaisesRegex(broker_module.BrokerStartupError, "owner"):
                second.serve_forever()

            first.shutdown()
            first_thread.join(timeout=2)
            self.assertFalse(first_thread.is_alive())

    def test_owner_lock_holder_replaces_only_a_stale_same_owner_socket(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            socket_path = root / "broker.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(socket_path))
            stale.close()
            broker = broker_module.Broker(
                socket_path=socket_path,
                lock_path=root / "broker.lock",
                client_factory=SerializedFakeTDLib,
                verify_peer_uid=False,
            )
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()

            self.assertTrue(broker.wait_until_ready(timeout=2))
            self.assertTrue(thread.is_alive())
            broker.shutdown()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_unsafe_socket_and_lock_symlinks_are_never_removed(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        for unsafe_name in ("socket", "lock"):
            with self.subTest(unsafe_name=unsafe_name), tempfile.TemporaryDirectory() as parent:
                root = Path(parent)
                target = root / "sentinel"
                target.write_text("keep", encoding="utf-8")
                socket_path = root / "broker.sock"
                lock_path = root / "broker.lock"
                link = socket_path if unsafe_name == "socket" else lock_path
                link.symlink_to(target)
                broker = broker_module.Broker(
                    socket_path=socket_path,
                    lock_path=lock_path,
                    client_factory=SerializedFakeTDLib,
                    verify_peer_uid=False,
                )

                with self.assertRaises(broker_module.BrokerStartupError):
                    broker.serve_forever()

                self.assertTrue(link.is_symlink())
                self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    async def test_idle_client_context_expires_without_persisting_state(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        now = [100.0]
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            broker = broker_module.Broker(
                socket_path=root / "broker.sock",
                lock_path=root / "broker.lock",
                client_factory=SerializedFakeTDLib,
                verify_peer_uid=False,
                context_clock=lambda: now[0],
                context_ttl_seconds=300,
            )
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=2))
            proxy = BrokerClient(socket_path=root / "broker.sock")
            resolved = await asyncio.to_thread(
                proxy.resolve, ResolveTargetRequest(target="@known_chat")
            )
            before = await asyncio.to_thread(proxy.health)
            now[0] += 301
            after = await asyncio.to_thread(proxy.health)

            proxy.close()
            broker.shutdown()
            thread.join(timeout=2)

        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(before["active_clients"], 1)
        self.assertEqual(after["active_clients"], 0)
        self.assertEqual(after["serialization_wait_count"], 0)

    async def test_active_client_context_does_not_expire_mid_request(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        now = [100.0]

        class BlockingBackend(SerializedFakeTDLib):
            def __init__(self) -> None:
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def ensure_ready(self) -> None:
                self.entered.set()
                self.release.wait(timeout=2)
                super().ensure_ready()

        backend = BlockingBackend()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            broker = broker_module.Broker(
                socket_path=root / "broker.sock",
                lock_path=root / "broker.lock",
                client_factory=lambda: backend,
                verify_peer_uid=False,
                context_clock=lambda: now[0],
                context_ttl_seconds=300,
            )
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=2))
            proxy = BrokerClient(socket_path=root / "broker.sock")
            request = asyncio.create_task(
                asyncio.to_thread(
                    proxy.resolve, ResolveTargetRequest(target="@known_chat")
                )
            )
            self.assertTrue(await asyncio.to_thread(backend.entered.wait, 2))
            now[0] += 301

            during = await asyncio.to_thread(proxy.health)
            backend.release.set()
            result = await request

            proxy.close()
            broker.shutdown()
            thread.join(timeout=2)

        self.assertEqual(during["active_clients"], 1)
        self.assertEqual(result.status, "resolved")

    def test_expired_request_is_not_started(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        created: list[SerializedFakeTDLib] = []
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)

            def client_factory() -> SerializedFakeTDLib:
                backend = SerializedFakeTDLib()
                created.append(backend)
                return backend

            broker = broker_module.Broker(
                socket_path=root / "broker.sock",
                lock_path=root / "broker.lock",
                client_factory=client_factory,
                verify_peer_uid=False,
            )
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=2))
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(str(root / "broker.sock"))
            send_frame(
                connection,
                {
                    "version": PROTOCOL_VERSION,
                    "client_id": "client_abcdefghijklmnopqrstuvwxyz012345",
                    "request_id": "request_abcdefghijklmnopqrstuvwxyz012345",
                    "operation": "resolve",
                    "payload": {"target": "@known_chat"},
                    "deadline": time.monotonic() - 1,
                },
                max_bytes=MAX_REQUEST_BYTES,
            )

            response = receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
            connection.close()
            broker.shutdown()
            thread.join(timeout=2)

        self.assertEqual(response["ok"], False)
        self.assertEqual(response["error"], "expired")
        self.assertEqual(created, [])

    def test_request_beyond_deadline_ceiling_is_not_started(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        created: list[SerializedFakeTDLib] = []
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            broker = broker_module.Broker(
                socket_path=root / "broker.sock",
                lock_path=root / "broker.lock",
                client_factory=lambda: created.append(SerializedFakeTDLib()) or created[-1],
                verify_peer_uid=False,
            )
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=2))
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(str(root / "broker.sock"))
            send_frame(
                connection,
                {
                    "version": PROTOCOL_VERSION,
                    "client_id": "client_abcdefghijklmnopqrstuvwxyz012345",
                    "request_id": "request_abcdefghijklmnopqrstuvwxyz012345",
                    "operation": "resolve",
                    "payload": {"target": "@known_chat"},
                    "deadline": time.monotonic() + 571,
                },
                max_bytes=MAX_REQUEST_BYTES,
            )

            response = receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
            connection.close()
            broker.shutdown()
            thread.join(timeout=2)

        self.assertEqual(response["ok"], False)
        self.assertEqual(response["error"], "invalid_request")
        self.assertEqual(created, [])

    def test_partial_frame_clients_do_not_prevent_prompt_shutdown(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            broker = broker_module.Broker(
                socket_path=root / "broker.sock",
                lock_path=root / "broker.lock",
                client_factory=SerializedFakeTDLib,
                verify_peer_uid=False,
                max_pending=2,
                max_workers=2,
            )
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=2))
            partials = []
            for wire in (b"\x00\x00", b"\x00\x00\x00\x20{"):
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.connect(str(root / "broker.sock"))
                connection.sendall(wire)
                partials.append(connection)
            time.sleep(0.05)

            started = time.monotonic()
            broker.shutdown()
            thread.join(timeout=1)
            elapsed = time.monotonic() - started
            for connection in partials:
                connection.close()

        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 1)

    async def test_full_pending_queue_rejects_excess_without_interrupting_admitted_work(
        self,
    ) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")

        class BlockingBackend(SerializedFakeTDLib):
            def __init__(self) -> None:
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def ensure_ready(self) -> None:
                self.entered.set()
                self.release.wait(timeout=2)
                super().ensure_ready()

        backend = BlockingBackend()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            broker = broker_module.Broker(
                socket_path=root / "broker.sock",
                lock_path=root / "broker.lock",
                client_factory=lambda: backend,
                verify_peer_uid=False,
                max_pending=1,
                max_workers=1,
            )
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=2))
            restart = mock.Mock()
            first = BrokerClient(
                socket_path=root / "broker.sock",
                restart_callback=restart,
            )
            second = BrokerClient(
                socket_path=root / "broker.sock",
                restart_callback=restart,
            )
            first_task = asyncio.create_task(
                asyncio.to_thread(
                    first.resolve, ResolveTargetRequest(target="@known_chat")
                )
            )
            self.assertTrue(await asyncio.to_thread(backend.entered.wait, 2))

            rejected = await asyncio.to_thread(
                second.resolve, ResolveTargetRequest(target="@known_chat")
            )
            backend.release.set()
            admitted = await first_task

            first.close()
            second.close()
            broker.shutdown()
            thread.join(timeout=2)

        self.assertEqual(rejected.status, "error")
        self.assertEqual(admitted.status, "resolved")
        restart.assert_not_called()
        self.assertEqual(backend.closed, 1)

    async def test_client_timeout_does_not_restart_or_interrupt_in_flight_work(
        self,
    ) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")

        class BlockingBackend(SerializedFakeTDLib):
            def __init__(self) -> None:
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def ensure_ready(self) -> None:
                self.entered.set()
                self.release.wait(timeout=2)
                super().ensure_ready()

        backend = BlockingBackend()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            broker = broker_module.Broker(
                socket_path=root / "broker.sock",
                lock_path=root / "broker.lock",
                client_factory=lambda: backend,
                verify_peer_uid=False,
            )
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=2))
            restart = mock.Mock()
            proxy = BrokerClient(
                socket_path=root / "broker.sock",
                request_timeout=0.05,
                restart_callback=restart,
            )

            timed_out = await asyncio.to_thread(
                proxy.resolve, ResolveTargetRequest(target="@known_chat")
            )
            self.assertTrue(backend.entered.wait(timeout=2))
            backend.release.set()
            await asyncio.sleep(0.1)
            follow_up = await asyncio.to_thread(
                BrokerClient(socket_path=root / "broker.sock").resolve,
                ResolveTargetRequest(target="@known_chat"),
            )

            broker.shutdown()
            thread.join(timeout=2)

        self.assertEqual(timed_out.status, "error")
        self.assertEqual(follow_up.status, "resolved")
        restart.assert_not_called()

    async def test_broker_restart_expires_existing_discovery_cursor(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            socket_path = root / "broker.sock"
            lock_path = root / "broker.lock"
            first_broker = broker_module.Broker(
                socket_path=socket_path,
                lock_path=lock_path,
                client_factory=SerializedFakeTDLib,
                verify_peer_uid=False,
            )
            first_thread = threading.Thread(target=first_broker.serve_forever)
            first_thread.start()
            self.assertTrue(first_broker.wait_until_ready(timeout=2))
            proxy = BrokerClient(socket_path=socket_path)
            request = DiscoverTargetsRequest(
                hypotheses=["garden group", "seed exchange"], scope="both"
            )
            first_page = await asyncio.to_thread(proxy.discover, request)
            first_broker.shutdown()
            first_thread.join(timeout=2)

            second_broker = broker_module.Broker(
                socket_path=socket_path,
                lock_path=lock_path,
                client_factory=SerializedFakeTDLib,
                verify_peer_uid=False,
            )
            second_thread = threading.Thread(target=second_broker.serve_forever)
            second_thread.start()
            self.assertTrue(second_broker.wait_until_ready(timeout=2))
            after_restart = await asyncio.to_thread(
                proxy.discover,
                request.model_copy(update={"cursor": first_page.next_cursor}),
            )
            proxy.close()
            second_broker.shutdown()
            second_thread.join(timeout=2)

        self.assertEqual(first_page.status, "page")
        self.assertEqual(after_restart.status, "expired")

    async def test_provider_failure_never_logs_or_returns_private_request_data(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        sentinel = "PRIVATE_PROVIDER_AND_QUERY_SENTINEL"

        class FailingBackend(SerializedFakeTDLib):
            def ensure_ready(self) -> None:
                raise TDLibError(sentinel)

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as parent, contextlib.redirect_stdout(
            output
        ), contextlib.redirect_stderr(output):
            root = Path(parent)
            broker = broker_module.Broker(
                socket_path=root / "broker.sock",
                lock_path=root / "broker.lock",
                client_factory=FailingBackend,
                verify_peer_uid=False,
            )
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=2))
            proxy = BrokerClient(socket_path=root / "broker.sock")
            response = await asyncio.to_thread(
                proxy.resolve, ResolveTargetRequest(target="@private_query")
            )
            health = await asyncio.to_thread(proxy.health)
            proxy.close()
            broker.shutdown()
            thread.join(timeout=2)

        combined = response.model_dump_json() + str(health) + output.getvalue()
        self.assertEqual(response.status, "error")
        self.assertNotIn(sentinel, combined)
        self.assertNotIn("private_query", combined)

    async def test_broker_dispatches_search_and_isolates_discovery_cursors(self) -> None:
        broker_module = importlib.import_module("telegram_search_mcp.broker")
        with tempfile.TemporaryDirectory() as parent:
            socket_path = Path(parent) / "broker.sock"
            created: list[SerializedFakeTDLib] = []

            def client_factory() -> SerializedFakeTDLib:
                backend = SerializedFakeTDLib()
                created.append(backend)
                return backend

            broker = broker_module.Broker(
                socket_path=socket_path,
                client_factory=client_factory,
                verify_peer_uid=False,
            )
            broker_thread = threading.Thread(target=broker.serve_forever)
            broker_thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=2))
            first = BrokerClient(socket_path=socket_path)
            second = BrokerClient(socket_path=socket_path)
            request = DiscoverTargetsRequest(
                hypotheses=["garden group", "seed exchange"], scope="both"
            )

            first_page = await asyncio.to_thread(first.discover, request)
            foreign_page = await asyncio.to_thread(
                second.discover,
                request.model_copy(update={"cursor": first_page.next_cursor}),
            )
            search = await asyncio.to_thread(
                second.search,
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "needle"}}
                ),
            )

            first.close()
            second.close()
            broker.shutdown()
            broker_thread.join(timeout=2)

        self.assertEqual(first_page.status, "page")
        self.assertIsNotNone(first_page.next_cursor)
        self.assertEqual(foreign_page.status, "expired")
        self.assertEqual(search.status, "no_match")
        self.assertTrue(search.coverage.complete)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].closed, 1)


if __name__ == "__main__":
    unittest.main()
