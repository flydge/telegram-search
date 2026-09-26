"""Bounded, memory-only progress state for account-wide Telegram discovery."""

from __future__ import annotations

import copy
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal, TypeAlias, TypeVar

ChatListName: TypeAlias = Literal["main", "archive"]
DiscoveryScope: TypeAlias = Literal["main", "archive", "both"]

_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_OFFSET_DIGESTS = 64
_Item = TypeVar("_Item")


def _is_strict_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class DiscoveryCursorExpired(RuntimeError):
    """A scan cursor is absent, expired, evicted, or bound to other inputs."""

    def __init__(self) -> None:
        super().__init__("discovery cursor expired")


class DiscoveryWorkBusy(RuntimeError):
    """Another caller currently owns the scan-wide work lease."""

    def __init__(self) -> None:
        super().__init__("discovery work is busy")


@dataclass(frozen=True)
class CatalogPosition:
    chat_id: int
    order: int


@dataclass
class CatalogLaneState:
    positions: dict[int, int] = field(default_factory=dict)
    emitted: set[int] = field(default_factory=set)
    end_reached: bool = False
    permanent_partial: bool = False
    retryable_error: bool = False
    hydration_retryable_ids: set[int] = field(default_factory=set)
    started: bool = False

    @property
    def pending(self) -> bool:
        return any(chat_id not in self.emitted for chat_id in self.positions)

    @property
    def terminal(self) -> bool:
        return self.permanent_partial or (
            self.end_reached
            and not self.pending
            and not self.hydration_retryable_ids
        )

    @property
    def status(self) -> str:
        if self.permanent_partial:
            return "partial"
        if self.retryable_error or self.hydration_retryable_ids:
            return "error"
        if self.end_reached and not self.pending:
            return "complete"
        if self.started or self.positions:
            return "scanning"
        return "not_started"


@dataclass(frozen=True)
class GlobalLaneKey:
    hypothesis_index: int
    chat_list: ChatListName


@dataclass
class GlobalLaneState:
    offset: str = ""
    started: bool = False
    complete: bool = False
    permanent_partial: bool = False
    retryable_error: bool = False
    pages_scanned: int = 0
    hits_seen: int = 0
    seen_offset_digests: set[bytes] = field(default_factory=set)

    @property
    def terminal(self) -> bool:
        return self.complete or self.permanent_partial

    @property
    def status(self) -> str:
        if self.permanent_partial:
            return "partial"
        if self.retryable_error:
            return "error"
        if self.complete:
            return "complete"
        if self.started:
            return "scanning"
        return "not_started"


@dataclass(frozen=True)
class DiscoveryWork:
    catalog_list: ChatListName | None
    global_lane: GlobalLaneKey | None
    lease_token: str


@dataclass
class DiscoveryScan:
    cursor: str
    hypothesis_digest: str
    hypothesis_count: int
    scope: DiscoveryScope
    catalog: dict[ChatListName, CatalogLaneState]
    global_lanes: dict[GlobalLaneKey, GlobalLaneState]
    returned_candidate_ids: set[int]
    catalog_pointer: int
    global_pointer: int
    touched_at: float
    active_lease_token: str | None


