"""Fresh v8 risks at source, registry, Broker and TDJSON boundaries.

Expected source shells, media facts and spans are hand-declared. Raw provider I/O
and local owner decisions are synthetic; all product components remain real.
Fixture text was derived by static AST reading, never importing previous tests.
"""
import copy
import hashlib
import json
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch
from pydantic import ValidationError

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.outgoing_drafts import DraftOwner
from telegram_search_mcp.outgoing_stage import stage_approved_document, retire_staged_document
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from telegram_search_mcp.reply_media_sources import media_content as parse_media, validate_content as validate_media, render_content as render_media
from telegram_search_mcp.tdjson import TDLibClient

BASELINE = Path(__file__).with_name('fixtures')/'reply_source_v1_v7.json'
CLIENT = 'client_' + 'a'*24
FOREIGN = 'client_' + 'b'*24
ANCHOR = {'chat_id': 123, 'message_id': 55}
CAPS = ('reply_media_targets', 'reply_formatted_targets', 'reply_identity_targets')
MIXED_CAPS = CAPS + ('reply_lexical_targets',)
POLICY = RuntimePolicy(enabled_capabilities=('send', 'reply_text_send', 'reply_artifact_send', *MIXED_CAPS))
MARKER = '[untrusted Telegram evidence] '
TEXT = '😀 @ada Ada #tag'
KINDS = ('document', 'photo', 'voice_note')
STYLES = (('Bold', 'bold'), ('Italic', 'italic'), ('Underline', 'underline'), ('Strikethrough', 'strikethrough'), ('Spoiler', 'spoiler'), ('Code', 'code'), ('Pre', 'pre'), ('PreCode', 'pre_code'), ('BlockQuote', 'block_quote'), ('ExpandableBlockQuote', 'expandable_block_quote'))
IDENTITIES = [
    {'@type': 'textEntity', 'offset': 3, 'length': 4, 'type': {'@type': 'textEntityTypeMention'}},
    {'@type': 'textEntity', 'offset': 8, 'length': 3, 'type': {'@type': 'textEntityTypeMentionName', 'user_id': 42}},
]
RECORDS = [{'type': 'mention', 'offset': 3, 'length': 4, 'text': '@ada'}, {'type': 'mention_name', 'offset': 8, 'length': 3, 'text': 'Ada', 'user_id': 42}]
MEDIA_SHA = 'c2720445a45267813688ff73fa188aa060c1b661aefaf1650d42f690697b5ab3'
FACTS = {
    'document': {'file_name': 'evidence.txt', 'mime_type': 'text/plain', 'size': 10, 'unique_id_sha256': MEDIA_SHA},
    'photo': {'variants': [{'type': 'i', 'width': 2, 'height': 2, 'size': 10, 'unique_id_sha256': MEDIA_SHA}]},
    'voice_note': {'duration': 2, 'mime_type': 'audio/ogg', 'size': 10, 'unique_id_sha256': MEDIA_SHA},
}
PREFIX = '{"anchor":{"chat_id":123,"message_id":55},"message":{"@type":"message","auto_delete_in":0.0,"chat_id":123,"content":'
SUFFIX = ',"date":1700000000,"edit_date":0,"ephemeral_content":null,"ephemeral_message_id":0,"forward_info":null,"id":55,"import_info":null,"is_from_offline":false,"is_outgoing":false,"receiver_id":null,"reply_markup":null,"reply_to":null,"scheduling_state":null,"self_destruct_in":0.0,"self_destruct_type":null,"sender_id":{"@type":"messageSenderUser","user_id":8},"sending_state":null,"topic_id":null},"version":'


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def entity(kind, offset=0, length=3, **metadata):
    return {'@type': 'textEntity', 'offset': offset, 'length': length, 'type': {'@type': 'textEntityType'+kind, **metadata}}


def source_shell(text=TEXT, entities=None, **changes):
    return {'@type': 'message', 'chat_id': 123, 'id': 55,
        'sender_id': {'@type': 'messageSenderUser', 'user_id': 8},
        'is_outgoing': False, 'is_from_offline': False, 'ephemeral_message_id': 0,
        'date': 1700000000, 'edit_date': 0, 'self_destruct_in': 0.0, 'auto_delete_in': 0.0,
        'content': {'@type': 'messageText', 'text': {'@type': 'formattedText',
            'text': text, 'entities': copy.deepcopy(IDENTITIES if entities is None else entities)}}, **changes}


