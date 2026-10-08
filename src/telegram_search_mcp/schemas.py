"""Strict public schemas for the bounded MCP surface."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from .sanitize import render_evidence
from .boolean_query import normalize_search_text, boolean_matches, boolean_seeds


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


class AttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    anchor: EvidenceAnchor


class AttachmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    status: Literal["complete", "too_large", "unsupported", "not_found", "blocked", "error", "limit_reached"]
    anchor: EvidenceAnchor
    media_type: Literal["document", "photo", "audio", "voice_note", "video", "video_note"] | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    artifact_id: str | None = None
    artifact_path: str | None = None
    expires_at: datetime | None = None
    coverage: Literal["complete", "none"] = "none"
    detail: str

    @model_validator(mode="after")
    def validate_complete(self) -> "AttachmentResponse":
        if self.status == "complete":
            if (self.coverage != "complete" or self.media_type is None or self.size_bytes is None
                or self.sha256 is None or self.artifact_id is None or self.artifact_path is None
                or self.expires_at is None):
                raise ValueError("complete attachment requires artifact metadata")
        elif self.coverage != "none" or self.artifact_id is not None or self.artifact_path is not None:
            raise ValueError("incomplete attachment cannot advertise an artifact")
        return self


ArtifactId = Annotated[
    str, Field(strict=True, min_length=108, max_length=140,
               pattern=r"^artifact_[0-9a-f]{32}_[0-9a-f]{64}_[0-9]+$")
]


class ReadAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    artifact_id: ArtifactId
    max_chars: Annotated[int, Field(strict=True, ge=1, le=20_000)] = 20_000
    max_pages: Annotated[int, Field(strict=True, ge=1, le=5)] = 5


class AttachmentImage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    artifact_id: ArtifactId
    artifact_path: str
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    page_number: int | None = None


class ReadAttachmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    status: Literal["complete", "partial", "unsupported", "expired", "error"]
    source_anchor: EvidenceAnchor | None = None
    text: str = ""
    processed_bytes: int = 0
    total_bytes: int | None = None
    processed_pages: int = 0
    total_pages: int | None = None
    images: Annotated[list[AttachmentImage], Field(max_length=5)] = Field(default_factory=list)
    coverage_complete: bool = False
    detail: str


class AnalyzeMediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    artifact_id: ArtifactId
    start_seconds: Annotated[float, Field(strict=True, ge=0, le=86400)] = 0.0


class MediaSegment(BaseModel):
    start_seconds: float
    end_seconds: float
    text: str
    uncertain: bool


class MediaFrameResult(BaseModel):
    time_seconds: float
    source: Literal["time", "scene"]
    artifact_id: ArtifactId
    artifact_path: str
    mime_type: Literal["image/jpeg"] = "image/jpeg"


class AnalyzeMediaResponse(BaseModel):
    status: Literal["complete", "partial", "transcription_unavailable", "unsupported", "expired", "error"]
    source_anchor: EvidenceAnchor | None = None
    duration_seconds: float | None = None
    window_start_seconds: float | None = None
    window_end_seconds: float | None = None
    transcription_status: str = "not_attempted"
    transcribed_start_seconds: float | None = None
    transcribed_end_seconds: float | None = None
    language: str | None = None
    segments: Annotated[list[MediaSegment], Field(max_length=500)] = Field(default_factory=list)
    frame_status: str = "not_applicable"
    frames: Annotated[list[MediaFrameResult], Field(max_length=12)] = Field(default_factory=list)
    coverage_complete: bool = False
    detail: str = ""


class CreateLocalArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    file_name: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    content: Annotated[str, Field(strict=True, min_length=1, max_length=500_000)] | None = None
    content_base64: Annotated[str, Field(strict=True, min_length=1, max_length=700_000)] | None = None

    @model_validator(mode="after")
    def exactly_one_content(self) -> "CreateLocalArtifactRequest":
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("exactly one of content or content_base64 is required")
        return self


class CreateLocalArtifactResponse(BaseModel):
    status: Literal["complete", "error"]
    artifact_id: ArtifactId | None = None
    artifact_path: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    file_name: str | None = None
    detail: str


class BeginLocalUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    file_name: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    kind: Literal["document", "photo", "voice_note"]
    size_bytes: Annotated[int, Field(strict=True, ge=1, le=64 * 1024 * 1024)]
    sha256: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class BeginLocalUploadResponse(BaseModel):
    status: Literal["ready", "error"]
    upload_id: str | None = None
    expires_at: datetime | None = None
    detail: str


class AppendLocalUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    upload_id: Annotated[str, Field(strict=True, pattern=r"^upload_[0-9a-f]{32}$")]
    index: Annotated[int, Field(strict=True, ge=0, le=1024)]
    content_base64: Annotated[str, Field(strict=True, min_length=1, max_length=700_000)]
    sha256: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class AppendLocalUploadResponse(BaseModel):
    status: Literal["accepted", "error"]
    upload_id: str | None = None
    next_index: int | None = None
    received_bytes: int | None = None
    detail: str


class FinishLocalUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    upload_id: Annotated[str, Field(strict=True, pattern=r"^upload_[0-9a-f]{32}$")]


class FinishLocalUploadResponse(CreateLocalArtifactResponse):
    pass


class PrepareArtifactSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    artifact_id: ArtifactId
    recipient: ChatId
    display_name: Annotated[str, Field(strict=True, min_length=1, max_length=255)]
    mime_type: Annotated[str, Field(strict=True, min_length=3, max_length=127)]
    caption: Annotated[str, Field(strict=True, max_length=1024)] = ""
    kind: Literal["document", "photo", "voice_note"] = "document"


class PrepareArtifactSendResponse(BaseModel):
    status: Literal["prepared", "blocked", "error"]
    draft_id: str | None = None
    recipient: int | None = None
    recipient_title: str | None = None
    display_name: str | None = None
    mime_type: str | None = None
    caption: str | None = None
    kind: Literal["document", "photo", "voice_note"] | None = None
    duration_seconds: int | None = None
    waveform_base64: str | None = None
    source_sha256: str | None = None
    source_display_name: str | None = None
    converted: bool = False
    sha256: str | None = None
    size_bytes: int | None = None
    expires_at: datetime | None = None
    approval_required: bool = True
    detail: str


class SendPreparedArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    draft_id: Annotated[str, Field(strict=True, pattern=r"^draft_[0-9a-f]{32}$")]
    approved: Annotated[bool, Field(strict=True)]


class SendPreparedArtifactResponse(BaseModel):
    status: Literal["sent", "failed", "outcome_unknown", "not_approved", "expired", "recipient_changed", "error"]
    draft_id: str | None = None
    recipient: int | None = None
    message_id: int | None = None
    detail: str


class PrepareTextSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    recipient: ChatId
    text: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]


class PrepareTextSendResponse(BaseModel):
    status: Literal["prepared", "blocked", "error"]
    draft_id: str | None = None
    recipient: int | None = None
    recipient_title: str | None = None
    text: str | None = None
    sha256: str | None = None
    expires_at: datetime | None = None
    approval_required: bool = True
    detail: str


class SendPreparedTextRequest(SendPreparedArtifactRequest):
    pass


class SendPreparedTextResponse(SendPreparedArtifactResponse):
    pass


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


class MessageContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    anchor: EvidenceAnchor
    radius: Annotated[int, Field(strict=True, ge=0, le=4)] = 2


class MessageContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    status: Literal["complete", "partial", "not_found", "blocked", "error"]
    anchor: EvidenceAnchor
    messages: Annotated[list[ContextEvidence], Field(max_length=9)] = Field(default_factory=list)
    coverage_complete: bool
    detail: str


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


class SelectedMessageAnchor(EvidenceAnchor):
    """One committed provider message, never an outgoing temporary ID."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    message_id: Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]


SelectedMessageAnchors = Annotated[list[SelectedMessageAnchor], Field(min_length=1, max_length=20)]


class ReadMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    anchors: SelectedMessageAnchors

    @model_validator(mode="after")
    def unique_anchors(self) -> "ReadMessagesRequest":
        if len({(a.chat_id, a.message_id) for a in self.anchors}) != len(self.anchors):
            raise ValueError("selected anchors must be unique")
        return self


