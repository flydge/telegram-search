from __future__ import annotations

import importlib
import socket
import threading
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from telegram_search_mcp.broker_protocol import (
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    receive_request as receive_wire_request,
    send_frame,
)
from telegram_search_mcp.schemas import (
    DiscoverTargetsRequest,
    ResolveTargetRequest,
    SearchRequest,
)



def receive_request(connection):
    """Simulated broker transport performs the real contract exchange first."""
    from telegram_search_mcp.contract import contract_descriptor
    from telegram_search_mcp.config import RuntimePolicy
    hello = receive_wire_request(connection)
    assert hello["operation"] == "handshake"
    descriptor = {**contract_descriptor(RuntimePolicy()), "broker_generation": "broker_" + "a" * 32}
    send_frame(connection, {"version": PROTOCOL_VERSION, "request_id": hello["request_id"],
                            "ok": True, "result": descriptor}, max_bytes=MAX_RESPONSE_BYTES)
    return receive_wire_request(connection)

def resolved_response() -> dict[str, object]:
    return {
        "status": "resolved",
        "resolved_target": {
            "chat_id": -1001,
            "title": "Known Chat",
            "chat_type": "supergroup",
        },
        "match_kind": "exact_username",
        "coverage": {
            "complete": True,
            "saved_messages": {"status": "not_requested", "detail": "lane not required"},
            "exact_username": {"status": "complete", "detail": "direct lookup complete"},
            "search_chats": {"status": "not_requested", "detail": "lane not required"},
            "search_chats_on_server": {
                "status": "not_requested",
                "detail": "lane not required",
            },
            "recent_main": {"status": "not_requested", "detail": "lane not required"},
            "hydration": {"status": "not_requested", "detail": "lane not required"},
            "detail": "complete target resolution coverage",
        },
    }