def file_facts():
    return {'@type': 'file', 'id': 1, 'size': 10, 'expected_size': 10,
        'local': {'@type': 'localFile', 'path': '', 'can_be_downloaded': True,
            'can_be_deleted': False, 'is_downloading_active': False, 'is_downloading_completed': False,
            'download_offset': 0, 'downloaded_prefix_size': 0, 'downloaded_size': 0},
        'remote': {'@type': 'remoteFile', 'id': 'remote', 'unique_id': 'unique',
            'is_uploading_active': False, 'is_uploading_completed': True, 'uploaded_size': 10}}


def media_content(kind='document', text='Caption', entities=()):
    caption = {'@type': 'formattedText', 'text': text, 'entities': copy.deepcopy(list(entities))}
    if kind == 'document':
        return {'@type': 'messageDocument', 'caption': caption, 'document': {'@type': 'document',
            'file_name': 'evidence.txt', 'mime_type': 'text/plain', 'document': file_facts()}}
    if kind == 'photo':
        return {'@type': 'messagePhoto', 'caption': caption, 'has_spoiler': False, 'is_secret': False,
            'show_caption_above_media': False, 'photo': {'@type': 'photo', 'has_stickers': False,
            'sizes': [{'@type': 'photoSize', 'type': 'i', 'width': 2, 'height': 2,
                'photo': file_facts(), 'progressive_sizes': []}]}}
    return {'@type': 'messageVoiceNote', 'caption': caption, 'is_listened': False,
        'voice_note': {'@type': 'voiceNote', 'duration': 2, 'waveform': 'AAAA',
            'mime_type': 'audio/ogg', 'voice': file_facts()}}


class CaptionRaw:
    """Fresh fake of raw provider transport, never native initialization."""
    def __init__(self):
        self.sent = []; self.events = deque(); self.source = caption_source()
        self.mode = 'sent'; self.hook = None; self.account = 7; self.can_reply = True

    def outgoing(self, content, identifier=-10, **changes):
        return {'@type': 'message', 'id': identifier, 'chat_id': 123, 'is_outgoing': True,
            'content': content, 'reply_to': {'@type': 'messageReplyToMessage', 'chat_id': 123,
            'message_id': 55, 'checklist_task_id': 0, 'poll_option_id': '', 'origin_send_date': 0}, **changes}

    def send(self, request):
        self.sent.append(copy.deepcopy(request)); kind = request['@type']
        if self.hook: self.hook(request)
        if kind == 'getAuthorizationState': value = {'@type': 'authorizationStateReady'}
        elif kind == 'getMe': value = {'@type': 'user', 'id': self.account}
        elif kind == 'getChat': value = {'@type': 'chat', 'id': request['chat_id'], 'title': 'Recipient', 'type': {'@type': 'chatTypePrivate'}}
        elif kind == 'getMessage': value = copy.deepcopy(self.source)
        elif kind == 'getMessageProperties': value = {'@type': 'messageProperties', 'can_be_replied': self.can_reply}
        elif kind == 'sendMessage':
            inp = request['input_message_content']
            content = {'@type': 'messageText', 'text': copy.deepcopy(inp['text'])} if inp['@type'] == 'inputMessageText' else media_content(text=inp['caption']['text'])
            pre = self.outgoing(content, sending_state={'@type': 'messageSendingStatePending', 'sending_id': request['options']['sending_id']})
            self.events.append({**pre, '@extra': request['@extra']})
            if self.mode == 'lost': return
            final = self.outgoing(content, 701, sending_state=None)
            if self.mode == 'wrong': final['reply_to']['message_id'] = 56
            self.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': final})
            return
        else: raise AssertionError(kind)
        self.events.append({**value, '@extra': request['@extra']})

    def receive(self, timeout):
        return self.events.popleft() if self.events else None

    def close(self): pass


class CaptionFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.raw = CaptionRaw(); self.client = TDLibClient(raw=self.raw)
        self.approvals = []
        self.broker = Broker(socket_path=self.root/'broker.sock', artifact_store=ArtifactStore(cache_dir=self.root/'cache'),
            client_factory=lambda: self.client, policy=POLICY, approval_prompt=self.approve)
        self.addCleanup(self.broker.shutdown)
        path = self.root/'evidence.txt'; path.write_bytes(b'bounded document')
        self.artifact = self.broker._artifact_store.store(path, kind='document')
        if self.artifact_path:
            for name, function in [('stage_approved_document', stage_approved_document), ('retire_staged_document', retire_staged_document)]:
                manager = patch('telegram_search_mcp.broker.'+name, side_effect=lambda value, fn=function: fn(value, root=self.root/'stage'))
                manager.start(); self.addCleanup(manager.stop)
            manager = patch('telegram_search_mcp.tdjson.STAGING_ROOT', self.root/'stage'); manager.start(); self.addCleanup(manager.stop)

    def approve(self, **facts):
        self.approvals.append(facts); return True

    def dispatch(self, operation, payload, client=CLIENT):
        return self.broker._dispatch({'operation': operation, 'payload': payload, 'client_id': client,
            'deadline': time.monotonic()+10, 'broker_generation': self.broker._generation})

    def prepare_payload(self):
        return {'recipient': 123, 'reply_to': ANCHOR,
            **({'artifact_id': self.artifact.artifact_id, 'display_name': 'evidence.txt', 'mime_type': 'text/plain',
                'caption': 'Caption', 'kind': 'document'} if self.artifact_path else {'text': 'Final reply'})}

    @property
    def preparation(self): return 'prepare_reply_artifact_send' if self.artifact_path else 'prepare_reply_text_send'
    @property
    def operations(self): return ('get_reply_artifact_draft', 'update_reply_artifact_draft', 'refresh_reply_artifact_draft') if self.artifact_path else ('get_reply_draft', 'update_reply_draft', 'refresh_reply_draft')
    @property
    def send_operation(self): return 'send_prepared_artifact' if self.artifact_path else 'send_prepared_text'
    @property
    def revision(self): return {'caption': 'New caption'} if self.artifact_path else {'text': 'New reply'}

    def prepare(self):
        result = self.dispatch(self.preparation, self.prepare_payload())
        self.assertEqual(result['status'], 'prepared'); return result['reply']

    def send(self, did): return self.dispatch(self.send_operation, {'draft_id': did, 'approved': True})
    def sends(self): return [request for request in self.raw.sent if request['@type'] == 'sendMessage']
    def restore(self, kind='document', lexical=False):
        self.broker._policy = POLICY; self.raw.source = caption_source(kind, lexical=lexical); self.raw.mode = 'sent'; self.raw.hook = None; self.raw.can_reply = True
        self.broker._approval_prompt = self.approve


def caption_source(kind='document', text=TEXT, spans=None, *, lexical=False, **shell):
    entities = copy.deepcopy(IDENTITIES if spans is None else spans)
    if lexical: entities.append(entity('Hashtag', 12, 4))
    return source_shell(content=media_content(kind, text, entities), **shell)


def primary_file(raw):
    content = raw['content']
    if content['@type'] == 'messageDocument': return content['document']['document']
    if content['@type'] == 'messagePhoto': return content['photo']['sizes'][0]['photo']
    return content['voice_note']['voice']


def expected_projection(kind='document', text=TEXT, spans=None):
    # Exact shell and stable media facts are independently hand-declared.
    content = {'kind': kind, 'caption': {'@type': 'formattedText', 'text': text,
        'entities': copy.deepcopy(IDENTITIES if spans is None else spans)}, 'media': FACTS[kind]}
    return PREFIX + encode(content) + SUFFIX + '8}'


def expected_evidence(kind='document', text=TEXT, records=None, facts=None):
    return MARKER + 'Media target: ' + encode({'kind': kind, 'caption': {
        'text': text, 'offset_basis': 'original source UTF-16 code units',
        'entities': RECORDS if records is None else records},
        'media': FACTS[kind] if facts is None else facts})


