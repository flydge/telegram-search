"""In-process, single-use approvals for sending cached local artifacts.

The broker owns the actual TDLib call. A claim marks the attempt before the
broker invokes it, so a lost or uncertain response cannot be retried here.
"""

from __future__ import annotations

import re
import hashlib
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal

from .artifact_store import ArtifactStore, ArtifactStoreError, DOCUMENT_LIMIT
from .outgoing_media import PHOTO_LIMIT
from .outgoing_voice import MAX_VOICE_BYTES
from .sanitize import sanitize_telegram_text
from .reply_drafts import ReplySource

DRAFT_TTL_SECONDS = 15 * 60
DEFAULT_CAPACITY = 4096
_MIME_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")
_DRAFT_ID = re.compile(r"draft_[0-9a-f]{32}\Z")
ReceiptStatus = Literal["sent", "failed", "outcome_unknown"]


class DraftError(RuntimeError):
    """The draft cannot proceed through the requested transition."""


def _safe_display_name(display_name: str) -> str:
    safe_name = sanitize_telegram_text(
        display_name.replace("\\", "/").rsplit("/", 1)[-1], max_length=255
    )
    if safe_name in {"", ".", ".."}:
        safe_name = "attachment"
    if len(safe_name.encode("utf-8")) > 240:
        raise ValueError("display_name is too long for local staging")
    return safe_name


def _safe_caption(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.replace("\r\n", "\n").replace("\r", "\n"))
    result = "".join(character for character in normalized
                     if character == "\n" or character == "\t" or
                     not unicodedata.category(character).startswith("C"))
    if len(result) > 1024:
        raise ValueError("caption exceeds 1024 characters after normalization")
    return result


def _safe_message_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    result = unicodedata.normalize("NFKC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if len(result) > 4096 or not result.strip() or any(
        unicodedata.category(character).startswith("C") and character not in "\n\t"
        for character in result
    ):
        raise ValueError("text is empty, too long, or contains control characters")
    return result


@dataclass(frozen=True)
class DraftOwner:
    client_id: str
    account_id: int

    def __post_init__(self) -> None:
        if (type(self.client_id) is not str or
                re.fullmatch(r"client_[A-Za-z0-9_-]{24,80}", self.client_id) is None or
                type(self.account_id) is not int or not 0 < self.account_id < 2**53):
            raise ValueError("invalid draft owner")


@dataclass(frozen=True)
class OutgoingDraft:
    draft_id: str
    artifact_id: str
    sha256: str
    size_bytes: int
    display_name: str
    mime_type: str
    caption: str
    recipient: int
    expires_at: float
    recipient_title: str = ""
    approval_required: bool = True
    kind: Literal["document", "photo", "voice_note", "text"] = "document"
    text: str = ""
    duration_seconds: int | None = None
    waveform_base64: str | None = None
    source_sha256: str | None = None
    source_display_name: str | None = None
    converted: bool = False
    reply_source: ReplySource | None = None


@dataclass(frozen=True)
class ClaimedDraft:
    draft: OutgoingDraft
    path: Path | None


@dataclass(frozen=True)
class SendReceipt:
    draft_id: str
    status: ReceiptStatus
    message_id: int | None
    finished_at: float
    evidence: str = "none"


@dataclass
class _Entry:
    draft: OutgoingDraft
    owner: DraftOwner
    state: str = "pending"
    receipt: SendReceipt | None = None
    artifact_expires_at: float | None = None
    attempted_at: float | None = None
    provider: object | None = None
    provider_epoch: object | None = None


