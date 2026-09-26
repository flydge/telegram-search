"""Bounded account-wide Telegram target evidence discovery."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from .discovery_state import (
    CatalogPosition,
    ChatListName,
    DiscoveryCursorExpired,
    DiscoveryRegistry,
    DiscoveryScan,
    DiscoveryWork,
    DiscoveryWorkBusy,
    GlobalLaneKey,
)
from .sanitize import normalize_search_text, render_evidence, sanitize_telegram_text
from .schemas import (
    DISCOVERY_COVERAGE_DETAIL,
    CatalogCoverage,
    CatalogLaneCoverage,
    DiscoverTargetsRequest,
    DiscoveryCandidate,
    DiscoveryCoverage,
    DiscoveryScope,
    GlobalMessageCoverage,
    GlobalMessageLaneCoverage,
    TargetDiscoveryResponse,
    _normalize_resolve_target,
)
from .tdjson import (
    AuthorizationBlocked,
    GlobalMessageEnvelopeError,
    GlobalMessagePage,
    MessageNotFound,
    SecretChatRejected,
    TDLibError,
)

_CATALOG_PAGE_SIZE = 15
_GLOBAL_PAGE_SIZE = 10
_MAX_CANDIDATES = 25
_MAX_MESSAGE_EVIDENCE = 10
_SNIPPET_LIMIT = 512
_REGISTRY_TTL_SECONDS = 300
_REGISTRY_CAPACITY = 4
_NORMALIZATION_LIMIT = 2**31 - 1
_CAPTION_EVIDENCE_CONTENT_TYPES = frozenset(
    {
        "messageAnimation",
        "messageAudio",
        "messageDocument",
        "messagePaidMedia",
        "messagePhoto",
        "messageVideo",
        "messageVoiceNote",
    }
)


class TargetDiscoveryClient(Protocol):
    def get_chat_list_prefix(self, chat_list: ChatListName) -> list[int]: ...

    def load_more_chats(
        self,
        chat_list: ChatListName,
        on_positions: Callable[[list[CatalogPosition]], None],
    ) -> bool: ...

    def search_global_messages(
        self,
        chat_list: ChatListName,
        query: str,
        *,
        offset: str,
    ) -> GlobalMessagePage: ...

    def resolve_target(self, target: str | int) -> dict[str, Any]: ...

    def get_message(self, chat_id: int, message_id: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _ProviderEvidence:
    chat_id: int
    message_id: int
    date: int
    text: str


@dataclass(frozen=True)
class _HydratedChat:
    chat_id: int
    title: str
    chat_type: str


def _requested_lists(scope: DiscoveryScope) -> tuple[ChatListName, ...]:
    if scope == "both":
        return ("main", "archive")
    return (scope,)


def _hypothesis_digest(hypotheses: list[str]) -> str:
    normalized = (_normalize_resolve_target(value) for value in hypotheses)
    return hashlib.sha256("\x1f".join(normalized).encode("utf-8")).hexdigest()


def _message_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if not isinstance(content, dict):
        return None
    content_type = content.get("@type")
    if content_type == "messageText":
        key = "text"
    elif content_type in _CAPTION_EVIDENCE_CONTENT_TYPES:
        key = "caption"
    else:
        return None
    formatted = content.get(key)
    if not isinstance(formatted, dict):
        return None
    text = formatted.get("text")
    return text if isinstance(text, str) else None


def _provider_evidence(message: object) -> _ProviderEvidence | None:
    if not isinstance(message, dict) or message.get("@type") != "message":
        return None
    chat_id = message.get("chat_id")
    message_id = message.get("id")
    date = message.get("date")
    text = _message_text(message)
    if (
        type(chat_id) is not int
        or chat_id == 0
        or type(message_id) is not int
        or message_id == 0
        or type(date) is not int
        or date < 0
        or text is None
        or not normalize_search_text(text, max_length=_NORMALIZATION_LIMIT)
    ):
        return None
    return _ProviderEvidence(chat_id, message_id, date, text)


def _chat_type(chat: dict[str, Any]) -> str | None:
    chat_type = chat.get("type")
    if not isinstance(chat_type, dict):
        return None
    type_name = chat_type.get("@type")
    if type_name == "chatTypePrivate":
        return "private"
    if type_name == "chatTypeBasicGroup":
        return "basic_group"
    if type_name == "chatTypeSupergroup":
        return "channel" if chat_type.get("is_channel") is True else "supergroup"
    return None


def _hydrate_chat(expected_id: int, chat: object) -> _HydratedChat | None:
    if not isinstance(chat, dict):
        return None
    if type(chat.get("id")) is not int or chat["id"] != expected_id:
        return None
    mapped_type = _chat_type(chat)
    title = sanitize_telegram_text(chat.get("title"), max_length=255)
    if mapped_type is None:
        return None
    return _HydratedChat(expected_id, title, mapped_type)


def _metadata_kind(title: str, hypothesis: str) -> str | None:
    normalized_title = _normalize_resolve_target(title)
    normalized_hypothesis = _normalize_resolve_target(hypothesis)
    if normalized_title == normalized_hypothesis:
        return "normalized_title"
    if normalized_title.replace(" ", "") == normalized_hypothesis.replace(" ", ""):
        return "compact_title"
    shorter = min(len(normalized_title), len(normalized_hypothesis))
    if shorter >= 3 and (
        normalized_title in normalized_hypothesis
        or normalized_hypothesis in normalized_title
    ):
        return "title_substring"
    return None


def _terminal_coverage(
    request: DiscoverTargetsRequest,
    status: Literal["blocked", "error", "expired"],
) -> DiscoveryCoverage:
    requested = set(_requested_lists(request.scope))
    catalog_status = "expired" if status == "expired" else status
    global_status = "not_started" if status == "expired" else status
    catalog = {}
    for chat_list in ("main", "archive"):
        lane_status = catalog_status if chat_list in requested else "not_requested"
        catalog[chat_list] = CatalogLaneCoverage(
            status=lane_status,
            scanned_count=0,
            emitted_count=0,
            end_reached=False,
        )
    return DiscoveryCoverage(
        complete=False,
        catalog=CatalogCoverage(**catalog),
        global_messages=GlobalMessageCoverage(
            lanes=[
                GlobalMessageLaneCoverage(
                    hypothesis_index=index,
                    chat_list=chat_list,
                    status=global_status,
                    pages_scanned=0,
                    hits_seen=0,
                )
                for index in range(len(request.hypotheses))
                for chat_list in _requested_lists(request.scope)
            ]
        ),
        hydration="blocked" if status == "blocked" else "error",
        detail=DISCOVERY_COVERAGE_DETAIL,
    )


def terminal_discovery_response(
    request: DiscoverTargetsRequest,
    status: Literal["blocked", "error", "expired"],
) -> TargetDiscoveryResponse:
    return TargetDiscoveryResponse(
        status=status,
        coverage=_terminal_coverage(request, status),
    )


def _busy_discovery_response(
    request: DiscoverTargetsRequest,
    cursor: str,
) -> TargetDiscoveryResponse:
    requested = set(_requested_lists(request.scope))
    return TargetDiscoveryResponse(
        status="page",
        coverage=DiscoveryCoverage(
            complete=False,
            catalog=CatalogCoverage(
                **{
                    chat_list: CatalogLaneCoverage(
                        status=(
                            "scanning"
                            if chat_list in requested
                            else "not_requested"
                        ),
                        scanned_count=0,
                        emitted_count=0,
                        end_reached=False,
                    )
                    for chat_list in ("main", "archive")
                }
            ),
            global_messages=GlobalMessageCoverage(
                lanes=[
                    GlobalMessageLaneCoverage(
                        hypothesis_index=index,
                        chat_list=chat_list,
                        status="not_started",
                        pages_scanned=0,
                        hits_seen=0,
                    )
                    for index in range(len(request.hypotheses))
                    for chat_list in _requested_lists(request.scope)
                ]
            ),
            hydration="complete",
            detail=DISCOVERY_COVERAGE_DETAIL,
        ),
        next_cursor=cursor,
    )


def _coverage_from_state(
    state: DiscoveryScan,
    *,
    hydration_partial: bool,
) -> DiscoveryCoverage:
    catalog_payload: dict[str, CatalogLaneCoverage] = {}
    for chat_list in ("main", "archive"):
        lane = state.catalog.get(chat_list)
        if lane is None:
            catalog_payload[chat_list] = CatalogLaneCoverage(
                status="not_requested",
                scanned_count=0,
                emitted_count=0,
                end_reached=False,
            )
            continue
        status = {
            "not_started": "scanning",
            "partial": "error",
        }.get(lane.status, lane.status)
        catalog_payload[chat_list] = CatalogLaneCoverage(
            status=status,
            scanned_count=len(lane.positions),
            emitted_count=len(lane.emitted),
            end_reached=status == "complete",
        )
    global_lanes = [
        GlobalMessageLaneCoverage(
            hypothesis_index=key.hypothesis_index,
            chat_list=key.chat_list,
            status=lane.status,
            pages_scanned=lane.pages_scanned,
            hits_seen=lane.hits_seen,
        )
        for key, lane in state.global_lanes.items()
    ]
    permanent_partial = any(
        lane.permanent_partial for lane in state.catalog.values()
    ) or any(lane.permanent_partial for lane in state.global_lanes.values())
    hydration = "partial" if hydration_partial or permanent_partial else "complete"
    complete = (
        all(
            lane.status == "complete"
            for lane in catalog_payload.values()
            if lane.status != "not_requested"
        )
        and all(lane.status == "complete" for lane in global_lanes)
        and hydration == "complete"
    )
    return DiscoveryCoverage(
        complete=complete,
        catalog=CatalogCoverage(**catalog_payload),
        global_messages=GlobalMessageCoverage(lanes=global_lanes),
        hydration=hydration,
        detail=DISCOVERY_COVERAGE_DETAIL,
    )


class TargetDiscoveryService:
    def __init__(
        self,
        *,
        client: TargetDiscoveryClient,
        registry: DiscoveryRegistry | None = None,
    ) -> None:
        self._client = client
        self._registry = (
            registry
            if registry is not None
            else DiscoveryRegistry(
                ttl_seconds=_REGISTRY_TTL_SECONDS,
                capacity=_REGISTRY_CAPACITY,
            )
        )

    def discover(self, request: DiscoverTargetsRequest) -> TargetDiscoveryResponse:
        digest = _hypothesis_digest(request.hypotheses)
        cursor = request.cursor
        lease_token: str | None = None
        work: DiscoveryWork | None = None
        try:
            if cursor is None:
                cursor = self._registry.start(
                    hypothesis_digest=digest,
                    hypothesis_count=len(request.hypotheses),
                    scope=request.scope,
                )
            state = self._registry.bind(
                cursor,
                hypothesis_digest=digest,
                hypothesis_count=len(request.hypotheses),
                scope=request.scope,
            )
            work = self._registry.next_work(cursor)
            if work is None:
                return self._response(
                    cursor, state, [], hydration_partial=False
                )
            lease_token = work.lease_token

            catalog_ids: list[int] = []
            catalog_sources: dict[int, set[ChatListName]] = defaultdict(set)
            if work.catalog_list is not None:
                catalog_ids = self._advance_catalog(
                    cursor,
                    work.catalog_list,
                    lease_token,
                    has_pending=state.catalog[work.catalog_list].pending,
                )
                for chat_id in catalog_ids:
                    catalog_sources[chat_id].add(work.catalog_list)

            global_page: GlobalMessagePage | None = None
            global_key = work.global_lane
            provider_items: list[_ProviderEvidence] = []
            integrity_partial = False
            if global_key is not None:
                global_page, provider_items, integrity_partial = self._load_global(
                    cursor,
                    global_key,
                    request.hypotheses[global_key.hypothesis_index],
                    lease_token,
                )

            (
                candidates,
                hydration_partial,
                retryable_global,
                permanent_global,
            ) = self._build_candidates(
                cursor=cursor,
                lease_token=lease_token,
                hypotheses=request.hypotheses,
                catalog_ids=catalog_ids,
                catalog_sources=catalog_sources,
                global_key=global_key,
                provider_items=provider_items,
            )

            if global_key is not None and global_page is not None:
                if integrity_partial or permanent_global:
                    self._registry.record_global_page(
                        cursor,
                        global_key,
                        next_offset=global_page.next_offset,
                        hits=len(global_page.messages),
                        lease_token=lease_token,
                    )
                    self._registry.record_permanent_partial(
                        cursor, global_key, lease_token=lease_token
                    )
                    if retryable_global:
                        candidates = [
                            candidate
                            for candidate in candidates
                            if not candidate.message_evidence
                        ]
                elif retryable_global:
                    self._registry.record_retryable_error(
                        cursor, global_key, lease_token=lease_token
                    )
                    candidates = [
                        candidate
                        for candidate in candidates
                        if not candidate.message_evidence
                    ]
                else:
                    self._registry.record_global_page(
                        cursor,
                        global_key,
                        next_offset=global_page.next_offset,
                        hits=len(global_page.messages),
                        lease_token=lease_token,
                    )

            self._registry.record_returned_candidates(
                cursor,
                (candidate.chat_id for candidate in candidates),
                lease_token=lease_token,
            )
            state = self._registry.bind(
                cursor,
                hypothesis_digest=digest,
                hypothesis_count=len(request.hypotheses),
                scope=request.scope,
            )
            return self._response(
                cursor,
                state,
                candidates,
                hydration_partial=hydration_partial or integrity_partial,
            )
        except DiscoveryCursorExpired:
            return terminal_discovery_response(request, "expired")
        except DiscoveryWorkBusy:
            if cursor is None:
                return terminal_discovery_response(request, "error")
            return _busy_discovery_response(request, cursor)
        except AuthorizationBlocked:
            if cursor is not None and lease_token is not None and work is not None:
                try:
                    if work.catalog_list is not None:
                        self._registry.record_catalog_permanent_partial(
                            cursor,
                            work.catalog_list,
                            lease_token=lease_token,
                        )
                    if work.global_lane is not None:
                        self._registry.record_permanent_partial(
                            cursor,
                            work.global_lane,
                            lease_token=lease_token,
                        )
                except (DiscoveryCursorExpired, RuntimeError):
                    pass
            return terminal_discovery_response(request, "blocked")
        except (TypeError, ValueError, RuntimeError, TDLibError):
            return terminal_discovery_response(request, "error")
        finally:
            if cursor is not None and lease_token is not None:
                try:
                    self._registry.release_work(cursor, lease_token)
                except (DiscoveryCursorExpired, RuntimeError):
                    pass

    def _advance_catalog(
        self,
        cursor: str,
        chat_list: ChatListName,
        lease_token: str,
        *,
        has_pending: bool,
    ) -> list[int]:
        if not has_pending:
            try:
                prefix = self._client.get_chat_list_prefix(chat_list)
                if (
                    not isinstance(prefix, list)
                    or len(prefix) > _CATALOG_PAGE_SIZE
                    or any(
                        type(chat_id) is not int or chat_id == 0
                        for chat_id in prefix
                    )
                ):
                    raise ValueError("invalid catalog prefix")
                self._registry.record_catalog_positions(
                    cursor,
                    chat_list,
                    (
                        CatalogPosition(chat_id, (1 << 63) - 1 - index)
                        for index, chat_id in enumerate(prefix)
                    ),
                    lease_token=lease_token,
                )

                def record_positions(positions: list[CatalogPosition]) -> None:
                    self._registry.record_catalog_positions(
                        cursor,
                        chat_list,
                        positions,
                        lease_token=lease_token,
                    )

                end_reached = self._client.load_more_chats(
                    chat_list, record_positions
                )
                if type(end_reached) is not bool:
                    raise ValueError("invalid catalog completion marker")
                if end_reached:
                    self._registry.mark_catalog_end(
                        cursor, chat_list, lease_token=lease_token
                    )
            except AuthorizationBlocked:
                raise
            except TDLibError:
                self._registry.record_catalog_retryable_error(
                    cursor, chat_list, lease_token=lease_token
                )
        return self._registry.catalog_page(
            cursor,
            chat_list,
            limit=_CATALOG_PAGE_SIZE,
            lease_token=lease_token,
        )

    def _load_global(
        self,
        cursor: str,
        key: GlobalLaneKey,
        hypothesis: str,
        lease_token: str,
    ) -> tuple[GlobalMessagePage | None, list[_ProviderEvidence], bool]:
        lane = self._registry.global_lane(cursor, key)
        try:
            page = self._client.search_global_messages(
                key.chat_list, hypothesis, offset=lane.offset
            )
        except AuthorizationBlocked:
            raise
        except GlobalMessageEnvelopeError:
            self._registry.record_permanent_partial(
                cursor, key, lease_token=lease_token
            )
            return None, [], True
        except TDLibError:
            self._registry.record_retryable_error(
                cursor, key, lease_token=lease_token
            )
            return None, [], False
        if (
            not isinstance(page, GlobalMessagePage)
            or not isinstance(page.messages, list)
            or len(page.messages) > _GLOBAL_PAGE_SIZE
            or not isinstance(page.next_offset, str)
            or type(page.integrity_partial) is not bool
        ):
            self._registry.record_permanent_partial(
                cursor, key, lease_token=lease_token
            )
            return None, [], True
        items: list[_ProviderEvidence] = []
        integrity_partial = page.integrity_partial
        for raw_message in page.messages:
            evidence = _provider_evidence(raw_message)
            if evidence is None:
                integrity_partial = True
            else:
                items.append(evidence)
        return page, items, integrity_partial

    def _build_candidates(
        self,
        *,
        cursor: str,
        lease_token: str,
        hypotheses: list[str],
        catalog_ids: list[int],
        catalog_sources: dict[int, set[ChatListName]],
        global_key: GlobalLaneKey | None,
        provider_items: list[_ProviderEvidence],
    ) -> tuple[list[DiscoveryCandidate], bool, bool, bool]:
        ordered_ids = list(
            dict.fromkeys(
                [
                    *catalog_ids,
                    *(item.chat_id for item in provider_items),
                ]
            )
        )[:_MAX_CANDIDATES]
        global_chat_ids = {item.chat_id for item in provider_items}
        hydrated: dict[int, _HydratedChat] = {}
        hydration_partial = False
        retryable_global = False
        permanent_global = False
        for chat_id in ordered_ids:
            try:
                chat = self._client.resolve_target(chat_id)
            except AuthorizationBlocked:
                raise
            except (MessageNotFound, SecretChatRejected):
                hydration_partial = True
                permanent_global = permanent_global or chat_id in global_chat_ids
                self._mark_catalog_sources_partial(
                    cursor, catalog_sources.get(chat_id, set()), lease_token
                )
                continue
            except TDLibError:
                hydration_partial = True
                retryable_global = retryable_global or chat_id in global_chat_ids
                self._mark_catalog_sources_retryable(
                    cursor,
                    catalog_sources.get(chat_id, set()),
                    chat_id,
                    lease_token,
                )
                continue
            checked = _hydrate_chat(chat_id, chat)
            if checked is None:
                hydration_partial = True
                permanent_global = permanent_global or chat_id in global_chat_ids
                self._mark_catalog_sources_partial(
                    cursor, catalog_sources.get(chat_id, set()), lease_token
                )
                continue
            if not checked.title:
                if chat_id in catalog_sources and chat_id not in global_chat_ids:
                    self._mark_catalog_sources_hydrated(
                        cursor,
                        catalog_sources[chat_id],
                        chat_id,
                        lease_token,
                    )
                    continue
                hydration_partial = True
                permanent_global = True
                self._mark_catalog_sources_partial(
                    cursor, catalog_sources.get(chat_id, set()), lease_token
                )
                continue
            hydrated[chat_id] = checked
            self._mark_catalog_sources_hydrated(
                cursor,
                catalog_sources.get(chat_id, set()),
                chat_id,
                lease_token,
            )

        message_evidence: dict[int, list[dict[str, Any]]] = defaultdict(list)
        if global_key is not None and not retryable_global:
            seen_messages: set[tuple[int, int]] = set()
            for provider in provider_items[:_MAX_MESSAGE_EVIDENCE]:
                identity = (provider.chat_id, provider.message_id)
                if identity in seen_messages or provider.chat_id not in hydrated:
                    continue
                seen_messages.add(identity)
                try:
                    rehydrated = self._client.get_message(*identity)
                except AuthorizationBlocked:
                    raise
                except (MessageNotFound, SecretChatRejected):
                    hydration_partial = True
                    permanent_global = True
                    continue
                except TDLibError:
                    hydration_partial = True
                    retryable_global = True
                    message_evidence.clear()
                    break
                checked = _provider_evidence(rehydrated)
                if (
                    checked is None
                    or checked.chat_id != provider.chat_id
                    or checked.message_id != provider.message_id
                    or normalize_search_text(
                        checked.text, max_length=_NORMALIZATION_LIMIT
                    )
                    != normalize_search_text(
                        provider.text, max_length=_NORMALIZATION_LIMIT
                    )
                ):
                    hydration_partial = True
                    permanent_global = True
                    continue
                try:
                    date_utc = datetime.fromtimestamp(checked.date, tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    hydration_partial = True
                    permanent_global = True
                    continue
                message_evidence[checked.chat_id].append(
                    {
                        "hypothesis_index": global_key.hypothesis_index,
                        "chat_list": global_key.chat_list,
                        "message_id": checked.message_id,
                        "date_utc": date_utc,
                        "snippet": render_evidence(
                            checked.text, max_length=_SNIPPET_LIMIT
                        ),
                        "evidence_anchor": {
                            "chat_id": checked.chat_id,
                            "message_id": checked.message_id,
                        },
                    }
                )

        candidates: list[DiscoveryCandidate] = []
        for chat_id in ordered_ids:
            hydrated_chat = hydrated.get(chat_id)
            if hydrated_chat is None:
                continue
            metadata = []
            if chat_id in catalog_sources:
                for index, hypothesis in enumerate(hypotheses):
                    kind = _metadata_kind(hydrated_chat.title, hypothesis)
                    if kind is not None:
                        metadata.append({"hypothesis_index": index, "kind": kind})
            messages = message_evidence.get(chat_id, [])
            if not metadata and not messages:
                continue
            memberships = set(catalog_sources.get(chat_id, set()))
            if messages and global_key is not None:
                memberships.add(global_key.chat_list)
            candidates.append(
                DiscoveryCandidate(
                    chat_id=chat_id,
                    title=hydrated_chat.title,
                    chat_type=hydrated_chat.chat_type,
                    list_membership=[
                        item for item in ("main", "archive") if item in memberships
                    ],
                    metadata_evidence=metadata,
                    message_evidence=messages,
                )
            )
        return candidates, hydration_partial, retryable_global, permanent_global

    def _mark_catalog_sources_partial(
        self,
        cursor: str,
        sources: set[ChatListName],
        lease_token: str,
    ) -> None:
        for chat_list in sources:
            self._registry.record_catalog_permanent_partial(
                cursor, chat_list, lease_token=lease_token
            )

    def _mark_catalog_sources_retryable(
        self,
        cursor: str,
        sources: set[ChatListName],
        chat_id: int,
        lease_token: str,
    ) -> None:
        for chat_list in sources:
            self._registry.record_catalog_hydration_retry(
                cursor,
                chat_list,
                [chat_id],
                lease_token=lease_token,
            )

    def _mark_catalog_sources_hydrated(
        self,
        cursor: str,
        sources: set[ChatListName],
        chat_id: int,
        lease_token: str,
    ) -> None:
        for chat_list in sources:
            self._registry.record_catalog_hydrated(
                cursor,
                chat_list,
                [chat_id],
                lease_token=lease_token,
            )

    def _response(
        self,
        cursor: str,
        state: DiscoveryScan,
        candidates: list[DiscoveryCandidate],
        *,
        hydration_partial: bool,
    ) -> TargetDiscoveryResponse:
        coverage = _coverage_from_state(
            state, hydration_partial=hydration_partial
        )
        partial = (
            coverage.hydration != "complete"
            or any(
                lane.status == "error"
                for lane in (coverage.catalog.main, coverage.catalog.archive)
            )
            or any(
                lane.status in {"partial", "error"}
                for lane in coverage.global_messages.lanes
            )
        )
        status = "complete" if coverage.complete else "partial" if partial else "page"
        return TargetDiscoveryResponse(
            status=status,
            candidates=candidates,
            coverage=coverage,
            next_cursor=cursor,
        )