class IdentityMediaSourceTests(unittest.TestCase):
    def accepted(self, raw):
        try: return source_from_message(raw, 123, 55)
        except ValueError as error: self.fail('approved identity caption must yield complete v8 evidence: '+str(error))

    def rejected(self, raw):
        with self.assertRaises(ValueError): source_from_message(raw, 123, 55)

    def test_three_media_kinds_bind_both_identity_labels_original_ranges_and_authority(self):
        # Bug caught: dropping identity spans/user ID/media facts or choosing an old version.
        for kind in KINDS:
            with self.subTest(kind=kind):
                source = self.accepted(caption_source(kind))
                self.assertEqual(source.projection_json, expected_projection(kind))
                self.assertEqual(source.source_sha256, hashlib.sha256(expected_projection(kind).encode()).hexdigest())
                self.assertEqual(source.target().text, expected_evidence(kind))
                self.assertEqual(source.required_capabilities, CAPS)
                self.assertTrue(source.is_media)
                self.assertFalse(source.target().sanitized)
                self.assertFalse(source.target().truncated)
                self.assertEqual(ReplySource(source.projection_json).target().text, expected_evidence(kind))
                with self.assertRaises(ValueError): _ = source.required_capability
                for label, typ, metadata in (('mention', 'Mention', {}), ('mention_name', 'MentionName', {'user_id': 2**53-1})):
                    one = self.accepted(caption_source(kind, 'abc', [entity(typ, 0, 3, **metadata)]))
                    self.assertEqual(one.target().text, expected_evidence(kind, 'abc',
                        [{'type': label, 'offset': 0, 'length': 3, 'text': 'abc', **metadata}]))

    def test_lexical_capability_is_required_exactly_when_a_lexical_label_is_present(self):
        for typ, label, text in (('Hashtag', 'hashtag', '#tag'), ('Cashtag', 'cashtag', '$USD'), ('BotCommand', 'bot_command', '/go!')):
            spans = [*IDENTITIES, entity(typ, 12, 4)]
            source = self.accepted(caption_source(text='😀 @ada Ada '+text, spans=spans))
            self.assertEqual(source.required_capabilities, MIXED_CAPS)
            self.assertEqual(source.target().text, expected_evidence(text='😀 @ada Ada '+text,
                records=[*RECORDS, {'type': label, 'offset': 12, 'length': 4, 'text': text}]))
        self.assertEqual(self.accepted(caption_source()).required_capabilities, CAPS)

    def test_all_optional_styles_keep_closed_nonoverlap_and_language_evidence(self):
        for typ, label in STYLES:
            metadata = {'language': 'python'} if typ == 'PreCode' else {}
            raw = caption_source(text='@x abc', spans=[entity('Mention', 0, 2), entity(typ, 3, 3, **metadata)])
            self.assertEqual(self.accepted(raw).target().text, expected_evidence(text='@x abc', records=[
                {'type': 'mention', 'offset': 0, 'length': 2, 'text': '@x'},
                {'type': label, 'offset': 3, 'length': 3, 'text': 'abc', **metadata}]))
        for typ, _ in STYLES[:5]:
            nested = [entity(typ, 0, 4), entity('MentionName', 1, 2, user_id=42)]
            self.assertEqual(self.accepted(caption_source(text='abcd', spans=nested)).required_capabilities, CAPS)

    def test_original_utf16_survives_normalization_escaping_and_span_rendering(self):
        raw = caption_source(text='ﬃ 😀 @Ａ', spans=[entity('Bold', 0, 7), entity('Mention', 5, 2)])
        source = self.accepted(raw)
        self.assertEqual(source.target().text, expected_evidence(text='ffi 😀 @A', records=[
            {'type': 'bold', 'offset': 0, 'length': 7, 'text': 'ffi 😀 @A'},
            {'type': 'mention', 'offset': 5, 'length': 2, 'text': '@A'}]))
        self.assertTrue(source.target().sanitized)
        self.assertEqual(source.projection_json, expected_projection(text='ﬃ 😀 @Ａ', spans=raw['content']['caption']['entities']))
        raw = caption_source(text='"@x"\n\\', spans=[entity('Mention', 1, 2)])
        self.assertEqual(self.accepted(raw).target().text, expected_evidence(text='"@x" \\', records=[
            {'type': 'mention', 'offset': 1, 'length': 2, 'text': '@x'}]))
        for offset, length in ((0, 1), (1, 1), (1, 2)):
            self.rejected(caption_source(text='😀x', spans=[entity('Mention', offset, length)]))

    def test_low_level_identity_is_explicit_and_v8_requires_an_identity_entity(self):
        for kind in KINDS:
            self.accepted(caption_source(kind))
            raw = caption_source(kind)['content']
            with self.assertRaises(ValueError): parse_media(raw)
            with self.assertRaises(ValueError): parse_media(raw, lexical=True)
            expected = json.loads(expected_projection(kind))['message']['content']
            with self.assertRaises(ValueError): validate_media(expected, styled=True)
            with self.assertRaises(ValueError): render_media(expected, MARKER, styled=True)
            self.assertEqual(parse_media(raw, identity=True), expected)
            validate_media(expected, identity=True)
            self.assertEqual(render_media(expected, MARKER, identity=True), (expected_evidence(kind), False))
            for spans in ([], [entity('Bold')], [entity('Hashtag')]):
                raw = media_content(kind, 'abc', spans)
                with self.assertRaises(ValueError): parse_media(raw, identity=True, lexical=bool(spans))

    def test_private_v8_cannot_be_forged_under_old_media_versions_or_nonmedia_versions(self):
        source = self.accepted(caption_source())
        value = json.loads(source.projection_json)
        for version in (1, 2, 3, 4, 5, 6, 7, True, '8', 9):
            with self.subTest(version=version), self.assertRaises(ValueError): ReplySource(encode({**value, 'version': version}))
        for spans in ([], [entity('Bold')], [entity('Hashtag')]):
            projection = json.loads(expected_projection(text='abc', spans=spans))
            with self.assertRaises(ValueError): ReplySource(encode(projection))
        text_source = self.accepted(caption_source())
        projection = json.loads(text_source.projection_json)
        projection['message']['content'] = source_shell()['content']
        with self.assertRaises(ValueError): ReplySource(encode(projection))
        value['message']['content']['caption']['entities'].reverse()
        with self.assertRaises(ValueError): ReplySource(encode(value))
        with self.assertRaises(ValueError): ReplySource(' '+source.projection_json)

    def test_malformed_identity_metadata_ids_coordinates_and_unknown_labels_refuse(self):
        bads = []
        for user_id in (True, False, 0, -1, 2**53, 2**53+1, '42', 42.0, None):
            bads.append(caption_source(text='abc', spans=[entity('MentionName', user_id=user_id)]))
        for typ in ('Mention', 'MentionName'):
            for extra in ({'url': 'x'}, {'language': ''}, {'extra': None}):
                bads.append(caption_source(text='abc', spans=[entity(typ, **({'user_id': 42} if typ=='MentionName' else {}), **extra)]))
        bads.append(caption_source(text='abc', spans=[entity('Mention', user_id=42)]))
        bads.append(caption_source(text='abc', spans=[entity('MentionName')]))
        for field, values in (('offset', (True, False, -1, 2**31, '0', 0.0)), ('length', (True, False, 0, -1, 2**31, '3', 3.0))):
            for value in values:
                raw = caption_source(text='abc', spans=[entity('Mention')]); raw['content']['caption']['entities'][0][field] = value; bads.append(raw)
        for typ in ('Url', 'TextUrl', 'CustomEmoji', 'EmailAddress', 'PhoneNumber', 'BankCardNumber', 'MediaTimestamp', 'DateTime', 'Unknown'):
            bads.append(caption_source(text='abc', spans=[entity('Mention'), entity(typ)]))
        for raw in bads: self.rejected(raw)
        for field in ('caption_extra', 'entity_extra', 'caption_type'):
            raw = caption_source()
            if field=='caption_extra': raw['content']['caption']['extra'] = None
            elif field=='entity_extra': raw['content']['caption']['entities'][0]['extra'] = None
            else: raw['content']['caption']['@type'] = 'other'
            self.rejected(raw)

    def test_duplicates_crossing_identity_lexical_code_and_quote_overlap_refuse(self):
        bad_spans = [
            [entity('Mention', 0, 2)]*2,
            [entity('MentionName', 0, 3, user_id=42), entity('MentionName', 1, 2, user_id=43)],
            [entity('Mention', 0, 3), entity('Hashtag', 1, 2)],
            [entity('Bold', 0, 3), entity('Mention', 2, 2)],
        ]
        for typ in ('Code', 'Pre', 'PreCode', 'BlockQuote', 'ExpandableBlockQuote'):
            bad_spans.append([entity(typ, 0, 4, **({'language': ''} if typ=='PreCode' else {})), entity('Mention', 1, 2)])
        for spans in bad_spans: self.rejected(caption_source(text='abcd', spans=spans))

    def test_caption_entity_source_and_complete_display_bounds_refuse_without_truncation(self):
        self.rejected(caption_source(text='@'+'x'*1024, spans=[entity('Mention', 0, 1025)]))
        self.rejected(caption_source(text='x'*33, spans=[entity('Mention', i, 1) for i in range(33)]))
        huge = caption_source(text='x'*1024, spans=[entity('Mention', 0, 1024), *[entity(typ, 0, 1024) for typ, _ in STYLES[:5]]])
        self.rejected(huge)
        for text in ('', ' ', '\x00', '\ud800'):
            self.rejected(caption_source(text=text, spans=[entity('Mention', 0, 1)]))
        for raw in (caption_source(chat_id=124), caption_source(id=56), caption_source(topic_id={'@type': 'messageTopicThread', 'message_thread_id': 1}), caption_source(reply_to={}), caption_source(sending_state={})):
            self.rejected(raw)
        for kind in KINDS:
            raw = caption_source(kind); raw['content']['extra'] = None; self.rejected(raw)
        raw = caption_source('photo'); raw['content']['has_spoiler'] = True; self.rejected(raw)
        raw = caption_source('voice_note'); raw['content']['voice_note']['mime_type'] = 'audio/mp3'; self.rejected(raw)

    def test_user_id_only_and_media_identity_drift_change_the_raw_approval_digest(self):
        source = self.accepted(caption_source())
        changed = caption_source(); changed['content']['caption']['entities'][1]['type']['user_id'] = 43
        self.assertNotEqual(source.source_sha256, self.accepted(changed).source_sha256)
        for kind in KINDS:
            raw = caption_source(kind); before = self.accepted(raw).source_sha256
            primary_file(raw)['remote']['unique_id'] = 'replacement'
            self.assertNotEqual(before, self.accepted(raw).source_sha256)

    def test_exact_caption_entity_display_and_canonical_byte_bounds_stay_closed(self):
        source = self.accepted(caption_source(text='x'*1024, spans=[entity('Mention', 0, 1024)]))
        self.assertFalse(source.target().truncated)
        spans = [entity('MentionName', i, 1, user_id=42) for i in range(32)]
        self.assertEqual(len(json.loads(self.accepted(caption_source(text='x'*32, spans=spans)).projection_json)['message']['content']['caption']['entities']), 32)
        text = 'x'*880
        spans = [entity('Bold', 0, 880), entity('Italic', 0, 880), entity('Mention', 0, 880)]
        records = [{'type': label, 'offset': 0, 'length': 880, 'text': text} for label in ('bold', 'italic', 'mention')]
        facts = {**FACTS['document'], 'file_name': 'n'*126}
        want = expected_evidence(text=text, records=records, facts=facts)
        self.assertEqual(len(want), 4096)
        raw = caption_source(text=text, spans=spans); raw['content']['document']['file_name'] = 'n'*126
        self.assertEqual(self.accepted(raw).target().text, want)
        raw['content']['document']['file_name'] += 'n'; self.rejected(raw)
        for value in (' '*65537, encode({'version': 8, 'padding': '😀'*17000})):
            with self.assertRaises(ValueError): ReplySource(value)

    def test_v1_through_v7_match_frozen_data_bytes_display_and_authority(self):
        # Data comparison only; no old test/helper or accepted suite is executed.
        for item in json.loads(BASELINE.read_text()):
            source = source_from_message(item['raw'], 123, 55)
            with self.subTest(version=item['version']):
                self.assertEqual(source.projection_json, item['projection_json'])
                self.assertEqual(source.target().model_dump(mode='json'), item['target'])
                self.assertEqual(source.required_capabilities, tuple(item['required_capabilities']))
                self.assertEqual(source.is_media, item['is_media'])


