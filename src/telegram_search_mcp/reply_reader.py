"""Read a selected message's bounded same-chat ancestry, without embedded quotes."""
from __future__ import annotations

import time
from typing import Any

from .message_reader import _message
from .schemas import MessageReadResult, ReadReplyChainRequest, ReadReplyChainResponse, SelectedMessageAnchor
from .tdjson import AuthorizationBlocked, MessageNotFound, SecretChatRejected, TDLibDeadlineExceeded, TDLibError


def reply_response(request: ReadReplyChainRequest, results: list[MessageReadResult],
                   reason: str) -> ReadReplyChainResponse:
    chain_complete = reason == "no_parent"
    complete = chain_complete and all(r.coverage_complete for r in results)
    status = "complete" if complete else "partial"
    if not any(r.message is not None for r in results):
        if reason == "authorization_unavailable":
            status = "blocked"
        elif reason in {"provider_error", "invalid_provider_message", "broker_unavailable"}:
            status = "error"
    return ReadReplyChainResponse(anchor=request.anchor, max_depth=request.max_depth,
        status=status, results=results, chain_complete=chain_complete,
        coverage_complete=complete, stop_reason=reason)


def read_reply_chain(client: Any, request: ReadReplyChainRequest, *,
                     deadline: float | None = None) -> ReadReplyChainResponse:
    deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + 30)
    results: list[MessageReadResult] = []
    visited: set[int] = set()
    anchor = request.anchor
    text_budget = 100000
    reason, status, issue = "provider_error", "error", "provider_error"

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise TDLibDeadlineExceeded("reply-chain deadline")

    try:
        with client.request_budget(deadline):
            check_deadline()
            client.ensure_ready()
            check_deadline()
            chat = client.resolve_target(anchor.chat_id)
            check_deadline()
            if (not isinstance(chat, dict) or chat.get("@type") != "chat" or
                    type(chat.get("id")) is not int or chat["id"] != anchor.chat_id):
                status, issue = "wrong_chat", "chat_mismatch"
                raise ValueError("invalid reply-chain chat")
            kind = chat.get("type")
            if not isinstance(kind, dict):
                raise ValueError("invalid reply-chain chat type")
            if kind.get("@type") == "chatTypeSecret":
                raise SecretChatRejected("secret chat")
            if kind.get("@type") not in {"chatTypePrivate", "chatTypeBasicGroup", "chatTypeSupergroup"}:
                raise ValueError("unsupported reply-chain chat type")
            while len(results) < request.max_depth:
                check_deadline()
                raw = client.get_message(anchor.chat_id, anchor.message_id)
                check_deadline()
                if not isinstance(raw, dict) or raw.get("@type") != "message":
                    raise ValueError("invalid reply-chain message")
                if type(raw.get("chat_id")) is not int or raw["chat_id"] != anchor.chat_id:
                    status, issue = "wrong_chat", "chat_mismatch"
                    raise ValueError("invalid reply-chain chat")
                if type(raw.get("id")) is not int or raw["id"] != anchor.message_id:
                    issue = "message_mismatch"
                    raise ValueError("invalid reply-chain identity")
                result = _message(client, raw, anchor, text_budget)
                check_deadline()
                results.append(result)
                visited.add(anchor.message_id)
                if result.message is None:
                    return reply_response(request, results, "unsupported_content")
                if result.message.text:
                    text_budget -= len(result.message.text.value)
                pointer = result.message.reply_to
                if "reply_unavailable" in result.issues:
                    reason = "reply_unavailable"
                elif pointer is None:
                    reason = "no_parent"
                elif pointer.kind == "story":
                    reason = "story_reply"
                elif pointer.chat_id != request.anchor.chat_id:
                    reason = "cross_chat"
                elif pointer.message_id in visited:
                    reason = "cycle"
                elif len(results) == request.max_depth:
                    reason = "depth_limit"
                elif text_budget <= 0:
                    reason = "budget_exhausted"
                else:
                    anchor = SelectedMessageAnchor(chat_id=pointer.chat_id, message_id=pointer.message_id)
                    continue
                return reply_response(request, results, reason)
    except AuthorizationBlocked:
        reason, status, issue = "authorization_unavailable", "blocked", "authorization_unavailable"
    except SecretChatRejected:
        reason, status, issue = "secret_chat", "unsupported", "secret_chat"
    except MessageNotFound:
        reason, status, issue = "message_unavailable", "not_found", "message_unavailable"
    except TDLibDeadlineExceeded:
        reason, status, issue = "budget_exhausted", "partial", "budget_exhausted"
    except (TDLibError, TimeoutError, OSError):
        pass
    except (ValueError, TypeError, OverflowError):
        reason = "invalid_provider_message"
        if issue == "provider_error":
            issue = "invalid_provider_message"
    results.append(MessageReadResult(anchor=anchor, status=status, coverage_complete=False, issues=[issue]))
    return reply_response(request, results, reason)
