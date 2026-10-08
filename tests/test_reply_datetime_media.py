"""Fresh F13 boundary tests; all provider/artifact fixtures are synthetic.

Breaks caught: losing original DateTime evidence at the media intersection,
underreporting its conjunctive authority, accepting relabelled/malformed stored
facts, and allowing an approval to survive source/revision changes. No accepted
test helpers, TDLib initialization, network, or broker runtime is used.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.draft_models import DraftPreview
from telegram_search_mcp.outgoing_drafts import DraftError, DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.reply_artifact_drafts import ReplyArtifactDraftPreview
from telegram_search_mcp.reply_drafts import ReplyDraftPreview, ReplySource, source_from_message
from telegram_search_mcp.tdjson import TDLibClient, MessageSendNotAttempted


CHAT = -731
MESSAGE = 2048
ACCOUNT = 61
MARKER = '[untrusted Telegram evidence] '
UNIQUE_SHA = 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
BASE_CAPS = ('reply_media_targets', 'reply_formatted_targets', 'reply_datetime_targets')
OWNER = DraftOwner('client_datetime_media_fresh_000001', ACCOUNT)


def wire_json(value):
    """Independent wire oracle: serialize literals, never production builders."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def entity(offset=2, length=5, kind='textEntityTypeDateTime', **fields):
    if kind == 'textEntityTypeDateTime' and not fields:
        fields = {'unix_time': -1}
    return {'@type': 'textEntity', 'offset': offset, 'length': length,
            'type': {'@type': kind, **fields}}


def caption(text='😀today', entities=None):
    return {'@type': 'formattedText', 'text': text,
            'entities': [entity()] if entities is None else copy.deepcopy(entities)}


def provider_file():
    return {'@type': 'file', 'id': 901, 'size': 17, 'expected_size': 17,
            'local': {'@type': 'localFile', 'path': '/synthetic/provider-private.bin',
                      'can_be_downloaded': True, 'can_be_deleted': False,
                      'is_downloading_active': False, 'is_downloading_completed': False,
                      'download_offset': 0, 'downloaded_prefix_size': 0, 'downloaded_size': 0},
            'remote': {'@type': 'remoteFile', 'id': 'synthetic-raw-file-id', 'unique_id': 'abc',
                       'is_uploading_active': False, 'is_uploading_completed': False, 'uploaded_size': 0}}


def provider_content(kind, formatted=None):
    formatted = caption() if formatted is None else copy.deepcopy(formatted)
    if kind == 'document':
        return {'@type': 'messageDocument', 'caption': formatted,
                'document': {'@type': 'document', 'file_name': 'synthetic.txt',
                             'mime_type': 'text/plain', 'document': provider_file()}}
    if kind == 'photo':
        return {'@type': 'messagePhoto', 'caption': formatted, 'has_spoiler': False,
                'is_secret': False, 'show_caption_above_media': False,
                'photo': {'@type': 'photo', 'has_stickers': False,
                          'sizes': [{'@type': 'photoSize', 'type': 'm', 'width': 30, 'height': 20,
                                     'photo': provider_file(), 'progressive_sizes': []}]}}
    if kind == 'voice_note':
        return {'@type': 'messageVoiceNote', 'caption': formatted, 'is_listened': False,
                'voice_note': {'@type': 'voiceNote', 'duration': 3, 'waveform': 'AA==',
                               'mime_type': 'audio/ogg', 'voice': provider_file()}}
    raise AssertionError('unknown fixture kind')


def shell(content):
    # Explicit relevant TDLib shell, independent from the source projection.
    return {'@type': 'message', 'chat_id': CHAT, 'id': MESSAGE,
            'sender_id': {'@type': 'messageSenderUser', 'user_id': 62},
            'is_outgoing': False, 'is_from_offline': False, 'ephemeral_message_id': 0,
            'date': 1700000000, 'edit_date': 0, 'self_destruct_in': 0.0, 'auto_delete_in': 0.0,
            'sending_state': None, 'scheduling_state': None, 'topic_id': None,
            'self_destruct_type': None, 'ephemeral_content': None, 'receiver_id': None,
            'reply_to': None, 'forward_info': None, 'import_info': None, 'reply_markup': None,
            'content': copy.deepcopy(content)}