class IdentityMediaLifecycle:
    def revoke(self, *missing):
        self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c not in missing))

    def test_each_media_source_has_full_owner_evidence_exact_plain_request_and_no_resend(self):
        for kind in KINDS:
            self.restore(kind); preview = self.prepare(); did = preview['draft']['draft_id']
            self.assertEqual(preview['reply_target']['text'], expected_evidence(kind))
            self.assertEqual(self.broker._drafts.source_required_capabilities(did, owner=DraftOwner(CLIENT, 7)), CAPS)
            self.assertEqual(self.dispatch(self.operations[0], {'draft_id': did})['reply'], preview)
            self.assertEqual(self.send(did)['status'], 'sent')
            self.assertEqual(self.approvals[-1]['reply_artifact_preview' if self.artifact_path else 'reply_preview'], preview)
            wire = self.sends()[-1]
            self.assertEqual(wire['chat_id'], 123)
            self.assertEqual(wire['reply_to'], {'@type': 'inputMessageReplyToMessage', 'message_id': 55, 'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''})
            self.assertIsNone(wire['topic_id']); self.assertIsNone(wire['reply_markup'])
            self.assertEqual(wire['input_message_content']['caption' if self.artifact_path else 'text']['entities'], [])
            self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual((len(self.sends()), len(self.approvals)), (3, 3))
        self.assertTrue(all(request['@type'] in ('getAuthorizationState', 'getMe', 'getChat', 'getMessage', 'getMessageProperties', 'sendMessage') for request in self.raw.sent))

    def test_each_required_capability_blocks_prepare_get_update_refresh_and_owner(self):
        for lexical in (False, True):
            for missing in MIXED_CAPS if lexical else CAPS:
                self.restore(lexical=lexical); self.revoke(missing)
                self.assertEqual(self.dispatch(self.preparation, self.prepare_payload())['status'], 'unavailable')
                self.broker._policy = POLICY; did = self.prepare()['draft']['draft_id']; self.revoke(missing)
                for op, extra in ((self.operations[0], {}), (self.operations[1], self.revision), (self.operations[2], {})):
                    self.assertEqual(self.dispatch(op, {'draft_id': did, **extra})['status'], 'unavailable')
                self.assertEqual(self.send(did)['status'], 'expired')
                self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_invalid_or_overflowing_source_and_outgoing_entities_never_mutate_registry(self):
        before = self.broker._drafts.known_ids()
        bads = [caption_source(text='x'*1025, spans=[entity('Mention', 0, 1025)]), caption_source(text='x'*1024, spans=[entity('Mention', 0, 1024), *[entity(typ, 0, 1024) for typ, _ in STYLES[:5]]]), caption_source(spans=[entity('MentionName', user_id=True)]), caption_source(id=56)]
        for raw in bads:
            self.raw.source = raw
            self.assertEqual(self.dispatch(self.preparation, self.prepare_payload())['status'], 'unavailable')
            self.assertEqual(self.broker._drafts.known_ids(), before)
        self.raw.source = caption_source()
        with self.assertRaises(ValidationError): self.dispatch(self.preparation, {**self.prepare_payload(), 'entities': [entity('Mention')]})
        self.assertEqual(self.broker._drafts.known_ids(), before)
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_revision_invalidates_old_approval_and_refresh_requires_old_and_new_authority(self):
        preview = self.prepare(); did = preview['draft']['draft_id']
        revision = self.dispatch(self.operations[1], {'draft_id': did, **self.revision})['reply']
        self.assertEqual(revision['reply_target'], preview['reply_target'])
        self.assertNotEqual(revision['preview_sha256'], preview['preview_sha256'])
        self.assertEqual(self.send(did)['status'], 'expired')
        did = revision['draft']['draft_id']; self.raw.source = source_shell('plain', [])
        for missing in CAPS:
            self.revoke(missing); self.assertEqual(self.dispatch(self.operations[2], {'draft_id': did})['status'], 'unavailable'); self.broker._policy = POLICY
        plain = self.dispatch(self.operations[2], {'draft_id': did})['reply']; self.assertEqual(self.send(did)['status'], 'expired')
        did = plain['draft']['draft_id']; self.raw.source = caption_source(lexical=True)
        for missing in MIXED_CAPS:
            self.revoke(missing); self.assertEqual(self.dispatch(self.operations[2], {'draft_id': did})['status'], 'unavailable'); self.broker._policy = POLICY
        mixed = self.dispatch(self.operations[2], {'draft_id': did})['reply']
        self.assertEqual(self.send(did)['status'], 'expired')
        self.assertEqual(self.broker._drafts.source_required_capabilities(mixed['draft']['draft_id'], owner=DraftOwner(CLIENT, 7)), MIXED_CAPS)
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_user_id_only_change_before_owner_and_during_owner_prevents_transport(self):
        for phase in ('before', 'owner'):
            self.restore(); did = self.prepare()['draft']['draft_id']
            if phase=='before': self.raw.source['content']['caption']['entities'][1]['type']['user_id'] = 43
            else:
                def approve(**facts):
                    self.approvals.append(facts); self.raw.source['content']['caption']['entities'][1]['type']['user_id'] = 43; return True
                self.broker._approval_prompt = approve
            self.assertEqual(self.send(did)['status'], 'failed')
            self.assertIsNone(self.client._send_observations.snapshot(did))
        # Both paths show the immutable owner preview; final source freshness refuses.
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 2))

    def test_caption_shell_and_each_media_identity_drift_during_owner_prevents_transport(self):
        for kind in KINDS:
            for drift in ('media', 'size', 'shell', 'caption', 'range', 'label'):
                self.restore(kind); did = self.prepare()['draft']['draft_id']
                def approve(**facts):
                    self.approvals.append(facts)
                    if drift=='media': primary_file(self.raw.source)['remote']['unique_id'] = 'replacement'
                    elif drift=='size': primary_file(self.raw.source)['size'] = 11
                    elif drift=='shell': self.raw.source['edit_date'] = 1
                    elif drift=='caption': self.raw.source['content']['caption']['text'] = '😀 @ADA Ada #tag'
                    elif drift=='range': self.raw.source['content']['caption']['entities'][0]['length'] = 3
                    else: self.raw.source['content']['caption']['entities'][0]['type']['@type'] = 'textEntityTypeHashtag'
                    return True
                self.broker._approval_prompt = approve
                self.assertEqual(self.send(did)['status'], 'failed')
                self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 18))

    def test_each_source_capability_revoked_during_owner_and_final_provider_read_refuses(self):
        for phase in ('owner', 'properties'):
            for missing in MIXED_CAPS:
                self.restore(lexical=True); did = self.prepare()['draft']['draft_id']
                if phase=='owner':
                    def approve(**facts): self.approvals.append(facts); self.revoke(missing); return True
                    self.broker._approval_prompt = approve
                else:
                    self.raw.hook = lambda request: self.revoke(missing) if request['@type']=='getMessageProperties' else None
                self.assertNotEqual(self.send(did)['status'], 'sent')
                self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 8))

    def test_real_registration_capability_drift_discards_unattempted_observation(self):
        registration = type(self.client._send_observations).register.__code__
        for missing in MIXED_CAPS:
            self.restore(lexical=True); did = self.prepare()['draft']['draft_id']; observed = []
            def after_registration(frame, event, arg):
                if event=='return' and frame.f_code is registration:
                    observed.append(self.client._send_observations.snapshot(did)); self.revoke(missing)
            prior = sys.getprofile(); sys.setprofile(after_registration)
            try: self.assertEqual(self.send(did)['status'], 'failed')
            finally: sys.setprofile(prior); self.broker._policy = POLICY
            self.assertEqual(len(observed), 1); self.assertIsNotNone(observed[0])
            self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.send(did)['status'], 'failed')
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 4))

    def test_denial_unknown_and_restored_capabilities_never_renew_attempt(self):
        self.broker._approval_prompt = lambda **facts: self.approvals.append(facts) or False
        did = self.prepare()['draft']['draft_id']; self.assertEqual(self.send(did)['status'], 'not_approved')
        self.assertEqual(len(self.sends()), 0)
        self.restore(); self.raw.mode = 'wrong'; did = self.prepare()['draft']['draft_id']
        self.assertEqual(self.send(did)['status'], 'outcome_unknown')
        count = len(self.sends()); decisions = len(self.approvals)
        for missing in CAPS:
            self.revoke(missing); self.assertEqual(self.send(did)['status'], 'expired')
            self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['status'], 'outcome_unknown')
        self.broker._policy = POLICY
        self.assertEqual(self.send(did)['status'], 'outcome_unknown')
        self.assertEqual((len(self.sends()), len(self.approvals)), (count, decisions))

    def test_volatile_provider_fields_and_entity_order_do_not_invalidate_immutable_facts(self):
        for kind in KINDS:
            self.restore(kind); did = self.prepare()['draft']['draft_id']
            def approve(**facts):
                self.approvals.append(facts); self.raw.source['content']['caption']['entities'].reverse()
                file = primary_file(self.raw.source); file['local']['path'] = '/never/read'; file['local']['downloaded_size'] = 10
                file['remote']['uploaded_size'] = 11; self.raw.source['views'] = 99
                return True
            self.broker._approval_prompt = approve
            self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual((len(self.sends()), len(self.approvals)), (3, 3))

    def test_foreign_and_generic_draft_routes_cannot_expose_v8_evidence(self):
        did = self.prepare()['draft']['draft_id']
        for op, extra in ((self.operations[0], {}), (self.operations[1], self.revision), (self.operations[2], {})):
            self.assertEqual(self.dispatch(op, {'draft_id': did, **extra}, FOREIGN)['status'], 'unavailable')
        for op, extra in (('get_draft', {}), ('update_draft', self.revision), ('refresh_draft', {})):
            self.assertEqual(self.dispatch(op, {'draft_id': did, **extra})['status'], 'unavailable')
        self.assertEqual(self.dispatch(self.operations[0], {'draft_id': did})['reply']['reply_target']['text'], expected_evidence())
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))


class IdentityMediaTextTests(IdentityMediaLifecycle, CaptionFixture, unittest.TestCase):
    artifact_path = False


class IdentityMediaArtifactTests(IdentityMediaLifecycle, CaptionFixture, unittest.TestCase):
    artifact_path = True
