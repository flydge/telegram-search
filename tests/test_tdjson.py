from __future__ import annotations

import tempfile
import unittest
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from telegram_search_mcp.config import TDLIB_LIBRARY, validate_tdlib_runtime
from telegram_search_mcp.keychain import ApiCredentials
from telegram_search_mcp.discovery_state import (
    CatalogPosition,
    DiscoveryRegistry,
    GlobalLaneKey,
)
from telegram_search_mcp.tdjson import (
    AuthorizationBlocked,
    ForbiddenTDLibRequest,
    LegacyTDJson,
    SecretChatRejected,
    TDLibError,
    TDLibClient,
)


def text_message(chat_id: int, message_id: int, text: str) -> dict[str, Any]:
    return {
        "@type": "message",
        "id": message_id,
        "chat_id": chat_id,
        "date": 1_700_000_000,
        "sender_id": {"@type": "messageSenderUser", "user_id": 7},
        "content": {
            "@type": "messageText",
            "text": {"@type": "formattedText", "text": text, "entities": []},
        },
    }


class ScriptedRaw:
    def __init__(self, replies: dict[str, list[dict[str, Any]]]) -> None:
        self.replies = {key: deque(value) for key, value in replies.items()}
        self.pending: deque[dict[str, Any]] = deque()
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        reply = dict(self.replies[payload["@type"]].popleft())
        reply["@extra"] = payload["@extra"]
        self.pending.append(reply)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        del timeout
        return self.pending.popleft() if self.pending else None

    def close(self) -> None:
        pass


class StreamingRaw:
    def __init__(self, transactions: dict[str, list[list[dict[str, Any]]]]) -> None:
        self.transactions = {
            key: deque(list(transaction) for transaction in values)
            for key, values in transactions.items()
        }
        self.pending: deque[dict[str, Any]] = deque()
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        transaction = self.transactions[payload["@type"]].popleft()
        for index, raw_reply in enumerate(transaction):
            reply = dict(raw_reply)
            if index == len(transaction) - 1:
                reply["@extra"] = payload["@extra"]
            self.pending.append(reply)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        del timeout
        return self.pending.popleft() if self.pending else None

    def close(self) -> None:
        pass


def positioned_chat(chat_id: int, chat_list: str, order: object) -> dict[str, Any]:
    list_type = "chatListMain" if chat_list == "main" else "chatListArchive"
    wire_order = str(order) if isinstance(order, int) and not isinstance(order, bool) else order
    return {
        "@type": "chat",
        "id": chat_id,
        "title": "Synthetic Chat",
        "type": {"@type": "chatTypeSupergroup", "is_channel": False},
        "positions": [
            {
                "@type": "chatPosition",
                "list": {"@type": list_type},
                "order": wire_order,
                "is_pinned": False,
                "source": None,
            }
        ],
    }