def message(kind='document', formatted=None):
    return shell(provider_content(kind, formatted))


def stable_media(kind):
    if kind == 'document':
        return {'file_name': 'synthetic.txt', 'mime_type': 'text/plain', 'size': 17,
                'unique_id_sha256': UNIQUE_SHA}
    if kind == 'photo':
        return {'variants': [{'type': 'm', 'width': 30, 'height': 20, 'size': 17,
                              'unique_id_sha256': UNIQUE_SHA}]}
    return {'duration': 3, 'mime_type': 'audio/ogg', 'size': 17, 'unique_id_sha256': UNIQUE_SHA}


def expected_projection(kind='document', formatted=None, version=10):
    if formatted is None:
        formatted = caption(entities=[entity(unix_time=-1, formatting_type=None)])
    content = {'kind': kind, 'caption': copy.deepcopy(formatted), 'media': stable_media(kind)}
    return {'version': version, 'anchor': {'chat_id': CHAT, 'message_id': MESSAGE},
            'message': shell(content)}


def expected_datetime_display(kind):
    return MARKER + 'Media target: ' + wire_json({
        'kind': kind, 'caption': {'text': '😀today',
            'offset_basis': 'original source UTF-16 code units',
            'entities': [{'type': 'date_time', 'offset': 2, 'length': 5, 'text': 'today',
                          'provider_type': 'textEntityTypeDateTime', 'unix_time': -1,
                          'formatting_type': None}]}, 'media': stable_media(kind)})


def draft_preview(draft):
    facts = {'draft_id': draft.draft_id, 'account_id': ACCOUNT, 'recipient': CHAT,
             'recipient_title': draft.recipient_title, 'kind': draft.kind,
             'expires_at': datetime.fromtimestamp(draft.expires_at, timezone.utc),
             'sha256': draft.sha256, 'size_bytes': draft.size_bytes}
    if draft.kind == 'text':
        facts['text'] = draft.text
    else:
        facts.update(artifact_id=draft.artifact_id, display_name=draft.display_name,
                     mime_type=draft.mime_type, caption=draft.caption)
    return DraftPreview(**facts)


