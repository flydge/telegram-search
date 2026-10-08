"""Memory-only exact numeric identity handles with fresh, non-atomic guards."""
from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .message_reader import read_messages
from .sanitize import sanitize_telegram_text
from .schemas import (ReadMessagesRequest, ReadTargetMessagesRequest, ReadTargetMessagesResponse,
                      VerifyTargetRequest, VerifyTargetResponse, VerifiedTargetMetadata)
from .tdjson import (AuthorizationBlocked, MessageNotFound, SecretChatRejected,
                     TDLibDeadlineExceeded, TDLibError)

_HANDLE = re.compile(r"^target_[0-9a-f]{64}$")


class TargetGuardFailure(RuntimeError):
    """Fatal wrapper guard; intentionally outside every legacy TDLib catch."""
    def __init__(self, status: str = "invalid_handle") -> None:
        self.status = status
        super().__init__("verified target operation failed safely")


@dataclass(frozen=True)
class TargetBinding:
    account_id: int
    client_id: str
    broker_generation: str
    contract_version: int
    chat_id: int
    chat_identity: tuple
    expires_monotonic: float
    expires_at: datetime


def _positive(value: object) -> int:
    if type(value) is not int or not 0 < value < 2**53:
        raise TargetGuardFailure("invalid_target")
    return value


def _identity(chat: object, chat_id: int) -> tuple[tuple, VerifiedTargetMetadata]:
    if (not isinstance(chat, dict) or chat.get("@type") != "chat" or
            type(chat.get("id")) is not int or chat["id"] != chat_id):
        raise TargetGuardFailure("invalid_target")
    raw_type = chat.get("type")
    if not isinstance(raw_type, dict):
        raise TargetGuardFailure("invalid_target")
    kind = raw_type.get("@type")
    keys = {"chatTypePrivate":("user_id", "private"), "chatTypeBasicGroup":("basic_group_id", "basic_group"),
            "chatTypeSupergroup":("supergroup_id", "supergroup")}
    if type(kind) is not str or kind not in keys:
        raise TargetGuardFailure("invalid_target")
    key, public_kind = keys[kind]
    native_id = _positive(raw_type.get(key))
    identity = (kind, native_id)
    if kind == "chatTypeSupergroup":
        channel = raw_type.get("is_channel")
        if type(channel) is not bool:
            raise TargetGuardFailure("invalid_target")
        identity += (channel,)
        public_kind = "channel" if channel else "supergroup"
    title = chat.get("title")
    if not isinstance(title, str):
        raise TargetGuardFailure("invalid_target")
    title = sanitize_telegram_text(title[:65536], max_length=255)
    if not title:
        raise TargetGuardFailure("invalid_target")
    return identity, VerifiedTargetMetadata(chat_id=chat_id, title=title, chat_type=public_kind)


