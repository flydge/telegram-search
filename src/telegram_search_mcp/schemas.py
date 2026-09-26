"""Strict public schemas for the exact four-tool MCP surface."""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from .sanitize import render_evidence


def _reject_zero_chat_id(value: int) -> int:
    if value == 0:
        raise ValueError("numeric chat_id must be nonzero")
    return value


def validate_search_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("date must be an ISO 8601 date-time string") from None
    else:
        raise ValueError("date must be an ISO 8601 date-time string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("date must include a UTC offset")
    return parsed


def _normalize_resolve_target(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    folded = "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith("C")
    ).casefold()
    characters: list[str] = []
    pending_space = False
    for character in folded:
        category = unicodedata.category(character)
        if character.isspace() or category.startswith("P"):
            pending_space = bool(characters)
            continue
        if pending_space:
            characters.append(" ")
            pending_space = False
        characters.append(character)
    return "".join(characters).strip()


def _strip_resolve_target(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("target must be a string")
    target = value.strip()
    lowered = target.casefold()
    if not target or not _normalize_resolve_target(target) or "*" in target:
        raise ValueError("target must be nonempty and must not contain wildcards")
    if "/" in target or "\\" in target or "=" in target:
        raise ValueError("target must be a chat description, not a path or credential")
    if lowered.startswith("account:") or lowered.startswith("account="):
        raise ValueError("target must not be an account selector")
    if any(secret in lowered for secret in ("api_hash", "api_id", "password", "token=")):
        raise ValueError("target must not contain credentials")
    return target


UsernameTarget = Annotated[
    str,
    Field(strict=True, min_length=6, max_length=33, pattern=r"^@[A-Za-z0-9_]+$"),
]
NumericTarget = Annotated[
    int,
    Field(strict=True, ge=-(2**63), le=2**63 - 1),
    AfterValidator(_reject_zero_chat_id),
]
SearchTarget = UsernameTarget | NumericTarget
SearchDateTime = Annotated[datetime, BeforeValidator(validate_search_datetime)]
SearchLimit = Annotated[int, Field(strict=True, ge=1, le=20)]
ContextMessageCount = Annotated[int, Field(strict=True, ge=0, le=4)]
RequireComplete = Annotated[bool, Field(strict=True)]

ChatId = Annotated[
    int,
    Field(strict=True, ge=-(2**63), le=2**63 - 1),
    AfterValidator(_reject_zero_chat_id),
]
MessageId = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
TargetTitle = Annotated[str, Field(strict=True, min_length=1, max_length=255)]
ResolveTargetInput = Annotated[
    str,
    BeforeValidator(_strip_resolve_target),
    Field(strict=True, min_length=1, max_length=128),
]
ResolutionStatus = Literal[
    "resolved",
    "ambiguous",
    "not_found",
    "discovery_required",
    "incomplete",
    "blocked",
    "error",
]
ChatType = Literal["self", "private", "basic_group", "supergroup", "channel"]
MatchKind = Literal[
    "saved_messages_alias",
    "exact_username",
    "normalized_title",
    "compact_title",
    "fuzzy_title",
]
DiscoveryLaneStatus = Literal["not_requested", "complete", "incomplete", "blocked", "error"]


class ResolveTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    target: ResolveTargetInput


class ResolvedTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    chat_id: ChatId
    title: TargetTitle
    chat_type: ChatType


class TargetCandidate(ResolvedTarget):
    match_kind: MatchKind


class TargetDiscoveryLaneCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    status: DiscoveryLaneStatus
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]


class TargetDiscoveryCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    complete: Annotated[bool, Field(strict=True)]
    saved_messages: TargetDiscoveryLaneCoverage
    exact_username: TargetDiscoveryLaneCoverage
    search_chats: TargetDiscoveryLaneCoverage
    search_chats_on_server: TargetDiscoveryLaneCoverage
    recent_main: TargetDiscoveryLaneCoverage
    hydration: TargetDiscoveryLaneCoverage
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]


class TargetResolutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    status: ResolutionStatus
    resolved_target: ResolvedTarget | None = None
    match_kind: MatchKind | None = None
    candidates: list[TargetCandidate] = Field(default_factory=list, max_length=5)
    coverage: TargetDiscoveryCoverage

    @model_validator(mode="after")
    def validate_resolution(self) -> "TargetResolutionResponse":
        complete = self.coverage.complete
        no_target = self.resolved_target is None
        no_match = self.match_kind is None
        no_candidates = not self.candidates
        if self.status == "resolved":
            if not complete or no_target or no_match or self.match_kind == "fuzzy_title" or not no_candidates:
                raise ValueError("resolved response requires complete coverage, a non-fuzzy match, a target, and no candidates")
        elif self.status == "ambiguous":
            if not complete or not no_target or not no_match or not 1 <= len(self.candidates) <= 5:
                raise ValueError("ambiguous response requires complete coverage and 1 to 5 candidates only")
        elif self.status == "not_found":
            if not complete or not no_target or not no_match or not no_candidates:
                raise ValueError("not_found response requires complete coverage and no target, match, or candidates")
        elif self.status in {"discovery_required", "incomplete", "blocked", "error"}:
            if complete or not no_target or not no_match or not no_candidates:
                raise ValueError(
                    "discovery_required, incomplete, blocked, and error responses "
                    "require incomplete coverage and no result"
                )
        return self


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    text: Annotated[str, Field(strict=True, max_length=512)] | None = None
    file_name: Annotated[str, Field(strict=True, max_length=255)] | None = None
    mime_type: Annotated[str, Field(strict=True, max_length=127)] | None = None
    contains_number: Annotated[
        bool, Field(strict=True, json_schema_extra={"const": True})
    ] | None = None
    media_type: Literal[
        "document",
        "photo",
        "video",
        "audio",
        "animation",
        "voice_note",
        "video_note",
        "sticker",
    ] | None = None

    @model_validator(mode="after")
    def validate_nonempty(self) -> "SearchQuery":
        populated = False
        for field in ("text", "file_name", "mime_type"):
            value = getattr(self, field)
            if value is None:
                continue
            value = value.strip()
            if not value or "*" in value:
                raise ValueError(f"{field} must be nonempty and must not contain wildcards")
            setattr(self, field, value)
            populated = True
        if self.contains_number is False:
            raise ValueError("contains_number must be true when provided")
        populated = populated or self.media_type is not None or self.contains_number is True
        if not populated:
            raise ValueError("query must include at least one search field")
        if self.mime_type is not None and (
            self.mime_type.count("/") != 1 or any(ch.isspace() for ch in self.mime_type)
        ):
            raise ValueError("mime_type must be an exact type/subtype value")
        return self


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    target: SearchTarget
    query: SearchQuery
    date_from: SearchDateTime | None = None
    date_to: SearchDateTime | None = None
    limit: SearchLimit = 20
    context_messages: ContextMessageCount = 0
    require_complete: RequireComplete = True

    @model_validator(mode="after")
    def validate_request(self) -> "SearchRequest":
        for name in ("date_from", "date_to"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must include a UTC offset")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be later than date_to")
        return self


CoverageState = Literal["not_requested", "not_started", "complete", "incomplete"]


class Coverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    complete: bool
    text_status: CoverageState
    metadata_status: CoverageState
    date_from: datetime | None = None
    date_to: datetime | None = None
    detail: str


class EvidenceAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: ChatId
    message_id: MessageId


DiscoveryScope = Literal["main", "archive", "both"]
DiscoveryStatus = Literal["page", "complete", "partial", "blocked", "error", "expired"]
ChatListName = Literal["main", "archive"]
CatalogLaneStatus = Literal[
    "not_requested", "scanning", "complete", "blocked", "error", "expired"
]
GlobalLaneStatus = Literal[
    "not_started", "scanning", "complete", "partial", "blocked", "error"
]
HydrationStatus = Literal["complete", "partial", "blocked", "error"]
MetadataEvidenceKind = Literal[
    "normalized_title",
    "compact_title",
    "title_substring",
    "tdlib_local",
    "tdlib_server",
]
DISCOVERY_COVERAGE_DETAIL = "discovery coverage details redacted"
DiscoveryCoverageDetail = Literal[DISCOVERY_COVERAGE_DETAIL]
HypothesisInput = Annotated[
    str,
    BeforeValidator(_strip_resolve_target),
    Field(strict=True, min_length=1, max_length=128),
]
DiscoveryCursor = Annotated[
    str,
    Field(strict=True, min_length=21, max_length=128, pattern=r"^scan_[A-Za-z0-9_-]+$"),
]
HypothesisIndex = Annotated[int, Field(strict=True, ge=0, le=4)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]

