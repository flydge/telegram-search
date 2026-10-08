from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram_search_mcp.discovery_state import CatalogPosition, DiscoveryRegistry
from telegram_search_mcp.schemas import (
    DiscoverTargetsRequest,
    ResolveTargetRequest,
    SearchRequest,
)
from telegram_search_mcp.search_service import SearchService, _extract_file


class FileMetadataTests(unittest.TestCase):
    def test_voice_note_uses_nested_voice_file_size(self) -> None:
        evidence = _extract_file({"content": {
            "@type": "messageVoiceNote",
            "voice_note": {"mime_type": "audio/ogg", "voice": {"@type": "file", "id": 3, "size": 77}},
        }})
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.size, 77)
from telegram_search_mcp.tdjson import (
    AuthorizationBlocked,
    GlobalMessagePage,
    MessageNotFound,
    SecretChatRejected,
    TDLibError,
)

_SERVER_MESSAGE_ID_STEP = 1 << 20

_TRANSIENT_METADATA_SUBPROCESS = r"""
import json
import sys
from pathlib import Path

detector = Path(sys.argv[1])

import telegram_search_mcp.config as config

if hasattr(config, "INDEX_ROOT"):
    config.INDEX_ROOT = detector

from telegram_search_mcp.schemas import SearchRequest
from telegram_search_mcp.search_service import SearchService
from test_search_service import (
    FakeTDLibClient,
    _SERVER_MESSAGE_ID_STEP,
    document_message,
)


class LegacyCompatibleFake(FakeTDLibClient):
    def get_account_id(self) -> int:
        self.calls.append("getMe")
        return 700


hit = document_message(20 * _SERVER_MESSAGE_ID_STEP, "Synthetic Notes.pdf")
client = LegacyCompatibleFake()
client.history_pages = {0: [hit], 19 * _SERVER_MESSAGE_ID_STEP: []}
client.messages[hit["id"]] = hit
response = SearchService(client=client).search(
    SearchRequest.model_validate(
        {"target": -1001, "query": {"file_name": "Notes"}}
    )
)
print(
    json.dumps(
        {
            "files": sorted(
                str(path.relative_to(detector)) for path in detector.rglob("*")
            ),
            "status": response.status,
        }
    )
)
"""


def text_message(
    message_id: int,
    text: str,
    *,
    chat_id: int = -1001,
    date: int = 1_750_000_000,
    user_id: int = 7,
) -> dict[str, Any]:
    return {
        "@type": "message",
        "id": message_id,
        "chat_id": chat_id,
        "date": date,
        "sender_id": {"@type": "messageSenderUser", "user_id": user_id},
        "content": {
            "@type": "messageText",
            "text": {"@type": "formattedText", "text": text, "entities": []},
        },
    }


def document_message(
    message_id: int,
    file_name: str,
    *,
    caption: str = "Board pack",
    chat_id: int = -1001,
    date: int = 1_750_000_000,
) -> dict[str, Any]:
    return {
        "@type": "message",
        "id": message_id,
        "chat_id": chat_id,
        "date": date,
        "sender_id": {"@type": "messageSenderUser", "user_id": 7},
        "content": {
            "@type": "messageDocument",
            "caption": {"@type": "formattedText", "text": caption, "entities": []},
            "document": {
                "file_name": file_name,
                "mime_type": "application/pdf",
                "document": {
                    "@type": "file",
                    "id": 88,
                    "size": 1234,
                    "expected_size": 1234,
                    "local": {"path": "/private/secret/download/path", "is_downloading_completed": False},
                    "remote": {"id": "remote", "unique_id": "unique"},
                },
            },
        },
    }