class VerifiedTargetReader:
    """One client/generation store; no provider I/O occurs under its state lock."""
    def __init__(self, *, client: Any, client_id: str, broker_generation: str,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._client = client
        self._client_id = client_id
        self._generation = broker_generation
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._entries: dict[str, TargetBinding] = {}
        self._active = 0
        self._pending = 0
        self._closed = False
        self._operation_state = threading.local()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._entries.clear()

    def _purge(self) -> None:
        now = self._clock()
        for token in tuple(self._entries):
            if now >= self._entries[token].expires_monotonic:
                del self._entries[token]

    def _reserve(self, token: str | None = None) -> TargetBinding | None:
        with self._lock:
            self._purge()
            if self._closed:
                raise TargetGuardFailure()
            binding = self._entries.get(token) if token is not None else None
            if token is not None and (not _HANDLE.fullmatch(token) or binding is None):
                raise TargetGuardFailure()
            if self._active >= 4 or (token is None and len(self._entries) + self._pending >= 16):
                raise TargetGuardFailure("capacity")
            self._active += 1
            if token is None:
                self._pending += 1
            return binding

    def _release(self, *, issuing: bool) -> None:
        with self._lock:
            self._active -= 1
            if issuing:
                self._pending -= 1

    @contextmanager
    def _budget(self, deadline: float):
        previous = getattr(self._operation_state, "deadline", float("inf"))
        self._operation_state.deadline = min(previous, deadline)
        try:
            with self._client.request_budget(deadline):
                yield
        finally:
            self._operation_state.deadline = previous

    def _lifecycle_locked(self, token: str | None, binding: TargetBinding | None) -> None:
        if self._closed:
            raise TargetGuardFailure()
        if time.monotonic() >= getattr(self._operation_state, "deadline", float("inf")):
            raise TargetGuardFailure("error")
        if binding is not None:
            if (self._entries.get(token) is not binding or self._clock() >= binding.expires_monotonic or
                    binding.client_id != self._client_id or binding.broker_generation != self._generation or
                    binding.contract_version != 1):
                self._entries.pop(token, None)
                raise TargetGuardFailure()

    def _lifecycle(self, token: str | None, binding: TargetBinding | None) -> None:
        with self._lock:
            self._lifecycle_locked(token, binding)

    def _invalidate(self, token: str | None) -> None:
        with self._lock:
            if token is not None:
                self._entries.pop(token, None)

    def _account(self, token: str | None, binding: TargetBinding | None, expected: int | None = None) -> int:
        self._lifecycle(token, binding)
        try:
            account = _positive(self._client.get_account_id())
        except AuthorizationBlocked:
            raise TargetGuardFailure("blocked") from None
        except TargetGuardFailure:
            raise TargetGuardFailure("error") from None
        except (TDLibError, TimeoutError, OSError, ValueError, TypeError):
            raise TargetGuardFailure("error") from None
        self._lifecycle(token, binding)
        reference = binding.account_id if binding is not None else expected
        if reference is not None and account != reference:
            self._invalidate(token)
            raise TargetGuardFailure()
        return account

    def _hydrate(self, chat_id: int, token: str | None, binding: TargetBinding | None,
                 expected_account: int | None = None) -> tuple[tuple, VerifiedTargetMetadata]:
        account = self._account(token, binding, expected_account)
        try:
            chat = self._client.resolve_target(chat_id)
        except AuthorizationBlocked:
            raise TargetGuardFailure("blocked") from None
        except (SecretChatRejected, MessageNotFound):
            raise TargetGuardFailure("invalid_handle" if binding else "invalid_target") from None
        except (TDLibError, TimeoutError, OSError):
            raise TargetGuardFailure("error") from None
        self._account(token, binding, account)
        try:
            identity, target = _identity(chat, chat_id)
        except (TargetGuardFailure, ValueError, TypeError, OverflowError):
            raise TargetGuardFailure("invalid_handle" if binding else "invalid_target") from None
        if binding is not None and identity != binding.chat_identity:
            self._invalidate(token)
            raise TargetGuardFailure()
        return identity, target

    def verify(self, request: VerifyTargetRequest, *, deadline: float | None = None) -> VerifyTargetResponse:
        request = VerifyTargetRequest.model_validate(request.model_dump())
        reserved = False
        try:
            self._reserve();reserved = True
            deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + 30)
            with self._budget(deadline):
                self._lifecycle(None, None)
                try:
                    self._client.ensure_ready()
                except AuthorizationBlocked:
                    raise TargetGuardFailure("blocked") from None
                except (TDLibError, TimeoutError, OSError):
                    raise TargetGuardFailure("error") from None
                account = self._account(None, None)
                identity, target = self._hydrate(request.target, None, None, account)
                self._account(None, None, account)
                now = self._clock()
                expires_at = self._wall_clock().astimezone(timezone.utc) + timedelta(seconds=300)
                token = "target_" + secrets.token_hex(32)
                binding = TargetBinding(account, self._client_id, self._generation, 1,
                    request.target, identity, now + 300, expires_at)
                candidate = VerifyTargetResponse(status="verified",target_handle=token,target=target,expires_at=expires_at)
                with self._lock:
                    self._lifecycle_locked(None, None)
                    if self._clock() >= binding.expires_monotonic:
                        raise TargetGuardFailure()
                    if token in self._entries:
                        raise TargetGuardFailure("error")
                    # Publication linearizes here, after response construction, with close.
                    self._entries[token] = binding
                    return candidate
        except TargetGuardFailure as error:
            return VerifyTargetResponse(status=error.status)
        except (TDLibError, TimeoutError, OSError):
            return VerifyTargetResponse(status="error")
        finally:
            if reserved:self._release(issuing=True)

    def read(self, request: ReadTargetMessagesRequest, *, deadline: float | None = None) -> ReadTargetMessagesResponse:
        request = ReadTargetMessagesRequest.model_validate(request.model_dump())
        reserved = False
        token = request.target_handle
        try:
            binding = self._reserve(token);reserved = True
            deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + 30)
            with self._budget(deadline):
                facade = _GuardedClient(self, token, binding)
                messages = read_messages(facade, ReadMessagesRequest(anchors=[
                    {"chat_id":binding.chat_id,"message_id":i} for i in request.message_ids]), deadline=deadline)
                _, target = self._hydrate(binding.chat_id, token, binding)
                self._account(token, binding)
                if messages.status not in {"complete", "partial"}:
                    raise TargetGuardFailure("blocked" if messages.status == "blocked" else "error")
                candidate = ReadTargetMessagesResponse(status=messages.status, target_handle=token,target=target,
                    expires_at=binding.expires_at,messages=messages)
                with self._lock:
                    self._lifecycle_locked(token, binding)
                    return candidate
        except TargetGuardFailure as error:
            if error.status != "capacity":self._invalidate(token)
            return ReadTargetMessagesResponse(status=error.status)
        except (TDLibError, TimeoutError, OSError):
            self._invalidate(token)
            return ReadTargetMessagesResponse(status="error")
        finally:
            if reserved:self._release(issuing=False)