class CaptionProjectionTests(unittest.TestCase):
    def test_each_media_kind_preserves_exact_caption_timestamp_and_stable_facts(self):
        # Catches v10 projection/display losing DateTime or leaking raw file facts.
        for kind in ('document', 'photo', 'voice_note'):
            with self.subTest(kind=kind):
                raw = message(kind)
                source = source_from_message(raw, CHAT, MESSAGE)
                expected = wire_json(expected_projection(kind))
                self.assertEqual(source.projection_json, expected)
                self.assertEqual(source.source_sha256, hashlib.sha256(expected.encode()).hexdigest())
                self.assertEqual(source.target().text, expected_datetime_display(kind))
                self.assertFalse(source.target().sanitized)
                self.assertFalse(source.target().truncated)
                self.assertTrue(source.is_media)
                self.assertEqual(source.required_capabilities, BASE_CAPS)
                self.assertEqual(ReplySource(expected).projection_json, expected)
                raw['content']['caption']['text'] = 'mutated provider object'
                self.assertEqual(source.projection_json, expected)
                for private in ('provider-private', 'synthetic-raw-file-id', '"id":901', '"path"'):
                    self.assertNotIn(private, source.projection_json)
                    self.assertNotIn(private, source.target().text)

    def test_omitted_and_null_format_share_identity_without_datetime_conversion(self):
        # Catches provider nullable omission changing authority/digest or interpreting time.
        for timestamp in (-2147483648, -1, 0, 2147483647):
            with self.subTest(timestamp=timestamp):
                omitted = message(formatted=caption(entities=[entity(unix_time=timestamp)]))
                explicit = message(formatted=caption(entities=[entity(unix_time=timestamp, formatting_type=None)]))
                left = source_from_message(omitted, CHAT, MESSAGE)
                right = source_from_message(explicit, CHAT, MESSAGE)
                self.assertEqual(left.projection_json, right.projection_json)
                evidence = json.loads(left.target().text.split('Media target: ', 1)[1])
                self.assertEqual(evidence['caption']['entities'], [{
                    'type': 'date_time', 'offset': 2, 'length': 5, 'text': 'today',
                    'provider_type': 'textEntityTypeDateTime', 'unix_time': timestamp, 'formatting_type': None}])

    def test_adjacent_entities_keep_their_complete_minimal_capability_sets(self):
        # Catches precedence shortcuts erasing adjacent lexical/identity authority.
        cases = [([], BASE_CAPS),
                 ([entity(8, 5, 'textEntityTypeHashtag')], BASE_CAPS + ('reply_lexical_targets',)),
                 ([entity(14, 4, 'textEntityTypeMentionName', user_id=53)], BASE_CAPS + ('reply_identity_targets',)),
                 ([entity(8, 5, 'textEntityTypeHashtag'), entity(14, 4, 'textEntityTypeMentionName', user_id=53)],
                  BASE_CAPS + ('reply_lexical_targets', 'reply_identity_targets'))]
        for kind in ('document', 'photo', 'voice_note'):
            for adjacent, want in cases:
                with self.subTest(kind=kind, adjacent=adjacent):
                    entities = [entity(), *adjacent, entity(19, 4, 'textEntityTypeBold')]
                    source = source_from_message(message(kind, caption('😀today #plan @Sam Bold', entities)), CHAT, MESSAGE)
                    self.assertEqual(source.required_capabilities, want)
                    with self.assertRaises(ValueError):
                        _ = source.required_capability
                    spans = json.loads(source.target().text.split('Media target: ', 1)[1])['caption']['entities']
                    self.assertEqual(spans[0]['text'], 'today')
                    self.assertEqual(spans[-1], {'type': 'bold', 'offset': 19, 'length': 4, 'text': 'Bold'})
                    if 'reply_identity_targets' in want:
                        self.assertIn({'type': 'mention_name', 'offset': 14, 'length': 4, 'text': '@Sam', 'user_id': 53}, spans)

    def test_canonical_entity_order_is_independent_of_provider_order(self):
        # Catches order-dependent hashes for identical, nonoverlapping evidence.
        entities = [entity(7, 1, 'textEntityTypeBold'), entity()]
        first = source_from_message(message(formatted=caption('😀today!', entities)), CHAT, MESSAGE)
        second = source_from_message(message(formatted=caption('😀today!', list(reversed(entities)))), CHAT, MESSAGE)
        self.assertEqual(first.projection_json, second.projection_json)
        self.assertEqual(json.loads(first.projection_json)['message']['content']['caption']['entities'], [
            entity(unix_time=-1, formatting_type=None), entity(7, 1, 'textEntityTypeBold')])

    def test_utf16_covered_text_precedes_display_sanitation(self):
        # Catches scalar offsets/NFKC text replacing exact provider coordinates.
        raw = message(formatted=caption('😀Ａtoday', [entity(3, 5)]))
        source = source_from_message(raw, CHAT, MESSAGE)
        self.assertEqual(json.loads(source.projection_json)['message']['content']['caption']['text'], '😀Ａtoday')
        evidence = json.loads(source.target().text.split('Media target: ', 1)[1])
        self.assertEqual(evidence['caption']['text'], '😀Atoday')
        self.assertEqual(evidence['caption']['entities'][0]['offset'], 3)
        self.assertEqual(evidence['caption']['entities'][0]['text'], 'today')
        self.assertTrue(source.target().sanitized)

    def test_private_volatile_file_changes_do_not_change_stable_media_identity(self):
        # Catches accidentally binding approvals to provider paths/download state.
        for kind in ('document', 'photo', 'voice_note'):
            with self.subTest(kind=kind):
                before = message(kind)
                changed = copy.deepcopy(before)
                content = changed['content']
                file = (content['document']['document'] if kind == 'document' else
                        content['photo']['sizes'][0]['photo'] if kind == 'photo' else content['voice_note']['voice'])
                file['id'] = 902
                file['remote']['id'] = 'other-volatile-id'
                file['local']['path'] = '/synthetic/other-private-path'
                self.assertEqual(source_from_message(before, CHAT, MESSAGE).projection_json,
                                 source_from_message(changed, CHAT, MESSAGE).projection_json)
                file['remote']['unique_id'] = 'different-stable-id'
                self.assertNotEqual(source_from_message(before, CHAT, MESSAGE).source_sha256,
                                    source_from_message(changed, CHAT, MESSAGE).source_sha256)