class TDLibBoundaryTests(unittest.TestCase):
    def test_serialization_wait_counter_records_contention_without_payloads(self) -> None:
        class ContendedRaw:
            def __init__(self) -> None:
                self.entered = threading.Event()
                self.release = threading.Event()
                self.extras: dict[int, str] = {}

            def send(self, payload: dict[str, Any]) -> None:
                self.extras[threading.get_ident()] = payload["@extra"]

            def receive(self, timeout: float) -> dict[str, Any] | None:
                del timeout
                self.entered.set()
                self.release.wait(timeout=2)
                return {
                    "@type": "authorizationStateReady",
                    "@extra": self.extras[threading.get_ident()],
                }

            def close(self) -> None:
                pass

        raw = ContendedRaw()
        client = TDLibClient(raw=raw)
        errors: list[BaseException] = []

        def ready() -> None:
            try:
                client.ensure_ready()
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=ready)
        second = threading.Thread(target=ready)
        first.start()
        self.assertTrue(raw.entered.wait(timeout=2))
        second.start()
        time.sleep(0.05)

        self.assertEqual(client.serialization_wait_count, 1)
        raw.release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertEqual(errors, [])

    def test_installed_runtime_matches_pinned_keg_and_legacy_abi(self) -> None:
        resolved = validate_tdlib_runtime()

        self.assertEqual(resolved, TDLIB_LIBRARY.resolve())
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from telegram_search_mcp.tdjson import LegacyTDJson; "
                "raw = LegacyTDJson(); raw.close()",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(probe.returncode, 0)
        self.assertEqual(probe.stdout, "")
        self.assertEqual(probe.stderr, "")

    def test_initialization_uses_current_top_level_parameter_schema(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [
                    {"@type": "authorizationStateWaitTdlibParameters"},
                    {"@type": "authorizationStateWaitEncryptionKey"},
                    {"@type": "authorizationStateReady"},
                ],
                "setTdlibParameters": [{"@type": "ok"}],
                "checkDatabaseEncryptionKey": [{"@type": "ok"}],
            }
        )
        with tempfile.TemporaryDirectory() as parent:
            client = TDLibClient(
                raw=raw,
                session_directory=Path(parent) / "tdlib",
                credential_loader=lambda: ApiCredentials(api_id=123456, api_hash="test-hash"),
            )

            client.ensure_ready()

        parameters = next(item for item in raw.sent if item["@type"] == "setTdlibParameters")
        self.assertNotIn("parameters", parameters)
        self.assertEqual(parameters["api_id"], 123456)
        self.assertEqual(parameters["api_hash"], "test-hash")
        self.assertTrue(parameters["use_message_database"])
        self.assertFalse(parameters["use_secret_chats"])
        self.assertFalse(parameters["use_file_database"])
        self.assertTrue(str(parameters["database_directory"]).endswith("/tdlib/db"))
        self.assertTrue(str(parameters["files_directory"]).endswith("/tdlib/files"))

    def test_forbidden_request_is_rejected_before_transport(self) -> None:
        raw = ScriptedRaw({})
        client = TDLibClient(raw=raw)

        with self.assertRaises(ForbiddenTDLibRequest):
            client._call({"@type": "sendMessage"})

        self.assertEqual(raw.sent, [])

    def test_non_ready_session_fails_closed_without_interactive_credentials(self) -> None:
        raw = ScriptedRaw(
            {"getAuthorizationState": [{"@type": "authorizationStateWaitPhoneNumber"}]}
        )
        client = TDLibClient(raw=raw)

        with self.assertRaises(AuthorizationBlocked):
            client.ensure_ready()

    def test_ready_state_is_refreshed_and_closed_state_blocks_the_next_search_boundary(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [
                    {"@type": "authorizationStateReady"},
                    {"@type": "authorizationStateClosed"},
                ]
            }
        )
        client = TDLibClient(raw=raw)

        client.ensure_ready()
        with self.assertRaises(AuthorizationBlocked):
            client.ensure_ready()

        self.assertEqual(
            [request["@type"] for request in raw.sent],
            ["getAuthorizationState", "getAuthorizationState"],
        )

    def test_authorization_update_during_data_call_clears_ready_and_stops_read(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "getChat": [
                    {"@type": "chat", "id": 42, "type": {"@type": "chatTypePrivate"}}
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        raw.pending.append(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateClosed"},
            }
        )

        with self.assertRaises(AuthorizationBlocked):
            client.resolve_target(42)

        self.assertEqual(raw.pending[-1]["@type"], "chat")
        sent_after_revocation = len(raw.sent)
        with self.assertRaises(AuthorizationBlocked):
            client.resolve_target(42)
        self.assertEqual(len(raw.sent), sent_after_revocation)

    def test_message_link_does_not_downgrade_authorization_loss_to_missing_link(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "getMessageLink": [
                    {"@type": "messageLink", "link": "https://t.me/known_chat/10"}
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        raw.pending.append(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateClosed"},
            }
        )

        with self.assertRaises(AuthorizationBlocked):
            client.get_message_link(-1001, 10)

    def test_chat_link_never_synthesizes_a_url_from_public_username_metadata(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "getMe": [{"@type": "user", "id": 700}],
                "getUser": [
                    {
                        "@type": "user",
                        "id": 77,
                        "first_name": "Example",
                        "last_name": "User",
                        "usernames": {
                            "@type": "usernames",
                            "active_usernames": ["example_user"],
                            "disabled_usernames": [],
                            "editable_username": "example_user",
                        },
                    }
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        link = client.get_chat_link(
            {
                "@type": "chat",
                "id": 77,
                "title": "Example User",
                "type": {"@type": "chatTypePrivate", "user_id": 77},
            }
        )

        self.assertIsNone(link)
        self.assertEqual(
            [request["@type"] for request in raw.sent],
            ["getAuthorizationState"],
        )

    def test_private_chat_link_rejects_missing_or_malformed_public_identity(self) -> None:
        for user in (
            {
                "@type": "user",
                "id": 77,
                "first_name": "No",
                "last_name": "Username",
                "usernames": {
                    "@type": "usernames",
                    "active_usernames": [],
                    "disabled_usernames": ["old_name"],
                    "editable_username": "",
                },
            },
            {
                "@type": "user",
                "id": 77,
                "first_name": "Bad",
                "last_name": "Username",
                "usernames": {
                    "@type": "usernames",
                    "active_usernames": ["bad-name"],
                    "disabled_usernames": [],
                    "editable_username": "bad-name",
                },
            },
            {
                "@type": "user",
                "id": 78,
                "first_name": "Wrong",
                "last_name": "Identity",
                "usernames": {
                    "@type": "usernames",
                    "active_usernames": ["example_user"],
                    "disabled_usernames": [],
                    "editable_username": "example_user",
                },
            },
        ):
            with self.subTest(user=user):
                raw = ScriptedRaw(
                    {
                        "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                        "getMe": [{"@type": "user", "id": 700}],
                        "getUser": [user],
                    }
                )
                client = TDLibClient(raw=raw)
                client.ensure_ready()

                self.assertIsNone(
                    client.get_chat_link(
                        {
                            "@type": "chat",
                            "id": 77,
                            "title": "Example User",
                            "type": {"@type": "chatTypePrivate", "user_id": 77},
                        }
                    )
                )

    def test_chat_link_skips_chats_without_a_private_counterpart(self) -> None:
        raw = ScriptedRaw(
            {"getAuthorizationState": [{"@type": "authorizationStateReady"}]}
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        link = client.get_chat_link(
            {
                "@type": "chat",
                "id": -1001,
                "title": "Example Group",
                "type": {"@type": "chatTypeSupergroup", "is_channel": False},
            }
        )

        self.assertIsNone(link)
        self.assertEqual([request["@type"] for request in raw.sent], ["getAuthorizationState"])

    def test_chat_link_rejects_saved_messages_identity(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "getMe": [{"@type": "user", "id": 77}],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        link = client.get_chat_link(
            {
                "@type": "chat",
                "id": 77,
                "title": "Saved Messages",
                "type": {"@type": "chatTypePrivate", "user_id": 77},
            }
        )

        self.assertIsNone(link)
        self.assertEqual(
            [request["@type"] for request in raw.sent],
            ["getAuthorizationState"],
        )

    def test_chat_link_never_touches_provider_after_authorization_loss(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "getMe": [{"@type": "user", "id": 700}],
                "getUser": [
                    {
                        "@type": "user",
                        "id": 77,
                        "first_name": "Example",
                        "last_name": "User",
                        "usernames": {
                            "@type": "usernames",
                            "active_usernames": ["example_user"],
                            "disabled_usernames": [],
                            "editable_username": "example_user",
                        },
                    }
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        raw.pending.append(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateClosed"},
            }
        )

        self.assertIsNone(
            client.get_chat_link(
                {
                    "@type": "chat",
                    "id": 77,
                    "title": "Example User",
                    "type": {"@type": "chatTypePrivate", "user_id": 77},
                }
            )
        )
        self.assertEqual(
            [request["@type"] for request in raw.sent],
            ["getAuthorizationState"],
        )

    def test_exact_target_resolution_uses_only_search_public_chat_or_get_chat(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "searchPublicChat": [
                    {"@type": "chat", "id": -10011, "type": {"@type": "chatTypeSupergroup"}}
                ],
                "getChat": [
                    {"@type": "chat", "id": 42, "type": {"@type": "chatTypePrivate"}}
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        by_name = client.resolve_target("@known_chat")
        by_id = client.resolve_target(42)

        self.assertEqual(by_name["id"], -10011)
        self.assertEqual(by_id["id"], 42)
        self.assertEqual(raw.sent[1]["username"], "known_chat")
        self.assertEqual(raw.sent[2]["chat_id"], 42)

    def test_numeric_target_resolution_rejects_unbound_or_invalid_chat_ids(self) -> None:
        for requested_id, returned_id in (
            (-1001, -2002),
            (1, True),
            (-1001, 0),
        ):
            with self.subTest(requested_id=requested_id, returned_id=returned_id):
                raw = ScriptedRaw(
                    {
                        "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                        "getChat": [
                            {
                                "@type": "chat",
                                "id": returned_id,
                                "type": {"@type": "chatTypePrivate"},
                            }
                        ],
                    }
                )
                client = TDLibClient(raw=raw)
                client.ensure_ready()

                with self.assertRaises(TDLibError):
                    client.resolve_target(requested_id)

                self.assertEqual(
                    [request["@type"] for request in raw.sent],
                    ["getAuthorizationState", "getChat"],
                )
                self.assertEqual(raw.sent[1]["chat_id"], requested_id)

    def test_secret_chat_is_rejected_after_exact_resolution(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "getChat": [
                    {"@type": "chat", "id": 99, "type": {"@type": "chatTypeSecret"}}
                ]
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        with self.assertRaises(SecretChatRejected):
            client.resolve_target(99)

    def test_self_chat_uses_verified_account_identity_and_fixed_private_chat_request(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "getMe": [{"@type": "user", "id": 700}],
                "createPrivateChat": [
                    {
                        "@type": "chat",
                        "id": 700,
                        "type": {"@type": "chatTypePrivate", "user_id": 700},
                    }
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        self.assertEqual(client.get_self_chat()["id"], 700)
        self.assertEqual(
            raw.sent,
            [
                {"@type": "getAuthorizationState", "@extra": "telegram-search-mcp-1"},
                {"@type": "getMe", "@extra": "telegram-search-mcp-2"},
                {
                    "@type": "createPrivateChat",
                    "user_id": 700,
                    "force": True,
                    "@extra": "telegram-search-mcp-3",
                },
            ],
        )

    def test_self_chat_rejects_invalid_identity_or_unverified_chat(self) -> None:
        cases = (
            (
                {"@type": "user", "id": True},
                None,
                TDLibError,
            ),
            (
                {"@type": "user", "id": 700},
                {"@type": "chat", "id": 0, "type": {"@type": "chatTypePrivate", "user_id": 700}},
                TDLibError,
            ),
            (
                {"@type": "user", "id": 700},
                {"@type": "chat", "id": 701, "type": {"@type": "chatTypePrivate", "user_id": 701}},
                TDLibError,
            ),
            (
                {"@type": "user", "id": 700},
                {"@type": "chat", "id": 701, "type": {"@type": "chatTypeSecret"}},
                SecretChatRejected,
            ),
        )
        for user, chat, error_type in cases:
            with self.subTest(user=user, chat=chat):
                replies: dict[str, list[dict[str, Any]]] = {
                    "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                    "getMe": [user],
                }
                if chat is not None:
                    replies["createPrivateChat"] = [chat]
                raw = ScriptedRaw(replies)
                client = TDLibClient(raw=raw)
                client.ensure_ready()

                with self.assertRaises(error_type):
                    client.get_self_chat()

    def test_known_chat_discovery_uses_fixed_requests_caps_and_provider_order(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "searchChats": [
                    {"@type": "chats", "total_count": 3, "chat_ids": [9, 3, 9, -2]}
                ],
                "searchChatsOnServer": [
                    {"@type": "chats", "total_count": 21, "chat_ids": list(range(1, 22))},
                    {"@type": "chats", "total_count": 2, "chat_ids": [8, 7]},
                ],
                "getChats": [
                    {"@type": "chats", "chat_ids": list(range(1, 52))}
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        self.assertEqual(client.search_known_chat_ids("book club"), ([9, 3, -2], True))
        self.assertEqual(
            client.search_known_chat_ids_on_server("book club"), (list(range(1, 21)), False)
        )
        self.assertEqual(client.search_known_chat_ids_on_server("exact"), ([8, 7], True))
        self.assertEqual(client.get_recent_main_chat_ids(), list(range(1, 51)))
        self.assertEqual(
            raw.sent,
            [
                {"@type": "getAuthorizationState", "@extra": "telegram-search-mcp-1"},
                {
                    "@type": "searchChats",
                    "query": "book club",
                    "limit": 20,
                    "type_filter": None,
                    "@extra": "telegram-search-mcp-2",
                },
                {
                    "@type": "searchChatsOnServer",
                    "query": "book club",
                    "limit": 20,
                    "type_filter": None,
                    "@extra": "telegram-search-mcp-3",
                },
                {
                    "@type": "searchChatsOnServer",
                    "query": "exact",
                    "limit": 20,
                    "type_filter": None,
                    "@extra": "telegram-search-mcp-4",
                },
                {
                    "@type": "getChats",
                    "chat_list": {"@type": "chatListMain"},
                    "limit": 50,
                    "@extra": "telegram-search-mcp-5",
                },
            ],
        )

    def test_known_chat_discovery_rejects_malformed_or_incomplete_response_data(self) -> None:
        cases = (
            ("search_known_chat_ids", "searchChats", {"@type": "users", "total_count": 0, "chat_ids": []}),
            ("search_known_chat_ids", "searchChats", {"@type": "chats", "total_count": True, "chat_ids": []}),
            ("search_known_chat_ids_on_server", "searchChatsOnServer", {"@type": "chats", "total_count": 0, "chat_ids": [0]}),
            ("search_known_chat_ids_on_server", "searchChatsOnServer", {"@type": "chats", "total_count": 0, "chat_ids": [True]}),
            ("get_recent_main_chat_ids", "getChats", {"@type": "chats", "chat_ids": "not-a-list"}),
        )
        for method_name, request_type, response in cases:
            with self.subTest(response=response):
                raw = ScriptedRaw(
                    {
                        "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                        request_type: [response],
                    }
                )
                client = TDLibClient(raw=raw)
                client.ensure_ready()

                with self.assertRaises(TDLibError):
                    getattr(client, method_name)("query") if method_name != "get_recent_main_chat_ids" else getattr(client, method_name)()

    def test_disallowed_discovery_and_mutation_requests_never_reach_transport(self) -> None:
        raw = ScriptedRaw({})
        client = TDLibClient(raw=raw)

        for request_type in (
            "searchPublicChats",
            "viewMessages",
            "sendMessage",
            "editMessageText",
            "deleteMessages",
            "addMessageReaction",
            "downloadFile",
        ):
            with self.subTest(request_type=request_type):
                with self.assertRaises(ForbiddenTDLibRequest):
                    client._call({"@type": request_type})

        self.assertEqual(raw.sent, [])

    def test_global_search_uses_fixed_main_archive_request_shape(self) -> None:
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "searchMessages": [
                    {
                        "@type": "foundMessages",
                        "total_count": -1,
                        "messages": [text_message(-1001, 11, "synthetic evidence")],
                        "next_offset": "opaque-next",
                    },
                    {
                        "@type": "foundMessages",
                        "total_count": 1,
                        "messages": [],
                        "next_offset": "",
                    },
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        first = client.search_global_messages("main", "garden", offset="")
        second = client.search_global_messages(
            "archive", "garden", offset="opaque-next"
        )

        self.assertEqual(first.next_offset, "opaque-next")
        self.assertEqual(first.messages[0]["id"], 11)
        self.assertEqual(second.next_offset, "")
        self.assertEqual(
            raw.sent[1],
            {
                "@type": "searchMessages",
                "chat_list": {"@type": "chatListMain"},
                "query": "garden",
                "offset": "",
                "limit": 10,
                "filter": None,
                "chat_type_filter": None,
                "min_date": 0,
                "max_date": 0,
                "@extra": "telegram-search-mcp-2",
            },
        )
        self.assertEqual(raw.sent[2]["chat_list"], {"@type": "chatListArchive"})
        self.assertEqual(raw.sent[2]["limit"], 10)

    def test_global_search_rejects_malformed_page_envelopes_without_echoing_values(self) -> None:
        sentinel = "DO-NOT-ECHO-SENSITIVE-PROVIDER-VALUE"
        malformed_pages = (
            {"@type": "messages", "total_count": 0, "messages": [], "next_offset": ""},
            {"@type": "foundMessages", "total_count": 0, "next_offset": ""},
            {"@type": "foundMessages", "total_count": 0, "messages": sentinel, "next_offset": ""},
            {"@type": "foundMessages", "total_count": True, "messages": [], "next_offset": ""},
            {"@type": "foundMessages", "total_count": -2, "messages": [], "next_offset": ""},
            {"@type": "foundMessages", "total_count": 0, "messages": [], "next_offset": 7},
        )
        for page in malformed_pages:
            with self.subTest(
                page_type=page.get("@type"),
                field_types=tuple(type(value).__name__ for value in page.values()),
            ):
                raw = ScriptedRaw(
                    {
                        "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                        "searchMessages": [page],
                    }
                )
                client = TDLibClient(raw=raw)
                client.ensure_ready()

                with self.assertRaises(TDLibError) as raised:
                    client.search_global_messages("main", "garden", offset="")

                self.assertNotIn(sentinel, str(raised.exception))
                self.assertEqual(
                    str(raised.exception),
                    "TDLib returned an invalid global message envelope",
                )

    def test_malformed_messages_are_omitted_with_integrity_partial_and_no_echo(self) -> None:
        sentinel = "DO-NOT-ECHO-MALFORMED-MESSAGE"
        malformed_messages = (
            text_message(True, 11, sentinel),
            text_message(-1001, 0, sentinel),
            {**text_message(-1001, 11, sentinel), "date": -1},
            {
                **text_message(-1001, 11, sentinel),
                "sender_id": {"@type": "messageSenderUser", "user_id": True},
            },
            {**text_message(-1001, 11, sentinel), "content": sentinel},
        )
        for malformed in malformed_messages:
            with self.subTest(fields=tuple(sorted(malformed))):
                raw = ScriptedRaw(
                    {
                        "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                        "searchMessages": [
                            {
                                "@type": "foundMessages",
                                "total_count": 1,
                                "messages": [malformed],
                                "next_offset": "trusted-next",
                            }
                        ],
                    }
                )
                client = TDLibClient(raw=raw)
                client.ensure_ready()

                page = client.search_global_messages("main", "garden", offset="")

                self.assertEqual(page.messages, [])
                self.assertEqual(page.next_offset, "trusted-next")
                self.assertTrue(page.integrity_partial)
                self.assertNotIn(sentinel, repr(page.messages))

    def test_global_search_keeps_valid_messages_and_offset_from_mixed_page(self) -> None:
        sentinel = "SENSITIVE-MALFORMED-MESSAGE"
        malformed = text_message(-1002, 12, sentinel)
        malformed["id"] = 0
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "searchMessages": [
                    {
                        "@type": "foundMessages",
                        "total_count": 2,
                        "messages": [
                            text_message(-1001, 11, "synthetic evidence"),
                            malformed,
                        ],
                        "next_offset": "trusted-next",
                    }
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        page = client.search_global_messages("main", "garden", offset="")

        self.assertEqual([message["id"] for message in page.messages], [11])
        self.assertEqual(page.next_offset, "trusted-next")
        self.assertTrue(page.integrity_partial)
        self.assertNotIn(sentinel, repr(page.messages))

    def test_global_search_skips_known_non_evidence_content_without_partial(self) -> None:
        sticker = text_message(-1001, 11, "discarded helper text")
        sticker["content"] = {
            "@type": "messageSticker",
            "sticker": {"@type": "sticker", "id": 77},
        }
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "searchMessages": [
                    {
                        "@type": "foundMessages",
                        "total_count": 1,
                        "messages": [sticker],
                        "next_offset": "next",
                    }
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        page = client.search_global_messages("main", "garden", offset="")

        self.assertEqual(page.messages, [])
        self.assertEqual(page.next_offset, "next")
        self.assertFalse(page.integrity_partial)

    def test_global_search_skips_known_message_chat_service_without_partial(self) -> None:
        service = text_message(-1001, 11, "discarded helper text")
        service["content"] = {
            "@type": "messageChatChangeTitle",
            "title": "Synthetic title",
        }
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "searchMessages": [
                    {
                        "@type": "foundMessages",
                        "total_count": 1,
                        "messages": [service],
                        "next_offset": "next",
                    }
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        page = client.search_global_messages("main", "garden", offset="")

        self.assertEqual(page.messages, [])
        self.assertEqual(page.next_offset, "next")
        self.assertFalse(page.integrity_partial)

    def test_global_search_marks_unknown_message_chat_variants_partial(self) -> None:
        integrity_results: list[bool] = []
        for content_type in ("messageChatFutureEvidence", "messageChat"):
            unknown = text_message(-1001, 11, "discarded helper text")
            unknown["content"] = {
                "@type": content_type,
                "caption": {
                    "@type": "formattedText",
                    "text": "sensitive unknown content",
                    "entities": [],
                },
            }
            raw = ScriptedRaw(
                {
                    "getAuthorizationState": [
                        {"@type": "authorizationStateReady"}
                    ],
                    "searchMessages": [
                        {
                            "@type": "foundMessages",
                            "total_count": 1,
                            "messages": [unknown],
                            "next_offset": "trusted-next",
                        }
                    ],
                }
            )
            client = TDLibClient(raw=raw)
            client.ensure_ready()

            page = client.search_global_messages("main", "garden", offset="")

            self.assertEqual(page.messages, [])
            self.assertEqual(page.next_offset, "trusted-next")
            integrity_results.append(page.integrity_partial)

        self.assertEqual(integrity_results, [True, True])

    def test_global_search_marks_unknown_content_partial_even_with_caption(self) -> None:
        unknown = text_message(-1001, 11, "discarded helper text")
        unknown["content"] = {
            "@type": "messageFutureEvidence",
            "caption": {
                "@type": "formattedText",
                "text": "sensitive unknown content",
                "entities": [],
            },
        }
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "searchMessages": [
                    {
                        "@type": "foundMessages",
                        "total_count": 1,
                        "messages": [unknown],
                        "next_offset": "next",
                    }
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        page = client.search_global_messages("main", "garden", offset="")

        self.assertEqual(page.messages, [])
        self.assertEqual(page.next_offset, "next")
        self.assertTrue(page.integrity_partial)

    def test_untrusted_global_envelope_uses_distinct_constant_redacted_error(self) -> None:
        sentinel = "SENSITIVE-UNTRUSTED-OFFSET"
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "searchMessages": [
                    {
                        "@type": "foundMessages",
                        "total_count": 0,
                        "messages": [],
                        "next_offset": {"secret": sentinel},
                    }
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        with self.assertRaises(TDLibError) as raised:
            client.search_global_messages("main", "garden", offset="")

        self.assertEqual(type(raised.exception).__name__, "GlobalMessageEnvelopeError")
        self.assertEqual(
            str(raised.exception),
            "TDLib returned an invalid global message envelope",
        )
        self.assertNotIn(sentinel, str(raised.exception))

    def test_integrity_partial_consumes_offset_while_retryable_error_preserves_it(self) -> None:
        registry = DiscoveryRegistry(ttl_seconds=300, capacity=4)
        cursor = registry.start(
            hypothesis_digest="c" * 64,
            hypothesis_count=2,
            scope="main",
        )
        key = GlobalLaneKey(0, "main")
        work = registry.next_work(cursor)
        self.assertIsNotNone(work)
        registry.record_global_page(
            cursor,
            key,
            next_offset="consumed-next",
            hits=1,
            lease_token=work.lease_token,
        )
        registry.record_permanent_partial(
            cursor,
            key,
            lease_token=work.lease_token,
        )
        registry.release_work(cursor, work.lease_token)
        partial = registry.global_lane(cursor, key)
        self.assertEqual((partial.offset, partial.status), ("consumed-next", "partial"))

        retry_registry = DiscoveryRegistry(ttl_seconds=300, capacity=4)
        retry_cursor = retry_registry.start(
            hypothesis_digest="d" * 64,
            hypothesis_count=2,
            scope="main",
        )
        retry_registry.record_global_page(
            retry_cursor, key, next_offset="stable-offset", hits=1
        )
        retry_work = retry_registry.next_work(retry_cursor)
        self.assertIsNotNone(retry_work)
        retry_registry.record_retryable_error(
            retry_cursor,
            key,
            lease_token=retry_work.lease_token,
        )
        retry_registry.release_work(retry_cursor, retry_work.lease_token)
        retryable = retry_registry.global_lane(retry_cursor, key)
        self.assertEqual((retryable.offset, retryable.status), ("stable-offset", "error"))

    def test_global_search_rejects_invalid_typed_arguments_before_transport(self) -> None:
        raw = ScriptedRaw({})
        client = TDLibClient(raw=raw)

        for chat_list, query, offset in (
            (None, "garden", ""),
            ("both", "garden", ""),
            ("main", 7, ""),
            ("main", "garden", None),
        ):
            with self.subTest(chat_list=chat_list, query=query, offset=offset):
                with self.assertRaises((TypeError, ValueError)):
                    client.search_global_messages(chat_list, query, offset=offset)

        self.assertEqual(raw.sent, [])

    def test_direct_global_requests_cannot_change_server_owned_shape(self) -> None:
        base = {
            "@type": "searchMessages",
            "chat_list": {"@type": "chatListMain"},
            "query": "garden",
            "offset": "",
            "limit": 10,
            "filter": None,
            "chat_type_filter": None,
            "min_date": 0,
            "max_date": 0,
        }
        mutations = (
            {"chat_list": None},
            {"chat_list": {"@type": "chatListFolder", "chat_folder_id": 1}},
            {"limit": 100},
            {"filter": {"@type": "searchMessagesFilterDocument"}},
            {"chat_type_filter": {"@type": "searchMessagesChatTypeFilterPrivate"}},
            {"min_date": 1},
            {"min_date": False},
            {"max_date": 1},
            {"max_date": False},
            {"extra_field": "caller-controlled"},
        )
        raw = ScriptedRaw({})
        client = TDLibClient(raw=raw)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ForbiddenTDLibRequest):
                    client._call({**base, **mutation})
        self.assertEqual(raw.sent, [])

    def test_catalog_prefix_and_load_use_fixed_list_scoped_shapes(self) -> None:
        raw = StreamingRaw(
            {
                "getAuthorizationState": [
                    [{"@type": "authorizationStateReady"}]
                ],
                "getChats": [
                    [{"@type": "chats", "chat_ids": [-1001, 7, -1001]}],
                    [{"@type": "chats", "chat_ids": [-2002]}],
                ],
                "loadChats": [
                    [
                        {"@type": "updateNewChat", "chat": positioned_chat(-1002, "main", 90)},
                        {
                            "@type": "updateChatPosition",
                            "chat_id": -1003,
                            "position": positioned_chat(-1003, "main", 80)["positions"][0],
                        },
                        {"@type": "updateUserStatus", "user_id": 7},
                        {"@type": "ok"},
                    ],
                    [{"@type": "error", "code": 404, "message": "Not Found"}],
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        self.assertEqual(client.get_chat_list_prefix("main"), [-1001, 7])
        self.assertEqual(client.get_chat_list_prefix("archive"), [-2002])
        seen: list[CatalogPosition] = []
        self.assertFalse(client.load_more_chats("main", seen.extend))
        self.assertEqual(
            seen,
            [CatalogPosition(-1002, 90), CatalogPosition(-1003, 80)],
        )
        self.assertTrue(client.load_more_chats("main", seen.extend))
        self.assertEqual(raw.sent[1]["limit"], 15)
        self.assertEqual(raw.sent[1]["chat_list"], {"@type": "chatListMain"})
        self.assertEqual(raw.sent[2]["chat_list"], {"@type": "chatListArchive"})
        self.assertEqual(raw.sent[3]["limit"], 50)
        self.assertEqual(raw.sent[3]["chat_list"], {"@type": "chatListMain"})

    def test_catalog_positions_parse_only_canonical_positive_int64_wire_strings(self) -> None:
        maximum = (1 << 63) - 1
        raw = StreamingRaw(
            {
                "getAuthorizationState": [[{"@type": "authorizationStateReady"}]],
                "loadChats": [
                    [
                        {
                            "@type": "updateNewChat",
                            "chat": positioned_chat(-1001, "main", str(maximum)),
                        },
                        {
                            "@type": "updateChatPosition",
                            "chat_id": -1002,
                            "position": positioned_chat(-1002, "main", "01")[
                                "positions"
                            ][0],
                        },
                        {
                            "@type": "updateChatPosition",
                            "chat_id": -1003,
                            "position": positioned_chat(
                                -1003, "main", str(maximum + 1)
                            )["positions"][0],
                        },
                        {
                            "@type": "updateChatPosition",
                            "chat_id": -1004,
                            "position": positioned_chat(-1004, "main", "-1")[
                                "positions"
                            ][0],
                        },
                        {
                            "@type": "updateChatPosition",
                            "chat_id": -1005,
                            "position": positioned_chat(-1005, "main", "9" * 5000)[
                                "positions"
                            ][0],
                        },
                        {"@type": "ok"},
                    ]
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        seen: list[CatalogPosition] = []

        self.assertFalse(client.load_more_chats("main", seen.extend))

        self.assertEqual(seen, [CatalogPosition(-1001, maximum)])

    def test_wire_string_suffix_is_emitted_before_catalog_can_complete(self) -> None:
        raw = StreamingRaw(
            {
                "getAuthorizationState": [[{"@type": "authorizationStateReady"}]],
                "loadChats": [
                    [
                        {
                            "@type": "updateNewChat",
                            "chat": positioned_chat(-1016, "main", "84"),
                        },
                        {
                            "@type": "updateNewChat",
                            "chat": positioned_chat(-1017, "main", "83"),
                        },
                        {"@type": "error", "code": 404, "message": "Not Found"},
                    ]
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        registry = DiscoveryRegistry(ttl_seconds=300, capacity=4)
        cursor = registry.start(
            hypothesis_digest="a" * 64,
            hypothesis_count=2,
            scope="main",
        )
        seen: list[CatalogPosition] = []

        ended = client.load_more_chats("main", seen.extend)
        registry.record_catalog_positions(cursor, "main", seen)
        if ended:
            registry.mark_catalog_end(cursor, "main")

        self.assertEqual(registry.catalog_page(cursor, "main"), [-1016, -1017])
        self.assertFalse(registry.is_complete(cursor))
        for index in (0, 1):
            registry.record_global_page(
                cursor,
                GlobalLaneKey(index, "main"),
                next_offset="",
                hits=0,
            )
        self.assertTrue(registry.is_complete(cursor))

    def test_catalog_updates_ignore_malformed_secret_and_other_list_positions(self) -> None:
        secret = positioned_chat(91, "main", 100)
        secret["type"] = {"@type": "chatTypeSecret"}
        raw = StreamingRaw(
            {
                "getAuthorizationState": [[{"@type": "authorizationStateReady"}]],
                "loadChats": [
                    [
                        {"@type": "updateNewChat", "chat": secret},
                        {
                            "@type": "updateChatPosition",
                            "chat_id": True,
                            "position": positioned_chat(-2, "main", 90)["positions"][0],
                        },
                        {
                            "@type": "updateChatPosition",
                            "chat_id": -3,
                            "position": positioned_chat(-3, "archive", 80)["positions"][0],
                        },
                        {
                            "@type": "updateChatLastMessage",
                            "chat_id": -4,
                            "positions": [
                                positioned_chat(-4, "main", 70)["positions"][0],
                                {
                                    **positioned_chat(-4, "main", 60)["positions"][0],
                                    "order": "bad",
                                },
                            ],
                        },
                        {"@type": "ok"},
                    ]
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        seen: list[CatalogPosition] = []

        self.assertFalse(client.load_more_chats("main", seen.extend))

        self.assertEqual(seen, [CatalogPosition(-4, 70)])

    def test_catalog_updates_from_other_requests_seed_later_catalog_loads(self) -> None:
        raw = StreamingRaw(
            {
                "getAuthorizationState": [[{"@type": "authorizationStateReady"}]],
                "searchMessages": [
                    [
                        {
                            "@type": "updateChatPosition",
                            "chat_id": -1008,
                            "position": positioned_chat(-1008, "main", 88)[
                                "positions"
                            ][0],
                        },
                        {
                            "@type": "foundMessages",
                            "total_count": 0,
                            "messages": [],
                            "next_offset": "",
                        },
                    ]
                ],
                "loadChats": [
                    [{"@type": "error", "code": 404, "message": "Not Found"}]
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        client.search_global_messages("main", "garden", offset="")
        seen: list[CatalogPosition] = []
        ended = client.load_more_chats("main", seen.extend)

        self.assertTrue(ended)
        self.assertEqual(seen, [CatalogPosition(-1008, 88)])
        self.assertEqual(set(client._catalog_positions), {"main", "archive"})
        self.assertTrue(
            all(
                isinstance(chat_id, int) and isinstance(order, int)
                for positions in client._catalog_positions.values()
                for chat_id, order in positions.items()
            )
        )

    def test_preloaded_catalog_larger_than_prefix_is_available_to_repeated_scans(self) -> None:
        updates = [
            {
                "@type": "updateNewChat",
                "chat": positioned_chat(-2000 - index, "main", 1000 - index),
            }
            for index in range(20)
        ]
        raw = StreamingRaw(
            {
                "getAuthorizationState": [[{"@type": "authorizationStateReady"}]],
                "searchMessages": [
                    [
                        *updates,
                        {
                            "@type": "foundMessages",
                            "total_count": 0,
                            "messages": [],
                            "next_offset": "",
                        },
                    ]
                ],
                "getChats": [
                    [
                        {
                            "@type": "chats",
                            "chat_ids": [-2000 - index for index in range(15)],
                        }
                    ],
                    [
                        {
                            "@type": "chats",
                            "chat_ids": [-2000 - index for index in range(15)],
                        }
                    ],
                ],
                "loadChats": [
                    [{"@type": "error", "code": 404, "message": "Not Found"}],
                    [{"@type": "error", "code": 404, "message": "Not Found"}],
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        client.search_global_messages("main", "garden", offset="")

        snapshots: list[list[CatalogPosition]] = []
        for _ in range(2):
            self.assertEqual(len(client.get_chat_list_prefix("main")), 15)
            seen: list[CatalogPosition] = []
            self.assertTrue(client.load_more_chats("main", seen.extend))
            snapshots.append(seen)

        expected = [
            CatalogPosition(-2000 - index, 1000 - index) for index in range(20)
        ]
        self.assertEqual(snapshots, [expected, expected])

    def test_catalog_state_applies_both_lists_and_zero_order_removal(self) -> None:
        both_lists = positioned_chat(-3001, "main", 100)
        both_lists["positions"].append(
            positioned_chat(-3001, "archive", 90)["positions"][0]
        )
        raw = StreamingRaw(
            {
                "getAuthorizationState": [[{"@type": "authorizationStateReady"}]],
                "searchMessages": [
                    [
                        {"@type": "updateNewChat", "chat": both_lists},
                        {
                            "@type": "updateChatPosition",
                            "chat_id": -3001,
                            "position": positioned_chat(-3001, "main", 0)[
                                "positions"
                            ][0],
                        },
                        {
                            "@type": "foundMessages",
                            "total_count": 0,
                            "messages": [],
                            "next_offset": "",
                        },
                    ]
                ],
                "loadChats": [
                    [{"@type": "error", "code": 404, "message": "Not Found"}],
                    [{"@type": "error", "code": 404, "message": "Not Found"}],
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        client.search_global_messages("main", "garden", offset="")
        main: list[CatalogPosition] = []
        archive: list[CatalogPosition] = []

        self.assertTrue(client.load_more_chats("main", main.extend))
        self.assertTrue(client.load_more_chats("archive", archive.extend))

        self.assertEqual(main, [])
        self.assertEqual(archive, [CatalogPosition(-3001, 90)])

    def test_malformed_full_position_update_preserves_known_good_numeric_state(self) -> None:
        raw = StreamingRaw(
            {
                "getAuthorizationState": [[{"@type": "authorizationStateReady"}]],
                "searchMessages": [
                    [
                        {
                            "@type": "updateChatPosition",
                            "chat_id": -3002,
                            "position": positioned_chat(-3002, "main", 91)[
                                "positions"
                            ][0],
                        },
                        {
                            "@type": "updateChatLastMessage",
                            "chat_id": -3002,
                            "positions": [
                                positioned_chat(-3002, "main", "01")["positions"][0]
                            ],
                        },
                        {
                            "@type": "foundMessages",
                            "total_count": 0,
                            "messages": [],
                            "next_offset": "",
                        },
                    ]
                ],
                "loadChats": [
                    [{"@type": "error", "code": 404, "message": "Not Found"}]
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        client.search_global_messages("main", "garden", offset="")
        seen: list[CatalogPosition] = []

        self.assertTrue(client.load_more_chats("main", seen.extend))

        self.assertEqual(seen, [CatalogPosition(-3002, 91)])

    def test_catalog_update_is_retained_and_delivered_before_transient_load_error(self) -> None:
        sentinel = "SENSITIVE-TRANSIENT-PROVIDER-ERROR"
        raw = StreamingRaw(
            {
                "getAuthorizationState": [[{"@type": "authorizationStateReady"}]],
                "loadChats": [
                    [
                        {
                            "@type": "updateNewChat",
                            "chat": positioned_chat(-4001, "main", 77),
                        },
                        {"@type": "error", "code": 500, "message": sentinel},
                    ],
                    [{"@type": "error", "code": 404, "message": "Not Found"}],
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        first: list[CatalogPosition] = []

        with self.assertRaises(TDLibError) as raised:
            client.load_more_chats("main", first.extend)

        self.assertNotIn(sentinel, str(raised.exception))
        self.assertEqual(first, [CatalogPosition(-4001, 77)])
        second: list[CatalogPosition] = []
        self.assertTrue(client.load_more_chats("main", second.extend))
        self.assertEqual(second, first)

    def test_load_more_chats_propagates_non_404_errors_redacted(self) -> None:
        sentinel = "SENSITIVE-PROVIDER-FAILURE"
        raw = StreamingRaw(
            {
                "getAuthorizationState": [[{"@type": "authorizationStateReady"}]],
                "loadChats": [
                    [{"@type": "error", "code": 500, "message": sentinel}]
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        with self.assertRaises(TDLibError) as raised:
            client.load_more_chats("main", lambda _: None)

        self.assertNotIn(sentinel, str(raised.exception))
        self.assertIn("code 500", str(raised.exception))

    def test_read_only_operations_emit_only_the_fixed_request_shapes(self) -> None:
        message = {
            "@type": "message",
            "id": 10,
            "chat_id": -1001,
            "sender_id": {"@type": "messageSenderUser", "user_id": 7},
            "content": {"@type": "messageText", "text": {"text": "needle"}},
        }
        raw = ScriptedRaw(
            {
                "getAuthorizationState": [{"@type": "authorizationStateReady"}],
                "getMe": [{"@type": "user", "id": 700}],
                "searchChatMessages": [
                    {
                        "@type": "foundChatMessages",
                        "total_count": 1,
                        "messages": [message],
                        "next_from_message_id": 0,
                    }
                ],
                "getChatHistory": [
                    {"@type": "messages", "total_count": 1, "messages": [message]},
                    {"@type": "messages", "total_count": 1, "messages": [message]},
                ],
                "getMessage": [message],
                "getMessageLink": [
                    {"@type": "messageLink", "link": "https://t.me/known_chat/10"}
                ],
                "getUser": [
                    {
                        "@type": "user",
                        "id": 7,
                        "first_name": "Alice",
                        "last_name": "Example",
                        "usernames": {"@type": "usernames", "active_usernames": ["alice"]},
                    }
                ],
            }
        )
        client = TDLibClient(raw=raw)
        client.ensure_ready()

        self.assertEqual(client.get_account_id(), 700)
        self.assertEqual(
            client.search_chat_messages(-1001, "needle", from_message_id=0, limit=100)[
                "messages"
            ][0]["id"],
            10,
        )
        self.assertEqual(client.get_chat_history(-1001, from_message_id=0, limit=100)[0]["id"], 10)
        self.assertEqual(client.get_message(-1001, 10)["id"], 10)
        self.assertEqual(client.get_context_messages(-1001, 10, 1)[0]["id"], 10)
        self.assertEqual(client.get_message_link(-1001, 10), "https://t.me/known_chat/10")
        self.assertEqual(client.get_sender_name(message), "Alice Example (@alice)")

        request_types = [item["@type"] for item in raw.sent]
        self.assertEqual(
            request_types,
            [
                "getAuthorizationState",
                "getMe",
                "searchChatMessages",
                "getChatHistory",
                "getMessage",
                "getChatHistory",
                "getMessageLink",
                "getUser",
            ],
        )
        search_request = raw.sent[2]
        self.assertEqual(search_request["chat_id"], -1001)
        self.assertEqual(search_request["query"], "needle")
        self.assertIsNone(search_request["filter"])


if __name__ == "__main__":
    unittest.main()