class _GuardedClient:
    def __init__(self, store: VerifiedTargetReader, token: str, binding: TargetBinding) -> None:
        self.store, self.token, self.binding = store, token, binding

    def request_budget(self, deadline: float):
        return self.store._client.request_budget(deadline)

    def ensure_ready(self):
        self._call(self.store._client.ensure_ready, content=False)

    def resolve_target(self, target):
        if type(target) is not int or target != self.binding.chat_id:
            raise TargetGuardFailure()
        _, metadata = self.store._hydrate(target, self.token, self.binding)
        # Legacy reader needs only ID/type; exact native envelope was checked above.
        return {"id":metadata.chat_id,"type":{"@type":self.binding.chat_identity[0]}}

    def get_message(self, chat_id, message_id):
        if chat_id != self.binding.chat_id:
            raise TargetGuardFailure()
        raw = self._call(lambda:self.store._client.get_message(chat_id, message_id), missing=True)
        if (not isinstance(raw,dict) or raw.get("@type") != "message" or
                type(raw.get("chat_id")) is not int or raw["chat_id"] != chat_id or
                type(raw.get("id")) is not int or raw["id"] != message_id):
            raise TargetGuardFailure()
        return raw

    def get_sender_identity(self, sender):
        value = self._call(lambda:self.store._client.get_sender_identity(sender))
        kind = "user" if sender.get("@type") == "messageSenderUser" else "chat"
        expected_id = sender.get("user_id" if kind == "user" else "chat_id")
        if (not isinstance(value,dict) or value.get("kind") != kind or
                type(value.get("id")) is not int or value["id"] != expected_id or
                not isinstance(value.get("display_name"),str)):
            raise TargetGuardFailure("error")
        return value

    def _call(self, fn, *, missing: bool = False, content: bool = True):
        if content:self.store._hydrate(self.binding.chat_id, self.token, self.binding)
        self.store._account(self.token, self.binding)
        try:
            value = fn()
        except MessageNotFound:
            self.store._account(self.token, self.binding)
            if missing:raise
            raise TargetGuardFailure("error") from None
        except AuthorizationBlocked:
            raise TargetGuardFailure("blocked") from None
        except TDLibDeadlineExceeded:
            self.store._account(self.token, self.binding)
            raise
        except (TDLibError, TimeoutError, OSError):
            raise TargetGuardFailure("error") from None
        self.store._account(self.token, self.binding)
        return value