class RejectionTests(unittest.TestCase):
    def reject(self, formatted):
        with self.assertRaises(ValueError):
            source_from_message(message(formatted=formatted), CHAT, MESSAGE)

    def test_timestamp_requires_exact_signed_int32_without_coercion(self):
        # Catches bool/float/string coercion and widening the pinned provider int32.
        for invalid in (True, False, None, '-1', -1.0, -2147483649, 2147483648, [], {}):
            with self.subTest(invalid=invalid):
                self.reject(caption(entities=[entity(unix_time=invalid)]))

    def test_no_nonnull_format_or_open_datetime_shape_is_accepted(self):
        # Catches silently supporting unknown format semantics or extra payload.
        for fmt in ({'@type': 'textEntityTypeDateTimeFormatRelative'},
                    {'@type': 'textEntityTypeDateTimeFormatAbsolute'}, {}, '', False, 0, []):
            with self.subTest(format=fmt):
                self.reject(caption(entities=[entity(unix_time=0, formatting_type=fmt)]))
        for typ in ({'@type': 'textEntityTypeDateTime'},
                    {'@type': 'textEntityTypeDateTime', 'unix_time': 0, 'timezone': 'UTC'}):
            bad = entity()
            bad['type'] = typ
            self.reject(caption(entities=[bad]))

    def test_split_surrogates_zero_negative_boolean_and_out_of_range_spans_are_rejected(self):
        # Catches reuse of scalar offsets and missing UTF-16 boundary checks.
        for offset, length in ((1, 1), (0, 1), (2, 0), (-1, 5), (True, 5), (2, True),
                               (2, 6), (2147483648, 1), (2, 2147483648)):
            with self.subTest(offset=offset, length=length):
                self.reject(caption(entities=[entity(offset, length)]))

    def test_every_overlap_with_datetime_is_rejected_even_nested_styles(self):
        # Catches treating DateTime as an ordinary nestable style.
        for other in (entity(2, 5, 'textEntityTypeBold'), entity(3, 1, 'textEntityTypeItalic'),
                      entity(0, 7, 'textEntityTypeBlockQuote'), entity(3, 4, 'textEntityTypeHashtag'),
                      entity(3, 4, 'textEntityTypeMentionName', user_id=53), entity(3, 4), entity()):
            with self.subTest(other=other):
                self.reject(caption(entities=[entity(), other]))

    def test_datetime_does_not_bypass_adjacent_entity_validation(self):
        # Catches accepting identity/style/link data merely because v10 was chosen.
        for adjacent in (entity(8, 4, 'textEntityTypeMentionName', user_id=True),
                         entity(8, 4, 'textEntityTypeMentionName', user_id=0),
                         entity(8, 4, 'textEntityTypeUrl'),
                         entity(8, 4, 'textEntityTypeCustomEmoji', custom_emoji_id=1),
                         entity(8, 4, 'textEntityTypeBold', ignored=True)):
            with self.subTest(adjacent=adjacent):
                self.reject(caption('😀today next', [entity(), adjacent]))

    def test_closed_caption_shape_entity_count_and_full_evidence_bounds_fail_closed(self):
        # Catches truncation or silently dropping malformed surplus caption data.
        for change in ('extra_caption', 'extra_entity', 'wrong_entity_type', 'too_many', 'oversize'):
            bad = caption()
            if change == 'extra_caption': bad['ignored'] = True
            elif change == 'extra_entity': bad['entities'][0]['ignored'] = True
            elif change == 'wrong_entity_type': bad['entities'][0]['@type'] = 'notTextEntity'
            elif change == 'too_many': bad['entities'] = [entity()] * 33
            else: bad['text'] = '😀today' + 'x' * 1024
            with self.subTest(change=change): self.reject(bad)
        self.reject(caption('"' * 1024, [entity(0, 1024)]))

    def test_v10_presence_and_old_version_relabelling_cannot_erase_authority(self):
        # Catches deserializing v10 without DateTime or downgrading its capability set.
        for legacy_version in range(1, 10):
            bad = expected_projection(version=legacy_version)
            with self.subTest(version=legacy_version), self.assertRaises(ValueError):
                ReplySource(wire_json(bad))
        for replacement in ([], [entity(2, 5, 'textEntityTypeBold')], [entity(2, 5, 'textEntityTypeMention')]):
            bad = expected_projection(formatted=caption(entities=replacement))
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                ReplySource(wire_json(bad))
        for invalid_version in (True, 10.0, '10', 11):
            bad = expected_projection(version=invalid_version)
            with self.subTest(version=invalid_version), self.assertRaises(ValueError):
                ReplySource(wire_json(bad))

    def test_v10_roundtrip_rejects_noncanonical_or_open_projected_facts(self):
        # Catches trusted-store inputs gaining weaker checks than provider inputs.
        for alteration in ('extra_top', 'extra_media', 'missing_null', 'raw_content', 'wrong_anchor', 'media_type'):
            bad = expected_projection()
            if alteration == 'extra_top': bad['ignored'] = 1
            elif alteration == 'extra_media': bad['message']['content']['media']['path'] = '/synthetic/private'
            elif alteration == 'missing_null': del bad['message']['content']['caption']['entities'][0]['type']['formatting_type']
            elif alteration == 'raw_content': bad['message']['content'] = provider_content('document')
            elif alteration == 'wrong_anchor': bad['anchor']['message_id'] = MESSAGE + 1
            else: bad['message']['content']['kind'] = 'video'
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                ReplySource(wire_json(bad))
        with self.assertRaises(ValueError):
            ReplySource(json.dumps(expected_projection(), ensure_ascii=False))

    def test_v10_keeps_the_media_kind_safety_checks_at_both_boundaries(self):
        # Catches new DateTime dispatch bypassing the established media validator.
        for kind in ('document', 'photo', 'voice_note'):
            for alteration in ('empty_identity', 'unsafe_kind_facts'):
                with self.subTest(kind=kind, alteration=alteration):
                    raw = message(kind)
                    stored = expected_projection(kind)
                    content = raw['content']
                    facts = stored['message']['content']['media']
                    if kind == 'document':
                        file = content['document']['document']
                        if alteration == 'unsafe_kind_facts':
                            content['document']['file_name'] = 'bad\x00name'
                            facts['file_name'] = 'bad\x00name'
                    elif kind == 'photo':
                        file = content['photo']['sizes'][0]['photo']
                        if alteration == 'unsafe_kind_facts':
                            content['photo']['sizes'][0]['width'] = True
                            facts['variants'][0]['width'] = True
                    else:
                        file = content['voice_note']['voice']
                        if alteration == 'unsafe_kind_facts':
                            content['voice_note']['mime_type'] = 'audio/mpeg'
                            facts['mime_type'] = 'audio/mpeg'
                    if alteration == 'empty_identity':
                        file['size'] = 0
                        (facts['variants'][0] if kind == 'photo' else facts)['size'] = 0
                    with self.assertRaises(ValueError): source_from_message(raw, CHAT, MESSAGE)
                    with self.assertRaises(ValueError): ReplySource(wire_json(stored))