class FakeTDLibClient:
    def __init__(self) -> None:
        self.ready_error: Exception | None = None
        self.resolve_error: Exception | None = None
        self.chat = {
            "@type": "chat",
            "id": -1001,
            "title": "Known Chat",
            "type": {"@type": "chatTypeSupergroup"},
        }
        self.search_pages: dict[int, dict[str, Any]] = {}
        self.history_pages: dict[int, list[dict[str, Any]]] = {}
        self.messages: dict[int, dict[str, Any] | Exception] = {}
        self.context: dict[int, list[dict[str, Any]]] = {}
        self.links: dict[int, str | None] = {}
        self.chat_link: str | None = None
        self.names: dict[int, str] = {7: "Alice Example"}
        self.global_pages: dict[tuple[str, str, str], GlobalMessagePage | Exception] = {}
        self.calls: list[str] = []

    def ensure_ready(self) -> None:
        self.calls.append("getAuthorizationState")
        if self.ready_error:
            raise self.ready_error

    def resolve_target(self, target: str | int) -> dict[str, Any]:
        self.calls.append("resolve_target")
        del target
        if self.resolve_error:
            raise self.resolve_error
        return self.chat

    def search_chat_messages(
        self, chat_id: int, query: str, *, from_message_id: int, limit: int
    ) -> dict[str, Any]:
        self.calls.append("searchChatMessages")
        self.last_text_search = (chat_id, query, from_message_id, limit)
        return self.search_pages[from_message_id]

    def get_chat_history(
        self, chat_id: int, *, from_message_id: int, limit: int
    ) -> list[dict[str, Any]]:
        self.calls.append("getChatHistory")
        self.last_history_search = (chat_id, from_message_id, limit)
        return self.history_pages[from_message_id]

    def get_message(self, chat_id: int, message_id: int) -> dict[str, Any]:
        self.calls.append("getMessage")
        del chat_id
        value = self.messages[message_id]
        if isinstance(value, Exception):
            raise value
        return value

    def get_sender_name(self, message: dict[str, Any]) -> str:
        self.calls.append("getUser")
        return self.names[message["sender_id"]["user_id"]]

    def get_context_messages(
        self, chat_id: int, message_id: int, radius: int
    ) -> list[dict[str, Any]]:
        self.calls.append("getChatHistory")
        del chat_id, radius
        return self.context.get(message_id, [])

    def get_message_link(self, chat_id: int, message_id: int) -> str | None:
        self.calls.append("getMessageLink")
        del chat_id
        return self.links.get(message_id)

    def get_chat_link(self, chat: dict[str, Any]) -> str | None:
        self.calls.append("getChatLink")
        del chat
        return self.chat_link

    def close(self) -> None:
        pass

    def get_self_chat(self) -> dict[str, Any]:
        self.calls.append("getSelfChat")
        if self.resolve_error:
            raise self.resolve_error
        return {
            "@type": "chat",
            "id": 700,
            "title": "Saved Messages",
            "type": {"@type": "chatTypePrivate", "user_id": 700},
        }

    def search_known_chat_ids(self, query: str) -> tuple[list[int], bool]:
        self.calls.append("searchChats")
        del query
        return ([], True)

    def search_known_chat_ids_on_server(self, query: str) -> tuple[list[int], bool]:
        self.calls.append("searchChatsOnServer")
        del query
        return ([], True)

    def get_recent_main_chat_ids(self) -> list[int]:
        self.calls.append("getChats")
        return []

    def get_chat_list_prefix(self, chat_list: str) -> list[int]:
        self.calls.append("getChats")
        del chat_list
        return []

    def load_more_chats(
        self,
        chat_list: str,
        on_positions: Callable[[list[CatalogPosition]], None],
    ) -> bool:
        self.calls.append("loadChats")
        del chat_list
        on_positions([])
        return True

    def search_global_messages(
        self, chat_list: str, query: str, *, offset: str
    ) -> GlobalMessagePage:
        self.calls.append("searchMessages")
        value = self.global_pages.get(
            (query, chat_list, offset),
            GlobalMessagePage(messages=[], next_offset="", integrity_partial=False),
        )
        if isinstance(value, Exception):
            raise value
        return value


