"""Thin per-process client for the owner-only TelegramSearch broker."""

from __future__ import annotations

import secrets
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .broker_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_DEADLINE_SECONDS,
    PROTOCOL_VERSION,
    BrokerProtocolError,
    receive_frame,
    send_frame,
)
from .schemas import (
    DiscoverTargetsRequest,
    ResolveTargetRequest,
    SearchRequest,
    SearchResponse,
    TargetDiscoveryResponse,
    TargetResolutionResponse,
)
from .search_service import terminal_search_response
from .target_discovery import terminal_discovery_response
from .target_resolver import terminal_resolution_response


class BrokerUnavailable(RuntimeError):
    """Raised when the local broker cannot safely complete a request."""


Connector = Callable[[Path], socket.socket]


def _connect(socket_path: Path) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(socket_path))
    except Exception:
        connection.close()
        raise
    return connection


def _request_launchd_restart() -> None:
    from .launch_agent import restart_launch_agent

    restart_launch_agent()


class BrokerClient:
    """Public-model-aware proxy with one isolated process-lifetime client ID."""

    def __init__(
        self,
        *,
        socket_path: Path,
        connector: Connector = _connect,
        request_timeout: float = 570.0,
        client_id: str | None = None,
        restart_callback: Callable[[], None] | None = None,
        retry_backoff_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if request_timeout <= 0 or request_timeout > MAX_DEADLINE_SECONDS:
            raise ValueError("request_timeout must be between zero and 570 seconds")
        if retry_backoff_seconds < 0 or retry_backoff_seconds > 5:
            raise ValueError("retry_backoff_seconds must be between zero and five")
        self._socket_path = socket_path
        self._connector = connector
        self._request_timeout = float(request_timeout)
        self._client_id = client_id or f"client_{secrets.token_urlsafe(24)}"
        self._restart_callback = restart_callback or _request_launchd_restart
        self._retry_backoff_seconds = float(retry_backoff_seconds)
        self._sleeper = sleeper
        self._closed = False

    @property
    def client_id(self) -> str:
        return self._client_id

    def _request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = f"request_{secrets.token_urlsafe(24)}"
        deadline = time.monotonic() + self._request_timeout
        envelope = {
            "version": PROTOCOL_VERSION,
            "client_id": self._client_id,
            "request_id": request_id,
            "operation": operation,
            "payload": payload,
            "deadline": deadline,
        }

        def connect() -> socket.socket:
            return self._connector(self._socket_path)

        try:
            connection = connect()
        except OSError:
            try:
                self._restart_callback()
            except Exception:
                pass
            self._sleeper(self._retry_backoff_seconds)
            try:
                connection = connect()
            except OSError as error:
                raise BrokerUnavailable("TelegramSearch broker is unavailable") from error
        try:
            with connection:
                connection.settimeout(self._request_timeout)
                send_frame(connection, envelope, max_bytes=MAX_REQUEST_BYTES)
                response = receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
        except (OSError, BrokerProtocolError) as error:
            raise BrokerUnavailable("TelegramSearch broker is unavailable") from error
        if not isinstance(response, dict):
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        if (
            type(response.get("version")) is not int
            or response.get("version") != PROTOCOL_VERSION
            or response.get("request_id") != request_id
            or type(response.get("ok")) is not bool
        ):
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        if response["ok"] is not True:
            if (
                set(response) != {"version", "request_id", "ok", "error"}
                or response["error"]
                not in {"expired", "invalid_request", "overloaded", "unavailable"}
            ):
                raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
            raise BrokerUnavailable("TelegramSearch broker could not complete the request")
        if set(response) != {"version", "request_id", "ok", "result"}:
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        result = response["result"]
        if not isinstance(result, dict):
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        return result

    def resolve(self, request: ResolveTargetRequest) -> TargetResolutionResponse:
        try:
            result = self._request("resolve", request.model_dump(mode="json"))
            return TargetResolutionResponse.model_validate(result)
        except (BrokerUnavailable, ValidationError):
            return terminal_resolution_response("error")

    def discover(self, request: DiscoverTargetsRequest) -> TargetDiscoveryResponse:
        try:
            result = self._request("discover", request.model_dump(mode="json"))
            return TargetDiscoveryResponse.model_validate(result)
        except (BrokerUnavailable, ValidationError):
            return terminal_discovery_response(request, "error")

    def search(self, request: SearchRequest) -> SearchResponse:
        try:
            result = self._request("search", request.model_dump(mode="json"))
            return SearchResponse.model_validate(result)
        except (BrokerUnavailable, ValidationError):
            return terminal_search_response(request, "error")

    def check(self) -> str:
        result = self._request("check", {})
        if set(result) != {"status"} or result["status"] not in {
            "ready",
            "blocked",
            "error",
        }:
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        return result["status"]

    def health(self) -> dict[str, Any]:
        result = self._request("health", {})
        expected = {
            "uptime_seconds",
            "queue_depth",
            "active_clients",
            "completed_requests",
            "error_count",
            "peak_overlap",
            "serialization_wait_count",
        }
        if set(result) != expected:
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        if type(result["uptime_seconds"]) not in (int, float) or result[
            "uptime_seconds"
        ] < 0:
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        if any(
            type(result[name]) is not int or result[name] < 0
            for name in expected - {"uptime_seconds"}
        ):
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._request("release_client", {})
        except BrokerUnavailable:
            pass
