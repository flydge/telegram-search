from __future__ import annotations

import unittest
from collections import defaultdict
from typing import Any, Callable

from telegram_search_mcp.discovery_state import CatalogPosition, DiscoveryRegistry
from telegram_search_mcp.schemas import DiscoverTargetsRequest
from telegram_search_mcp.target_discovery import TargetDiscoveryService
from telegram_search_mcp.tdjson import (
    AuthorizationBlocked,
    GlobalMessagePage,
    MessageNotFound,
    TDLibError,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def message(
    chat_id: int,
    message_id: int,
    text: str,
    *,
    date: int = 1_750_000_000,
    content_type: str = "messageText",
) -> dict[str, Any]:
    content_key = "text" if content_type == "messageText" else "caption"
    return {
        "@type": "message",
        "id": message_id,
        "chat_id": chat_id,
        "date": date,
        "sender_id": {"@type": "messageSenderUser", "user_id": 7},
        "content": {
            "@type": content_type,
            content_key: {"@type": "formattedText", "text": text, "entities": []},
        },
    }


def chat(
    chat_id: int,
    title: str,
    *,
    chat_type: str = "chatTypeSupergroup",
    is_channel: bool = False,
) -> dict[str, Any]:
    type_data: dict[str, Any] = {"@type": chat_type}
    if chat_type == "chatTypeSupergroup":
        type_data["is_channel"] = is_channel
    if chat_type == "chatTypePrivate":
        type_data["user_id"] = abs(chat_id)
    return {"@type": "chat", "id": chat_id, "title": title, "type": type_data}


def page(
    messages: list[dict[str, Any]],
    *,
    next_offset: str,
    integrity_partial: bool = False,
) -> GlobalMessagePage:
    return GlobalMessagePage(
        messages=messages,
        next_offset=next_offset,
        integrity_partial=integrity_partial,
    )


class FakeDiscoveryClient:
    def __init__(self) -> None:
        self.prefixes: dict[str, list[int]] = {"main": [], "archive": []}
        self.positions: dict[str, list[CatalogPosition]] = {"main": [], "archive": []}
        self.catalog_scripts: dict[str, list[bool | Exception]] = defaultdict(list)
        self.global_scripts: dict[
            tuple[str, str, str], list[GlobalMessagePage | Exception]
        ] = defaultdict(list)
        self.chats: dict[int, dict[str, Any] | Exception] = {}
        self.messages: dict[tuple[int, int], dict[str, Any] | Exception] = {}
        self.on_resolve: Callable[[int], None] | None = None
        self.calls: list[tuple[object, ...]] = []

    def get_chat_list_prefix(self, chat_list: str) -> list[int]:
        self.calls.append(("get_chat_list_prefix", chat_list))
        return list(self.prefixes[chat_list])

    def load_more_chats(
        self,
        chat_list: str,
        on_positions: Callable[[list[CatalogPosition]], None],
    ) -> bool:
        self.calls.append(("load_more_chats", chat_list))
        on_positions(list(self.positions[chat_list]))
        if self.catalog_scripts[chat_list]:
            result = self.catalog_scripts[chat_list].pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return True

    def search_global_messages(
        self, chat_list: str, query: str, *, offset: str
    ) -> GlobalMessagePage:
        self.calls.append(("search_global_messages", chat_list, query, offset))
        scripted = self.global_scripts[(query, chat_list, offset)]
        if not scripted:
            return page([], next_offset="")
        result = scripted.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def resolve_target(self, target: str | int) -> dict[str, Any]:
        self.calls.append(("get_chat", target))
        assert isinstance(target, int)
        if self.on_resolve is not None:
            self.on_resolve(target)
        value = self.chats[target]
        if isinstance(value, Exception):
            raise value
        return value

    def get_message(self, chat_id: int, message_id: int) -> dict[str, Any]:
        self.calls.append(("get_message", chat_id, message_id))
        value = self.messages[(chat_id, message_id)]
        if isinstance(value, Exception):
            raise value
        return value


def registry(clock: FakeClock | None = None) -> DiscoveryRegistry:
    return DiscoveryRegistry(clock=clock or FakeClock(), ttl_seconds=300, capacity=4)


def discover(
    service: TargetDiscoveryService,
    hypotheses: list[str],
    *,
    scope: str = "main",
    cursor: str | None = None,
):
    return service.discover(
        DiscoverTargetsRequest.model_validate(
            {"hypotheses": hypotheses, "scope": scope, "cursor": cursor}
        )
    )


def run_until_complete(
    service: TargetDiscoveryService,
    hypotheses: list[str],
    *,
    scope: str = "main",
):
    cursor = None
    accumulated: dict[int, dict[str, set[tuple[object, ...]]]] = defaultdict(
        lambda: {"metadata": set(), "messages": set(), "lists": set()}
    )
    for _ in range(20):
        response = discover(service, hypotheses, scope=scope, cursor=cursor)
        for candidate in response.candidates:
            accumulated[candidate.chat_id]["lists"].update(
                (item,) for item in candidate.list_membership
            )
            accumulated[candidate.chat_id]["metadata"].update(
                (item.hypothesis_index, item.kind)
                for item in candidate.metadata_evidence
            )
            accumulated[candidate.chat_id]["messages"].update(
                (item.hypothesis_index, item.chat_list, item.message_id)
                for item in candidate.message_evidence
            )
        if response.status == "complete":
            return response, accumulated
        if response.status != "page":
            raise AssertionError(f"discovery did not complete: {response.status}")
        cursor = response.next_cursor
    raise AssertionError("discovery exceeded the 20-call completion guard")


def lane_coverage(response: object, *, hypothesis_index: int, chat_list: str):
    return next(
        lane
        for lane in response.coverage.global_messages.lanes
        if lane.hypothesis_index == hypothesis_index and lane.chat_list == chat_list
    )


class TargetDiscoveryTests(unittest.TestCase):
    def test_reentrant_caller_cannot_publish_complete_while_work_is_leased(self) -> None:
        client = FakeDiscoveryClient()
        client.catalog_scripts["main"].extend([False, False, True])
        service = TargetDiscoveryService(client=client, registry=registry())
        first = discover(service, ["garden", "seeds"])
        second = discover(service, ["garden", "seeds"], cursor=first.next_cursor)
        client.positions["main"] = [CatalogPosition(-1001, 30)]
        client.chats[-1001] = []  # type: ignore[assignment]
        nested = []

        def probe_during_hydration(_: int) -> None:
            client.on_resolve = None
            nested.append(
                discover(
                    service,
                    ["garden", "seeds"],
                    cursor=second.next_cursor,
                )
            )

        client.on_resolve = probe_during_hydration

        outer = discover(
            service,
            ["garden", "seeds"],
            cursor=second.next_cursor,
        )

        self.assertEqual(len(nested), 1)
        self.assertNotEqual(nested[0].status, "complete")
        self.assertFalse(nested[0].coverage.complete)
        self.assertEqual(nested[0].candidates, [])
        self.assertEqual(outer.status, "partial")

    def test_candidate_can_be_found_only_from_messages_without_title_overlap(self) -> None:
        client = FakeDiscoveryClient()
        first = message(-1001, 11, "seed exchange schedule")
        second = message(-1001, 12, "community garden meeting")
        client.global_scripts[("seed exchange", "main", "")].append(
            page([first], next_offset="")
        )
        client.global_scripts[("community garden", "main", "")].append(
            page([second], next_offset="")
        )
        client.chats[-1001] = chat(-1001, "Weekend Circle")
        client.messages[(-1001, 11)] = first
        client.messages[(-1001, 12)] = second

        _, accumulated = run_until_complete(
            TargetDiscoveryService(client=client, registry=registry()),
            ["seed exchange", "community garden"],
        )

        candidate = accumulated[-1001]
        self.assertEqual(candidate["metadata"], set())
        self.assertEqual({item[0] for item in candidate["messages"]}, {0, 1})

    def test_global_evidence_is_rehydrated_sanitized_and_same_chat_anchored(self) -> None:
        client = FakeDiscoveryClient()
        provider = message(-1001, 11, "garden\u202e\nmeeting")
        client.global_scripts[("garden", "archive", "")].append(
            page([provider], next_offset="")
        )
        client.chats[-1001] = chat(-1001, "Weekend Circle")
        client.messages[(-1001, 11)] = provider

        response = discover(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden", "seeds"],
            scope="archive",
        )

        candidate = response.candidates[0]
        evidence = candidate.message_evidence[0]
        self.assertEqual(evidence.chat_list, "archive")
        self.assertEqual(candidate.chat_id, evidence.evidence_anchor.chat_id)
        self.assertEqual(evidence.message_id, evidence.evidence_anchor.message_id)
        self.assertTrue(evidence.snippet.startswith("[untrusted Telegram evidence]"))
        self.assertNotIn("\u202e", evidence.snippet)

    def test_short_page_with_next_offset_remains_incomplete(self) -> None:
        client = FakeDiscoveryClient()
        client.global_scripts[("garden", "main", "")].append(
            page([], next_offset="still-more")
        )
        response = discover(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden", "seeds"],
        )

        lane = lane_coverage(response, hypothesis_index=0, chat_list="main")
        self.assertEqual(lane.status, "scanning")
        self.assertFalse(response.coverage.complete)
        self.assertEqual(response.status, "page")

    def test_cross_chat_or_edited_rehydration_is_permanently_partial(self) -> None:
        cases = (
            message(-2002, 11, "garden"),
            message(-1001, 11, "edited away"),
            MessageNotFound("SYNTHETIC_PRIVATE_MESSAGE"),
        )
        for rehydrated in cases:
            with self.subTest(rehydrated=type(rehydrated).__name__):
                client = FakeDiscoveryClient()
                provider = message(-1001, 11, "garden")
                client.global_scripts[("garden", "main", "")].append(
                    page([provider], next_offset="trusted-next")
                )
                client.chats[-1001] = chat(-1001, "Weekend Circle")
                client.messages[(-1001, 11)] = rehydrated

                response = discover(
                    TargetDiscoveryService(client=client, registry=registry()),
                    ["garden", "seeds"],
                )

                self.assertEqual(response.status, "partial")
                self.assertEqual(response.candidates, [])
                lane = lane_coverage(response, hypothesis_index=0, chat_list="main")
                self.assertEqual(lane.status, "partial")
                self.assertNotIn("SYNTHETIC_PRIVATE_MESSAGE", response.model_dump_json())

    def test_unknown_rehydrated_content_is_rejected_even_with_a_caption(self) -> None:
        client = FakeDiscoveryClient()
        provider = message(-1001, 11, "garden")
        rehydrated = message(
            -1001,
            11,
            "garden",
            content_type="messageFutureEvidence",
        )
        client.global_scripts[("garden", "main", "")].append(
            page([provider], next_offset="")
        )
        client.chats[-1001] = chat(-1001, "Weekend Circle")
        client.messages[(-1001, 11)] = rehydrated

        response = discover(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden", "seeds"],
        )

        self.assertEqual(response.status, "partial")
        self.assertEqual(response.candidates, [])
        self.assertEqual(
            lane_coverage(response, hypothesis_index=0, chat_list="main").status,
            "partial",
        )

    def test_malformed_provider_message_and_integrity_flag_consume_trusted_offset_first(self) -> None:
        for provider_page in (
            page([message(-1001, 11, "garden", date=-1)], next_offset="trusted-next"),
            page([], next_offset="trusted-next", integrity_partial=True),
        ):
            with self.subTest(integrity_partial=provider_page.integrity_partial):
                client = FakeDiscoveryClient()
                client.global_scripts[("garden", "main", "")].append(provider_page)
                service = TargetDiscoveryService(client=client, registry=registry())

                first = discover(service, ["garden", "seeds"])
                second = discover(service, ["garden", "seeds"], cursor=first.next_cursor)

                self.assertEqual(first.status, "partial")
                self.assertEqual(
                    lane_coverage(first, hypothesis_index=0, chat_list="main").pages_scanned,
                    1,
                )
                self.assertEqual(second.status, "partial")

    def test_secret_chat_evidence_is_dropped_and_marks_partial(self) -> None:
        client = FakeDiscoveryClient()
        provider = message(-1001, 11, "garden")
        client.global_scripts[("garden", "main", "")].append(
            page([provider], next_offset="")
        )
        client.chats[-1001] = chat(-1001, "Secret", chat_type="chatTypeSecret")
        client.messages[(-1001, 11)] = provider

        response = discover(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden", "seeds"],
        )

        self.assertEqual(response.status, "partial")
        self.assertEqual(response.candidates, [])

    def test_malformed_exact_chat_hydration_is_dropped_and_marks_partial(self) -> None:
        client = FakeDiscoveryClient()
        client.positions["main"] = [CatalogPosition(-1001, 30)]
        client.chats[-1001] = []  # type: ignore[assignment]

        response = discover(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden", "seeds"],
        )

        self.assertEqual(response.status, "partial")
        self.assertEqual(response.candidates, [])
        self.assertEqual(response.coverage.catalog.main.status, "error")

    def test_wrong_exact_chat_id_hydration_is_dropped_and_marks_partial(self) -> None:
        for returned_id in (True, 0, -2002):
            with self.subTest(returned_id=returned_id):
                client = FakeDiscoveryClient()
                client.positions["main"] = [CatalogPosition(-1001, 30)]
                client.chats[-1001] = chat(
                    returned_id,
                    "Garden Circle",
                    chat_type="chatTypePrivate",
                )

                response = discover(
                    TargetDiscoveryService(client=client, registry=registry()),
                    ["garden", "seeds"],
                )

                self.assertEqual(response.status, "partial")
                self.assertEqual(response.candidates, [])
                self.assertEqual(response.coverage.hydration, "partial")
                self.assertEqual(response.coverage.catalog.main.status, "error")

    def test_repeated_offset_becomes_permanent_partial_without_looping(self) -> None:
        client = FakeDiscoveryClient()
        client.global_scripts[("garden", "main", "")].append(
            page([], next_offset="repeat")
        )
        client.global_scripts[("garden", "main", "repeat")].append(
            page([], next_offset="repeat")
        )
        service = TargetDiscoveryService(client=client, registry=registry())

        first = discover(service, ["garden", "seeds"])
        second = discover(service, ["garden", "seeds"], cursor=first.next_cursor)
        third = discover(service, ["garden", "seeds"], cursor=second.next_cursor)

        self.assertEqual(third.status, "partial")
        self.assertEqual(
            lane_coverage(third, hypothesis_index=0, chat_list="main").status,
            "partial",
        )

    def test_retryable_provider_error_preserves_cursor_and_same_offset_for_retry(self) -> None:
        client = FakeDiscoveryClient()
        client.global_scripts[("garden", "main", "")].extend(
            [TDLibError("SYNTHETIC_PROVIDER_SENTINEL"), page([], next_offset="")]
        )
        service = TargetDiscoveryService(client=client, registry=registry())

        first = discover(service, ["garden", "seeds"])
        second = discover(service, ["garden", "seeds"], cursor=first.next_cursor)
        third = discover(service, ["garden", "seeds"], cursor=second.next_cursor)

        self.assertEqual(first.status, "partial")
        self.assertIsNotNone(first.next_cursor)
        self.assertNotIn("SYNTHETIC_PROVIDER_SENTINEL", first.model_dump_json())
        garden_calls = [
            call
            for call in client.calls
            if call[0] == "search_global_messages" and call[2] == "garden"
        ]
        self.assertEqual([call[3] for call in garden_calls], ["", ""])
        self.assertEqual(third.status, "complete")

    def test_envelope_integrity_survives_transient_message_hydration(self) -> None:
        client = FakeDiscoveryClient()
        provider = message(-1001, 11, "garden")
        client.global_scripts[("garden", "main", "")].extend(
            [
                page(
                    [provider],
                    next_offset="trusted-next",
                    integrity_partial=True,
                ),
                page([provider], next_offset=""),
            ]
        )
        client.chats[-1001] = chat(-1001, "Weekend Circle")
        client.messages[(-1001, 11)] = TDLibError("SYNTHETIC_TRANSIENT")
        service = TargetDiscoveryService(client=client, registry=registry())

        first = discover(service, ["garden", "seeds"])
        client.messages[(-1001, 11)] = provider
        second = discover(service, ["garden", "seeds"], cursor=first.next_cursor)
        third = discover(service, ["garden", "seeds"], cursor=second.next_cursor)

        first_lane = lane_coverage(first, hypothesis_index=0, chat_list="main")
        self.assertEqual(first.status, "partial")
        self.assertEqual(first_lane.status, "partial")
        self.assertEqual(first_lane.pages_scanned, 1)
        self.assertEqual(second.status, "partial")
        self.assertEqual(third.status, "partial")
        garden_calls = [
            call
            for call in client.calls
            if call[0] == "search_global_messages" and call[2] == "garden"
        ]
        self.assertEqual(len(garden_calls), 1)

    def test_cross_chat_integrity_survives_another_transient_hit(self) -> None:
        client = FakeDiscoveryClient()
        edited = message(-1001, 11, "garden")
        transient = message(-1001, 12, "garden notes")
        client.global_scripts[("garden", "main", "")].extend(
            [
                page([edited, transient], next_offset="trusted-next"),
                page([edited, transient], next_offset=""),
            ]
        )
        client.chats[-1001] = chat(-1001, "Weekend Circle")
        client.messages[(-1001, 11)] = message(-2002, 11, "garden")
        client.messages[(-1001, 12)] = TDLibError("SYNTHETIC_TRANSIENT")
        service = TargetDiscoveryService(client=client, registry=registry())

        first = discover(service, ["garden", "seeds"])
        client.messages[(-1001, 11)] = edited
        client.messages[(-1001, 12)] = transient
        second = discover(service, ["garden", "seeds"], cursor=first.next_cursor)
        third = discover(service, ["garden", "seeds"], cursor=second.next_cursor)

        self.assertEqual(first.status, "partial")
        self.assertEqual(
            lane_coverage(first, hypothesis_index=0, chat_list="main").status,
            "partial",
        )
        self.assertEqual(second.status, "partial")
        self.assertEqual(third.status, "partial")

    def test_authorization_loss_never_allows_old_cursor_to_skip_catalog_evidence(self) -> None:
        client = FakeDiscoveryClient()
        client.catalog_scripts["main"].extend([False, True])
        service = TargetDiscoveryService(client=client, registry=registry())
        first = discover(service, ["garden", "seeds"])
        client.positions["main"] = [CatalogPosition(-1001, 30)]
        client.chats[-1001] = chat(-1001, "Garden Circle")
        client.global_scripts[("seeds", "main", "")].append(
            AuthorizationBlocked("SYNTHETIC_AUTH")
        )

        blocked = discover(
            service,
            ["garden", "seeds"],
            cursor=first.next_cursor,
        )
        resumed = discover(
            service,
            ["garden", "seeds"],
            cursor=first.next_cursor,
        )

        self.assertEqual(blocked.status, "blocked")
        self.assertNotEqual(resumed.status, "complete")
        self.assertFalse(resumed.coverage.complete)
        self.assertEqual(resumed.candidates, [])
        self.assertEqual(
            [call for call in client.calls if call == ("get_chat", -1001)],
            [],
        )

    def test_transient_catalog_hydration_retries_and_recovers(self) -> None:
        client = FakeDiscoveryClient()
        client.positions["main"] = [
            CatalogPosition(-1001, 20),
            CatalogPosition(-1002, 10),
        ]
        client.chats[-1001] = TDLibError("SYNTHETIC_TRANSIENT")
        client.chats[-1002] = chat(-1002, "Garden Two")
        service = TargetDiscoveryService(client=client, registry=registry())

        first = discover(service, ["garden", "seeds"])
        client.chats[-1001] = chat(-1001, "Garden One")
        second = discover(service, ["garden", "seeds"], cursor=first.next_cursor)

        self.assertEqual(first.status, "partial")
        self.assertEqual([item.chat_id for item in first.candidates], [-1002])
        self.assertEqual(second.status, "complete")
        self.assertEqual([item.chat_id for item in second.candidates], [-1001])
        self.assertEqual(
            len([call for call in client.calls if call == ("get_chat", -1001)]),
            2,
        )

    def test_transient_catalog_retry_does_not_abandon_sixteenth_id(self) -> None:
        client = FakeDiscoveryClient()
        chat_ids = [-1000 - index for index in range(16)]
        client.positions["main"] = [
            CatalogPosition(chat_id, 100 - index)
            for index, chat_id in enumerate(chat_ids)
        ]
        client.chats = {
            chat_id: chat(chat_id, f"Garden {index}")
            for index, chat_id in enumerate(chat_ids)
        }
        client.chats[chat_ids[0]] = TDLibError("SYNTHETIC_TRANSIENT")
        service = TargetDiscoveryService(client=client, registry=registry())

        first = discover(service, ["garden", "seeds"])
        client.chats[chat_ids[0]] = chat(chat_ids[0], "Garden Zero")
        second = discover(service, ["garden", "seeds"], cursor=first.next_cursor)

        returned = {item.chat_id for item in [*first.candidates, *second.candidates]}
        self.assertEqual(first.status, "partial")
        self.assertEqual(second.status, "complete")
        self.assertEqual(returned, set(chat_ids))

    def test_authorization_loss_is_blocked_and_redacted(self) -> None:
        client = FakeDiscoveryClient()
        client.global_scripts[("garden", "main", "")].append(
            AuthorizationBlocked("SYNTHETIC_AUTH_SENTINEL")
        )
        response = discover(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden", "seeds"],
        )

        self.assertEqual(response.status, "blocked")
        self.assertIsNone(response.next_cursor)
        self.assertEqual(response.candidates, [])
        self.assertNotIn("SYNTHETIC_AUTH_SENTINEL", response.model_dump_json())

    def test_catalog_only_candidate_uses_normalized_compact_and_substring_rules(self) -> None:
        client = FakeDiscoveryClient()
        client.positions["main"] = [
            CatalogPosition(-1001, 30),
            CatalogPosition(-1002, 20),
            CatalogPosition(-1003, 10),
        ]
        client.chats = {
            -1001: chat(-1001, "Garden Circle"),
            -1002: chat(-1002, "Seed-Exchange"),
            -1003: chat(-1003, "The Community Garden Board"),
        }

        response = discover(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden circle", "seedexchange", "community garden"],
        )

        evidence = {
            candidate.chat_id: {item.kind for item in candidate.metadata_evidence}
            for candidate in response.candidates
        }
        self.assertEqual(evidence[-1001], {"normalized_title"})
        self.assertEqual(evidence[-1002], {"compact_title"})
        self.assertEqual(evidence[-1003], {"title_substring"})

    def test_catalog_only_blank_titles_are_acknowledged_without_hiding_later_candidate(self) -> None:
        blank_ids = [-1000 - index for index in range(15)]
        later_id = -2000
        client = FakeDiscoveryClient()
        client.positions["main"] = [
            CatalogPosition(chat_id, 100 - index)
            for index, chat_id in enumerate([*blank_ids, later_id])
        ]
        client.chats = {
            chat_id: chat(
                chat_id,
                "" if index % 2 == 0 else "\u202e\u2066\x00",
                chat_type="chatTypePrivate",
            )
            for index, chat_id in enumerate(blank_ids)
        }
        client.chats[later_id] = chat(
            later_id,
            "Garden Circle",
            chat_type="chatTypePrivate",
        )
        service = TargetDiscoveryService(client=client, registry=registry())

        first = discover(service, ["garden circle", "seeds"])
        second = discover(
            service,
            ["garden circle", "seeds"],
            cursor=first.next_cursor,
        )

        self.assertEqual(first.status, "page")
        self.assertEqual(first.candidates, [])
        self.assertEqual(first.coverage.hydration, "complete")
        self.assertEqual(second.status, "complete")
        self.assertTrue(second.coverage.complete)
        self.assertEqual(second.coverage.hydration, "complete")
        self.assertEqual(
            [(candidate.chat_id, candidate.title) for candidate in second.candidates],
            [(later_id, "Garden Circle")],
        )

    def test_unavailable_title_with_global_evidence_remains_permanently_partial(self) -> None:
        client = FakeDiscoveryClient()
        provider = message(-1001, 11, "garden")
        client.positions["main"] = [CatalogPosition(-1001, 30)]
        client.global_scripts[("garden", "main", "")].append(
            page([provider], next_offset="")
        )
        client.chats[-1001] = chat(
            -1001,
            "\u202e\u2066\x00",
            chat_type="chatTypePrivate",
        )
        client.messages[(-1001, 11)] = provider

        response = discover(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden", "seeds"],
        )

        self.assertEqual(response.status, "partial")
        self.assertEqual(response.candidates, [])
        self.assertEqual(response.coverage.hydration, "partial")
        self.assertEqual(response.coverage.catalog.main.status, "error")
        self.assertEqual(
            lane_coverage(response, hypothesis_index=0, chat_list="main").status,
            "partial",
        )

    def test_metadata_and_message_evidence_merge_after_one_chat_hydration(self) -> None:
        client = FakeDiscoveryClient()
        client.positions["main"] = [CatalogPosition(-1001, 30)]
        provider = message(-1001, 11, "seed exchange")
        client.global_scripts[("garden circle", "main", "")].append(
            page([provider], next_offset="")
        )
        client.chats[-1001] = chat(-1001, "Garden Circle")
        client.messages[(-1001, 11)] = provider

        response = discover(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden circle", "seed exchange"],
        )

        candidate = response.candidates[0]
        self.assertEqual(len(candidate.metadata_evidence), 1)
        self.assertEqual(len(candidate.message_evidence), 1)
        self.assertEqual(
            [call for call in client.calls if call == ("get_chat", -1001)],
            [("get_chat", -1001)],
        )

    def test_main_and_archive_provenance_remain_separate(self) -> None:
        client = FakeDiscoveryClient()
        main_hit = message(-1001, 11, "garden")
        archive_hit = message(-2002, 12, "garden")
        client.global_scripts[("garden", "main", "")].append(
            page([main_hit], next_offset="")
        )
        client.global_scripts[("garden", "archive", "")].append(
            page([archive_hit], next_offset="")
        )
        client.chats = {
            -1001: chat(-1001, "Main Circle"),
            -2002: chat(-2002, "Archive Circle"),
        }
        client.messages[(-1001, 11)] = main_hit
        client.messages[(-2002, 12)] = archive_hit

        _, accumulated = run_until_complete(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden", "seeds"],
            scope="both",
        )

        self.assertIn((0, "main", 11), accumulated[-1001]["messages"])
        self.assertIn((0, "archive", 12), accumulated[-2002]["messages"])

    def test_per_call_caps_are_25_candidates_and_10_message_evidence(self) -> None:
        client = FakeDiscoveryClient()
        client.positions["main"] = [
            CatalogPosition(-1000 - index, 100 - index) for index in range(15)
        ]
        hits = [message(-2000 - index, 100 + index, "garden") for index in range(10)]
        client.global_scripts[("garden", "main", "")].append(
            page(hits, next_offset="")
        )
        for index in range(15):
            client.chats[-1000 - index] = chat(-1000 - index, f"Garden {index}")
        for hit in hits:
            client.chats[hit["chat_id"]] = chat(hit["chat_id"], "Unrelated")
            client.messages[(hit["chat_id"], hit["id"])] = hit

        response = discover(
            TargetDiscoveryService(client=client, registry=registry()),
            ["garden", "seeds"],
        )

        self.assertEqual(len(response.candidates), 25)
        self.assertEqual(
            sum(len(candidate.message_evidence) for candidate in response.candidates),
            10,
        )

    def test_cursor_mismatch_and_expiry_return_no_evidence(self) -> None:
        clock = FakeClock()
        service = TargetDiscoveryService(
            client=FakeDiscoveryClient(), registry=registry(clock)
        )
        first = discover(service, ["garden", "seeds"])

        mismatched = discover(
            service,
            ["garden", "orchard"],
            cursor=first.next_cursor,
        )
        self.assertEqual(mismatched.status, "expired")
        self.assertEqual(mismatched.candidates, [])

        clock.advance(300)
        expired = discover(service, ["garden", "seeds"], cursor=first.next_cursor)
        self.assertEqual(expired.status, "expired")
        self.assertIsNone(expired.next_cursor)


if __name__ == "__main__":
    unittest.main()