class OutgoingDraftRegistry:
    def __init__(
        self,
        store: ArtifactStore,
        *,
        clock: Callable[[], float] = time.time,
        capacity: int = DEFAULT_CAPACITY,
        attempt_clock: Callable[[], float] = time.monotonic,
        max_ttl_seconds: int = DRAFT_TTL_SECONDS,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if type(max_ttl_seconds) is not int or not 1 <= max_ttl_seconds <= 86400:
            raise ValueError("draft TTL must be an integer from 1 to 86400 seconds")
        self._store = store
        self._clock = clock
        self._attempt_clock = attempt_clock
        self._capacity = capacity
        self._max_ttl_seconds = max_ttl_seconds
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def is_owned_artifact_reply(self, draft_id: str, *, client_id: str) -> bool:
        """Private budget routing only; never exposes draft/account facts."""
        with self._lock:
            entry=self._entries.get(draft_id)
            return (entry is not None and entry.owner.client_id==client_id
                    and entry.draft.reply_source is not None and entry.draft.kind!='text')

    def prepare(
        self,
        *,
        owner: DraftOwner,
        artifact_id: str,
        recipient: int,
        recipient_title: str = "",
        display_name: str,
        mime_type: str,
        caption: str,
        kind: Literal["document", "photo", "voice_note"] = "document",
        duration_seconds: int | None = None,
        waveform_base64: str | None = None,
        source_sha256: str | None = None,
        source_display_name: str | None = None,
        converted: bool = False,
        reply_source: ReplySource | None = None,
        local_admission_guard: Callable[[], None] | None = None,
    ) -> OutgoingDraft:
        """Record the exact server-issued artifact and proposed send details."""
        self._validate_owner(owner)
        if type(recipient) is not int or recipient == 0:
            raise ValueError("recipient must be a nonzero numeric chat ID")
        if kind not in {"document", "photo", "voice_note"}:
            raise ValueError("unsupported send kind")
        if not isinstance(display_name, str):
            raise ValueError("display_name must be a string")
        safe_name = _safe_display_name(display_name)
        if not isinstance(mime_type, str) or len(mime_type) > 127 or not _MIME_TYPE.fullmatch(mime_type):
            raise ValueError("mime_type must be a valid media type")
        if not isinstance(caption, str) or len(caption) > 1024 or "\x00" in caption:
            raise ValueError("caption must be text of at most 1024 characters")
        safe_caption = _safe_caption(caption)
        if reply_source is not None and (not isinstance(reply_source, ReplySource) or reply_source.anchor[0] != recipient):
            raise DraftError("draft is unavailable")
        try:
            artifact = self._store.lookup(artifact_id)
        except ArtifactStoreError as error:
            raise DraftError("artifact is unavailable") from error
        if artifact is None:
            raise DraftError("artifact is unavailable")
        limit = {"document": DOCUMENT_LIMIT, "photo": PHOTO_LIMIT,
                 "voice_note": MAX_VOICE_BYTES}[kind]
        if not 0 < artifact.size_bytes <= limit:
            raise DraftError("artifact exceeds send limit")
        now = self._clock()
        expires_at = min(now + self._max_ttl_seconds, artifact.expires_at)
        if expires_at <= now:
            raise DraftError("artifact has expired")
        with self._lock:
            if local_admission_guard is not None:
                local_admission_guard()
            self._prune_locked(now)
            if len(self._entries) >= self._capacity:
                raise DraftError("draft capacity is exhausted")
            draft_id = f"draft_{uuid.uuid4().hex}"
            if draft_id in self._entries:
                raise DraftError("draft ID collision")
            draft = OutgoingDraft(
                draft_id=draft_id,
                artifact_id=artifact_id,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                display_name=safe_name,
                mime_type=mime_type,
                caption=safe_caption,
                recipient=recipient,
                expires_at=expires_at,
                recipient_title=recipient_title,
                kind=kind,
                duration_seconds=duration_seconds,
                waveform_base64=waveform_base64,
                source_sha256=source_sha256,
                source_display_name=source_display_name,
                converted=converted, reply_source=reply_source,
            )
            self._entries[draft_id] = _Entry(draft=draft, owner=owner,
                                            artifact_expires_at=artifact.expires_at)
            return draft

    def prepare_text(self, *, owner: DraftOwner, recipient: int, text: str, recipient_title: str = "", reply_source: ReplySource | None = None) -> OutgoingDraft:
        self._validate_owner(owner)
        if type(recipient) is not int or recipient == 0:
            raise ValueError("recipient must be a nonzero numeric chat ID")
        if reply_source is not None and (not isinstance(reply_source, ReplySource) or reply_source.anchor[0] != recipient):
            raise DraftError("draft is unavailable")
        safe_text = _safe_message_text(text)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            if len(self._entries) >= self._capacity:
                raise DraftError("draft capacity is exhausted")
            draft_id = f"draft_{uuid.uuid4().hex}"
            if draft_id in self._entries:
                raise DraftError("draft ID collision")
            draft = OutgoingDraft(
                draft_id=draft_id, artifact_id="", sha256=hashlib.sha256(safe_text.encode("utf-8")).hexdigest(),
                size_bytes=len(safe_text.encode("utf-8")), display_name="Text message", mime_type="text/plain",
                caption="", recipient=recipient, expires_at=now + self._max_ttl_seconds,
                kind="text", text=safe_text, recipient_title=recipient_title, reply_source=reply_source,
            )
            self._entries[draft_id] = _Entry(draft=draft, owner=owner)
            return draft

    def revise(self, draft_id: str, *, owner: DraftOwner, recipient_title: str,
               text: str | None = None, caption: str | None = None,
               reply: bool = False, reply_source: ReplySource | None = None,
               local_admission_guard: Callable[[], None] | None = None) -> OutgoingDraft:
        """Replace one live revision atomically against claim/cancel/replacement.

        The ID is the revision token. No approval or terminal receipt transfers
        to its replacement. Neither refresh nor update can revive expired data.
        """
        with self._lock:
            entry = self._get(draft_id, owner)
            self._pending_locked(entry, verify_artifact=False)
            old = entry.draft
            if (old.reply_source is not None) != reply:
                raise DraftError("draft is unavailable")
            changes = {}
            if reply_source is not None:
                if not reply or reply_source.anchor != old.reply_source.anchor:
                    raise DraftError("draft is unavailable")
                changes["reply_source"] = reply_source
            if text is not None:
                if old.kind != "text" or caption is not None:
                    raise DraftError("draft is unavailable")
                safe_text = _safe_message_text(text)
                data = safe_text.encode("utf-8")
                changes.update(text=safe_text, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
            if caption is not None:
                if old.kind == "text" or not isinstance(caption, str) or len(caption) > 1024 or "\x00" in caption:
                    raise DraftError("draft is unavailable")
                changes["caption"] = _safe_caption(caption)
            artifact = self._artifact_locked(entry) if old.kind != "text" else None
            # Hashing may have crossed the deadline while the lock was held.
            if local_admission_guard is not None:
                local_admission_guard()
            now = self._clock()
            if now >= old.expires_at:
                raise DraftError("draft is unavailable")
            expires_at = now + self._max_ttl_seconds
            if artifact is not None:
                if entry.artifact_expires_at is None:
                    raise DraftError("draft is unavailable")
                expires_at = min(expires_at, entry.artifact_expires_at, artifact.expires_at)
            if expires_at <= now:
                raise DraftError("draft is unavailable")
            self._prune_locked(now)
            if len(self._entries) >= self._capacity:
                raise DraftError("draft capacity is exhausted")
            new_id = f"draft_{uuid.uuid4().hex}"
            if new_id in self._entries:
                raise DraftError("draft ID collision")
            revised = replace(old, draft_id=new_id, recipient_title=recipient_title,
                              expires_at=expires_at, **changes)
            self._entries[new_id] = _Entry(draft=revised, owner=owner,
                                          artifact_expires_at=entry.artifact_expires_at)
            entry.state = "superseded"
            return revised

    def known_ids(self) -> set[str]:
        """Return current draft IDs after bounded expiry pruning."""
        with self._lock:
            self._prune_locked(self._clock())
            return set(self._entries)

    def _prune_locked(self, now: float) -> None:
        self._entries = {
            draft_id: entry for draft_id, entry in self._entries.items()
            if (self._attempt_clock() < entry.attempted_at + 900 if entry.attempted_at is not None
                else entry.draft.expires_at > now)
        }

    def claim(self, draft_id: str, *, owner: DraftOwner, approved: bool, provider: object | None = None, provider_epoch: object | None = None) -> ClaimedDraft:
        """Atomically consume a live, explicitly approved draft before sending."""
        if approved is not True:
            raise DraftError("explicit approval is required")
        with self._lock:
            entry = self._get(draft_id, owner)
            if entry.state != "pending":
                raise DraftError("draft has already been used")
            path = self._pending_locked(entry)
            entry.state = "claimed"
            entry.attempted_at = self._attempt_clock()
            entry.provider = provider
            entry.provider_epoch = provider_epoch
            return ClaimedDraft(draft=entry.draft, path=path)

    def peek(self, draft_id: str, *, owner: DraftOwner) -> OutgoingDraft:
        """Inspect an unexpired draft without claiming a send attempt."""
        with self._lock:
            entry = self._get(draft_id, owner)
            self._pending_locked(entry)
            return entry.draft

    def receipt(self, draft_id: str, *, owner: DraftOwner) -> SendReceipt | None:
        """Return a prior terminal receipt without starting another attempt."""
        with self._lock:
            return self._get(draft_id, owner).receipt

    def is_reply(self, draft_id: str, *, owner: DraftOwner) -> bool:
        with self._lock:
            return self._get(draft_id,owner).draft.reply_source is not None

    def source_required_capabilities(self, draft_id: str, *, owner: DraftOwner,
                                   provider: object | None = None, provider_epoch: object | None = None) -> tuple[str, ...]:
        """Classify gated sources under the owner/account, lifetime and epoch lock."""
        with self._lock:
            entry=self._get(draft_id,owner)
            source=entry.draft.reply_source
            capabilities=source.required_capabilities if source is not None else ()
            if not capabilities:
                return ()  # Preserve the existing plain-source replay semantics.
            if (self._clock() >= entry.draft.expires_at if entry.attempted_at is None
                    else self._attempt_clock() >= entry.attempted_at + 900):
                raise DraftError("draft is unavailable")
            if entry.attempted_at is not None and (provider is not None or provider_epoch is not None):
                if entry.provider is not provider or entry.provider_epoch is not provider_epoch:
                    raise DraftError("draft is unavailable")
            return capabilities

    def source_required_capability(self, draft_id: str, *, owner: DraftOwner,
                                   provider: object | None = None, provider_epoch: object | None = None) -> str | None:
        """Legacy singular classifier refuses to underreport conjunctive sources."""
        capabilities=self.source_required_capabilities(draft_id,owner=owner,
            provider=provider,provider_epoch=provider_epoch)
        if len(capabilities)>1:
            raise DraftError('reply source requires multiple capabilities')
        return capabilities[0] if capabilities else None

    def is_media_reply(self, draft_id: str, *, owner: DraftOwner) -> bool:
        return 'reply_media_targets' in self.source_required_capabilities(draft_id,owner=owner)

    def kind(self, draft_id: str, *, owner: DraftOwner) -> str:
        with self._lock:
            return self._get(draft_id, owner).draft.kind

    def finish(
        self,
        draft_id: str,
        *,
        owner: DraftOwner,
        status: ReceiptStatus,
        message_id: int | None = None,
        evidence: str | None = None,
    ) -> SendReceipt:
        """Record a terminal result; repeats return the original receipt."""
        if status not in {"sent", "failed", "outcome_unknown"}:
            raise ValueError("invalid receipt status")
        if message_id is not None and (type(message_id) is not int or message_id <= 0):
            raise ValueError("message_id must be a positive integer")
        if status == "sent" and message_id is None:
            raise ValueError("sent requires a positive final message ID")
        with self._lock:
            entry = self._get(draft_id, owner)
            if entry.receipt is not None:
                return entry.receipt
            if entry.state != "claimed":
                raise DraftError("draft has not been claimed")
            receipt = SendReceipt(
                draft_id=draft_id,
                status=status,
                message_id=message_id,
                finished_at=self._clock(),
                evidence=evidence or {"sent":"provider_confirmed", "failed":"provider_failed", "outcome_unknown":"none"}[status],
            )
            entry.receipt = receipt
            entry.state = "finished"
            return receipt

    def status_snapshot(self, draft_id: str, *, owner: DraftOwner):
        """Return content-free state without keeping a mutex during provider reads."""
        with self._lock:
            entry = self._get(draft_id, owner)
            now = self._clock()
            if entry.attempted_at is None:
                if now >= entry.draft.expires_at or entry.state != "pending":
                    raise DraftError("draft is unavailable")
                return ("pending", None, None, None, None)
            if self._attempt_clock() >= entry.attempted_at + 900:
                raise DraftError("draft is unavailable")
            return (entry.state, entry.receipt, entry.provider, entry.attempted_at, entry.provider_epoch)

    def reconcile(self, draft_id: str, *, owner: DraftOwner, provider: object, provider_epoch: object,
                  attempted_at: float, status: ReceiptStatus, message_id: int | None = None):
        """Only exact terminal evidence may refine uncertainty; no send is claimed."""
        if status not in {"sent", "failed"} or (status == "sent" and
                (type(message_id) is not int or not 0 < message_id < 2**53)):
            raise ValueError("invalid terminal observation")
        with self._lock:
            entry = self._get(draft_id, owner)
            if (entry.provider is not provider or entry.provider_epoch is not provider_epoch or entry.attempted_at != attempted_at or
                    self._attempt_clock() >= attempted_at + 900):
                raise DraftError("draft is unavailable")
            if entry.receipt is None or entry.receipt.status == "outcome_unknown":
                entry.receipt = SendReceipt(draft_id,status,message_id,self._clock(),
                    "provider_confirmed" if status == "sent" else "provider_failed")
                entry.state = "finished"
            return entry.receipt

    @staticmethod
    def _validate_owner(owner: DraftOwner) -> None:
        if not isinstance(owner, DraftOwner):
            raise ValueError("invalid draft owner")

    def _pending_locked(self, entry: _Entry, *, verify_artifact: bool = True) -> Path | None:
        if entry.state != "pending" or self._clock() >= entry.draft.expires_at:
            raise DraftError("draft is unavailable")
        if entry.draft.kind == "text" or not verify_artifact:
            return None
        artifact = self._artifact_locked(entry)
        if self._clock() >= entry.draft.expires_at:
            raise DraftError("draft is unavailable")
        return artifact.path

    def _artifact_locked(self, entry: _Entry):
        try:
            artifact = self._store.lookup(entry.draft.artifact_id)
        except (ArtifactStoreError, OSError, ValueError):
            artifact = None
        if (artifact is None or artifact.sha256 != entry.draft.sha256 or
                artifact.size_bytes != entry.draft.size_bytes):
            entry.state = "invalid"
            raise DraftError("draft is unavailable")
        return artifact

    def list_pending(self, *, owner: DraftOwner, limit: int = 20,
                     after_draft_id: str | None = None) -> tuple[list[OutgoingDraft], bool]:
        self._validate_owner(owner)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid draft limit")
        with self._lock:
            if after_draft_id is not None:
                anchor = self._get(after_draft_id, owner)
                if self._clock() >= anchor.draft.expires_at:
                    raise DraftError("draft is unavailable")
            result = []
            for draft_id in sorted(self._entries):
                entry = self._entries[draft_id]
                if entry.owner != owner or (after_draft_id is not None and draft_id <= after_draft_id):
                    continue
                try:
                    self._pending_locked(entry, verify_artifact=False)
                except DraftError:
                    continue
                result.append(entry.draft)
                if len(result) > limit:
                    break
            return result[:limit], len(result) > limit

    def cancel(self, draft_id: str, *, owner: DraftOwner) -> None:
        """Only a pending draft can lose the race to a provider claim."""
        with self._lock:
            entry = self._get(draft_id, owner)
            if self._clock() >= entry.draft.expires_at or entry.state not in {"pending", "cancelled"}:
                raise DraftError("draft is unavailable")
            entry.state = "cancelled"

    def _get(self, draft_id: str, owner: DraftOwner) -> _Entry:
        self._validate_owner(owner)
        if not isinstance(draft_id, str) or _DRAFT_ID.fullmatch(draft_id) is None:
            raise DraftError("unknown draft")
        try:
            entry = self._entries[draft_id]
            if entry.owner != owner:
                raise DraftError("unknown draft")
            return entry
        except KeyError:
            raise DraftError("unknown draft") from None