class LegacyBoundaryTests(unittest.TestCase):
    def test_one_literal_manifest_per_prior_version_keeps_its_bytes_display_and_authority(self):
        # New risk only: adding v10 must not relabel/change accepted v1-v9 contracts.
        plain = caption('today', [])
        bold = caption('today', [entity(0, 5, 'textEntityTypeBold')])
        lexical = caption('#plan', [entity(0, 5, 'textEntityTypeHashtag')])
        identity = caption('@Sam', [entity(0, 4, 'textEntityTypeMention')])
        dt = caption('today', [entity(0, 5, unix_time=-1, formatting_type=None)])
        cases = [(1, plain, (), False, None),
                 (2, plain, ('reply_media_targets',), True, None),
                 (3, bold, ('reply_formatted_targets',), False, 'bold'),
                 (4, bold, ('reply_media_targets', 'reply_formatted_targets'), True, 'bold'),
                 (5, lexical, ('reply_formatted_targets', 'reply_lexical_targets'), False, 'hashtag'),
                 (6, lexical, ('reply_media_targets', 'reply_formatted_targets', 'reply_lexical_targets'), True, 'hashtag'),
                 (7, identity, ('reply_formatted_targets', 'reply_identity_targets'), False, 'mention'),
                 (8, identity, ('reply_media_targets', 'reply_formatted_targets', 'reply_identity_targets'), True, 'mention'),
                 (9, dt, ('reply_formatted_targets', 'reply_datetime_targets'), False, 'date_time')]
        for version, fmt, caps, media, label in cases:
            with self.subTest(version=version):
                if media:
                    expected = expected_projection(formatted=fmt, version=version)
                    raw = message(formatted=fmt)
                else:
                    content = {'@type': 'messageText', 'text': fmt, 'link_preview': None, 'link_preview_options': None}
                    raw = shell(content)
                    expected = {'version': version, 'anchor': {'chat_id': CHAT, 'message_id': MESSAGE}, 'message': shell(content)}
                serialized = wire_json(expected)
                source = ReplySource(serialized)
                self.assertEqual(source_from_message(raw, CHAT, MESSAGE).projection_json, serialized)
                self.assertEqual(source.projection_json, serialized)
                self.assertEqual(source.required_capabilities, caps)
                self.assertEqual(source.is_media, media)
                if label is None:
                    want = (MARKER + 'Media target: ' + wire_json({'kind': 'document', 'caption': 'today', 'media': stable_media('document')})
                            if media else MARKER + 'today')
                else:
                    covered = fmt['text']
                    span = {'type': label, 'offset': 0, 'length': len(covered), 'text': covered}
                    if version == 9:
                        span.update(provider_type='textEntityTypeDateTime', unix_time=-1, formatting_type=None)
                    record = {'text': covered, 'offset_basis': 'original source UTF-16 code units', 'entities': [span]}
                    want = (MARKER + 'Media target: ' + wire_json({'kind': 'document', 'caption': record, 'media': stable_media('document')})
                            if media else MARKER + 'Formatted target: ' + wire_json(record))
                self.assertEqual(source.target().text, want)
                relabelled = copy.deepcopy(expected)
                relabelled['version'] = 10
                with self.assertRaises(ValueError): ReplySource(wire_json(relabelled))