_EVIDENCE_SNIPPET_LIMIT = 512
_EVIDENCE_PREFIX = render_evidence("", max_length=_EVIDENCE_SNIPPET_LIMIT)


def _validate_evidence_snippet(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("evidence snippet must be a string")
    if not value.startswith(_EVIDENCE_PREFIX):
        raise ValueError("evidence snippet must include the untrusted evidence marker")
    content = value[len(_EVIDENCE_PREFIX) :]
    if render_evidence(content, max_length=_EVIDENCE_SNIPPET_LIMIT) != value:
        raise ValueError("evidence snippet must be canonically sanitized")
    return value


EvidenceSnippet = Annotated[
    str,
    BeforeValidator(_validate_evidence_snippet),
    Field(strict=True, max_length=_EVIDENCE_SNIPPET_LIMIT),
]


class DiscoverTargetsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    hypotheses: Annotated[list[HypothesisInput], Field(min_length=2, max_length=5)]
    scope: DiscoveryScope = "both"
    cursor: DiscoveryCursor | None = None

    @model_validator(mode="after")
    def validate_unique_hypotheses(self) -> "DiscoverTargetsRequest":
        values = [_normalize_resolve_target(item) for item in self.hypotheses]
        if len(set(values)) != len(values):
            raise ValueError("hypotheses must be unique after normalization")
        return self


class MetadataEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    hypothesis_index: HypothesisIndex
    kind: MetadataEvidenceKind


class GlobalMessageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    hypothesis_index: HypothesisIndex
    chat_list: ChatListName
    message_id: MessageId
    date_utc: SearchDateTime
    snippet: EvidenceSnippet
    evidence_anchor: EvidenceAnchor

    @model_validator(mode="after")
    def validate_anchor(self) -> "GlobalMessageEvidence":
        if self.message_id != self.evidence_anchor.message_id:
            raise ValueError("message evidence and anchor must use the same message_id")
        return self


class DiscoveryCandidate(ResolvedTarget):
    list_membership: Annotated[list[ChatListName], Field(min_length=1, max_length=2)]
    metadata_evidence: Annotated[list[MetadataEvidence], Field(max_length=10)] = Field(
        default_factory=list
    )
    message_evidence: Annotated[list[GlobalMessageEvidence], Field(max_length=10)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_evidence(self) -> "DiscoveryCandidate":
        if len(set(self.list_membership)) != len(self.list_membership):
            raise ValueError("candidate list membership must be unique")
        memberships = set(self.list_membership)
        for evidence in self.message_evidence:
            if evidence.evidence_anchor.chat_id != self.chat_id:
                raise ValueError("message evidence anchor must use the candidate chat_id")
            if evidence.chat_list not in memberships:
                raise ValueError("message evidence chat list must match candidate membership")
        return self


class CatalogLaneCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    status: CatalogLaneStatus
    scanned_count: NonNegativeCount
    emitted_count: NonNegativeCount
    end_reached: Annotated[bool, Field(strict=True)]

    @model_validator(mode="after")
    def validate_progress(self) -> "CatalogLaneCoverage":
        if self.emitted_count > self.scanned_count:
            raise ValueError("catalog emitted_count cannot exceed scanned_count")
        if self.status == "not_requested" and (
            self.scanned_count != 0 or self.emitted_count != 0 or self.end_reached
        ):
            raise ValueError("an unrequested catalog lane cannot report progress")
        if (self.status == "complete") != self.end_reached:
            raise ValueError("catalog end_reached must exactly match complete status")
        return self


class CatalogCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    main: CatalogLaneCoverage
    archive: CatalogLaneCoverage


class GlobalMessageLaneCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    hypothesis_index: HypothesisIndex
    chat_list: ChatListName
    status: GlobalLaneStatus
    pages_scanned: NonNegativeCount
    hits_seen: NonNegativeCount

    @model_validator(mode="after")
    def validate_progress(self) -> "GlobalMessageLaneCoverage":
        if self.status == "not_started" and (self.pages_scanned != 0 or self.hits_seen != 0):
            raise ValueError("a not_started global lane cannot report progress")
        return self


class GlobalMessageCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    lanes: Annotated[list[GlobalMessageLaneCoverage], Field(min_length=2, max_length=10)]

    @model_validator(mode="after")
    def validate_lane_matrix(self) -> "GlobalMessageCoverage":
        keys = [(lane.hypothesis_index, lane.chat_list) for lane in self.lanes]
        if len(set(keys)) != len(keys):
            raise ValueError("global message lane keys must be unique")
        indexes = sorted({lane.hypothesis_index for lane in self.lanes})
        if indexes != list(range(len(indexes))) or not 2 <= len(indexes) <= 5:
            raise ValueError("global message hypothesis indexes must be contiguous from zero")
        chat_lists = {lane.chat_list for lane in self.lanes}
        expected = {(index, chat_list) for index in indexes for chat_list in chat_lists}
        if set(keys) != expected:
            raise ValueError("global message lanes must form a complete hypothesis/list matrix")
        return self


class DiscoveryCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    complete: Annotated[bool, Field(strict=True)]
    catalog: CatalogCoverage
    global_messages: GlobalMessageCoverage
    hydration: HydrationStatus
    detail: DiscoveryCoverageDetail

    @model_validator(mode="after")
    def validate_coverage(self) -> "DiscoveryCoverage":
        requested_lists = {
            chat_list
            for chat_list in ("main", "archive")
            if getattr(self.catalog, chat_list).status != "not_requested"
        }
        if not requested_lists:
            raise ValueError("discovery must request at least one catalog list")
        global_lists = {lane.chat_list for lane in self.global_messages.lanes}
        if global_lists != requested_lists:
            raise ValueError("catalog and global message coverage must use the same scope")
        fully_complete = (
            all(
                getattr(self.catalog, chat_list).status == "complete"
                and getattr(self.catalog, chat_list).end_reached
                for chat_list in requested_lists
            )
            and all(lane.status == "complete" for lane in self.global_messages.lanes)
            and self.hydration == "complete"
        )
        if self.complete != fully_complete:
            raise ValueError("coverage.complete must exactly represent all requested lanes")
        return self


class TargetDiscoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    status: DiscoveryStatus
    candidates: Annotated[list[DiscoveryCandidate], Field(max_length=25)] = Field(
        default_factory=list
    )
    coverage: DiscoveryCoverage
    next_cursor: DiscoveryCursor | None = None

    @model_validator(mode="after")
    def validate_response(self) -> "TargetDiscoveryResponse":
        usable = self.status in {"page", "partial", "complete"}
        if usable != (self.next_cursor is not None):
            raise ValueError("only usable discovery responses require a cursor")
        if (self.status == "complete") != self.coverage.complete:
            raise ValueError("complete status must exactly match complete coverage")
        if self.status in {"blocked", "error", "expired"} and self.candidates:
            raise ValueError("blocked, error, and expired responses cannot include evidence")
        if len({candidate.chat_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("discovery candidate chat IDs must be unique")
        if sum(len(candidate.message_evidence) for candidate in self.candidates) > 10:
            raise ValueError("a discovery response can include at most 10 message evidence items")

        lanes = self.coverage.global_messages.lanes
        hypothesis_count = len({lane.hypothesis_index for lane in lanes})
        requested_lists = {lane.chat_list for lane in lanes}
        for candidate in self.candidates:
            if not set(candidate.list_membership).issubset(requested_lists):
                raise ValueError("candidate membership must remain within requested scope")
            evidence = [*candidate.metadata_evidence, *candidate.message_evidence]
            if any(item.hypothesis_index >= hypothesis_count for item in evidence):
                raise ValueError("evidence hypothesis index is outside request coverage")
        return self


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram_url: str | None = None
    chat_url: str | None = None
    evidence_anchor: EvidenceAnchor


class FileEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    mime_type: str | None = None
    media_type: str
    size: int | None = None


class ContextEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: int
    date_utc: datetime
    sender: str
    snippet: str


class SearchMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: int
    message_id: int
    date_utc: datetime
    sender: str
    snippet: str
    context: list[ContextEvidence]
    file: FileEvidence | None = None
    source: SourceEvidence


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["matches", "no_match", "incomplete", "blocked", "error"]
    coverage: Coverage
    matches: list[SearchMatch]