class SelectedMessageText(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    value: Annotated[str, Field(strict=True, max_length=20000)]
    untrusted: Literal[True] = True
    sanitized: Annotated[bool, Field(strict=True)]
    truncated: Annotated[bool, Field(strict=True)]
    original_characters: Annotated[int, Field(strict=True, ge=0)]


class SelectedMessageSender(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["user", "chat"]
    id: ChatId
    display_name: Annotated[str, Field(strict=True, max_length=256)] | None
    display_name_sanitized: Annotated[bool, Field(strict=True)] = False
    display_name_truncated: Annotated[bool, Field(strict=True)] = False


class SelectedMessageReply(BaseModel):
    """Reply target metadata only; quoted/embedded content is not followed or returned."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["message", "story"]
    chat_id: ChatId | None = None
    message_id: Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)] | None = None
    story_id: Annotated[int, Field(strict=True, gt=0, le=2**31 - 1)] | None = None


class SelectedMessageTopic(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["thread", "forum", "direct_messages", "saved_messages"]
    id: Annotated[int, Field(strict=True, ge=-(2**53 - 1), le=2**53 - 1)]


class SelectedMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    content_kind: Literal["text", "document", "photo", "video", "audio", "animation", "voice_note", "video_note", "sticker"]
    text_role: Literal["text", "caption", "none"]
    text: SelectedMessageText | None
    sender: SelectedMessageSender
    is_outgoing: Annotated[bool, Field(strict=True)]
    date_utc: SearchDateTime | None
    edit_date_utc: SearchDateTime | None
    is_edited: Annotated[bool, Field(strict=True)]
    reply_to: SelectedMessageReply | None
    topic: SelectedMessageTopic | None
    media_album_id: Annotated[str, Field(pattern=r"^-?[0-9]{1,19}$")] | None
    source: SourceEvidence


MessageReadIssue = Literal[
    "text_truncated", "sender_unavailable", "sender_truncated", "reply_unavailable", "topic_unavailable",
    "budget_exhausted", "provider_error", "authorization_unavailable", "secret_chat", "message_unavailable",
    "chat_mismatch", "message_mismatch", "unsupported_content", "invalid_provider_message", "broker_unavailable",
]


class MessageReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    anchor: SelectedMessageAnchor
    status: Literal["complete", "partial", "not_found", "unsupported", "wrong_chat", "blocked", "error"]
    coverage_complete: Annotated[bool, Field(strict=True)]
    message: SelectedMessage | None = None
    issues: Annotated[list[MessageReadIssue], Field(max_length=8)] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_result(self) -> "MessageReadResult":
        if self.coverage_complete != (self.status == "complete"):
            raise ValueError("coverage must agree with per-anchor status")
        if self.status == "complete" and (self.message is None or self.issues):
            raise ValueError("complete selected read requires a message without issues")
        if self.status not in {"complete", "partial"} and self.message is not None:
            raise ValueError("unavailable result must not carry message content")
        if self.message is not None:
            if self.message.source.evidence_anchor != EvidenceAnchor(**self.anchor.model_dump()):
                raise ValueError("message source must match selected anchor")
            if self.message.text is not None and self.message.text.truncated and self.status == "complete":
                raise ValueError("truncated content cannot be complete")
        return self


class ReadMessagesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Literal[1] = 1
    status: Literal["complete", "partial", "blocked", "error"]
    coverage_complete: bool
    results: Annotated[list[MessageReadResult], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def consistent_coverage(self) -> "ReadMessagesResponse":
        complete = all(r.coverage_complete for r in self.results)
        if self.coverage_complete != complete or (self.status == "complete") != complete:
            raise ValueError("batch completeness must agree with every selected result")
        return self


ReplyDepth = Annotated[int, Field(strict=True, ge=1, le=10)]


class ReadReplyChainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    anchor: SelectedMessageAnchor
    max_depth: ReplyDepth = 10


ReplyStopReason = Literal[
    "no_parent", "depth_limit", "cycle", "cross_chat", "story_reply", "reply_unavailable",
    "message_unavailable", "unsupported_content", "invalid_provider_message", "provider_error",
    "authorization_unavailable", "secret_chat", "budget_exhausted", "broker_unavailable",
]


class ReadReplyChainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Literal[1] = 1
    anchor: SelectedMessageAnchor
    max_depth: ReplyDepth
    status: Literal["complete", "partial", "blocked", "error"]
    results: Annotated[list[MessageReadResult], Field(max_length=10)] = Field(default_factory=list)
    chain_complete: Annotated[bool, Field(strict=True)] = False
    coverage_complete: Annotated[bool, Field(strict=True)] = False
    stop_reason: ReplyStopReason
    provider_coverage: Literal["tdlib_observed"] = "tdlib_observed"

    @model_validator(mode="after")
    def consistent_chain(self) -> "ReadReplyChainResponse":
        if len(self.results) > self.max_depth:
            raise ValueError("reply chain exceeds selected depth")
        previous = None
        visited = set()
        text_size = 0
        for result in self.results:
            anchor = result.anchor
            if anchor.chat_id != self.anchor.chat_id or anchor.message_id in visited:
                raise ValueError("reply chain must be unique and stay in the selected chat")
            if previous is None:
                if anchor != self.anchor:
                    raise ValueError("reply chain must start at the selected anchor")
            else:
                pointer = previous.message.reply_to if previous.message else None
                if (pointer is None or pointer.kind != "message" or pointer.chat_id != anchor.chat_id
                        or pointer.message_id != anchor.message_id or "reply_unavailable" in previous.issues):
                    raise ValueError("reply chain must follow hydrated parent references")
            visited.add(anchor.message_id)
            if result.message and result.message.text:
                text_size += len(result.message.text.value)
            previous = result
        if text_size > 100000:
            raise ValueError("reply chain exceeds aggregate text budget")
        pointer = previous.message.reply_to if previous and previous.message else None
        if self.chain_complete != (self.stop_reason == "no_parent"):
            raise ValueError("only an observed absent parent completes a chain")
        if self.chain_complete and (previous is None or previous.message is None or pointer is not None
                                    or "reply_unavailable" in previous.issues):
            raise ValueError("chain completion requires a hydrated absent parent")
        complete = self.chain_complete and all(r.coverage_complete for r in self.results)
        if self.coverage_complete != complete or (self.status == "complete") != complete:
            raise ValueError("content completeness must agree with chain and node coverage")
        if self.stop_reason in {"depth_limit", "cycle", "cross_chat", "story_reply"}:
            if pointer is None:
                raise ValueError("reply stop requires an observed pointer")
            if self.stop_reason == "story_reply":
                valid = pointer.kind == "story"
            else:
                valid = pointer.kind == "message" and pointer.chat_id is not None and pointer.message_id is not None
                if self.stop_reason == "cross_chat":
                    valid = valid and pointer.chat_id != self.anchor.chat_id
                else:
                    valid = valid and pointer.chat_id == self.anchor.chat_id
                    valid = valid and (pointer.message_id in visited if self.stop_reason == "cycle" else
                                       len(self.results) == self.max_depth and pointer.message_id not in visited)
            if not valid:
                raise ValueError("reply stop disagrees with observed pointer")
        return self


HistoryTarget = Annotated[int, Field(strict=True, ge=-(2**53 - 1), le=2**53 - 1),
                          AfterValidator(_reject_zero_chat_id)]
HistoryCursor = Annotated[str, Field(strict=True, pattern=r"^history_[0-9a-f]{64}$", min_length=72, max_length=72)]


class ReadHistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: HistoryTarget
    mode: Literal["latest", "interval"]
    date_from: SearchDateTime | None = None
    date_to: SearchDateTime | None = None
    limit: SearchLimit = 20
    cursor: HistoryCursor | None = None

    @model_validator(mode="after")
    def bounded_scope(self) -> "ReadHistoryRequest":
        if self.mode == "latest":
            if self.date_from is not None or self.date_to is not None:
                raise ValueError("latest freezes its own date boundary and does not accept dates")
        else:
            if self.date_from is None or self.date_to is None or self.date_from >= self.date_to:
                raise ValueError("interval requires date_from < date_to with UTC offsets")
            for key in ("date_from", "date_to"):
                value = getattr(self, key).astimezone(timezone.utc)
                if not 0 <= value.timestamp() <= 2**31:
                    raise ValueError("history dates exceed the supported provider range")
                setattr(self, key, value)
        return self


class HistoryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: HistoryTarget
    mode: Literal["latest", "interval"]
    date_from: SearchDateTime | None
    date_to: SearchDateTime
    upper_message_id: Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)] | None
    order: Literal["message_id_desc"] = "message_id_desc"
    candidate_limit: Literal[100, 1000]
    page_limit: SearchLimit
    expires_at: SearchDateTime

    @model_validator(mode="after")
    def coherent_scope(self) -> "HistoryScope":
        if self.mode == "latest":
            if self.date_from is not None or self.candidate_limit != 100:
                raise ValueError("invalid latest scope")
        elif self.date_from is None or self.date_from >= self.date_to or self.candidate_limit != 1000:
            raise ValueError("invalid interval scope")
        return self


HistoryStopReason = Literal[
    "page_limit", "call_budget_exhausted", "scope_budget_exhausted", "provider_end_unverified",
    "provider_nonprogress", "invalid_provider_page", "provider_error", "authorization_unavailable",
    "secret_chat", "invalid_cursor", "capacity_exhausted", "broker_unavailable",
]


class ReadHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Literal[1] = 1
    status: Literal["page", "partial", "limit_reached", "invalid_cursor", "capacity_exhausted", "blocked", "error"]
    scope: HistoryScope | None = None
    results: Annotated[list[MessageReadResult], Field(max_length=20)] = Field(default_factory=list)
    page_complete: bool = False
    scope_complete: Literal[False] = False
    has_more: Literal[True] | None = None
    next_cursor: HistoryCursor | None = None
    stop_reason: HistoryStopReason
    scanned_candidates: Annotated[int, Field(strict=True, ge=0, le=1000)] = 0
    provider_coverage: Literal["tdlib_observed_unverified"] = "tdlib_observed_unverified"

    @model_validator(mode="after")
    def consistent_history(self) -> "ReadHistoryResponse":
        if (self.next_cursor is not None) != (self.status == "page"):
            raise ValueError("only a continuing page carries a cursor")
        if self.has_more is not None and self.next_cursor is None:
            raise ValueError("known pending candidates require continuation")
        if self.scope is None:
            if self.results or self.page_complete or self.next_cursor:
                raise ValueError("history evidence requires an established scope")
            return self
        scope = self.scope
        if len(self.results) > scope.page_limit or self.scanned_candidates > scope.candidate_limit:
            raise ValueError("history exceeded the frozen limits")
        if self.page_complete != (len(self.results) == scope.page_limit and all(r.coverage_complete for r in self.results)):
            raise ValueError("page completeness must agree with per-anchor outcomes")
        previous = None
        for result in self.results:
            mid = result.anchor.message_id
            if (result.anchor.chat_id != scope.target or scope.upper_message_id is None or
                    mid > scope.upper_message_id or (previous is not None and mid >= previous)):
                raise ValueError("history anchors must be ordered inside the frozen chat/ID scope")
            previous = mid
            if result.message is not None:
                date = result.message.date_utc
                if (date is None or date.tzinfo is None or date.utcoffset() is None or
                        date >= scope.date_to or (scope.date_from is not None and date < scope.date_from)):
                    raise ValueError("history message is outside the half-open date scope")
        return self


ForumTopicId = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
TopicCursor = Annotated[str, Field(strict=True, pattern=r"^topic_[0-9a-f]{64}$", min_length=70, max_length=70)]


class ListTopicsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: HistoryTarget
    limit: SearchLimit = 20
    cursor: TopicCursor | None = None


class ForumTopicReference(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["forum"] = "forum"
    id: ForumTopicId


class ForumTopicName(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    value: Annotated[str, Field(strict=True, max_length=256)]
    untrusted: Annotated[bool, Field(strict=True, json_schema_extra={"const": True})] = True
    sanitized: Annotated[bool, Field(strict=True)]
    truncated: Annotated[bool, Field(strict=True)]

    @model_validator(mode="after")
    def untrusted_name(self) -> "ForumTopicName":
        if not self.untrusted:
            raise ValueError("Telegram topic names are untrusted")
        return self


class ForumTopicMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: ForumTopicName
    creation_date_utc: SearchDateTime | None
    is_general: Annotated[bool, Field(strict=True)]
    is_closed: Annotated[bool, Field(strict=True)]
    is_hidden: Annotated[bool, Field(strict=True)]


TopicReadIssue = Literal["name_truncated", "name_unavailable", "creation_date_unavailable", "topic_unavailable",
                         "invalid_provider_topic", "provider_error", "authorization_unavailable", "budget_exhausted"]


class ForumTopicResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    chat_id: HistoryTarget
    topic: ForumTopicReference
    status: Literal["complete", "partial", "not_found", "error"]
    metadata: ForumTopicMetadata | None = None
    coverage_complete: Annotated[bool, Field(strict=True)] = False
    issues: Annotated[list[TopicReadIssue], Field(max_length=8)] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_topic(self) -> "ForumTopicResult":
        if self.coverage_complete != (self.status == "complete"):
            raise ValueError("topic completeness must agree with status")
        if self.status == "complete" and (self.metadata is None or self.issues):
            raise ValueError("complete topic requires metadata without issues")
        if self.status not in {"complete", "partial"} and self.metadata is not None:
            raise ValueError("unavailable topic must not carry metadata")
        if self.metadata is not None and self.status == "complete":
            if self.metadata.name.truncated or not self.metadata.name.value or self.metadata.creation_date_utc is None:
                raise ValueError("incomplete topic metadata cannot be complete")
        return self


class TopicScope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: HistoryTarget
    limit: SearchLimit
    candidate_limit: Literal[200] = 200
    provider_page_limit: Literal[10] = 10
    expires_at: SearchDateTime


TopicStopReason = Literal[
    "page_limit", "provider_nonprogress", "provider_end_unverified", "scope_budget_exhausted",
    "invalid_provider_page", "provider_error", "authorization_unavailable", "unsupported_forum", "secret_chat",
    "invalid_cursor", "capacity_exhausted", "broker_unavailable", "call_budget_exhausted",
]


class ListTopicsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Literal[1] = 1
    status: Literal["page", "partial", "limit_reached", "invalid_cursor", "capacity_exhausted", "unsupported", "blocked", "error"]
    scope: TopicScope | None = None
    results: Annotated[list[ForumTopicResult], Field(max_length=20)] = Field(default_factory=list)
    page_complete: Annotated[bool, Field(strict=True)] = False
    scope_complete: Annotated[bool, Field(strict=True, json_schema_extra={"const": False})] = False
    has_more: None = None
    next_cursor: TopicCursor | None = None
    stop_reason: TopicStopReason
    scanned_candidates: Annotated[int, Field(strict=True, ge=0, le=200)] = 0
    provider_pages: Annotated[int, Field(strict=True, ge=0, le=10)] = 0
    provider_coverage: Literal["tdlib_observed_unverified"] = "tdlib_observed_unverified"

    @model_validator(mode="after")
    def consistent_topics(self) -> "ListTopicsResponse":
        if self.scope_complete:
            raise ValueError("bounded forum listing cannot prove inventory completeness")
        if (self.next_cursor is not None) != (self.status == "page"):
            raise ValueError("only a continuing page carries a cursor")
        if self.scope is None:
            if self.results or self.page_complete or self.next_cursor or self.scanned_candidates or self.provider_pages:
                raise ValueError("topic evidence requires an established scope")
            return self
        if (len(self.results) > self.scope.limit or len(self.results) > self.scanned_candidates or
                (self.scanned_candidates and not self.provider_pages)):
            raise ValueError("topic outcomes exceed observed candidates or requested limit")
        if self.page_complete != (bool(self.results) and all(r.coverage_complete for r in self.results)):
            raise ValueError("page completeness must agree with observed topic outcomes")
        seen = set()
        for result in self.results:
            if result.chat_id != self.scope.target or result.topic.id in seen:
                raise ValueError("topics must be unique inside the selected chat")
            seen.add(result.topic.id)
        if self.next_cursor is not None and (not self.results or not self.provider_pages or
                ((self.provider_pages >= 10 or self.scanned_candidates >= 200) and
                 len(self.results) >= self.scanned_candidates)):
            raise ValueError("continuation requires outcomes and possible pending observations")
        return self


TopicHistoryCursor = Annotated[str, Field(strict=True, pattern=r"^topic_history_[0-9a-f]{64}$", min_length=78, max_length=78)]


class ReadTopicHistoryRequest(ReadHistoryRequest):
    topic: ForumTopicReference
    cursor: TopicHistoryCursor | None = None


class TopicHistoryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: HistoryTarget
    topic: ForumTopicReference
    mode: Literal["latest", "interval"]
    date_from: SearchDateTime | None
    date_to: SearchDateTime
    upper_message_id: Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)] | None
    order: Literal["message_id_desc"] = "message_id_desc"
    candidate_limit: Annotated[int, Field(strict=True, ge=200, le=200)] = 200
    provider_page_limit: Annotated[int, Field(strict=True, ge=10, le=10)] = 10
    page_limit: SearchLimit
    expires_at: SearchDateTime

    @model_validator(mode="after")
    def coherent_scope(self) -> "TopicHistoryScope":
        if self.mode == "latest":
            if self.date_from is not None:
                raise ValueError("latest topic history scope cannot accept a lower date")
        elif self.date_from is None or self.date_from >= self.date_to:
            raise ValueError("interval topic history requires half-open date bounds")
        for value in (self.date_from, self.date_to):
            if value is not None and not 0 <= value.timestamp() <= 2**31:
                raise ValueError("topic history dates exceed the supported provider range")
        return self


TopicHistoryStopReason = Literal[
    "page_limit", "call_budget_exhausted", "scope_budget_exhausted", "provider_end_unverified",
    "provider_nonprogress", "invalid_provider_page", "invalid_provider_topic", "topic_unavailable",
    "topic_mismatch", "provider_error", "authorization_unavailable", "unsupported_forum", "secret_chat",
    "invalid_cursor", "capacity_exhausted", "broker_unavailable",
]


class ReadTopicHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Literal[1] = 1
    status: Literal["page", "partial", "limit_reached", "invalid_cursor", "capacity_exhausted", "unsupported", "blocked", "error"]
    scope: TopicHistoryScope | None = None
    topic: ForumTopicResult | None = None
    results: Annotated[list[MessageReadResult], Field(max_length=20)] = Field(default_factory=list)
    page_complete: Annotated[bool, Field(strict=True)] = False
    scope_complete: Annotated[bool, Field(strict=True, json_schema_extra={"const": False})] = False
    has_more: None = None
    next_cursor: TopicHistoryCursor | None = None
    stop_reason: TopicHistoryStopReason
    scanned_candidates: Annotated[int, Field(strict=True, ge=0, le=200)] = 0
    processed_candidates: Annotated[int, Field(strict=True, ge=0, le=200)] = 0
    provider_pages: Annotated[int, Field(strict=True, ge=0, le=10)] = 0
    provider_coverage: Literal["tdlib_observed_unverified"] = "tdlib_observed_unverified"

    @model_validator(mode="after")
    def consistent_topic_history(self) -> "ReadTopicHistoryResponse":
        if self.scope_complete or (self.next_cursor is not None) != (self.status == "page"):
            raise ValueError("bounded topic history cannot prove completion or resume a terminal outcome")
        if self.processed_candidates > self.scanned_candidates:
            raise ValueError("processed topic candidates cannot exceed observations")
        if self.scope is None:
            if (self.topic or self.results or self.page_complete or self.next_cursor or self.scanned_candidates or
                    self.processed_candidates or self.provider_pages):
                raise ValueError("topic history evidence requires an established scope")
            return self
        scope = self.scope
        if self.topic is not None and (self.topic.chat_id != scope.target or self.topic.topic != scope.topic):
            raise ValueError("current topic metadata differs from history scope")
        if self.results and (self.topic is None or self.topic.status not in {"complete", "partial"}):
            raise ValueError("topic history bodies require current available topic metadata")
        if (len(self.results) > scope.page_limit or len(self.results) > self.processed_candidates or
                (self.scanned_candidates and not self.provider_pages)):
            raise ValueError("topic history outcomes exceed processed provider evidence")
        if self.page_complete != (bool(self.results) and all(r.coverage_complete for r in self.results)):
            raise ValueError("page completeness must agree with every returned outcome")
        previous = None
        characters = 0
        for result in self.results:
            mid = result.anchor.message_id
            if (result.anchor.chat_id != scope.target or scope.upper_message_id is None or mid > scope.upper_message_id or
                    (previous is not None and mid >= previous)):
                raise ValueError("topic history anchors must descend inside the frozen chat and head")
            previous = mid
            if result.message is not None:
                value = result.message
                if value.topic is None or value.topic.kind != "forum" or value.topic.id != scope.topic.id:
                    raise ValueError("topic history body lacks exact typed forum membership")
                if (value.date_utc is None or value.date_utc >= scope.date_to or
                        (scope.date_from is not None and value.date_utc < scope.date_from)):
                    raise ValueError("topic history body is outside the half-open date scope")
                characters += len(value.text.value) if value.text else 0
        if characters > 100000:
            raise ValueError("topic history text exceeds call limit")
        if self.next_cursor and (not self.processed_candidates or not self.provider_pages or
                ((self.scanned_candidates >= 200 or self.provider_pages >= 10) and
                 self.processed_candidates >= self.scanned_candidates)):
            raise ValueError("topic history continuation requires progress and possible pending observations")
        return self


def _strip_search_text(value: object) -> str:
    if type(value) is not str:
        raise ValueError("search text must be a string")
    value = value.strip()
    if (not value or len(value) > 512 or "*" in value or
            not any(not c.isspace() and not unicodedata.category(c).startswith("C") for c in value)):
        raise ValueError("search text must contain bounded non-wildcard lexical content")
    return value


SearchText = Annotated[str, BeforeValidator(_strip_search_text), Field(strict=True, min_length=1, max_length=512)]
SearchCursor = Annotated[str, Field(strict=True, pattern=r"^search_[0-9a-f]{64}$", min_length=71, max_length=71)]


class SearchSenderReference(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["user", "chat"]
    id: Annotated[int, Field(strict=True, ge=-(2**53 - 1), le=2**53 - 1)]

    @model_validator(mode="after")
    def valid_sender_id(self) -> "SearchSenderReference":
        if self.id == 0 or (self.kind == "user" and self.id < 0):
            raise ValueError("sender requires a positive user or nonzero chat identifier")
        return self


class BooleanSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    all: Annotated[list[SearchText], Field(strict=True, max_length=4)] = Field(default_factory=list)
    any: Annotated[list[SearchText], Field(strict=True, max_length=4)] = Field(default_factory=list)
    none: Annotated[list[SearchText], Field(strict=True, max_length=4)] = Field(default_factory=list)

    @model_validator(mode="after")
    def bounded_positive_query(self) -> "BooleanSearchQuery":
        if not (self.all or self.any) or len(self.all) + len(self.any) + len(self.none) > 8:
            raise ValueError("Boolean search requires positive terms and at most eight terms")
        for terms in (self.all, self.any, self.none):
            normalized = [normalize_search_text(term) for term in terms]
            if any(not term for term in normalized) or len(set(normalized)) != len(normalized):
                raise ValueError("Boolean terms must be nonempty and distinct after local normalization")
        return self


ExactSearchQuery = SearchText | BooleanSearchQuery
SearchMatchingSemantics = Literal["tdlib_lexical", "local_nfkc_casefold_whitespace_substring"]


class SearchMessagesRequest(ReadHistoryRequest):
    query: ExactSearchQuery
    cursor: SearchCursor | None = None
    sender: SearchSenderReference | None = None
    direction: Literal["incoming", "outgoing"] | None = None
    topic: ForumTopicReference | None = None


class SearchMessagesScope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: HistoryTarget
    query: ExactSearchQuery
    matching_semantics: SearchMatchingSemantics = "tdlib_lexical"
    sender: SearchSenderReference | None = None
    direction: Literal["incoming", "outgoing"] | None = None
    topic: ForumTopicReference | None = None
    mode: Literal["latest", "interval"]
    date_from: SearchDateTime | None
    date_to: SearchDateTime
    upper_message_id: Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)] | None
    order: Literal["message_id_desc"] = "message_id_desc"
    candidate_limit: Annotated[int, Field(strict=True, ge=200, le=200)] = 200
    provider_page_limit: Annotated[int, Field(strict=True, ge=10, le=10)] = 10
    page_limit: SearchLimit
    expires_at: SearchDateTime

    @model_validator(mode="after")
    def coherent_scope(self) -> "SearchMessagesScope":
        semantics = "local_nfkc_casefold_whitespace_substring" if isinstance(self.query, BooleanSearchQuery) else "tdlib_lexical"
        if "matching_semantics" not in self.model_fields_set:
            self.matching_semantics = semantics
        elif self.matching_semantics != semantics:
            raise ValueError("matching semantics must agree with query variant")
        if self.mode == "latest":
            if self.date_from is not None:
                raise ValueError("latest search scope cannot accept a lower date")
        elif self.date_from is None or self.date_from >= self.date_to:
            raise ValueError("interval search requires half-open date bounds")
        for name in ("date_from", "date_to", "expires_at"):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, value.astimezone(timezone.utc))
        for value in (self.date_from, self.date_to):
            if value is not None and not 0 <= value.timestamp() <= 2**31:
                raise ValueError("search dates exceed the supported provider range")
        return self


SearchMessageIssue = MessageReadIssue | Literal["evidence_changed"]


class SearchMessageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    anchor: SelectedMessageAnchor
    status: Literal["match", "partial", "evidence_changed", "not_found", "unsupported"]
    coverage_complete: Annotated[bool, Field(strict=True)]
    message: SelectedMessage | None = None
    issues: Annotated[list[SearchMessageIssue], Field(max_length=9)] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_result(self) -> "SearchMessageResult":
        if self.coverage_complete != (self.status == "match"):
            raise ValueError("search coverage must agree with the per-anchor status")
        if self.status == "match" and (self.message is None or self.issues):
            raise ValueError("search match requires a message without issues")
        if self.status == "partial" and (self.message is None or not self.issues):
            raise ValueError("partial search evidence requires a message and issues")
        if self.status not in {"match", "partial"} and self.message is not None:
            raise ValueError("unavailable or changed search evidence cannot carry content")
        if self.message is not None:
            lexical = self.message
            valid_role = (lexical.content_kind == "text" and lexical.text_role == "text") or (
                lexical.content_kind in {"document", "photo", "video", "audio", "animation", "voice_note"}
                and lexical.text_role == "caption")
            if lexical.text is None or not valid_role:
                raise ValueError("search evidence requires supported text or caption content")
            if self.message.source.evidence_anchor != EvidenceAnchor(**self.anchor.model_dump()):
                raise ValueError("search message source must match the selected anchor")
            if self.message.text is not None and self.message.text.truncated and self.status == "match":
                raise ValueError("truncated search content cannot be complete")
        return self


SearchMessagesStopReason = HistoryStopReason | Literal[
    "unsupported_forum", "topic_unavailable", "invalid_provider_topic",
]


class SearchBranchCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    index: Annotated[int, Field(strict=True, ge=0, le=3)]
    query: SearchText
    state: Literal["active", "provider_end_unverified", "provider_nonprogress", "scope_budget_exhausted", "interrupted"]
    scanned_candidates: Annotated[int, Field(strict=True, ge=0, le=200)]
    processed_candidates: Annotated[int, Field(strict=True, ge=0, le=200)]
    provider_pages: Annotated[int, Field(strict=True, ge=0, le=10)]

    @model_validator(mode="after")
    def bounded_observations(self) -> "SearchBranchCoverage":
        if self.processed_candidates > self.scanned_candidates or self.scanned_candidates > 20 * self.provider_pages:
            raise ValueError("branch counters exceed native observations")
        if self.state in {"provider_end_unverified", "provider_nonprogress"} and not self.provider_pages:
            raise ValueError("provider stop requires a native attempt")
        return self


class SearchMessagesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Annotated[int, Field(strict=True, ge=1, le=1, json_schema_extra={"const": 1})] = 1
    status: Literal["page", "partial", "limit_reached", "invalid_cursor", "capacity_exhausted", "unsupported", "blocked", "error"]
    scope: SearchMessagesScope | None = None
    results: Annotated[list[SearchMessageResult], Field(max_length=20)] = Field(default_factory=list)
    page_complete: Annotated[bool, Field(strict=True)] = False
    scope_complete: Annotated[bool, Field(strict=True, json_schema_extra={"const": False})] = False
    has_more: None = None
    next_cursor: SearchCursor | None = None
    stop_reason: SearchMessagesStopReason
    scanned_candidates: Annotated[int, Field(strict=True, ge=0, le=200)] = 0
    processed_candidates: Annotated[int, Field(strict=True, ge=0, le=200)] = 0
    provider_pages: Annotated[int, Field(strict=True, ge=0, le=10)] = 0
    provider_coverage: Literal["tdlib_lexical_unverified"] = "tdlib_lexical_unverified"
    branch_coverage: Annotated[list[SearchBranchCoverage], Field(max_length=4)] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_search(self) -> "SearchMessagesResponse":
        if self.scope_complete or (self.next_cursor is not None) != (self.status == "page"):
            raise ValueError("bounded lexical search cannot prove completion or resume a terminal outcome")
        if self.processed_candidates > self.scanned_candidates or self.scanned_candidates > 20 * self.provider_pages:
            raise ValueError("search counters exceed observed native page evidence")
        if self.scope is None:
            if (self.results or self.page_complete or self.next_cursor or self.scanned_candidates or
                    self.processed_candidates or self.provider_pages or self.branch_coverage):
                raise ValueError("search evidence requires an established scope")
            return self
        scope = self.scope
        if isinstance(scope.query, BooleanSearchQuery):
            seeds = boolean_seeds(scope.query)
            if len(self.branch_coverage) != len(seeds):
                raise ValueError("Boolean search requires coverage for every seed")
            for index, (branch, seed) in enumerate(zip(self.branch_coverage, seeds)):
                if branch.index != index or branch.query != seed:
                    raise ValueError("Boolean branch mapping differs from query")
                if self.next_cursor and branch.state in {"scope_budget_exhausted", "interrupted"}:
                    raise ValueError("closed branch cannot resume")
                if not self.next_cursor and branch.state == "active":
                    raise ValueError("terminal Boolean search must close active branches")
                if self.results and not branch.provider_pages:
                    raise ValueError("Boolean results require every seed to be primed")
            for name in ("scanned_candidates", "processed_candidates", "provider_pages"):
                if sum(getattr(branch, name) for branch in self.branch_coverage) != getattr(self, name):
                    raise ValueError("Boolean counters must equal branch sums")
        elif self.branch_coverage:
            raise ValueError("lexical string search cannot carry Boolean branches")
        if len(self.results) > scope.page_limit or len(self.results) > self.processed_candidates:
            raise ValueError("search outcomes exceed processed provider evidence")
        if self.page_complete != (bool(self.results) and all(r.coverage_complete for r in self.results)):
            raise ValueError("page completeness must agree with every returned outcome")
        previous = None
        characters = 0
        for result in self.results:
            mid = result.anchor.message_id
            if (result.anchor.chat_id != scope.target or scope.upper_message_id is None or mid > scope.upper_message_id or
                    (previous is not None and mid >= previous)):
                raise ValueError("search anchors must descend inside the frozen chat and head")
            previous = mid
            if result.message is not None:
                value = result.message
                if (value.date_utc is None or value.date_utc >= scope.date_to or
                        (scope.date_from is not None and value.date_utc < scope.date_from)):
                    raise ValueError("search body is outside the half-open date scope")
                if scope.sender is not None and (value.sender.kind != scope.sender.kind or value.sender.id != scope.sender.id):
                    raise ValueError("search body is outside the typed sender scope")
                if scope.direction is not None and value.is_outgoing != (scope.direction == "outgoing"):
                    raise ValueError("search body is outside the typed direction scope")
                if scope.topic is not None and (value.topic is None or value.topic.kind != "forum" or value.topic.id != scope.topic.id):
                    raise ValueError("search body is outside the typed forum scope")
                if (isinstance(scope.query, BooleanSearchQuery) and value.text is not None
                        and not value.text.truncated and not value.text.sanitized
                        and not boolean_matches(scope.query, value.text.value)):
                    raise ValueError("visible full search body violates Boolean query")
                characters += len(value.text.value) if value.text else 0
        if characters > 100000:
            raise ValueError("search text exceeds the call limit")
        if self.next_cursor and (not self.provider_pages or
                ((self.scanned_candidates >= 200 or self.provider_pages >= 10) and
                 self.processed_candidates >= self.scanned_candidates)):
            raise ValueError("search continuation requires native progress or pending observations")
        return self


# F7a is deliberately separate from discovery/catalog accumulation.
ChatListSelection = Literal["main", "archive", "both"]
ChatListName = Literal["main", "archive"]
ChatListCursor = Annotated[str, Field(strict=True, pattern=r"^chats_[0-9a-f]{64}$", min_length=70, max_length=70)]
ChatListCount = Annotated[int, Field(strict=True, ge=0, le=200)]
UnreadCount = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]


class ListChatsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    scope: ChatListSelection
    cursor: ChatListCursor | None = None
    limit: SearchLimit = 20


class ChatListScope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    selection: ChatListSelection
    selected_lists: Annotated[list[ChatListName], Field(min_length=1, max_length=2)]
    limit: SearchLimit
    prefix_limit_per_list: Annotated[int, Field(strict=True, ge=100, le=200)]
    expires_at: SearchDateTime

    @model_validator(mode="after")
    def selected_scope(self) -> "ChatListScope":
        selected = ["main", "archive"] if self.selection == "both" else [self.selection]
        if self.selected_lists != selected or self.prefix_limit_per_list != (100 if self.selection == "both" else 200):
            raise ValueError("chat listing scope must preserve selected list order and prefix bounds")
        return self


class ChatListIssues(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    duplicate_observation: ChatListCount = 0
    identity_mismatch: ChatListCount = 0
    secret: ChatListCount = 0
    invalid_metadata: ChatListCount = 0
    out_of_scope: ChatListCount = 0
    unavailable: ChatListCount = 0
    provider_error: ChatListCount = 0
    authorization_unavailable: ChatListCount = 0
    deadline: ChatListCount = 0


class ChatListCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    chat_list: ChatListName
    native_state: Literal["not_attempted", "observed", "invalid", "unavailable"] = "not_attempted"
    observed: ChatListCount = 0
    processed: ChatListCount = 0
    returned: ChatListCount = 0
    omitted: ChatListCount = 0
    pending: ChatListCount = Field(default=0, description="Unprocessed selected observations; terminal results offer no continuation.")
    issues: ChatListIssues = Field(default_factory=ChatListIssues)

    @model_validator(mode="after")
    def coverage_counts(self) -> "ChatListCoverage":
        if (self.processed != self.returned + self.omitted or self.observed != self.processed + self.pending or
                self.omitted != sum(self.issues.model_dump().values()) or
                (self.native_state != "observed" and self.observed != 0)):
            raise ValueError("chat list coverage counters are inconsistent")
        return self


class ChatListResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    chat_id: HistoryTarget
    kind: Literal["private", "basic_group", "supergroup", "channel"]
    title: ForumTopicName
    observed_list: ChatListName
    observed_rank: Annotated[int, Field(strict=True, ge=1, le=200)]
    current_lists: Annotated[list[ChatListName], Field(min_length=1, max_length=2)]
    unread_count: UnreadCount
    unread_mention_count: UnreadCount
    unread_reaction_count: UnreadCount
    is_marked_as_unread: Annotated[bool, Field(strict=True)]
    hydrated_at: SearchDateTime

    @model_validator(mode="after")
    def safe_metadata(self) -> "ChatListResult":
        from .sanitize import sanitize_telegram_text
        if self.current_lists != [name for name in ("main", "archive") if name in self.current_lists]:
            raise ValueError("current requested membership must be unique and ordered")
        if sanitize_telegram_text(self.title.value, max_length=256) != self.title.value:
            raise ValueError("chat title must already be sanitized")
        return self


ChatListStopReason = Literal["page_limit", "prefix_exhausted_unverified", "invalid_provider_prefix",
    "provider_error", "authorization_unavailable", "call_budget_exhausted", "invalid_cursor",
    "capacity_exhausted", "broker_unavailable"]


class ListChatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Annotated[int, Field(strict=True, ge=1, le=1, json_schema_extra={"const": 1})] = 1
    status: Literal["page", "prefix_exhausted_unverified", "partial", "error", "blocked", "invalid_cursor", "capacity_exhausted"]
    scope: ChatListScope | None = None
    results: Annotated[list[ChatListResult], Field(max_length=20)] = Field(default_factory=list)
    list_coverage: Annotated[list[ChatListCoverage], Field(max_length=2)] = Field(default_factory=list)
    observed_candidates: ChatListCount = 0
    processed_candidates: ChatListCount = 0
    returned_candidates: ChatListCount = 0
    omitted_candidates: ChatListCount = 0
    pending_candidates: ChatListCount = 0
    processed_this_page: Annotated[int, Field(strict=True, ge=0, le=20)] = 0
    returned_this_page: Annotated[int, Field(strict=True, ge=0, le=20)] = 0
    omitted_this_page: Annotated[int, Field(strict=True, ge=0, le=20)] = 0
    snapshot_complete: Annotated[bool, Field(strict=True)] = False
    page_complete: Annotated[bool, Field(strict=True)] = False
    scope_complete: Annotated[bool, Field(strict=True, json_schema_extra={"const": False})] = False
    has_more: None = None
    next_cursor: ChatListCursor | None = None
    stop_reason: ChatListStopReason
    provider_coverage: Literal["bounded_tdlib_prefix_observation"] = "bounded_tdlib_prefix_observation"
    metadata_freshness: Literal["tdlib_local_state_observation"] = "tdlib_local_state_observation"

    @model_validator(mode="after")
    def bounded_chat_listing(self) -> "ListChatsResponse":
        reasons = {
            "page": {"page_limit"}, "prefix_exhausted_unverified": {"prefix_exhausted_unverified"},
            "partial": {"call_budget_exhausted", "invalid_cursor"},
            "error": {"provider_error", "invalid_provider_prefix", "broker_unavailable"},
            "blocked": {"authorization_unavailable"}, "invalid_cursor": {"invalid_cursor"},
            "capacity_exhausted": {"capacity_exhausted"},
        }
        if self.stop_reason not in reasons[self.status]:
            raise ValueError("chat listing terminal reason differs from its status")
        if self.scope_complete or (self.next_cursor is not None) != (self.status == "page"):
            raise ValueError("chat listing cannot claim inventory completion or inconsistent continuation")
        counters = (("observed_candidates", "observed"), ("processed_candidates", "processed"),
                    ("returned_candidates", "returned"), ("omitted_candidates", "omitted"), ("pending_candidates", "pending"))
        if any(getattr(self, global_name) != sum(getattr(c, local_name) for c in self.list_coverage)
               for global_name, local_name in counters):
            raise ValueError("chat listing global counters must be per-list sums")
        if (self.returned_this_page != len(self.results) or
                self.processed_this_page != self.returned_this_page + self.omitted_this_page or
                self.processed_this_page > self.processed_candidates or self.returned_this_page > self.returned_candidates or
                self.omitted_this_page > self.omitted_candidates):
            raise ValueError("chat listing page counters are inconsistent")
        if self.scope is None:
            if (self.list_coverage or self.results or self.processed_this_page or self.page_complete or
                    self.snapshot_complete or self.next_cursor):
                raise ValueError("unscoped terminal result cannot carry observations")
            if self.status not in {"error", "blocked", "invalid_cursor", "capacity_exhausted"}:
                raise ValueError("unscoped listing must be terminal")
            return self
        scope = self.scope
        if ([c.chat_list for c in self.list_coverage] != scope.selected_lists or
                any(c.observed > scope.prefix_limit_per_list for c in self.list_coverage) or
                self.snapshot_complete != all(c.native_state == "observed" for c in self.list_coverage) or
                self.processed_this_page > scope.limit):
            raise ValueError("chat listing observation scope differs")
        expected_complete = (self.processed_this_page > 0 and self.omitted_this_page == 0 and
                             self.status in {"page", "prefix_exhausted_unverified"})
        if self.page_complete != expected_complete:
            raise ValueError("page completeness covers only successful metadata processing")
        if self.next_cursor and (not self.snapshot_complete or not self.pending_candidates or
                                 self.processed_this_page != scope.limit or self.stop_reason != "page_limit"):
            raise ValueError("continuation requires a frozen prefix and bounded processing progress")
        if self.status == "prefix_exhausted_unverified" and (not self.snapshot_complete or self.pending_candidates or
                                                            self.stop_reason != "prefix_exhausted_unverified"):
            raise ValueError("prefix exhaustion requires a fully processed observed prefix")
        if self.status in {"invalid_cursor", "capacity_exhausted"}:
            raise ValueError("cursor refusal cannot carry a scope")
        if any(sum(row.observed_list == coverage.chat_list for row in self.results) > coverage.returned
               for coverage in self.list_coverage):
            raise ValueError("chat listing rows exceed their observed list's returned metadata")
        seen: set[int] = set()
        last = (-1, 0)
        for row in self.results:
            if row.observed_list not in scope.selected_lists or any(x not in scope.selected_lists for x in row.current_lists):
                raise ValueError("chat listing row is outside selected lists")
            lane = scope.selected_lists.index(row.observed_list)
            coverage = self.list_coverage[lane]
            position = (lane, row.observed_rank)
            if row.chat_id in seen or position <= last or row.observed_rank > coverage.processed:
                raise ValueError("chat listing row duplicates or violates native observation order")
            if row.hydrated_at > scope.expires_at:
                raise ValueError("chat metadata observation is outside scope lifetime")
            seen.add(row.chat_id); last = position
        return self


SelectedSearchTargets = Annotated[list[HistoryTarget], Field(strict=True, min_length=1, max_length=5)]
SelectedSearchCursor = Annotated[str, Field(strict=True, pattern=r"^selected_search_[0-9a-f]{64}$",
                                          min_length=80, max_length=80)]
SelectedSearchStopReason = SearchMessagesStopReason | Literal["invalid_provider_chat", "account_changed"]
SelectedSearchStatus = Literal["page", "partial", "limit_reached", "invalid_cursor", "capacity_exhausted", "unsupported", "blocked", "error"]


class SearchChatsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    targets: SelectedSearchTargets
    query: ExactSearchQuery
    mode: Literal["latest", "interval"]
    date_from: SearchDateTime | None = None
    date_to: SearchDateTime | None = None
    sender: SearchSenderReference | None = None
    direction: Literal["incoming", "outgoing"] | None = None
    limit: SearchLimit = 20
    cursor: SelectedSearchCursor | None = None

    @model_validator(mode="after")
    def exact_selection(self) -> "SearchChatsRequest":
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("selected chats must be unique and ordered")
        child = SearchMessagesRequest(target=self.targets[0], **self.model_dump(exclude={"targets", "cursor"}))
        self.date_from, self.date_to = child.date_from, child.date_to
        return self


class SearchChatsScope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    targets: SelectedSearchTargets
    query: ExactSearchQuery
    matching_semantics: SearchMatchingSemantics = "tdlib_lexical"
    sender: SearchSenderReference | None = None
    direction: Literal["incoming", "outgoing"] | None = None
    mode: Literal["latest", "interval"]
    date_from: SearchDateTime | None
    date_to: SearchDateTime
    order: Literal["selected_chat_then_message_id_desc"] = "selected_chat_then_message_id_desc"
    candidate_limit: Annotated[int, Field(strict=True, ge=200, le=200)] = 200
    provider_page_limit: Annotated[int, Field(strict=True, ge=10, le=10)] = 10
    page_limit: SearchLimit
    expires_at: SearchDateTime

    @model_validator(mode="after")
    def frozen_selection(self) -> "SearchChatsScope":
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("selected chats must be unique")
        data = self.model_dump(exclude={"targets", "order"})
        if "matching_semantics" not in self.model_fields_set:
            data.pop("matching_semantics")
        child = SearchMessagesScope(target=self.targets[0], upper_message_id=None, **data)
        self.matching_semantics = child.matching_semantics
        self.date_from, self.date_to, self.expires_at = child.date_from, child.date_to, child.expires_at
        return self


class SelectedChatCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: HistoryTarget
    state: Literal["pending", "active", "stopped"] = "pending"
    status: SelectedSearchStatus | None = None
    stop_reason: SelectedSearchStopReason | None = None
    upper_message_id: Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)] | None = None
    scanned_candidates: Annotated[int, Field(strict=True, ge=0, le=200)] = 0
    processed_candidates: Annotated[int, Field(strict=True, ge=0, le=200)] = 0
    provider_pages: Annotated[int, Field(strict=True, ge=0, le=10)] = 0
    branch_coverage: Annotated[list[SearchBranchCoverage], Field(max_length=4)] = Field(default_factory=list)

    @model_validator(mode="after")
    def honest_lane(self) -> "SelectedChatCoverage":
        if self.processed_candidates > self.scanned_candidates or self.scanned_candidates > 20 * self.provider_pages:
            raise ValueError("selected chat counters exceed native observations")
        if self.state == "pending":
            if (self.status or self.stop_reason or self.upper_message_id or self.scanned_candidates or
                    self.processed_candidates or self.provider_pages or self.branch_coverage):
                raise ValueError("unprocessed selected chat cannot carry evidence")
        elif self.status is None or self.stop_reason is None or (self.status == "page") != (self.state == "active"):
            raise ValueError("selected chat state must agree with its local status")
        return self


class SearchChatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Annotated[int, Field(strict=True, ge=1, le=1, json_schema_extra={"const": 1})] = 1
    status: SelectedSearchStatus
    scope: SearchChatsScope | None = None
    current_index: Annotated[int, Field(strict=True, ge=0, le=4)] | None = None
    coverage: Annotated[list[SelectedChatCoverage], Field(max_length=5)] = Field(default_factory=list)
    results: Annotated[list[SearchMessageResult], Field(max_length=20)] = Field(default_factory=list)
    page_complete: Annotated[bool, Field(strict=True)] = False
    scope_complete: Annotated[bool, Field(strict=True, json_schema_extra={"const": False})] = False
    has_more: None = None
    next_cursor: SelectedSearchCursor | None = None
    stop_reason: SelectedSearchStopReason
    provider_coverage: Literal["tdlib_lexical_unverified"] = "tdlib_lexical_unverified"

    @model_validator(mode="after")
    def bounded_selected_evidence(self) -> "SearchChatsResponse":
        if self.scope_complete or (self.next_cursor is not None) != (self.status == "page"):
            raise ValueError("selected search cannot prove recall or resume a terminal group")
        if self.stop_reason in {"account_changed", "authorization_unavailable", "invalid_cursor"} and (self.results or self.next_cursor):
            raise ValueError("selected group lifecycle loss cannot return content or continuation")
        if self.scope is None:
            if self.current_index is not None or self.coverage or self.results or self.page_complete or self.next_cursor:
                raise ValueError("selected chat evidence requires a scope")
            return self
        scope = self.scope
        if [lane.target for lane in self.coverage] != scope.targets or self.current_index is None or self.current_index >= len(scope.targets):
            raise ValueError("coverage must map exactly to the ordered selection")
        current = self.coverage[self.current_index]
        if current.state == "pending" or any(lane.state == "active" for i,lane in enumerate(self.coverage) if i != self.current_index):
            raise ValueError("only the current selected chat may be active")
        if any(lane.state != "pending" for lane in self.coverage[self.current_index + 1:]):
            raise ValueError("future selected chats must remain unprocessed")
        if any(lane.state == "pending" for lane in self.coverage[:self.current_index]) and (
                self.results or self.next_cursor or any(lane.upper_message_id is not None or lane.scanned_candidates or
                    lane.processed_candidates or lane.provider_pages or lane.branch_coverage for lane in self.coverage)):
            raise ValueError("a failed all-chat preflight cannot carry content or native observations")
        if self.next_cursor:
            if any(lane.state != "stopped" for lane in self.coverage[:self.current_index]):
                raise ValueError("selected search cannot skip a pending chat")
            if current.state == "stopped" and self.current_index == len(scope.targets)-1:
                raise ValueError("all selected chats have stopped")
        elif current.state == "active":
            raise ValueError("terminal selected search must close its active chat")
        if self.stop_reason != current.stop_reason:
            raise ValueError("group stop must reflect the current selected chat")
        if self.page_complete != (bool(self.results) and all(result.coverage_complete for result in self.results)):
            raise ValueError("page completeness covers returned outcomes only")
        # Reuse the accepted child evidence contract without retaining or exposing child cursors.
        for index,lane in enumerate(self.coverage):
            if lane.state == "pending" or (not lane.provider_pages and not lane.branch_coverage):
                if index == self.current_index and self.results:
                    raise ValueError("selected results require native observations")
                continue
            child_scope = SearchMessagesScope(target=lane.target, upper_message_id=lane.upper_message_id,
                **scope.model_dump(exclude={"targets", "order"}))
            outcomes = self.results if index == self.current_index else []
            # Group-only reasons describe lifecycle loss, while child branches are interrupted.
            reason = lane.stop_reason if lane.stop_reason not in {"account_changed", "invalid_provider_chat"} else "invalid_cursor"
            SearchMessagesResponse(status=lane.status, scope=child_scope, results=outcomes,
                page_complete=bool(outcomes) and all(r.coverage_complete for r in outcomes),
                next_cursor="search_"+"0"*64 if lane.state == "active" else None,
                stop_reason=reason, scanned_candidates=lane.scanned_candidates,
                processed_candidates=lane.processed_candidates, provider_pages=lane.provider_pages,
                branch_coverage=lane.branch_coverage)
        return self


# Verified-target wrappers deliberately reuse, and never widen, selected reads.

def _target_contract_version(value: object) -> int:
    if type(value) is not int or value != 1:
        raise ValueError("verified target contract version must be integer 1")
    return value


def _target_untrusted(value: object) -> bool:
    if type(value) is not bool or value is not True:
        raise ValueError("verified target evidence must be untrusted")
    return value


def _strict_target_messages(value: object) -> ReadMessagesResponse | None:
    if value is None:
        return None
    if isinstance(value, ReadMessagesResponse):
        value = value.model_dump()
    if isinstance(value, dict):
        _target_contract_version(value.get("contract_version", 1))
        for row in value.get("results", []):
            if isinstance(row, dict):
                message = row.get("message")
                text = message.get("text") if isinstance(message, dict) else None
                if isinstance(text, dict):
                    _target_untrusted(text.get("untrusted", True))
    return ReadMessagesResponse.model_validate(value, strict=True)


def _verified_message_facts(row: MessageReadResult) -> None:
    message = row.message
    if message is None:
        return
    sender = message.sender
    if not -(2**53 - 1) <= sender.id <= 2**53 - 1 or (sender.kind == "user" and sender.id <= 0):
        raise ValueError("verified sender identity must be JSON-safe and users positive")
    if sender.display_name_truncated and (row.coverage_complete or "sender_truncated" not in row.issues):
        raise ValueError("truncated sender metadata requires incomplete coverage and issue")
    if sender.display_name is None and (row.coverage_complete or "sender_unavailable" not in row.issues or
                                        sender.display_name_truncated or sender.display_name_sanitized):
        raise ValueError("unavailable sender metadata requires incomplete coverage and issue")
    expected_role = ("text" if message.content_kind == "text" else
                     "none" if message.content_kind in {"video_note", "sticker"} else "caption")
    if message.text_role != expected_role or (message.text is None) != (expected_role == "none"):
        raise ValueError("verified content kind, text role and text presence must agree")
    text = message.text
    if text is not None:
        if (text.value and text.original_characters == 0 or
                not text.sanitized and len(text.value) > text.original_characters or
                not text.sanitized and not text.truncated and len(text.value) != text.original_characters):
            raise ValueError("verified text count must agree with observed text and transformations")
        if text.truncated and (row.coverage_complete or "text_truncated" not in row.issues):
            raise ValueError("truncated text requires incomplete coverage and issue")

TargetHandle = Annotated[str, Field(strict=True, min_length=71, max_length=71, pattern=r"^target_[0-9a-f]{64}$")]
TargetMessageId = Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]
TargetMessageIds = Annotated[list[TargetMessageId], Field(min_length=1, max_length=20)]


class VerifyTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: HistoryTarget


class ReadTargetMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target_handle: TargetHandle
    message_ids: TargetMessageIds

    @model_validator(mode="after")
    def distinct_ids(self) -> "ReadTargetMessagesRequest":
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("selected message IDs must be distinct")
        return self


class VerifiedTargetMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    chat_id: HistoryTarget
    title: Annotated[str, Field(strict=True, min_length=1, max_length=255)]
    chat_type: Literal["private", "basic_group", "supergroup", "channel"]
    untrusted: Annotated[Literal[True], BeforeValidator(_target_untrusted)] = True


class VerifyTargetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Annotated[Literal[1], BeforeValidator(_target_contract_version)] = 1
    status: Literal["verified", "invalid_target", "invalid_handle", "capacity", "blocked", "error"]
    target_handle: TargetHandle | None = None
    target: VerifiedTargetMetadata | None = None
    expires_at: SearchDateTime | None = None
    identity_semantics: Literal["fresh_observed_identity_not_atomic_snapshot"] = "fresh_observed_identity_not_atomic_snapshot"

    @model_validator(mode="after")
    def identity_envelope(self) -> "VerifyTargetResponse":
        fields = (self.target_handle, self.target, self.expires_at)
        if self.status == "verified":
            if any(v is None for v in fields):
                raise ValueError("verified identity requires handle, target and expiry")
            if self.expires_at.utcoffset() != timezone.utc.utcoffset(self.expires_at):
                raise ValueError("target expiry must use UTC")
        elif any(v is not None for v in fields):
            raise ValueError("terminal verification must carry no identity")
        return self


class ReadTargetMessagesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Annotated[Literal[1], BeforeValidator(_target_contract_version)] = 1
    status: Literal["complete", "partial", "invalid_handle", "capacity", "blocked", "error"]
    target_handle: TargetHandle | None = None
    target: VerifiedTargetMetadata | None = None
    expires_at: SearchDateTime | None = None
    messages: Annotated[ReadMessagesResponse | None, BeforeValidator(_strict_target_messages)] = None
    identity_semantics: Literal["fresh_observed_identity_not_atomic_snapshot"] = "fresh_observed_identity_not_atomic_snapshot"

    @model_validator(mode="after")
    def bounded_identity_read(self) -> "ReadTargetMessagesResponse":
        fields = (self.target_handle, self.target, self.expires_at, self.messages)
        if self.status in {"complete", "partial"}:
            if any(v is None for v in fields) or self.messages.status != self.status:
                raise ValueError("successful wrapper requires matching bounded messages and identity")
            if self.expires_at.utcoffset() != timezone.utc.utcoffset(self.expires_at):
                raise ValueError("target expiry must use UTC")
            anchors = [(r.anchor.chat_id, r.anchor.message_id) for r in self.messages.results]
            if len(set(anchors)) != len(anchors) or any(a[0] != self.target.chat_id for a in anchors):
                raise ValueError("handle read anchors must be distinct and belong to its target")
            for row in self.messages.results:
                _verified_message_facts(row)
            if sum(len(r.message.text.value) for r in self.messages.results if r.message and r.message.text) > 100000:
                raise ValueError("handle read exceeds aggregate text budget")
        elif any(v is not None for v in fields):
            raise ValueError("terminal handle read must carry no identity or content")
        return self