class RevisionBoundaryTests(unittest.TestCase):
    def test_text_and_artifact_revisions_keep_source_but_invalidate_old_approval(self):
        # Catches v10 bypassing revision invalidation or losing media capability classification.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ArtifactStore(cache_dir=root / 'cache')
            payload = root / 'new-synthetic.txt'
            payload.write_bytes(b'new synthetic reply payload')
            artifact = store.store(payload)
            registry = OutgoingDraftRegistry(store)
            source = source_from_message(message(), CHAT, MESSAGE)
            for kind in ('text', 'document'):
                with self.subTest(kind=kind):
                    if kind == 'text':
                        original = registry.prepare_text(owner=OWNER, recipient=CHAT, text='first reply', recipient_title='Synthetic chat', reply_source=source)
                        make_preview = ReplyDraftPreview.create
                        change = {'text': 'second reply'}
                    else:
                        original = registry.prepare(owner=OWNER, artifact_id=artifact.artifact_id, recipient=CHAT,
                            display_name='reply.txt', mime_type='text/plain', caption='first reply',
                            recipient_title='Synthetic chat', reply_source=source)
                        make_preview = ReplyArtifactDraftPreview.create
                        change = {'caption': 'second reply'}
                    inspected = registry.peek(original.draft_id, owner=OWNER)
                    first = make_preview(draft_preview(inspected), inspected.reply_source)
                    self.assertEqual(first.reply_target.text, expected_datetime_display('document'))
                    self.assertEqual(registry.source_required_capabilities(original.draft_id, owner=OWNER), BASE_CAPS)
                    revised = registry.revise(original.draft_id, owner=OWNER, recipient_title='Synthetic chat', reply=True, **change)
                    second = make_preview(draft_preview(revised), revised.reply_source)
                    self.assertNotEqual(first.preview_sha256, second.preview_sha256)
                    self.assertEqual(first.reply_target, second.reply_target)
                    with self.assertRaises(DraftError): registry.claim(original.draft_id, owner=OWNER, approved=True)
                    changed = source_from_message(message(formatted=caption(entities=[entity(unix_time=0)])), CHAT, MESSAGE)
                    refreshed = registry.revise(revised.draft_id, owner=OWNER, recipient_title='Synthetic chat', reply=True, reply_source=changed)
                    third = make_preview(draft_preview(refreshed), refreshed.reply_source)
                    self.assertNotEqual(second.preview_sha256, third.preview_sha256)
                    self.assertNotEqual(second.reply_target.source_sha256, third.reply_target.source_sha256)
                    with self.assertRaises(DraftError): registry.claim(revised.draft_id, owner=OWNER, approved=True)
                    with self.assertRaises(DraftError): registry.claim(refreshed.draft_id, owner=OWNER, approved=False)
                    claimed = registry.claim(refreshed.draft_id, owner=OWNER, approved=True)
                    self.assertEqual(claimed.draft.reply_source.source_sha256, changed.source_sha256)
                    registry.finish(refreshed.draft_id, owner=OWNER, status='outcome_unknown')
                    with self.assertRaises(DraftError): registry.claim(refreshed.draft_id, owner=OWNER, approved=True)


