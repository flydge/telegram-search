from __future__ import annotations

import unittest
from typing import Any

from telegram_search_mcp.schemas import ResolveTargetRequest, TargetResolutionResponse
from telegram_search_mcp.target_resolver import TargetResolver
from telegram_search_mcp.tdjson import (
    AuthorizationBlocked,
    MessageNotFound,
    SecretChatRejected,
    TDLibError,
)


def chat(
    chat_id: int,
    title: str,
    chat_type: str = "chatTypeSupergroup",
    *,
    is_channel: bool = False,
) -> dict[str, Any]:
    type_data: dict[str, Any] = {"@type": chat_type}
    if chat_type == "chatTypeSupergroup":
        type_data["is_channel"] = is_channel
    if chat_type == "chatTypePrivate":
        type_data["user_id"] = abs(chat_id)
    return {"@type": "chat", "id": chat_id, "title": title, "type": type_data}


class FakeResolverClient:
    def __init__(self) -> None:
        self.self_chat: dict[str, Any] | Exception = chat(
            700, "Saved Messages", "chatTypePrivate"
        )
        self.username_chat: dict[str, Any] | Exception = chat(
            -1001, "Known Chat", "chatTypeSupergroup"
        )
        self.calls: list[tuple[str, object | None]] = []

    def get_self_chat(self) -> dict[str, Any]:
        self.calls.append(("get_self_chat", None))
        if isinstance(self.self_chat, Exception):
            raise self.self_chat
        return self.self_chat

    def resolve_target(self, target: str | int) -> dict[str, Any]:
        self.calls.append(("resolve_target", target))
        if isinstance(self.username_chat, Exception):
            raise self.username_chat
        return self.username_chat


def resolve(client: FakeResolverClient, target: str) -> TargetResolutionResponse:
    return TargetResolver(client).resolve(ResolveTargetRequest(target=target))


class TargetResolverTests(unittest.TestCase):
    def test_normalization_empty_targets_never_reach_provider(self) -> None:
        for target in ("\u0000\u0001", "\u202e\u2066", " \t-—…! \n"):
            with self.subTest(target=repr(target)):
                client = FakeResolverClient()
                request = ResolveTargetRequest.model_construct(target=target)

                with self.assertRaises(ValueError):
                    TargetResolver(client).resolve(request)

                self.assertEqual(client.calls, [])

    def test_free_form_resolution_requires_discovery_without_provider_calls(self) -> None:
        client = FakeResolverClient()

        response = resolve(client, "synthetic compact target")

        self.assertEqual(response.status, "discovery_required")
        self.assertIsNone(response.resolved_target)
        self.assertEqual(response.candidates, [])
        self.assertEqual(client.calls, [])
        self.assertFalse(response.coverage.complete)

    def test_invalid_username_syntax_also_requires_discovery_without_provider_calls(self) -> None:
        client = FakeResolverClient()

        response = resolve(client, "@tiny")

        self.assertEqual(response.status, "discovery_required")
        self.assertEqual(client.calls, [])

    def test_saved_messages_aliases_use_only_the_verified_self_chat(self) -> None:
        for alias in (
            "Saved Messages",
            "Избранное",
            "Сохранённые сообщения",
            "Сохраненные сообщения",
            "saved-messages!",
        ):
            with self.subTest(alias=alias):
                client = FakeResolverClient()

                response = resolve(client, alias)

                self.assertEqual(response.status, "resolved")
                self.assertEqual(response.match_kind, "saved_messages_alias")
                self.assertEqual(response.resolved_target.chat_type, "self")
                self.assertEqual(client.calls, [("get_self_chat", None)])

    def test_exact_username_uses_only_direct_resolution_and_maps_chat_types(self) -> None:
        cases = (
            ("chatTypePrivate", False, "private"),
            ("chatTypeBasicGroup", False, "basic_group"),
            ("chatTypeSupergroup", False, "supergroup"),
            ("chatTypeSupergroup", True, "channel"),
        )
        for provider_type, is_channel, expected in cases:
            with self.subTest(provider_type=provider_type, is_channel=is_channel):
                client = FakeResolverClient()
                client.username_chat = chat(
                    -1001, "Known Chat", provider_type, is_channel=is_channel
                )

                response = resolve(client, "@known_chat")

                self.assertEqual(response.status, "resolved")
                self.assertEqual(response.match_kind, "exact_username")
                self.assertEqual(response.resolved_target.chat_type, expected)
                self.assertEqual(client.calls, [("resolve_target", "@known_chat")])

    def test_username_not_found_and_secret_are_complete_safe_absences(self) -> None:
        for error in (MessageNotFound("private detail"), SecretChatRejected("private detail")):
            with self.subTest(error=type(error).__name__):
                client = FakeResolverClient()
                client.username_chat = error

                response = resolve(client, "@known_chat")

                self.assertEqual(response.status, "not_found")
                self.assertTrue(response.coverage.complete)
                self.assertNotIn("private detail", response.model_dump_json())

        client = FakeResolverClient()
        client.username_chat = chat(99, "Secret", "chatTypeSecret")
        self.assertEqual(resolve(client, "@known_chat").status, "not_found")

    def test_direct_titles_are_sanitized_and_invalid_titles_fail_redacted(self) -> None:
        client = FakeResolverClient()
        client.username_chat = chat(-1001, "Safe\u202e Title\n")

        response = resolve(client, "@known_chat")

        self.assertEqual(response.resolved_target.title, "Safe Title")
        self.assertNotIn("\u202e", response.model_dump_json())

        client = FakeResolverClient()
        client.username_chat = chat(99, "\u202e\u0000", "chatTypePrivate")
        response = resolve(client, "@known_chat")
        self.assertEqual(response.status, "error")
        self.assertEqual(response.candidates, [])

    def test_direct_failures_return_strict_blocked_and_error_responses(self) -> None:
        for error, status, lane_status in (
            (AuthorizationBlocked("private auth"), "blocked", "blocked"),
            (TDLibError("private provider"), "error", "error"),
        ):
            with self.subTest(status=status):
                client = FakeResolverClient()
                client.username_chat = error

                response = resolve(client, "@known_chat")

                self.assertEqual(response.status, status)
                self.assertFalse(response.coverage.complete)
                self.assertEqual(response.coverage.exact_username.status, lane_status)
                self.assertEqual(response.candidates, [])
                self.assertNotIn(str(error), response.model_dump_json())


if __name__ == "__main__":
    unittest.main()