class SearchServiceTests(unittest.TestCase):
    def test_discover_reuses_one_memory_registry_across_cursor_calls(self) -> None:
        client = FakeTDLibClient()
        service = SearchService(
            client=client,
            discovery_registry=DiscoveryRegistry(ttl_seconds=300, capacity=4),
        )
        request = DiscoverTargetsRequest.model_validate(
            {"hypotheses": ["garden", "seeds"], "scope": "main"}
        )

        first = service.discover(request)
        second = service.discover(request.model_copy(update={"cursor": first.next_cursor}))

        self.assertEqual(first.status, "page")
        self.assertEqual(second.status, "complete")
        self.assertEqual(client.calls.count("getAuthorizationState"), 2)
        self.assertEqual(client.calls.count("loadChats"), 1)
        self.assertEqual(client.calls.count("searchMessages"), 2)

    def test_discover_maps_readiness_failures_to_redacted_terminal_status(self) -> None:
        for error, expected in (
            (AuthorizationBlocked("SYNTHETIC_AUTH_SENTINEL"), "blocked"),
            (TDLibError("SYNTHETIC_PROVIDER_SENTINEL"), "error"),
        ):
            with self.subTest(expected=expected):
                client = FakeTDLibClient()
                client.ready_error = error

                response = SearchService(client=client).discover(
                    DiscoverTargetsRequest.model_validate(
                        {"hypotheses": ["garden", "seeds"], "scope": "both"}
                    )
                )

                self.assertEqual(response.status, expected)
                self.assertEqual(response.candidates, [])
                self.assertIsNone(response.next_cursor)
                self.assertNotIn("SYNTHETIC", response.model_dump_json())

    def test_resolve_uses_the_shared_client_without_message_search(self) -> None:
        client = FakeTDLibClient()
        service = SearchService(client=client)

        response = service.resolve(ResolveTargetRequest(target="@known_chat"))

        self.assertEqual(response.status, "resolved")
        self.assertEqual(response.resolved_target.chat_id, -1001)
        self.assertEqual(client.calls, ["getAuthorizationState", "resolve_target"])

    def test_resolve_maps_readiness_and_provider_failures_without_leaking_details(self) -> None:
        cases = (
            (AuthorizationBlocked("private auth"), None, "blocked"),
            (None, AuthorizationBlocked("private revoked"), "blocked"),
            (None, TDLibError("private provider"), "error"),
        )
        for ready_error, resolve_error, expected in cases:
            with self.subTest(expected=expected, ready_error=ready_error is not None):
                client = FakeTDLibClient()
                client.ready_error = ready_error
                client.resolve_error = resolve_error

                response = SearchService(client=client).resolve(
                    ResolveTargetRequest(target="@known_chat")
                )

                self.assertEqual(response.status, expected)
                self.assertFalse(response.coverage.complete)
                self.assertNotIn("private", response.model_dump_json())

    def test_resolve_maps_readiness_tdlib_failure_to_redacted_error(self) -> None:
        client = FakeTDLibClient()
        client.ready_error = TDLibError("private readiness detail")

        response = SearchService(client=client).resolve(
            ResolveTargetRequest(target="@known_chat")
        )

        self.assertEqual(response.status, "error")
        self.assertFalse(response.coverage.complete)
        self.assertEqual(client.calls, ["getAuthorizationState"])
        self.assertNotIn("private readiness detail", response.model_dump_json())

    def test_resolve_excludes_a_secret_username_and_preserves_exact_search_behavior(self) -> None:
        client = FakeTDLibClient()
        client.resolve_error = SecretChatRejected("secret")
        service = SearchService(client=client)

        resolution = service.resolve(ResolveTargetRequest(target="@known_chat"))

        self.assertEqual(resolution.status, "not_found")
        client.resolve_error = None
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 0,
            "messages": [],
            "next_from_message_id": 0,
        }
        search = service.search(
            SearchRequest.model_validate(
                {"target": "@known_chat", "query": {"text": "still exact"}}
            )
        )
        self.assertEqual(search.status, "no_match")
        self.assertTrue(search.coverage.complete)

    def test_text_hit_is_rehydrated_sanitized_and_evidence_anchored(self) -> None:
        hit = text_message(10, "Needle\u202e result")
        previous = text_message(9, "before", date=1_749_999_900)
        following = text_message(11, "after", date=1_750_000_100)
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = hit
        client.context[10] = [previous, hit, following]
        client.links[10] = "https://t.me/known_chat/10"
        client.chat_link = "https://t.me/provider_returned_chat"
        with tempfile.TemporaryDirectory() as parent:
            service = SearchService(client=client)

            response = service.search(
                SearchRequest.model_validate(
                    {
                        "target": "@known_chat",
                        "query": {"text": "Needle"},
                        "context_messages": 1,
                    }
                )
            )

        self.assertEqual(response.status, "matches")
        self.assertTrue(response.coverage.complete)
        self.assertEqual(response.matches[0].sender, "Alice Example")
        self.assertEqual(response.matches[0].snippet, "[untrusted Telegram evidence] Needle result")
        self.assertEqual([item.message_id for item in response.matches[0].context], [9, 11])
        self.assertEqual(response.matches[0].source.evidence_anchor.chat_id, -1001)
        self.assertEqual(response.matches[0].source.evidence_anchor.message_id, 10)
        self.assertEqual(response.matches[0].source.telegram_url, "https://t.me/known_chat/10")
        self.assertEqual(
            response.matches[0].source.chat_url,
            "https://t.me/provider_returned_chat",
        )
        self.assertEqual(client.calls.count("getChatLink"), 1)

    def test_numeric_private_target_returns_chat_url_when_message_link_is_absent(self) -> None:
        hit = text_message(10, "Needle result", chat_id=77)
        client = FakeTDLibClient()
        client.chat = {
            "@type": "chat",
            "id": 77,
            "title": "Example User",
            "type": {"@type": "chatTypePrivate", "user_id": 77},
        }
        client.chat_link = "https://t.me/example_user"
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = hit

        response = SearchService(client=client).search(
            SearchRequest.model_validate(
                {"target": 77, "query": {"text": "Needle"}}
            )
        )

        self.assertEqual(response.status, "matches")
        self.assertIsNone(response.matches[0].source.telegram_url)
        self.assertEqual(response.matches[0].source.chat_url, "https://t.me/example_user")
        self.assertEqual(client.calls.count("getChatLink"), 1)

    def test_numeric_private_target_rejects_non_telegram_chat_url(self) -> None:
        hit = text_message(10, "Needle result", chat_id=77)
        client = FakeTDLibClient()
        client.chat = {
            "@type": "chat",
            "id": 77,
            "title": "Example User",
            "type": {"@type": "chatTypePrivate", "user_id": 77},
        }
        client.chat_link = "https://evil.example/chat"
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = hit

        response = SearchService(client=client).search(
            SearchRequest.model_validate(
                {"target": 77, "query": {"text": "Needle"}}
            )
        )

        self.assertEqual(response.status, "matches")
        self.assertIsNone(response.matches[0].source.chat_url)

    def test_absent_text_is_no_match_only_when_direct_coverage_is_complete(self) -> None:
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 0,
            "messages": [],
            "next_from_message_id": 0,
        }
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "known absent"}}
                )
            )

        self.assertEqual(response.status, "no_match")
        self.assertTrue(response.coverage.complete)

    def test_numeric_target_rejects_unbound_or_invalid_resolved_chat_id_before_search(self) -> None:
        for requested_id, returned_id in (
            (-1001, -2002),
            (1, True),
            (-1001, 0),
        ):
            with self.subTest(requested_id=requested_id, returned_id=returned_id):
                client = FakeTDLibClient()
                client.chat = {
                    "@type": "chat",
                    "id": returned_id,
                    "title": "SYNTHETIC_PRIVATE_CHAT_SENTINEL",
                    "type": {"@type": "chatTypeSupergroup"},
                }
                client.search_pages[0] = {
                    "@type": "foundChatMessages",
                    "total_count": 0,
                    "messages": [],
                    "next_from_message_id": 0,
                }

                response = SearchService(client=client).search(
                    SearchRequest.model_validate(
                        {"target": requested_id, "query": {"text": "needle"}}
                    )
                )

                self.assertEqual(response.status, "error")
                self.assertFalse(response.coverage.complete)
                self.assertEqual(response.coverage.detail, "Telegram search failed safely")
                self.assertEqual(response.matches, [])
                self.assertEqual(client.calls, ["getAuthorizationState", "resolve_target"])
                self.assertNotIn("SYNTHETIC_PRIVATE_CHAT_SENTINEL", response.model_dump_json())

    def test_partial_search_never_returns_no_match(self) -> None:
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 50,
            "messages": [],
            "next_from_message_id": 90,
        }
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {
                        "target": -1001,
                        "query": {"text": "needle"},
                        "require_complete": False,
                    }
                )
            )

        self.assertEqual(response.status, "incomplete")
        self.assertFalse(response.coverage.complete)

    def test_file_search_returns_metadata_without_exposing_download_path(self) -> None:
        hit = document_message(20 * _SERVER_MESSAGE_ID_STEP, "Quarterly Report.pdf")
        client = FakeTDLibClient()
        client.history_pages[0] = [hit]
        client.history_pages[19 * _SERVER_MESSAGE_ID_STEP] = []
        client.messages[20 * _SERVER_MESSAGE_ID_STEP] = hit
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"file_name": "report"}}
                )
            )

        self.assertEqual(response.status, "matches")
        self.assertEqual(response.matches[0].file.name, "Quarterly Report.pdf")
        self.assertEqual(response.matches[0].file.mime_type, "application/pdf")
        self.assertEqual(response.matches[0].file.media_type, "document")
        self.assertEqual(response.matches[0].file.size, 1234)
        self.assertEqual(response.matches[0].message_id, hit["id"])
        self.assertEqual(response.matches[0].source.evidence_anchor.message_id, hit["id"])
        self.assertNotIn("/private/secret", response.model_dump_json())
        self.assertNotIn("downloadFile", client.calls)
        self.assertNotIn("viewMessages", client.calls)

    def test_file_metadata_search_is_transient_and_creates_no_custom_files(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            detector = Path(parent)
            repository = Path(__file__).resolve().parents[1]
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(repository / "src"), str(repository / "tests"))
            )
            completed = subprocess.run(
                [sys.executable, "-c", _TRANSIENT_METADATA_SUBPROCESS, str(detector)],
                cwd=repository,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(completed.stdout)

        self.assertEqual(result["status"], "matches")
        self.assertEqual(result["files"], [])

    def test_search_service_constructor_has_no_index_or_filesystem_argument(self) -> None:
        parameters = tuple(inspect.signature(SearchService).parameters)

        self.assertIn("client", parameters)
        self.assertIn("discovery_registry", parameters)
        self.assertFalse(any("index" in name or "path" in name for name in parameters))

    def test_transient_metadata_ids_intersect_provider_text_ids(self) -> None:
        wanted = document_message(
            20 * _SERVER_MESSAGE_ID_STEP, "Garden Notes.pdf", caption="seed"
        )
        other = document_message(
            18 * _SERVER_MESSAGE_ID_STEP, "Other.pdf", caption="seed"
        )
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 2,
            "messages": [wanted, other],
            "next_from_message_id": 0,
        }
        client.history_pages = {
            0: [wanted, other],
            17 * _SERVER_MESSAGE_ID_STEP: [],
        }
        client.messages = {wanted["id"]: wanted, other["id"]: other}
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {
                        "target": -1001,
                        "query": {"text": "seed", "file_name": "Garden"},
                    }
                )
            )
        self.assertEqual([item.message_id for item in response.matches], [wanted["id"]])

    def test_numeric_content_scan_returns_only_in_range_exact_chat_text_and_captions(self) -> None:
        lower = 1_750_000_000
        text_hit = text_message(
            30 * _SERVER_MESSAGE_ID_STEP, "PIN 1234", date=lower + 30
        )
        caption_hit = document_message(
            20 * _SERVER_MESSAGE_ID_STEP,
            "notes.pdf",
            caption="reference ٤٢",
            date=lower + 20,
        )
        plain = text_message(
            15 * _SERVER_MESSAGE_ID_STEP, "no numeric content", date=lower + 15
        )
        wrong_chat = text_message(
            10 * _SERVER_MESSAGE_ID_STEP,
            "code 9999",
            chat_id=-2002,
            date=lower + 10,
        )
        too_old = text_message(
            5 * _SERVER_MESSAGE_ID_STEP, "old 7777", date=lower - 1
        )
        client = FakeTDLibClient()
        client.history_pages = {
            0: [text_hit, caption_hit, plain, wrong_chat, too_old],
            4 * _SERVER_MESSAGE_ID_STEP: [],
        }
        client.messages = {
            text_hit["id"]: text_hit,
            caption_hit["id"]: caption_hit,
        }

        response = SearchService(client=client).search(
            SearchRequest.model_validate(
                {
                    "target": -1001,
                    "query": {"contains_number": True},
                    "date_from": datetime.fromtimestamp(lower, tz=timezone.utc),
                    "date_to": datetime.fromtimestamp(lower + 100, tz=timezone.utc),
                }
            )
        )

        self.assertEqual(response.status, "matches")
        self.assertTrue(response.coverage.complete)
        self.assertEqual(
            [item.message_id for item in response.matches],
            [text_hit["id"], caption_hit["id"]],
        )
        self.assertEqual(
            [item.source.evidence_anchor.chat_id for item in response.matches],
            [-1001, -1001],
        )
        self.assertIn("1234", response.matches[0].snippet)
        self.assertIn("٤٢", response.matches[1].snippet)
        self.assertNotIn("viewMessages", client.calls)
        self.assertNotIn("downloadFile", client.calls)

    def test_numeric_content_scan_advances_across_history_pages(self) -> None:
        newest = text_message(30 * _SERVER_MESSAGE_ID_STEP, "first 100")
        oldest = text_message(20 * _SERVER_MESSAGE_ID_STEP, "second 200")
        client = FakeTDLibClient()
        client.history_pages = {
            0: [newest],
            29 * _SERVER_MESSAGE_ID_STEP: [oldest],
            19 * _SERVER_MESSAGE_ID_STEP: [],
        }
        client.messages = {newest["id"]: newest, oldest["id"]: oldest}

        response = SearchService(client=client).search(
            SearchRequest.model_validate(
                {"target": -1001, "query": {"contains_number": True}}
            )
        )

        self.assertEqual(response.status, "matches")
        self.assertTrue(response.coverage.complete)
        self.assertEqual(
            [item.message_id for item in response.matches],
            [newest["id"], oldest["id"]],
        )
        self.assertEqual(client.calls.count("getChatHistory"), 3)

    def test_numeric_content_intersects_text_and_file_predicates(self) -> None:
        wanted_text = text_message(30 * _SERVER_MESSAGE_ID_STEP, "code 1234")
        text_without_number = text_message(20 * _SERVER_MESSAGE_ID_STEP, "code pending")
        wanted_file = document_message(
            18 * _SERVER_MESSAGE_ID_STEP,
            "Report.pdf",
            caption="batch 42",
        )
        file_without_number = document_message(
            16 * _SERVER_MESSAGE_ID_STEP,
            "Report.pdf",
            caption="batch pending",
        )

        text_client = FakeTDLibClient()
        text_client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 2,
            "messages": [wanted_text, text_without_number],
            "next_from_message_id": 0,
        }
        text_client.history_pages = {
            0: [wanted_text, text_without_number],
            19 * _SERVER_MESSAGE_ID_STEP: [],
        }
        text_client.messages = {
            wanted_text["id"]: wanted_text,
            text_without_number["id"]: text_without_number,
        }

        text_response = SearchService(client=text_client).search(
            SearchRequest.model_validate(
                {
                    "target": -1001,
                    "query": {"text": "code", "contains_number": True},
                }
            )
        )

        file_client = FakeTDLibClient()
        file_client.history_pages = {
            0: [wanted_file, file_without_number],
            15 * _SERVER_MESSAGE_ID_STEP: [],
        }
        file_client.messages = {
            wanted_file["id"]: wanted_file,
            file_without_number["id"]: file_without_number,
        }
        file_response = SearchService(client=file_client).search(
            SearchRequest.model_validate(
                {
                    "target": -1001,
                    "query": {"file_name": "Report", "contains_number": True},
                }
            )
        )

        self.assertEqual(
            [item.message_id for item in text_response.matches], [wanted_text["id"]]
        )
        self.assertEqual(
            [item.message_id for item in file_response.matches], [wanted_file["id"]]
        )

    def test_partial_numeric_scan_never_returns_complete_absence(self) -> None:
        hit = text_message(20 * _SERVER_MESSAGE_ID_STEP, "number 321")
        client = FakeTDLibClient()
        client.history_pages[0] = [hit]
        client.messages[hit["id"]] = hit

        response = SearchService(client=client).search(
            SearchRequest.model_validate(
                {
                    "target": -1001,
                    "query": {"contains_number": True},
                    "require_complete": False,
                }
            )
        )

        self.assertEqual(response.status, "incomplete")
        self.assertFalse(response.coverage.complete)
        self.assertEqual([item.message_id for item in response.matches], [hit["id"]])

    def test_numeric_candidate_edited_to_remove_digits_is_fail_closed(self) -> None:
        provider_hit = text_message(20 * _SERVER_MESSAGE_ID_STEP, "temporary 8080")
        edited = text_message(20 * _SERVER_MESSAGE_ID_STEP, "digits removed")
        client = FakeTDLibClient()
        client.history_pages = {
            0: [provider_hit],
            19 * _SERVER_MESSAGE_ID_STEP: [],
        }
        client.messages[provider_hit["id"]] = edited

        response = SearchService(client=client).search(
            SearchRequest.model_validate(
                {"target": -1001, "query": {"contains_number": True}}
            )
        )

        self.assertEqual(response.status, "incomplete")
        self.assertFalse(response.coverage.complete)
        self.assertEqual(response.matches, [])

    def test_partial_transient_metadata_scan_never_returns_no_match(self) -> None:
        client = FakeTDLibClient()
        client.history_pages[0] = [
            document_message(20 * _SERVER_MESSAGE_ID_STEP, "Other.pdf")
        ]
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {
                        "target": -1001,
                        "query": {"file_name": "missing"},
                        "require_complete": False,
                    }
                )
            )
        self.assertEqual(response.status, "incomplete")
        self.assertFalse(response.coverage.complete)

    def test_complete_metadata_scan_reconciles_a_deleted_file(self) -> None:
        hit = document_message(20 * _SERVER_MESSAGE_ID_STEP, "removed.pdf")
        client = FakeTDLibClient()
        client.history_pages[0] = [hit]
        client.history_pages[19 * _SERVER_MESSAGE_ID_STEP] = []
        client.messages[20 * _SERVER_MESSAGE_ID_STEP] = hit
        with tempfile.TemporaryDirectory() as parent:
            service = SearchService(client=client)
            request = SearchRequest.model_validate(
                {"target": -1001, "query": {"file_name": "removed"}}
            )
            first = service.search(request)
            client.history_pages[0] = []

            second = service.search(request)

        self.assertEqual(first.status, "matches")
        self.assertEqual(second.status, "no_match")

    def test_metadata_history_advances_strictly_older_across_inclusive_short_pages(self) -> None:
        newest = document_message(
            20 * _SERVER_MESSAGE_ID_STEP, "newest.pdf", date=1_750_000_020
        )
        middle = document_message(
            18 * _SERVER_MESSAGE_ID_STEP, "middle.pdf", date=1_750_000_018
        )
        oldest = document_message(
            15 * _SERVER_MESSAGE_ID_STEP, "oldest.pdf", date=1_750_000_015
        )
        client = FakeTDLibClient()
        client.history_pages = {
            0: [newest],
            19 * _SERVER_MESSAGE_ID_STEP: [middle],
            17 * _SERVER_MESSAGE_ID_STEP: [oldest],
            14 * _SERVER_MESSAGE_ID_STEP: [],
        }
        client.messages[15 * _SERVER_MESSAGE_ID_STEP] = oldest
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"file_name": "oldest"}}
                )
            )

        self.assertEqual(response.status, "matches")
        self.assertTrue(response.coverage.complete)
        history_calls = [call for call in client.calls if call == "getChatHistory"]
        self.assertEqual(len(history_calls), 4)

    def test_complete_metadata_scan_reconciles_deletion_after_inclusive_boundary(self) -> None:
        retained = document_message(
            20 * _SERVER_MESSAGE_ID_STEP, "retained.pdf", date=1_750_000_020
        )
        removed = document_message(
            18 * _SERVER_MESSAGE_ID_STEP, "removed.pdf", date=1_750_000_018
        )
        client = FakeTDLibClient()
        client.history_pages = {
            0: [retained],
            19 * _SERVER_MESSAGE_ID_STEP: [removed],
            17 * _SERVER_MESSAGE_ID_STEP: [],
        }
        client.messages[18 * _SERVER_MESSAGE_ID_STEP] = removed
        with tempfile.TemporaryDirectory() as parent:
            service = SearchService(client=client)
            request = SearchRequest.model_validate(
                {"target": -1001, "query": {"file_name": "removed"}}
            )
            first = service.search(request)
            client.history_pages = {0: [retained], 19 * _SERVER_MESSAGE_ID_STEP: []}

            second = service.search(request)

        self.assertEqual(first.status, "matches")
        self.assertEqual(second.status, "no_match")
        self.assertTrue(second.coverage.complete)

    def test_stale_direct_hit_is_reconciled_before_return(self) -> None:
        stale = text_message(10, "needle")
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [stale],
            "next_from_message_id": 0,
        }
        client.messages[10] = MessageNotFound("gone")
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "needle"}}
                )
            )

        self.assertEqual(response.status, "no_match")
        self.assertEqual(response.matches, [])

    def test_authorization_failure_returns_blocked_without_resolving_a_chat(self) -> None:
        client = FakeTDLibClient()
        client.ready_error = AuthorizationBlocked("not ready")
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "needle"}}
                )
            )

        self.assertEqual(response.status, "blocked")
        self.assertEqual(client.calls, ["getAuthorizationState"])

    def test_authorization_loss_during_search_returns_blocked(self) -> None:
        client = FakeTDLibClient()
        client.resolve_error = AuthorizationBlocked("revoked sentinel")
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "needle"}}
                )
            )

        self.assertEqual(response.status, "blocked")
        self.assertEqual(client.calls, ["getAuthorizationState", "resolve_target"])

    def test_authorization_loss_during_hit_rehydration_returns_blocked(self) -> None:
        hit = text_message(10, "needle")
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = AuthorizationBlocked("revoked sentinel")
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "needle"}}
                )
            )

        self.assertEqual(response.status, "blocked")

    def test_long_rehydrated_hit_is_verified_before_display_truncation(self) -> None:
        query = "Quarterly   Plan"
        hit = text_message(10, "x" * 2_101 + " QUARTERLY PLAN approved")
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = hit
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": query}}
                )
            )

        self.assertEqual(response.status, "matches")
        self.assertTrue(response.coverage.complete)
        self.assertIn("QUARTERLY PLAN", response.matches[0].snippet)
        self.assertLessEqual(len(response.matches[0].snippet), 2000)

    def test_provider_hit_verification_uses_nfkc_casefold_and_collapsed_whitespace(self) -> None:
        hit = text_message(10, "Result: ＳＴＲＡＳＳＥ\n\tplan")
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = hit
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "straße   PLAN"}}
                )
            )

        self.assertEqual(response.status, "matches")
        self.assertIn("STRASSE plan", response.matches[0].snippet)

    def test_provider_word_hits_allow_separated_and_reordered_query_terms(self) -> None:
        cases = (
            ("x" * 2_101 + " quarterly revised plan approved", "quarterly plan"),
            ("The plan was approved in the quarterly review", "quarterly plan"),
        )
        for text, query in cases:
            with self.subTest(text=text[-60:], query=query):
                hit = text_message(10, text)
                client = FakeTDLibClient()
                client.search_pages[0] = {
                    "@type": "foundChatMessages",
                    "total_count": 1,
                    "messages": [hit],
                    "next_from_message_id": 0,
                }
                client.messages[10] = hit
                with tempfile.TemporaryDirectory() as parent:
                    response = SearchService(client=client).search(
                        SearchRequest.model_validate(
                            {"target": -1001, "query": {"text": query}}
                        )
                    )

                self.assertEqual(response.status, "matches")
                self.assertIn("quarterly", response.matches[0].snippet.casefold())
                self.assertIn("plan", response.matches[0].snippet.casefold())
                self.assertLessEqual(len(response.matches[0].snippet), 2000)

    def test_unchanged_provider_hit_survives_stricter_local_tokenization(self) -> None:
        hit = text_message(10, "Деньги")
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = hit
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "деньгах"}}
                )
            )

        self.assertEqual(response.status, "matches")
        self.assertTrue(response.coverage.complete)
        self.assertEqual([match.message_id for match in response.matches], [10])

    def test_edited_away_provider_hit_is_not_returned(self) -> None:
        provider_hit = text_message(10, "quarterly plan")
        edited_message = text_message(10, "message was edited after provider search")
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [provider_hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = edited_message
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "quarterly plan"}}
                )
            )

        self.assertEqual(response.status, "incomplete")
        self.assertFalse(response.coverage.complete)
        self.assertEqual(response.matches, [])

    def test_empty_provider_hit_remains_fail_closed(self) -> None:
        empty_hit = text_message(10, "")
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [empty_hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = empty_hit
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "needle"}}
                )
            )

        self.assertEqual(response.status, "incomplete")
        self.assertFalse(response.coverage.complete)
        self.assertEqual(response.matches, [])

    def test_cross_chat_rehydrated_provider_hit_remains_fail_closed(self) -> None:
        provider_hit = text_message(10, "needle")
        wrong_chat_message = text_message(10, "needle", chat_id=-2002)
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [provider_hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = wrong_chat_message
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"text": "needle"}}
                )
            )

        self.assertEqual(response.status, "incomplete")
        self.assertFalse(response.coverage.complete)
        self.assertEqual(response.matches, [])

    def test_cross_message_rehydrated_text_hit_remains_fail_closed(self) -> None:
        provider_hit = text_message(10, "needle")
        wrong_message = text_message(11, "needle")
        client = FakeTDLibClient()
        client.search_pages[0] = {
            "@type": "foundChatMessages",
            "total_count": 1,
            "messages": [provider_hit],
            "next_from_message_id": 0,
        }
        client.messages[10] = wrong_message

        response = SearchService(client=client).search(
            SearchRequest.model_validate(
                {
                    "target": -1001,
                    "query": {"text": "needle"},
                    "context_messages": 1,
                }
            )
        )

        self.assertEqual(response.status, "incomplete")
        self.assertFalse(response.coverage.complete)
        self.assertEqual(response.matches, [])
        self.assertNotIn("getUser", client.calls)
        self.assertNotIn("getMessageLink", client.calls)

    def test_cross_message_rehydrated_metadata_hit_remains_fail_closed(self) -> None:
        provider_hit = document_message(
            20 * _SERVER_MESSAGE_ID_STEP,
            "Quarterly Report.pdf",
        )
        wrong_message = document_message(
            21 * _SERVER_MESSAGE_ID_STEP,
            "Substituted Report.pdf",
        )
        client = FakeTDLibClient()
        client.history_pages = {
            0: [provider_hit],
            19 * _SERVER_MESSAGE_ID_STEP: [],
        }
        client.messages[provider_hit["id"]] = wrong_message

        response = SearchService(client=client).search(
            SearchRequest.model_validate(
                {"target": -1001, "query": {"file_name": "Report"}}
            )
        )

        self.assertEqual(response.status, "incomplete")
        self.assertFalse(response.coverage.complete)
        self.assertEqual(response.matches, [])
        self.assertNotIn("getUser", client.calls)
        self.assertNotIn("getMessageLink", client.calls)

    def test_filename_rehydration_uses_the_index_normalization_rule(self) -> None:
        hit = document_message(20 * _SERVER_MESSAGE_ID_STEP, "Report.pdf")
        client = FakeTDLibClient()
        client.history_pages[0] = [hit]
        client.history_pages[19 * _SERVER_MESSAGE_ID_STEP] = []
        client.messages[20 * _SERVER_MESSAGE_ID_STEP] = hit
        with tempfile.TemporaryDirectory() as parent:
            response = SearchService(client=client).search(
                SearchRequest.model_validate(
                    {"target": -1001, "query": {"file_name": "Ｒｅｐｏｒｔ"}}
                )
            )

        self.assertEqual(response.status, "matches")
        self.assertEqual(response.matches[0].file.name, "Report.pdf")


if __name__ == "__main__":
    unittest.main()