class RecordingProvider(TDLibClient):
    """Synthetic I/O boundary; real reply revalidation/content building stays intact."""
    def __init__(self, raw):
        # Deliberately do not initialize TDLib, ctypes, credentials or a broker.
        self.raw = copy.deepcopy(raw)
        self._request_context = SimpleNamespace(artifact_transport_attempted=False)
        self._send_observations = SimpleNamespace(discard_unattempted=lambda attempt: None)
        self.transports = []

    @contextmanager
    def _serialized_reply_call(self):
        yield

    def get_account_id(self): return ACCOUNT

    def resolve_target(self, chat_id):
        if chat_id != CHAT: raise AssertionError('unexpected recipient')
        return {'@type': 'chat', 'id': CHAT, 'title': 'Synthetic chat'}

    def get_message(self, chat_id, message_id):
        if (chat_id, message_id) != (CHAT, MESSAGE): raise AssertionError('unexpected source anchor')
        return copy.deepcopy(self.raw)

    def _call(self, request):
        if request != {'@type': 'getMessageProperties', 'chat_id': CHAT, 'message_id': MESSAGE}:
            raise AssertionError('unexpected provider call')
        return {'@type': 'messageProperties', 'can_be_replied': True}

    def _send_message_content(self, recipient, content, expected, **options):
        if options['pre_transport_guard']() is not True:
            raise MessageSendNotAttempted('revoked before transport')
        self.transports.append((recipient, copy.deepcopy(content), expected, options))
        return 4096


