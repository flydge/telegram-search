"""New-risk checks for the null-format DateTime text-source slice.

Only current product modules are imported. The in-file provider is a closed
synthetic raw boundary: no native TDLib, credentials, session or network access.
Fixtures and expected evidence are independently specified here.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import os
import tempfile
import unittest
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from telegram_search_mcp.approval_prompt import confirm_approved_send
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy, load_runtime_policy
from telegram_search_mcp.draft_models import CancelDraftRequest
from telegram_search_mcp.outgoing_stage import stage_approved_document, retire_staged_document
from telegram_search_mcp.reply_artifact_drafts import (
    GetReplyArtifactDraftRequest, PrepareReplyArtifactSendRequest,
    RefreshReplyArtifactDraftRequest, UpdateReplyArtifactDraftRequest,
)
from telegram_search_mcp.reply_drafts import (
    EVIDENCE_MARKER, GetReplyDraftRequest, PrepareReplyTextSendRequest,
    RefreshReplyDraftRequest, ReplySource, UpdateReplyDraftRequest, source_from_message,
)
from telegram_search_mcp.schemas import SendPreparedArtifactRequest, SendPreparedTextRequest
from telegram_search_mcp.send_status_models import GetSendStatusRequest
from telegram_search_mcp.tdjson import TDLibClient, ForbiddenTDLibRequest, _validate_bounded_request

CHAT = -10071
MESSAGE = 901
ACCOUNT = 42
CLIENT = "client_" + "d" * 32
DATE = "textEntityTypeDateTime"
CAPABILITIES = (
    "send", "reply_text_send", "reply_artifact_send", "reply_formatted_targets",
    "reply_datetime_targets", "reply_lexical_targets", "reply_identity_targets",
)
OMITTED = object()


def entity(kind=DATE, *, offset=0, length=4, unix_time=1700000000, formatting_type=OMITTED):
    typ = {"@type": kind}
    if kind == DATE:
        typ["unix_time"] = unix_time
        if formatting_type is not OMITTED:
            typ["formatting_type"] = formatting_type
    if kind == "textEntityTypeMentionName":
        typ["user_id"] = 51
    return {"@type": "textEntity", "offset": offset, "length": length, "type": typ}


def message(text="Date", entities=None):
    return {
        "@type": "message", "chat_id": CHAT, "id": MESSAGE,
        "sender_id": {"@type": "messageSenderUser", "user_id": 51},
        "is_outgoing": False, "is_from_offline": False, "ephemeral_message_id": 0,
        "date": 1700000000, "edit_date": 0, "self_destruct_in": 0.0,
        "auto_delete_in": 0.0, "sending_state": None, "scheduling_state": None,
        "topic_id": None, "self_destruct_type": None, "ephemeral_content": None,
        "receiver_id": None, "reply_to": None, "forward_info": None,
        "import_info": None, "reply_markup": None,
        "content": {"@type": "messageText", "text": {
            "@type": "formattedText", "text": text,
            "entities": [entity()] if entities is None else entities,
        }, "link_preview": None, "link_preview_options": None},
    }


def provider_file():
    return {"@type": "file", "id": 73, "size": 4, "expected_size": 4,
        "local": {"@type": "localFile", "path": "", "can_be_downloaded": True,
            "can_be_deleted": False, "is_downloading_active": False,
            "is_downloading_completed": False, "download_offset": 0,
            "downloaded_prefix_size": 0, "downloaded_size": 0},
        "remote": {"@type": "remoteFile", "id": "remote-73", "unique_id": "unique-73",
            "is_uploading_active": False, "is_uploading_completed": True, "uploaded_size": 4}}


def document_content(caption):
    return {"@type": "messageDocument", "document": {"@type": "document",
        "file_name": "fixture.txt", "mime_type": "text/plain", "thumbnail": None,
        "minithumbnail": None, "document": provider_file()}, "caption": caption}


def text_source(raw=None):
    return source_from_message(message() if raw is None else raw, CHAT, MESSAGE)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class SyntheticRaw:
    """Exact canned request family with immediate correlated response events."""
    def __init__(self):
        self.source = message()
        self.requests = []
        self.queue = deque()

    def send(self, request):
        self.requests.append(copy.deepcopy(request))
        kind = request["@type"]
        if kind == "getAuthorizationState":
            response = {"@type": "authorizationStateReady"}
        elif kind == "getMe":
            response = {"@type": "user", "id": ACCOUNT}
        elif kind == "getChat":
            if request["chat_id"] != CHAT:
                raise AssertionError("unexpected synthetic chat")
            response = {"@type": "chat", "id": CHAT, "title": "Synthetic room",
                "type": {"@type": "chatTypeSupergroup", "supergroup_id": 71, "is_channel": False}}
        elif kind == "getMessage":
            if (request["chat_id"], request["message_id"]) != (CHAT, MESSAGE):
                raise AssertionError("unexpected synthetic source")
            response = copy.deepcopy(self.source)
        elif kind == "getMessageProperties":
            response = {"@type": "messageProperties", "can_be_replied": True}
        elif kind == "sendMessage":
            content = request["input_message_content"]
            if content["@type"] == "inputMessageText":
                output = {"@type": "messageText", "text": copy.deepcopy(content["text"]),
                    "link_preview": None, "link_preview_options": copy.deepcopy(content["link_preview_options"])}
            elif content["@type"] == "inputMessageDocument":
                output = document_content(copy.deepcopy(content["caption"]))
            else:
                raise AssertionError("unexpected synthetic outgoing content")
            response = message(entities=[])
            response.update(id=-801, is_outgoing=True, content=output,
                reply_to={"@type": "messageReplyToMessage", "chat_id": CHAT, "message_id": MESSAGE,
                    "quote": None, "checklist_task_id": 0, "poll_option_id": "", "origin": None,
                    "origin_send_date": 0, "content": None},
                sending_state={"@type": "messageSendingStatePending",
                    "sending_id": request["options"]["sending_id"]})
            terminal = copy.deepcopy(response)
            terminal.update(id=902, sending_state=None)
            self.queue.append({**response, "@extra": request["@extra"]})
            self.queue.append({"@type": "updateMessageSendSucceeded", "old_message_id": -801, "message": terminal})
            return
        else:
            raise AssertionError("unexpected synthetic request: " + kind)
        self.queue.append({**response, "@extra": request["@extra"]})

    def receive(self, timeout):
        if self.queue:
            return self.queue.popleft()
        if timeout != 0:
            raise AssertionError("synthetic provider exhausted; no wait permitted")
        return None

    def close(self):
        pass


class ProjectionTests(unittest.TestCase):
    def test_signed_int32_and_optional_null_select_one_canonical_v9(self):
        # Break: treating unix_time as positive, coercing it, or failing to canonicalize omitted null.
        for stamp in (-2147483648, -1, 0, 2147483647):
            with self.subTest(stamp=stamp):
                absent = text_source(message(entities=[entity(unix_time=stamp)]))
                explicit = text_source(message(entities=[entity(unix_time=stamp, formatting_type=None)]))
                self.assertEqual(absent.projection_json, explicit.projection_json)
                projected = json.loads(absent.projection_json)
                self.assertEqual(projected["version"], 9)
                self.assertEqual(projected["message"]["content"]["text"]["entities"][0]["type"],
                    {"@type": DATE, "unix_time": stamp, "formatting_type": None})
                self.assertEqual(set(absent.required_capabilities), {"reply_formatted_targets", "reply_datetime_targets"})
                self.assertFalse(absent.is_media)

    def test_timestamp_and_format_type_are_strict_closed_provider_data(self):
        # Break: int coercion, accepting non-null display semantics, or unknown type fields.
        for stamp in (True, False, 1.0, "1", None, -2147483649, 2147483648):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                text_source(message(entities=[entity(unix_time=stamp)]))
        for formatting in (False, 0, "null", {}, {"@type": "dateTimeFormattingTypeRelative"},
                {"@type": "dateTimeFormattingTypeAbsolute", "format": "yyyy"}):
            with self.subTest(formatting=formatting), self.assertRaises(ValueError):
                text_source(message(entities=[entity(formatting_type=formatting)]))
        for mutation in ("missing", "extra"):
            raw = message()
            typ = raw["content"]["text"]["entities"][0]["type"]
            if mutation == "missing":
                typ.pop("unix_time")
            else:
                typ["timezone"] = "UTC"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                text_source(raw)

    def test_full_escaped_evidence_uses_original_utf16_before_sanitation(self):
        # Break: deriving offsets from normalized/sanitized text or hiding timestamp/null semantics.
        raw = message('😀 Ａ"x" #tag @user tail', [
            entity(offset=3, length=4, unix_time=-1),
            entity("textEntityTypeHashtag", offset=8, length=4),
            entity("textEntityTypeMention", offset=13, length=5),
            entity("textEntityTypeBold", offset=19, length=4)])
        source = text_source(raw)
        target = source.target()
        display = target.text.removeprefix(EVIDENCE_MARKER + "Formatted target: ")
        self.assertEqual(json.loads(display), {
            "text": '😀 A"x" #tag @user tail', "offset_basis": "original source UTF-16 code units",
            "entities": [
                {"type": "date_time", "provider_type": DATE, "offset": 3, "length": 4,
                    "text": 'A"x"', "unix_time": -1, "formatting_type": None},
                {"type": "hashtag", "offset": 8, "length": 4, "text": "#tag"},
                {"type": "mention", "offset": 13, "length": 5, "text": "@user"},
                {"type": "bold", "offset": 19, "length": 4, "text": "tail"}]})
        self.assertIn('A\\"x\\"', target.text)
        self.assertTrue(target.sanitized)
        self.assertFalse(target.truncated)
        self.assertEqual(source.raw_text, raw["content"]["text"]["text"])
        self.assertEqual(set(source.required_capabilities), {"reply_formatted_targets", "reply_datetime_targets",
            "reply_lexical_targets", "reply_identity_targets"})

    def test_surrogate_splits_invalid_ranges_and_entity_shapes_fail_closed(self):
        # Break: using scalar offsets, bool offsets, permissive entity keys or incomplete bounds.
        for offset, length in ((1, 1), (0, 1), (2, 0), (True, 2), (0, False), (-1, 2), (0, 99)):
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                text_source(message("😀Date", [entity(offset=offset, length=length)]))
        raw = message()
        raw["content"]["text"]["entities"][0]["extra"] = None
        with self.assertRaises(ValueError):
            text_source(raw)

    def test_datetime_excludes_every_overlap_but_accepts_adjacency(self):
        # Break: allowing nested DateTime semantics under any source family.
        kinds = (DATE, "textEntityTypeBold", "textEntityTypeHashtag", "textEntityTypeMentionName")
        for kind in kinds:
            for offset, length in ((0, 4), (1, 2), (0, 8), (3, 3)):
                with self.subTest(kind=kind, offset=offset, length=length), self.assertRaises(ValueError):
                    text_source(message("DateTail", [entity(), entity(kind, offset=offset, length=length)]))
            source = text_source(message("DateTail", [entity(), entity(kind, offset=4, length=4)]))
            self.assertEqual(len(json.loads(source.projection_json)["message"]["content"]["text"]["entities"]), 2)

    def test_timestamp_mutation_changes_source_and_preview_binding(self):
        # Break: hashing display-only data, retaining provider objects or omitting timestamps from the digest.
        raw = message()
        source = text_source(raw)
        before = source.projection_json
        raw["content"]["text"]["entities"][0]["type"]["unix_time"] += 1
        changed = text_source(raw)
        self.assertEqual(source.projection_json, before)
        self.assertNotEqual(source.source_sha256, changed.source_sha256)
        self.assertNotEqual(source.target().text, changed.target().text)
        self.assertEqual(source.source_sha256, hashlib.sha256(before.encode("utf-8")).hexdigest())

    def test_version_confusion_and_noncanonical_private_v9_are_rejected(self):
        # Break: reading a DateTime source under old authority or using v9 for non-DateTime data.
        value = json.loads(text_source().projection_json)
        for version in range(1, 9):
            confused = copy.deepcopy(value)
            confused["version"] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                ReplySource(encoded(confused))
        no_date = json.loads(text_source(message(entities=[entity("textEntityTypeBold")])).projection_json)
        no_date["version"] = 9
        with self.assertRaises(ValueError):
            ReplySource(encoded(no_date))
        value["message"]["content"]["text"]["entities"][0]["type"].pop("formatting_type")
        with self.assertRaises(ValueError):
            ReplySource(encoded(value))

    def test_media_caption_datetime_remains_unavailable(self):
        # Break: accidentally passing the DateTime validator through media source admission.
        for formatting in (OMITTED, None):
            raw = message()
            raw["content"] = document_content({"@type": "formattedText", "text": "Date",
                "entities": [entity(formatting_type=formatting)]})
            with self.subTest(formatting=formatting), self.assertRaises(ValueError):
                text_source(raw)

    def test_source_entity_and_complete_preview_bounds_do_not_truncate(self):
        # Break: unbounded entity work or silently replacing evidence with a shortened preview.
        accepted = text_source(message("D" * 32, [entity(length=1),
            *[entity("textEntityTypeBold", offset=i, length=1) for i in range(1, 32)]]))
        self.assertEqual(len(json.loads(accepted.projection_json)["message"]["content"]["text"]["entities"]), 32)
        with self.assertRaises(ValueError):
            text_source(message("D" * 33, [entity(offset=i, length=1) for i in range(33)]))
        for text in ("x" * 4097, '"' * 2100, "\x00Date", "\ud800Date", " " * 4):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                text_source(message(text, [entity(length=1)]))

    def test_existing_text_families_keep_their_exact_projection_and_authority(self):
        # Break: broadening old authority or emitting v9 for a non-DateTime source.
        cases = (([], 1, set()), ([entity("textEntityTypeBold")], 3, {"reply_formatted_targets"}),
            ([entity("textEntityTypeHashtag")], 5, {"reply_formatted_targets", "reply_lexical_targets"}),
            ([entity("textEntityTypeMentionName")], 7, {"reply_formatted_targets", "reply_identity_targets"}))
        for entities, version, caps in cases:
            with self.subTest(version=version):
                raw = message(entities=entities)
                source = text_source(raw)
                expected = {"version": version, "anchor": {"chat_id": CHAT, "message_id": MESSAGE}, "message": raw}
                self.assertEqual(source.projection_json, encoded(expected))
                self.assertEqual(set(source.required_capabilities), caps)
                if version == 1:
                    self.assertEqual(source.target().text, EVIDENCE_MARKER + "Date")
                else:
                    label = {3: "bold", 5: "hashtag", 7: "mention_name"}[version]
                    span = {"type": label, "offset": 0, "length": 4, "text": "Date"}
                    if version == 7:
                        span["user_id"] = 51
                    self.assertEqual(source.target().text, EVIDENCE_MARKER + "Formatted target: " + encoded({
                        "text": "Date", "offset_basis": "original source UTF-16 code units", "entities": [span]}))

    def test_existing_media_families_keep_exact_bytes_display_and_authority(self):
        # Break: propagating DateTime's new version or renderer flags into existing media families.
        cases = (([], 2, set()), ([entity("textEntityTypeBold")], 4, {"reply_formatted_targets"}),
            ([entity("textEntityTypeHashtag")], 6, {"reply_formatted_targets", "reply_lexical_targets"}),
            ([entity("textEntityTypeMentionName")], 8, {"reply_formatted_targets", "reply_identity_targets"}))
        media = {"file_name": "fixture.txt", "mime_type": "text/plain", "size": 4,
            "unique_id_sha256": hashlib.sha256(b"unique-73").hexdigest()}
        for entities, version, caps in cases:
            with self.subTest(version=version):
                raw = message()
                caption = {"@type": "formattedText", "text": "Date", "entities": entities}
                raw["content"] = document_content(caption)
                source = text_source(raw)
                expected_message = {**raw, "content": {"kind": "document", "caption": caption, "media": media}}
                self.assertEqual(source.projection_json, encoded({"version": version,
                    "anchor": {"chat_id": CHAT, "message_id": MESSAGE}, "message": expected_message}))
                self.assertEqual(set(source.required_capabilities), {"reply_media_targets", *caps})
                expected_caption = "Date"
                if version != 2:
                    span = {"type": {4: "bold", 6: "hashtag", 8: "mention_name"}[version],
                        "offset": 0, "length": 4, "text": "Date"}
                    if version == 8:
                        span["user_id"] = 51
                    expected_caption = {"text": "Date", "offset_basis": "original source UTF-16 code units", "entities": [span]}
                self.assertEqual(source.target().text, EVIDENCE_MARKER + "Media target: " + encoded({
                    "kind": "document", "caption": expected_caption, "media": media}))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="datetime-risk-")
        self.root = Path(self.temporary.name)
        self.raw = SyntheticRaw()
        self.provider = TDLibClient(raw=self.raw, session_directory=self.root / "unused-session",
            credential_loader=lambda: self.fail("credentials must never be loaded"))
        self.provider._ready = True
        self.dialogs = []
        self.approval_action = lambda: True

        def owner(**kwargs):
            self.dialogs.append(copy.deepcopy(kwargs))
            return self.approval_action()

        self.store = ArtifactStore(cache_dir=self.root / "artifacts")
        payload = self.root / "fixture.txt"
        payload.write_bytes(b"data")
        self.artifact = self.store.store(payload)
        self.broker = Broker(socket_path=self.root / "unused.sock", client_factory=lambda: self.provider,
            artifact_store=self.store, approval_prompt=owner, policy=RuntimePolicy(enabled_capabilities=CAPABILITIES))
        self.stack = ExitStack()
        staging = self.root / "staging"
        self.stack.enter_context(patch("telegram_search_mcp.tdjson.STAGING_ROOT", staging))
        self.stack.enter_context(patch("telegram_search_mcp.broker.stage_approved_document",
            functools.partial(stage_approved_document, root=staging)))
        self.stack.enter_context(patch("telegram_search_mcp.broker.retire_staged_document",
            functools.partial(retire_staged_document, root=staging)))

    def tearDown(self):
        self.stack.close()
        self.broker.shutdown()
        self.broker._executor.shutdown(wait=True)
        self.provider.close()
        self.temporary.cleanup()

    def prepare(self, family="text"):
        anchor = {"chat_id": CHAT, "message_id": MESSAGE}
        if family == "text":
            request = PrepareReplyTextSendRequest(recipient=CHAT, text="Plain reply", reply_to=anchor)
            return self.broker._prepare_reply_text_send(request, client_id=CLIENT)
        request = PrepareReplyArtifactSendRequest(recipient=CHAT, artifact_id=self.artifact.artifact_id,
            display_name="fixture.txt", mime_type="text/plain", kind="document", caption="Plain caption", reply_to=anchor)
        return self.broker._prepare_reply_artifact_send(request, client_id=CLIENT)

    def inspect(self, draft_id, family="text"):
        if family == "text":
            return self.broker._get_reply_draft(GetReplyDraftRequest(draft_id=draft_id), client_id=CLIENT)
        return self.broker._get_reply_artifact_draft(GetReplyArtifactDraftRequest(draft_id=draft_id), client_id=CLIENT)

    def send(self, draft_id, family="text", approved=True):
        if family == "text":
            return self.broker._send_prepared_text(SendPreparedTextRequest(draft_id=draft_id, approved=approved), client_id=CLIENT)
        return self.broker._send_prepared_artifact(SendPreparedArtifactRequest(draft_id=draft_id, approved=approved), client_id=CLIENT)

    def effects(self):
        return [r for r in self.raw.requests if r["@type"] == "sendMessage"]

    def test_new_capability_is_default_off_and_trusted_config_can_enable_it(self):
        # Break: enabling new sources by default or failing closed config recognition.
        self.assertNotIn("reply_datetime_targets", RuntimePolicy().enabled_capabilities)
        directory = self.root / "policy"
        directory.mkdir(mode=0o700)
        path = directory / "runtime.toml"
        path.write_text('config_version = 1\nenabled_capabilities = ["reply_datetime_targets"]\n', encoding="utf-8")
        os.chmod(path, 0o600)
        self.assertEqual(load_runtime_policy(path).enabled_capabilities, ("reply_datetime_targets",))

    def test_each_reply_path_requires_the_exact_mixed_source_authority(self):
        # Break: admitting DateTime under only formatted, or overlooking coexisting identity/lexical entities.
        self.raw.source = message("Date#tag@user", [entity(), entity("textEntityTypeHashtag", offset=4),
            entity("textEntityTypeMention", offset=8, length=5)])
        for family in ("text", "artifact"):
            for missing in ("reply_formatted_targets", "reply_datetime_targets", "reply_lexical_targets", "reply_identity_targets"):
                self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != missing))
                with self.subTest(family=family, missing=missing):
                    refused = self.prepare(family)
                    self.assertEqual(refused.model_dump(mode="json"), {"status": "unavailable", "reply": None, "detail": "reply draft is unavailable"})
            self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
            self.assertEqual(self.prepare(family).status, "prepared")
        self.assertEqual(self.effects(), [])
        self.assertEqual(self.dialogs, [])

    def test_datetime_only_does_not_require_unrelated_source_capabilities(self):
        # Break: charging authority for absent source families.
        self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES[:5])
        self.assertEqual(self.prepare().status, "prepared")
        self.assertEqual(self.prepare("artifact").status, "prepared")

    def test_prepare_inspect_update_preserve_immutable_source_and_invalidate_prior_revision(self):
        # Break: refreshing source during an outgoing-only update or leaving old revisions sendable.
        for family in ("text", "artifact"):
            with self.subTest(family=family):
                prepared = self.prepare(family)
                self.assertEqual(prepared.status, "prepared")
                old = prepared.reply.draft.draft_id
                before = len([r for r in self.raw.requests if r["@type"] == "getMessage"])
                self.raw.source["content"]["text"]["entities"][0]["type"]["unix_time"] += 1
                inspected = self.inspect(old, family)
                self.assertEqual(inspected.reply, prepared.reply)
                if family == "text":
                    revised = self.broker._revise_reply_draft(UpdateReplyDraftRequest(draft_id=old, text="Revised"), client_id=CLIENT)
                else:
                    revised = self.broker._revise_reply_artifact_draft(UpdateReplyArtifactDraftRequest(draft_id=old, caption="Revised"), client_id=CLIENT)
                self.assertEqual(revised.status, "revised")
                self.assertEqual(revised.reply.reply_target, prepared.reply.reply_target)
                self.assertNotEqual(revised.reply.preview_sha256, prepared.reply.preview_sha256)
                self.assertEqual(len([r for r in self.raw.requests if r["@type"] == "getMessage"]), before)
                self.assertEqual(self.inspect(old, family).status, "unavailable")
                self.assertEqual(self.send(old, family).status, "expired")
        self.assertEqual(self.effects(), [])

    def test_refresh_requires_old_and_new_source_authority_before_invalidating_old(self):
        # Break: laundering old authority through refresh or accepting a newly introduced family.
        for family in ("text", "artifact"):
            with self.subTest(family=family):
                self.raw.source = message()
                prepared = self.prepare(family)
                self.assertEqual(prepared.status, "prepared")
                old = prepared.reply.draft.draft_id
                refresh = (lambda: self.broker._revise_reply_draft(RefreshReplyDraftRequest(draft_id=old), client_id=CLIENT)) if family == "text" else (
                    lambda: self.broker._revise_reply_artifact_draft(RefreshReplyArtifactDraftRequest(draft_id=old), client_id=CLIENT))
                self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != "reply_datetime_targets"))
                before = len(self.raw.requests)
                self.assertEqual(refresh().status, "unavailable")
                self.assertFalse(any(r["@type"] == "getMessage" for r in self.raw.requests[before:]))
                self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != "reply_identity_targets"))
                self.raw.source = message("Date@user", [entity(), entity("textEntityTypeMention", offset=4, length=5)])
                self.assertEqual(refresh().status, "unavailable")
                self.assertEqual(self.inspect(old, family).reply, prepared.reply)
                self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
                revised = refresh()
                self.assertEqual(revised.status, "revised")
                self.assertNotEqual(revised.reply.reply_target.source_sha256, prepared.reply.reply_target.source_sha256)
                self.assertNotEqual(revised.reply.preview_sha256, prepared.reply.preview_sha256)
                self.assertEqual(self.inspect(old, family).status, "unavailable")

    def test_source_timestamp_or_format_drift_after_owner_rendering_never_sends(self):
        # Break: trusting the displayed snapshot instead of re-reading DateTime semantics before transport.
        for family in ("text", "artifact"):
            for mutation in ("timestamp", "format"):
                with self.subTest(family=family, mutation=mutation):
                    self.raw.source = message()
                    prepared = self.prepare(family)
                    self.assertEqual(prepared.status, "prepared")
                    draft_id = prepared.reply.draft.draft_id
                    frozen = prepared.reply.model_dump(mode="json")
                    def drift():
                        typ = self.raw.source["content"]["text"]["entities"][0]["type"]
                        if mutation == "timestamp":
                            typ["unix_time"] += 1
                        else:
                            typ["formatting_type"] = {"@type": "dateTimeFormattingTypeRelative"}
                        return True
                    self.approval_action = drift
                    result = self.send(draft_id, family)
                    self.assertEqual(result.status, "failed")
                    key = "reply_preview" if family == "text" else "reply_artifact_preview"
                    self.assertEqual(self.dialogs[-1][key], frozen)
                    self.assertEqual(self.effects(), [])
                    status = self.broker._get_send_status(GetSendStatusRequest(draft_id=draft_id), client_id=CLIENT)
                    self.assertEqual((status.status, status.evidence), ("failed", "local_failed"))
                    self.assertEqual(self.send(draft_id, family).status, "failed")
                    self.assertEqual(self.effects(), [])

    def test_capability_revoked_during_dialog_prevents_claim_and_raw_send(self):
        # Break: assuming source authority remains current across owner confirmation.
        for family in ("text", "artifact"):
            self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
            prepared = self.prepare(family)
            self.assertEqual(prepared.status, "prepared")
            def revoke():
                self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != "reply_datetime_targets"))
                return True
            self.approval_action = revoke
            self.assertEqual(self.send(prepared.reply.draft.draft_id, family).status, "expired")
        self.assertEqual(self.effects(), [])

    def test_owner_denial_and_false_approval_retain_pending_snapshot_without_effect(self):
        # Break: consuming drafts or touching transport after either missing approval channel.
        self.approval_action = lambda: False
        for family in ("text", "artifact"):
            with self.subTest(family=family):
                prepared = self.prepare(family)
                self.assertEqual(prepared.status, "prepared")
                draft_id = prepared.reply.draft.draft_id
                before = len(self.dialogs)
                self.assertEqual(self.send(draft_id, family, approved=False).status, "not_approved")
                self.assertEqual(len(self.dialogs), before)
                self.assertEqual(self.send(draft_id, family).status, "not_approved")
                self.assertEqual(self.inspect(draft_id, family).reply, prepared.reply)
        self.assertEqual(self.effects(), [])

    def test_each_path_sends_plain_raw_entities_once_and_status_has_no_source_data(self):
        # Break: forwarding source entities, missing exact reply correlation, or resending a receipt.
        for family in ("text", "artifact"):
            with self.subTest(family=family):
                prepared = self.prepare(family)
                self.assertEqual(prepared.status, "prepared")
                draft_id = prepared.reply.draft.draft_id
                result = self.send(draft_id, family)
                self.assertEqual((result.status, result.message_id), ("sent", 902))
                request = self.effects()[-1]
                self.assertEqual(request["reply_to"], {"@type": "inputMessageReplyToMessage", "message_id": MESSAGE,
                    "quote": None, "checklist_task_id": 0, "poll_option_id": ""})
                outgoing = request["input_message_content"]["text" if family == "text" else "caption"]
                self.assertEqual(outgoing, {"@type": "formattedText", "text": "Plain reply" if family == "text" else "Plain caption", "entities": []})
                _validate_bounded_request({k: v for k, v in request.items() if k != "@extra"})
                forged = copy.deepcopy(request)
                forged.pop("@extra")
                forged["input_message_content"]["text" if family == "text" else "caption"]["entities"] = [entity(formatting_type=None)]
                with self.assertRaises(ForbiddenTDLibRequest):
                    _validate_bounded_request(forged)
                before = len(self.effects())
                before_dialog = len(self.dialogs)
                self.assertEqual(self.send(draft_id, family).status, "sent")
                self.assertEqual((len(self.effects()), len(self.dialogs)), (before, before_dialog))
                status = self.broker._get_send_status(GetSendStatusRequest(draft_id=draft_id), client_id=CLIENT)
                self.assertEqual((status.status, status.evidence, status.message_id), ("sent", "provider_confirmed", 902))
                self.assertEqual(set(status.model_dump()), {"draft_id", "status", "evidence", "detail", "message_id"})
                self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != "reply_datetime_targets"))
                self.assertEqual(self.send(draft_id, family).status, "expired")
                self.assertEqual(len(self.effects()), before)
                self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
        hydration = [{k: v for k, v in r.items() if k != "@extra"} for r in self.raw.requests if r["@type"] == "getMessage"]
        self.assertTrue(hydration)
        self.assertTrue(all(r == {"@type": "getMessage", "chat_id": CHAT, "message_id": MESSAGE} for r in hydration))
        self.assertEqual(len(self.effects()), 2)

    def test_datetime_pending_draft_cancel_stays_unsendable(self):
        # Break: allowing cancellation to resurrect a DateTime-bound revision.
        prepared = self.prepare()
        self.assertEqual(prepared.status, "prepared")
        draft_id = prepared.reply.draft.draft_id
        self.assertEqual(self.broker._cancel_draft(CancelDraftRequest(draft_id=draft_id), client_id=CLIENT).status, "cancelled")
        self.assertEqual(self.send(draft_id).status, "expired")
        self.assertEqual(self.effects(), [])

    def test_actual_owner_renderer_keeps_complete_date_evidence_and_hashes(self):
        # Break: dropping DateTime facts or bound digests when rendering the out-of-band owner surface.
        for family in ("text", "artifact"):
            prepared = self.prepare(family)
            self.assertEqual(prepared.status, "prepared")
            self.send(prepared.reply.draft.draft_id, family, approved=False)
            kwargs = {"recipient_title": "Synthetic room", "recipient": CHAT,
                "display_name": prepared.reply.draft.display_name or "text", "size_bytes": prepared.reply.draft.size_bytes,
                "sha256": prepared.reply.draft.sha256, "caption": prepared.reply.draft.caption,
                "text": prepared.reply.draft.text or "", "kind": prepared.reply.draft.kind,
                "reply_preview" if family == "text" else "reply_artifact_preview": prepared.reply.model_dump(mode="json")}
            captured = []
            def no_process(command, **options):
                captured.append(command)
                return SimpleNamespace(returncode=0, stdout=b"APPROVED")
            with patch("telegram_search_mcp.approval_prompt.subprocess.run", no_process):
                self.assertTrue(confirm_approved_send(**kwargs))
            self.assertEqual(len(captured), 1)
            rendered = captured[0][-1]
            self.assertIn(prepared.reply.reply_target.text, rendered)
            self.assertIn(prepared.reply.reply_target.source_sha256, rendered)
            self.assertIn(prepared.reply.preview_sha256, rendered)
            self.assertIn('"formatting_type":null', rendered)
            self.assertIn('"provider_type":"textEntityTypeDateTime"', rendered)


class ClosedInputTests(unittest.TestCase):
    def test_source_datetime_controls_never_enter_existing_public_requests(self):
        # Break: allowing caller-controlled timestamp/format/source entities into public draft inputs.
        draft_id = "draft_" + "c" * 32
        anchor = {"chat_id": CHAT, "message_id": MESSAGE}
        artifact = "artifact_" + "a" * 32 + "_" + "b" * 64 + "_4"
        cases = (
            (PrepareReplyTextSendRequest, {"recipient": CHAT, "text": "Plain", "reply_to": anchor}),
            (GetReplyDraftRequest, {"draft_id": draft_id}),
            (UpdateReplyDraftRequest, {"draft_id": draft_id, "text": "Plain"}),
            (RefreshReplyDraftRequest, {"draft_id": draft_id}),
            (PrepareReplyArtifactSendRequest, {"recipient": CHAT, "artifact_id": artifact, "kind": "document",
                "display_name": "fixture.txt", "mime_type": "text/plain", "caption": "", "reply_to": anchor}),
            (GetReplyArtifactDraftRequest, {"draft_id": draft_id}),
            (UpdateReplyArtifactDraftRequest, {"draft_id": draft_id, "caption": "Plain"}),
            (RefreshReplyArtifactDraftRequest, {"draft_id": draft_id}),
        )
        for model, payload in cases:
            model.model_validate(payload)
            for key, value in (("unix_time", 1700000000), ("formatting_type", None), ("entities", [entity()])):
                with self.subTest(model=model.__name__, key=key), self.assertRaises(ValidationError):
                    model.model_validate({**payload, key: value})


if __name__ == "__main__":
    unittest.main()