class DiscoveryRegistry:
    """Retain only bounded scan mechanics; raw discovery evidence stays call-local."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = 300,
        capacity: int = 4,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not _is_strict_integer(capacity) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._clock = clock
        self._ttl_seconds = float(ttl_seconds)
        self._capacity = capacity
        self._scans: dict[str, DiscoveryScan] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _validate_binding(
        hypothesis_digest: object,
        hypothesis_count: object,
        scope: object,
    ) -> tuple[str, int, DiscoveryScope]:
        if not isinstance(hypothesis_digest, str) or not _DIGEST_PATTERN.fullmatch(
            hypothesis_digest
        ):
            raise ValueError("hypothesis_digest must be a lowercase SHA-256 digest")
        if (
            not _is_strict_integer(hypothesis_count)
            or hypothesis_count < 2
            or hypothesis_count > 5
        ):
            raise ValueError("hypothesis_count must be between 2 and 5")
        if scope not in ("main", "archive", "both"):
            raise ValueError("scope must be main, archive, or both")
        return hypothesis_digest, hypothesis_count, scope

    @staticmethod
    def _chat_lists(scope: DiscoveryScope) -> tuple[ChatListName, ...]:
        if scope == "both":
            return ("main", "archive")
        return (scope,)

    def _purge_expired(self, now: float) -> None:
        expired = [
            cursor
            for cursor, scan in self._scans.items()
            if now - scan.touched_at >= self._ttl_seconds
        ]
        for cursor in expired:
            del self._scans[cursor]

    def start(
        self,
        *,
        hypothesis_digest: str,
        hypothesis_count: int,
        scope: DiscoveryScope,
    ) -> str:
        digest, count, checked_scope = self._validate_binding(
            hypothesis_digest, hypothesis_count, scope
        )
        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            while len(self._scans) >= self._capacity:
                oldest = min(self._scans.values(), key=lambda item: item.touched_at)
                del self._scans[oldest.cursor]
            cursor = f"scan_{secrets.token_urlsafe(24)}"
            while cursor in self._scans:
                cursor = f"scan_{secrets.token_urlsafe(24)}"
            chat_lists = self._chat_lists(checked_scope)
            global_keys = tuple(
                GlobalLaneKey(index, chat_list)
                for index in range(count)
                for chat_list in chat_lists
            )
            self._scans[cursor] = DiscoveryScan(
                cursor=cursor,
                hypothesis_digest=digest,
                hypothesis_count=count,
                scope=checked_scope,
                catalog={chat_list: CatalogLaneState() for chat_list in chat_lists},
                global_lanes={key: GlobalLaneState() for key in global_keys},
                returned_candidate_ids=set(),
                catalog_pointer=0,
                global_pointer=0,
                touched_at=now,
                active_lease_token=None,
            )
            return cursor

    def _scan(self, cursor: object, *, touch: bool = True) -> DiscoveryScan:
        now = self._clock()
        self._purge_expired(now)
        if not isinstance(cursor, str):
            raise DiscoveryCursorExpired()
        scan = self._scans.get(cursor)
        if scan is None:
            raise DiscoveryCursorExpired()
        if touch:
            scan.touched_at = now
        return scan

    def bind(
        self,
        cursor: str,
        *,
        hypothesis_digest: str,
        hypothesis_count: int,
        scope: DiscoveryScope,
    ) -> DiscoveryScan:
        try:
            digest, count, checked_scope = self._validate_binding(
                hypothesis_digest, hypothesis_count, scope
            )
        except ValueError as error:
            raise DiscoveryCursorExpired() from error
        with self._lock:
            scan = self._scan(cursor, touch=False)
            if (
                not hmac.compare_digest(scan.hypothesis_digest, digest)
                or scan.hypothesis_count != count
                or scan.scope != checked_scope
            ):
                raise DiscoveryCursorExpired()
            scan.touched_at = self._clock()
            return copy.deepcopy(scan)

    @staticmethod
    def _next_available(
        items: tuple[_Item, ...],
        pointer: int,
        available: Callable[[_Item], bool],
    ) -> tuple[_Item | None, int]:
        if not items:
            return None, pointer
        for step in range(len(items)):
            index = (pointer + step) % len(items)
            item = items[index]
            if available(item):
                return item, (index + 1) % len(items)
        return None, pointer

    def next_work(self, cursor: str) -> DiscoveryWork | None:
        with self._lock:
            scan = self._scan(cursor)
            if scan.active_lease_token is not None:
                raise DiscoveryWorkBusy()
            catalog_lists = tuple(scan.catalog)
            catalog_list, scan.catalog_pointer = self._next_available(
                catalog_lists,
                scan.catalog_pointer,
                lambda item: not scan.catalog[item].terminal,
            )
            global_keys = tuple(scan.global_lanes)
            global_lane, scan.global_pointer = self._next_available(
                global_keys,
                scan.global_pointer,
                lambda item: not scan.global_lanes[item].terminal,
            )
            if catalog_list is None and global_lane is None:
                return None
            lease_token = f"work_{secrets.token_urlsafe(24)}"
            scan.active_lease_token = lease_token
            return DiscoveryWork(
                catalog_list=catalog_list,
                global_lane=global_lane,
                lease_token=lease_token,
            )

    @staticmethod
    def _require_lease(scan: DiscoveryScan, lease_token: str | None) -> None:
        active = scan.active_lease_token
        if active is None:
            if lease_token is not None:
                raise RuntimeError("discovery work is reserved")
            return
        if not isinstance(lease_token, str) or not hmac.compare_digest(active, lease_token):
            raise RuntimeError("discovery work is reserved")

    def release_work(self, cursor: str, lease_token: str) -> None:
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            scan.active_lease_token = None

    def record_catalog_positions(
        self,
        cursor: str,
        chat_list: ChatListName,
        positions: Iterable[CatalogPosition],
        *,
        lease_token: str | None = None,
    ) -> None:
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            lane = self._catalog_lane(scan, chat_list)
            checked: list[CatalogPosition] = []
            for position in positions:
                if (
                    not isinstance(position, CatalogPosition)
                    or not _is_strict_integer(position.chat_id)
                    or position.chat_id == 0
                    or not _is_strict_integer(position.order)
                    or position.order <= 0
                ):
                    raise ValueError("invalid catalog position")
                checked.append(position)
            lane.started = True
            lane.retryable_error = False
            for position in checked:
                lane.positions[position.chat_id] = position.order

    def catalog_page(
        self,
        cursor: str,
        chat_list: ChatListName,
        *,
        limit: int = 15,
        lease_token: str | None = None,
    ) -> list[int]:
        if not _is_strict_integer(limit) or not 1 <= limit <= 15:
            raise ValueError("catalog page limit must be between 1 and 15")
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            lane = self._catalog_lane(scan, chat_list)
            pending = sorted(
                (
                    (order, chat_id)
                    for chat_id, order in lane.positions.items()
                    if chat_id not in lane.emitted
                ),
                reverse=True,
            )[:limit]
            chat_ids = [chat_id for _, chat_id in pending]
            lane.emitted.update(chat_ids)
            return chat_ids

    def mark_catalog_end(
        self,
        cursor: str,
        chat_list: ChatListName,
        *,
        lease_token: str | None = None,
    ) -> None:
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            lane = self._catalog_lane(scan, chat_list)
            lane.started = True
            lane.end_reached = True
            lane.retryable_error = False

    def record_catalog_retryable_error(
        self,
        cursor: str,
        chat_list: ChatListName,
        *,
        lease_token: str | None = None,
    ) -> None:
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            lane = self._catalog_lane(scan, chat_list)
            lane.started = True
            lane.retryable_error = True

    def record_catalog_permanent_partial(
        self,
        cursor: str,
        chat_list: ChatListName,
        *,
        lease_token: str | None = None,
    ) -> None:
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            lane = self._catalog_lane(scan, chat_list)
            lane.started = True
            lane.retryable_error = False
            lane.permanent_partial = True

    def record_catalog_hydration_retry(
        self,
        cursor: str,
        chat_list: ChatListName,
        chat_ids: Iterable[int],
        *,
        lease_token: str | None = None,
    ) -> None:
        checked = list(chat_ids)
        if not checked or not all(
            _is_strict_integer(chat_id) and chat_id != 0 for chat_id in checked
        ):
            raise ValueError("catalog retry IDs must be non-zero integers")
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            lane = self._catalog_lane(scan, chat_list)
            if any(
                chat_id not in lane.positions or chat_id not in lane.emitted
                for chat_id in checked
            ):
                raise ValueError("catalog retry IDs must be emitted catalog IDs")
            lane.emitted.difference_update(checked)
            lane.hydration_retryable_ids.update(checked)

    def record_catalog_hydrated(
        self,
        cursor: str,
        chat_list: ChatListName,
        chat_ids: Iterable[int],
        *,
        lease_token: str | None = None,
    ) -> None:
        checked = list(chat_ids)
        if not all(
            _is_strict_integer(chat_id) and chat_id != 0 for chat_id in checked
        ):
            raise ValueError("hydrated catalog IDs must be non-zero integers")
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            lane = self._catalog_lane(scan, chat_list)
            if any(chat_id not in lane.positions for chat_id in checked):
                raise ValueError("hydrated catalog IDs must be known catalog IDs")
            lane.hydration_retryable_ids.difference_update(checked)

    @staticmethod
    def _catalog_lane(
        scan: DiscoveryScan, chat_list: object
    ) -> CatalogLaneState:
        if chat_list not in scan.catalog:
            raise ValueError("chat list is outside this scan")
        return scan.catalog[chat_list]

    @staticmethod
    def _global_lane(scan: DiscoveryScan, key: object) -> GlobalLaneState:
        if key not in scan.global_lanes:
            raise ValueError("global lane is outside this scan")
        return scan.global_lanes[key]

    def global_lane(self, cursor: str, key: GlobalLaneKey) -> GlobalLaneState:
        with self._lock:
            scan = self._scan(cursor)
            return copy.deepcopy(self._global_lane(scan, key))

    def record_global_page(
        self,
        cursor: str,
        key: GlobalLaneKey,
        *,
        next_offset: str,
        hits: int,
        lease_token: str | None = None,
    ) -> None:
        if not isinstance(next_offset, str):
            raise ValueError("next_offset must be a string")
        if not _is_strict_integer(hits) or hits < 0:
            raise ValueError("hits must be a non-negative integer")
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            lane = self._global_lane(scan, key)
            lane.started = True
            lane.retryable_error = False
            lane.pages_scanned += 1
            lane.hits_seen += hits
            if next_offset == "":
                lane.complete = True
                return
            offset_digest = hashlib.sha256(
                next_offset.encode("utf-8", errors="surrogatepass")
            ).digest()
            if (
                offset_digest in lane.seen_offset_digests
                or len(lane.seen_offset_digests) >= _MAX_OFFSET_DIGESTS
            ):
                lane.permanent_partial = True
                return
            lane.seen_offset_digests.add(offset_digest)
            lane.offset = next_offset

    def record_retryable_error(
        self,
        cursor: str,
        key: GlobalLaneKey,
        *,
        lease_token: str | None = None,
    ) -> None:
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            lane = self._global_lane(scan, key)
            lane.started = True
            lane.retryable_error = True

    def record_permanent_partial(
        self,
        cursor: str,
        key: GlobalLaneKey,
        *,
        lease_token: str | None = None,
    ) -> None:
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            lane = self._global_lane(scan, key)
            lane.started = True
            lane.retryable_error = False
            lane.permanent_partial = True

    def record_returned_candidates(
        self,
        cursor: str,
        chat_ids: Iterable[int],
        *,
        lease_token: str | None = None,
    ) -> None:
        checked = list(chat_ids)
        if not all(_is_strict_integer(chat_id) and chat_id != 0 for chat_id in checked):
            raise ValueError("candidate chat IDs must be non-zero integers")
        with self._lock:
            scan = self._scan(cursor)
            self._require_lease(scan, lease_token)
            scan.returned_candidate_ids.update(checked)

    def was_candidate_returned(self, cursor: str, chat_id: int) -> bool:
        if not _is_strict_integer(chat_id) or chat_id == 0:
            return False
        with self._lock:
            scan = self._scan(cursor)
            return chat_id in scan.returned_candidate_ids

    def is_complete(self, cursor: str) -> bool:
        with self._lock:
            scan = self._scan(cursor)
            catalog_complete = all(
                lane.end_reached
                and not lane.pending
                and not lane.retryable_error
                and not lane.hydration_retryable_ids
                and not lane.permanent_partial
                for lane in scan.catalog.values()
            )
            global_complete = all(
                lane.complete
                and not lane.retryable_error
                and not lane.permanent_partial
                for lane in scan.global_lanes.values()
            )
            return catalog_complete and global_complete