class RevalidationBoundaryTests(unittest.TestCase):
    def send(self, provider, source, kind, guard=lambda: True):
        options = {'reply_source': source, 'expected_account_id': ACCOUNT,
                   'expected_recipient_title': 'Synthetic chat', 'attempt_id': 'synthetic-attempt',
                   'pre_send_guard': guard}
        if kind == 'text':
            return provider.send_reply_text_message(CHAT, 'plain approved reply', **options)
        return provider.send_reply_artifact_message(CHAT, Path('/synthetic/reply.txt'),
            'plain approved caption', kind='document', **options)

    def test_changed_datetime_or_media_evidence_refuses_before_transport_for_both_reply_kinds(self):
        # Catches revalidation hashing only caption text and ignoring time/identity.
        for kind in ('text', 'document'):
            for change in ('timestamp', 'text', 'stable_identity', 'format'):
                with self.subTest(kind=kind, change=change):
                    raw = message()
                    source = source_from_message(raw, CHAT, MESSAGE)
                    provider = RecordingProvider(raw)
                    content = provider.raw['content']
                    if change == 'timestamp': content['caption']['entities'][0]['type']['unix_time'] = 0
                    elif change == 'text': content['caption']['text'] = '😀later'
                    elif change == 'stable_identity': content['document']['document']['remote']['unique_id'] = 'changed'
                    else: content['caption']['entities'][0]['type']['formatting_type'] = {'@type': 'textEntityTypeDateTimeFormatRelative'}
                    with self.assertRaises(MessageSendNotAttempted): self.send(provider, source, kind)
                    self.assertEqual(provider.transports, [])

    def test_missing_any_source_capability_refuses_both_kinds_before_transport(self):
        # Catches each independent opt-in being treated as an alternative authority.
        source = source_from_message(message(), CHAT, MESSAGE)
        for kind in ('text', 'document'):
            for missing in BASE_CAPS:
                with self.subTest(kind=kind, missing=missing):
                    provider = RecordingProvider(message())
                    enabled = set(BASE_CAPS) - {missing}
                    guard = lambda: all(cap in enabled for cap in source.required_capabilities)
                    with self.assertRaises(MessageSendNotAttempted): self.send(provider, source, kind, guard)
                    self.assertEqual(provider.transports, [])

    def test_unchanged_source_reaches_only_exact_plain_reply_transport(self):
        # Catches DateTime evidence escaping into outgoing entities, date actions or recipient choice.
        source = source_from_message(message(), CHAT, MESSAGE)
        for kind in ('text', 'document'):
            with self.subTest(kind=kind):
                provider = RecordingProvider(message())
                self.assertEqual(self.send(provider, source, kind), 4096)
                self.assertEqual(len(provider.transports), 1)
                recipient, content, expected, options = provider.transports[0]
                self.assertEqual(recipient, CHAT)
                self.assertEqual(options['reply_anchor'], (CHAT, MESSAGE))
                self.assertEqual(options['attempt_id'], 'synthetic-attempt')
                if kind == 'text':
                    self.assertEqual(expected, 'messageText')
                    self.assertEqual(content['text'], {'@type': 'formattedText', 'text': 'plain approved reply', 'entities': []})
                    self.assertEqual(content['clear_draft'], False)
                    self.assertEqual(content['link_preview_options'], {
                        '@type': 'linkPreviewOptions', 'is_disabled': True, 'url': '',
                        'force_small_media': False, 'force_large_media': False, 'show_above_text': False})
                else:
                    self.assertEqual(expected, 'messageDocument')
                    self.assertEqual(content['caption'], {'@type': 'formattedText', 'text': 'plain approved caption', 'entities': []})
                    self.assertEqual(content['document'], {'@type': 'inputDocument',
                        'document': {'@type': 'inputFileLocal', 'path': '/synthetic/reply.txt'},
                        'thumbnail': None, 'disable_content_type_detection': True})


if __name__ == '__main__':
    unittest.main()
