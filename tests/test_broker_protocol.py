from __future__ import annotations

import importlib
import socket
import struct
import unittest


class BrokerProtocolTests(unittest.TestCase):
    def test_round_trip_preserves_one_strict_allowlisted_request(self) -> None:
        try:
            protocol = importlib.import_module("telegram_search_mcp.broker_protocol")
        except ModuleNotFoundError:
            self.fail("broker protocol is not implemented")

        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        request = {
            "version": protocol.PROTOCOL_VERSION,
            "client_id": "client_abcdefghijklmnopqrstuvwxyz012345",
            "request_id": "request_abcdefghijklmnopqrstuvwxyz012345",
            "operation": "resolve",
            "payload": {"target": "@known_chat"},
            "deadline": 42.5,
            "broker_generation": None,
        }

        protocol.send_frame(left, request, max_bytes=1024)
        received = protocol.receive_request(right)

        self.assertEqual(received, request)

    def test_unknown_operation_is_rejected_before_dispatch(self) -> None:
        try:
            protocol = importlib.import_module("telegram_search_mcp.broker_protocol")
        except ModuleNotFoundError:
            self.fail("broker protocol is not implemented")

        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        protocol.send_frame(
            left,
            {
                "version": protocol.PROTOCOL_VERSION,
                "client_id": "client_abcdefghijklmnopqrstuvwxyz012345",
                "request_id": "request_abcdefghijklmnopqrstuvwxyz012345",
                "operation": "raw_execute",
                "payload": {},
                "deadline": 42.5,
            "broker_generation": None,
            },
            max_bytes=1024,
        )

        with self.assertRaisesRegex(Exception, "operation"):
            protocol.receive_request(right)

    def test_duplicate_json_keys_are_rejected_as_ambiguous(self) -> None:
        protocol = importlib.import_module("telegram_search_mcp.broker_protocol")
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        body = (
            b'{"version":1,'
            b'"client_id":"client_abcdefghijklmnopqrstuvwxyz012345",'
            b'"request_id":"request_abcdefghijklmnopqrstuvwxyz012345",'
            b'"operation":"resolve","operation":"search",'
            b'"payload":{"target":"@known_chat"},"deadline":42.5}'
        )
        left.sendall(struct.pack("!I", len(body)) + body)

        with self.assertRaisesRegex(Exception, "JSON"):
            protocol.receive_request(right)

    def test_partial_and_oversized_frames_fail_closed(self) -> None:
        protocol = importlib.import_module("telegram_search_mcp.broker_protocol")
        for wire in (
            b"\x00\x00",
            struct.pack("!I", protocol.MAX_REQUEST_BYTES + 1),
            struct.pack("!I", 12) + b"{}",
        ):
            with self.subTest(wire_length=len(wire)):
                left, right = socket.socketpair()
                left.sendall(wire)
                left.close()
                self.addCleanup(right.close)
                with self.assertRaises(Exception):
                    protocol.receive_request(right)


if __name__ == "__main__":
    unittest.main()