class BrokerClientTests(unittest.TestCase):
    def test_request_timeout_cannot_exceed_the_protocol_ceiling(self) -> None:
        module = importlib.import_module("telegram_search_mcp.broker_client")

        with self.assertRaisesRegex(ValueError, "570"):
            module.BrokerClient(
                socket_path=Path("/unused/test.sock"),
                request_timeout=570.001,
            )

    def _round_trip(
        self,
        *,
        operation: str,
        result: dict[str, Any],
        invoke: Callable[[Any], object],
    ) -> tuple[object, dict[str, object]]:
        module = importlib.import_module("telegram_search_mcp.broker_client")
        proxy_socket, broker_socket = socket.socketpair()
        observed: list[dict[str, object]] = []

        def broker() -> None:
            request = receive_request(broker_socket)
            observed.append(request)
            send_frame(
                broker_socket,
                {
                    "version": PROTOCOL_VERSION,
                    "request_id": request["request_id"],
                    "ok": True,
                    "result": result,
                },
                max_bytes=MAX_RESPONSE_BYTES,
            )
            broker_socket.close()

        thread = threading.Thread(target=broker)
        thread.start()
        client = module.BrokerClient(
            socket_path=Path("/unused/test.sock"),
            connector=lambda _path: proxy_socket,
        )
        response = invoke(client)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed[0]["operation"], operation)
        return response, observed[0]

    def test_resolve_round_trips_and_revalidates_the_public_model(self) -> None:
        try:
            module = importlib.import_module("telegram_search_mcp.broker_client")
        except ModuleNotFoundError:
            self.fail("broker client is not implemented")

        proxy_socket, broker_socket = socket.socketpair()
        observed: list[dict[str, object]] = []

        def broker() -> None:
            request = receive_request(broker_socket)
            observed.append(request)
            send_frame(
                broker_socket,
                {
                    "version": PROTOCOL_VERSION,
                    "request_id": request["request_id"],
                    "ok": True,
                    "result": resolved_response(),
                },
                max_bytes=MAX_RESPONSE_BYTES,
            )
            broker_socket.close()

        thread = threading.Thread(target=broker)
        thread.start()
        client = module.BrokerClient(
            socket_path=Path("/unused/test.sock"),
            connector=lambda _path: proxy_socket,
        )

        response = client.resolve(ResolveTargetRequest(target="@known_chat"))
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(response.status, "resolved")
        self.assertEqual(response.resolved_target.chat_id, -1001)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["operation"], "resolve")
        self.assertEqual(observed[0]["payload"], {"target": "@known_chat"})
        self.assertTrue(str(observed[0]["client_id"]).startswith("client_"))

    def test_discover_round_trips_the_strict_public_request_and_response(self) -> None:
        result = {
            "status": "blocked",
            "candidates": [],
            "coverage": {
                "complete": False,
                "catalog": {
                    chat_list: {
                        "status": "blocked",
                        "scanned_count": 0,
                        "emitted_count": 0,
                        "end_reached": False,
                    }
                    for chat_list in ("main", "archive")
                },
                "global_messages": {
                    "lanes": [
                        {
                            "hypothesis_index": index,
                            "chat_list": chat_list,
                            "status": "blocked",
                            "pages_scanned": 0,
                            "hits_seen": 0,
                        }
                        for index in range(2)
                        for chat_list in ("main", "archive")
                    ]
                },
                "hydration": "blocked",
                "detail": "discovery coverage details redacted",
            },
            "next_cursor": None,
        }
        request = DiscoverTargetsRequest(
            hypotheses=["garden group", "seed exchange"], scope="both"
        )

        response, observed = self._round_trip(
            operation="discover",
            result=result,
            invoke=lambda client: client.discover(request),
        )

        self.assertEqual(response.status, "blocked")
        self.assertEqual(observed["payload"]["hypotheses"], request.hypotheses)

    def test_search_round_trips_the_strict_public_request_and_response(self) -> None:
        result = {
            "status": "blocked",
            "coverage": {
                "complete": False,
                "text_status": "not_started",
                "metadata_status": "not_requested",
                "date_from": None,
                "date_to": None,
                "detail": "TDLib authorization is not ready",
            },
            "matches": [],
        }
        request = SearchRequest.model_validate(
            {"target": -1001, "query": {"text": "needle"}}
        )

        response, observed = self._round_trip(
            operation="search",
            result=result,
            invoke=lambda client: client.search(request),
        )

        self.assertEqual(response.status, "blocked")
        self.assertEqual(observed["payload"]["query"]["text"], "needle")
        self.assertNotIn("api_hash", observed["payload"])

    def test_transport_failures_return_redacted_existing_public_schemas(self) -> None:
        sentinel = "PRIVATE_BROKER_FAILURE_SENTINEL"

        def unavailable(_path: Path) -> socket.socket:
            raise FileNotFoundError(sentinel)

        module = importlib.import_module("telegram_search_mcp.broker_client")
        client = module.BrokerClient(
            socket_path=Path("/unused/test.sock"),
            connector=unavailable,
            restart_callback=lambda: None,
            retry_backoff_seconds=0,
            sleeper=lambda _seconds: None,
        )

        resolution = client.resolve(ResolveTargetRequest(target="@known_chat"))
        discovery = client.discover(
            DiscoverTargetsRequest(
                hypotheses=["garden group", "seed exchange"], scope="both"
            )
        )
        search = client.search(
            SearchRequest.model_validate(
                {"target": -1001, "query": {"text": "needle"}}
            )
        )

        self.assertEqual(resolution.status, "error")
        self.assertEqual(discovery.status, "error")
        self.assertEqual(search.status, "error")
        serialized = "".join(
            (
                resolution.model_dump_json(),
                discovery.model_dump_json(),
                search.model_dump_json(),
            )
        )
        self.assertNotIn(sentinel, serialized)

    def test_health_returns_only_broker_aggregate_fields(self) -> None:
        result = {
            "uptime_seconds": 12.5,
            "queue_depth": 1,
            "active_clients": 2,
            "completed_requests": 3,
            "error_count": 0,
            "peak_overlap": 2,
            "serialization_wait_count": 1,
        }

        health, observed = self._round_trip(
            operation="health",
            result=result,
            invoke=lambda client: client.health(),
        )

        self.assertEqual(health, result)
        self.assertEqual(observed["payload"], {})

    def test_check_returns_only_a_fixed_readiness_status(self) -> None:
        status, observed = self._round_trip(
            operation="check",
            result={"status": "ready"},
            invoke=lambda client: client.check(),
        )

        self.assertEqual(status, "ready")
        self.assertEqual(observed["payload"], {})

    def test_connection_loss_requests_one_restart_then_retries_once(self) -> None:
        module = importlib.import_module("telegram_search_mcp.broker_client")
        proxy_socket, broker_socket = socket.socketpair()
        connection_attempts: list[int] = []
        restarts: list[str] = []
        sleeps: list[float] = []

        def connector(_path: Path) -> socket.socket:
            connection_attempts.append(len(connection_attempts) + 1)
            if len(connection_attempts) == 1:
                raise ConnectionRefusedError("synthetic first loss")
            return proxy_socket

        def broker() -> None:
            request = receive_request(broker_socket)
            send_frame(
                broker_socket,
                {
                    "version": PROTOCOL_VERSION,
                    "request_id": request["request_id"],
                    "ok": True,
                    "result": resolved_response(),
                },
                max_bytes=MAX_RESPONSE_BYTES,
            )
            broker_socket.close()

        thread = threading.Thread(target=broker)
        thread.start()
        client = module.BrokerClient(
            socket_path=Path("/unused/test.sock"),
            connector=connector,
            restart_callback=lambda: restarts.append("restart"),
            retry_backoff_seconds=0.25,
            sleeper=sleeps.append,
        )

        response = client.resolve(ResolveTargetRequest(target="@known_chat"))
        thread.join(timeout=2)

        self.assertEqual(response.status, "resolved")
        self.assertEqual(connection_attempts, [1, 2])
        self.assertEqual(restarts, ["restart"])
        self.assertEqual(sleeps, [0.25])

    def test_lost_discovery_response_does_not_advance_the_registry_twice(self) -> None:
        module = importlib.import_module("telegram_search_mcp.broker_client")
        proxy_socket, broker_socket = socket.socketpair()
        connection_attempts: list[int] = []
        restarts: list[str] = []
        sleeps: list[float] = []

        def connector(_path: Path) -> socket.socket:
            connection_attempts.append(len(connection_attempts) + 1)
            return proxy_socket

        def broker() -> None:
            receive_request(broker_socket)
            broker_socket.close()

        thread = threading.Thread(target=broker)
        thread.start()
        client = module.BrokerClient(
            socket_path=Path("/unused/test.sock"),
            connector=connector,
            restart_callback=lambda: restarts.append("restart"),
            retry_backoff_seconds=0.25,
            sleeper=sleeps.append,
        )

        response = client.discover(
            DiscoverTargetsRequest(
                hypotheses=["garden group", "seed exchange"],
                scope="both",
            )
        )
        thread.join(timeout=2)

        self.assertEqual(response.status, "error")
        self.assertEqual(connection_attempts, [1])
        self.assertEqual(restarts, [])
        self.assertEqual(sleeps, [])

    def test_receive_timeout_does_not_restart_or_retry_an_in_flight_request(self) -> None:
        module = importlib.import_module("telegram_search_mcp.broker_client")
        proxy_socket, broker_socket = socket.socketpair()
        connection_attempts: list[int] = []
        restarts: list[str] = []
        request_received = threading.Event()
        release = threading.Event()

        def connector(_path: Path) -> socket.socket:
            connection_attempts.append(len(connection_attempts) + 1)
            return proxy_socket

        def broker() -> None:
            receive_request(broker_socket)
            request_received.set()
            release.wait(timeout=2)
            broker_socket.close()

        thread = threading.Thread(target=broker)
        thread.start()
        client = module.BrokerClient(
            socket_path=Path("/unused/test.sock"),
            connector=connector,
            request_timeout=0.05,
            restart_callback=lambda: restarts.append("restart"),
            retry_backoff_seconds=0,
        )

        response = client.resolve(ResolveTargetRequest(target="@known_chat"))
        self.assertTrue(request_received.wait(timeout=2))
        release.set()
        thread.join(timeout=2)

        self.assertEqual(response.status, "error")
        self.assertEqual(connection_attempts, [1])
        self.assertEqual(restarts, [])

    def test_boolean_protocol_version_is_rejected_before_public_model_use(self) -> None:
        module = importlib.import_module("telegram_search_mcp.broker_client")
        proxy_socket, broker_socket = socket.socketpair()

        def broker() -> None:
            request = receive_request(broker_socket)
            send_frame(
                broker_socket,
                {
                    "version": True,
                    "request_id": request["request_id"],
                    "ok": True,
                    "result": resolved_response(),
                },
                max_bytes=MAX_RESPONSE_BYTES,
            )
            broker_socket.close()

        thread = threading.Thread(target=broker)
        thread.start()
        client = module.BrokerClient(
            socket_path=Path("/unused/test.sock"),
            connector=lambda _path: proxy_socket,
        )

        response = client.resolve(ResolveTargetRequest(target="@known_chat"))
        thread.join(timeout=2)

        self.assertEqual(response.status, "error")


if __name__ == "__main__":
    unittest.main()
