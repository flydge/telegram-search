"""New identity-text risks through real source, registry, Broker and TDJSON code.

The sole external double is raw TDLib I/O; owner decisions are local callbacks.
No previous test/helper module is imported. Expected source facts and preview
records are hand-derived, including the UTF-16 offset after an astral character.
"""
import copy
import hashlib
import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy, load_runtime_policy
from telegram_search_mcp.outgoing_drafts import DraftError, DraftOwner
from telegram_search_mcp.outgoing_stage import stage_approved_document, retire_staged_document
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from telegram_search_mcp.tdjson import TDLibClient

CLIENT = 'client_' + 'a' * 24
FOREIGN = 'client_' + 'b' * 24
ANCHOR = {'chat_id': 123, 'message_id': 55}
CAPS = ('reply_formatted_targets', 'reply_identity_targets')
MIXED_CAPS = CAPS + ('reply_lexical_targets',)
BASE = ('send', 'reply_text_send', 'reply_artifact_send')
POLICY = RuntimePolicy(enabled_capabilities=BASE + MIXED_CAPS + ('reply_media_targets',))
MARKER = '[untrusted Telegram evidence] '
TEXT = '😀 @ada Ada #tag'
IDENTITIES = [
    {'@type': 'textEntity', 'offset': 3, 'length': 4, 'type': {'@type': 'textEntityTypeMention'}},
    {'@type': 'textEntity', 'offset': 8, 'length': 3, 'type': {'@type': 'textEntityTypeMentionName', 'user_id': 42}},
]
DISPLAY = MARKER + 'Formatted target: {"entities":[{"length":4,"offset":3,"text":"@ada","type":"mention"},{"length":3,"offset":8,"text":"Ada","type":"mention_name","user_id":42}],"offset_basis":"original source UTF-16 code units","text":"😀 @ada Ada #tag"}'
SHELL_PREFIX = '{"anchor":{"chat_id":123,"message_id":55},"message":{"@type":"message","auto_delete_in":0.0,"chat_id":123,"content":'
SHELL_SUFFIX = ',"date":1700000000,"edit_date":0,"ephemeral_content":null,"ephemeral_message_id":0,"forward_info":null,"id":55,"import_info":null,"is_from_offline":false,"is_outgoing":false,"receiver_id":null,"reply_markup":null,"reply_to":null,"scheduling_state":null,"self_destruct_in":0.0,"self_destruct_type":null,"sender_id":{"@type":"messageSenderUser","user_id":8},"sending_state":null,"topic_id":null},"version":'
IDENTITY_CONTENT = '{"@type":"messageText","link_preview":null,"link_preview_options":null,"text":{"@type":"formattedText","entities":[{"@type":"textEntity","length":4,"offset":3,"type":{"@type":"textEntityTypeMention"}},{"@type":"textEntity","length":3,"offset":8,"type":{"@type":"textEntityTypeMentionName","user_id":42}}],"text":"😀 @ada Ada #tag"}}'
CANONICAL = SHELL_PREFIX + IDENTITY_CONTENT + SHELL_SUFFIX + '7}'


def serialize(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def entity(kind, offset=0, length=3, **metadata):
    return {'@type': 'textEntity', 'offset': offset, 'length': length,
            'type': {'@type': 'textEntityType' + kind, **metadata}}


def target(text=TEXT, entities=None, **changes):
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


def old_forms():
    """Independent fixtures for the six unchanged source authority classes."""
    return [target('plain', []), target(content=media_content()), target('abc', [entity('Bold')]),
        target(content=media_content(text='abc', entities=[entity('Italic')])),
        target('#tag', [entity('Hashtag', 0, 4)]),
        target(content=media_content(text='#tag', entities=[entity('Hashtag', 0, 4)]))]


def old_expected_content(version):
    plain = lambda text, entities: {'@type': 'messageText', 'link_preview': None,
        'link_preview_options': None, 'text': {'@type': 'formattedText', 'text': text, 'entities': entities}}
    if version == 1: return plain('plain', [])
    if version == 3: return plain('abc', [entity('Bold')])
    if version == 5: return plain('#tag', [entity('Hashtag', 0, 4)])
    text, entities = ('Caption', []) if version == 2 else ('abc', [entity('Italic')]) if version == 4 else ('#tag', [entity('Hashtag', 0, 4)])
    return {'kind': 'document', 'caption': {'@type': 'formattedText', 'text': text, 'entities': entities},
        'media': {'file_name': 'evidence.txt', 'mime_type': 'text/plain', 'size': 10,
            'unique_id_sha256': 'c2720445a45267813688ff73fa188aa060c1b661aefaf1650d42f690697b5ab3'}}


class IdentitySourceTests(unittest.TestCase):
    def accepted(self, raw):
        try: return source_from_message(raw, 123, 55)
        except ValueError: self.fail('approved identity TEXT source must yield complete bounded evidence')

    def test_complete_v7_utf16_identity_evidence_and_declared_user_id(self):
        source = self.accepted(target())
        self.assertEqual(source.projection_json, CANONICAL)
        self.assertEqual(source.source_sha256, hashlib.sha256(CANONICAL.encode()).hexdigest())
        self.assertEqual(source.target().text, DISPLAY)
        self.assertEqual(source.required_capabilities, CAPS)
        self.assertFalse(source.is_media)
        self.assertFalse(source.target().sanitized)
        self.assertFalse(source.target().truncated)
        with self.assertRaises(ValueError): _ = source.required_capability

    def test_provider_labels_do_not_infer_identity_or_visible_grammar(self):
        for kind, metadata, record in (
            ('Mention', {}, {'type': 'mention', 'offset': 0, 'length': 3, 'text': 'abc'}),
            ('MentionName', {'user_id': 2**53-1}, {'type': 'mention_name', 'offset': 0, 'length': 3, 'text': 'abc', 'user_id': 2**53-1})):
            source = self.accepted(target('abc', [entity(kind, **metadata)]))
            self.assertEqual(json.loads(source.target().text.split('Formatted target: ', 1)[1]),
                {'text': 'abc', 'offset_basis': 'original source UTF-16 code units', 'entities': [record]})
            self.assertEqual(source.required_capabilities, CAPS)

    def test_style_nesting_and_lexical_conjunction_are_complete(self):
        raw = target(entities=[entity('Bold', 0, 16), *IDENTITIES, entity('Hashtag', 12, 4)])
        source = self.accepted(raw)
        self.assertEqual(source.required_capabilities, MIXED_CAPS)
        records = json.loads(source.target().text.split('Formatted target: ', 1)[1])['entities']
        self.assertEqual(records, [{'type': 'bold', 'offset': 0, 'length': 16, 'text': TEXT},
            {'type': 'mention', 'offset': 3, 'length': 4, 'text': '@ada'},
            {'type': 'mention_name', 'offset': 8, 'length': 3, 'text': 'Ada', 'user_id': 42},
            {'type': 'hashtag', 'offset': 12, 'length': 4, 'text': '#tag'}])

    def test_identity_order_is_canonical_and_id_only_edit_changes_digest(self):
        source = self.accepted(target())
        raw = target(); raw['content']['text']['entities'].reverse()
        self.assertEqual(self.accepted(raw).projection_json, CANONICAL)
        raw['content']['text']['entities'][0]['type']['user_id'] = 43
        self.assertNotEqual(self.accepted(raw).source_sha256, source.source_sha256)
        stored = json.loads(CANONICAL); stored['message']['content']['text']['entities'].reverse()
        with self.assertRaises(ValueError): ReplySource(serialize(stored))

    def test_exact_shapes_strict_int53_user_ids_and_utf16_boundaries_refuse(self):
        invalid = [entity('MentionName', user_id=value) for value in (True, False, None, 0, -1, 2**53, '42', 42.0)]
        invalid += [entity('MentionName'), entity('MentionName', user_id=42, url='x'),
            entity('Mention', user_id=42), entity('Mention', extra=None), entity('Url'), entity('EmailAddress'),
            entity('PhoneNumber'), entity('TextUrl', url='https://example.invalid'),
            entity('CustomEmoji', custom_emoji_id=1), entity('DateTime', unix_time=1, formatting_type={})]
        invalid += [entity('MentionName', offset, length, user_id=42) for offset, length in
            ((True, 1), ('0', 1), (0, True), (0, 0), (-1, 1), (1, 1), (0, 1), (0, 17), (2**31, 1))]
        extra = entity('MentionName', user_id=42); extra['extra'] = 1; invalid.append(extra)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError): source_from_message(target(entities=[item]), 123, 55)

    def test_identity_overlaps_crossings_and_duplicates_refuse(self):
        cases = [[entity('MentionName', 0, 3, user_id=42), entity(kind, 1, 2, **metadata)]
            for kind, metadata in [('Mention', {}), ('MentionName', {'user_id': 43}),
                ('Hashtag', {}), ('Cashtag', {}), ('BotCommand', {}), ('Code', {}), ('Pre', {}),
                ('PreCode', {'language': 'python'}), ('BlockQuote', {}), ('ExpandableBlockQuote', {})]]
        cases += [[entity('MentionName', 0, 3, user_id=42)] * 2,
            [entity('MentionName', 0, 3, user_id=42), entity('MentionName', 0, 3, user_id=43)],
            [entity('Bold', 0, 3), entity('MentionName', 2, 3, user_id=42)]]
        for entities in cases:
            with self.subTest(entities=entities), self.assertRaises(ValueError): source_from_message(target('abcde', entities), 123, 55)

    def test_escape_then_complete_bound_never_silently_truncates_spans(self):
        source = self.accepted(target('Ａ\\"\n', [entity('MentionName', 0, 4, user_id=42)]))
        self.assertEqual(source.target().text, MARKER + 'Formatted target: {"entities":[{"length":4,"offset":0,"text":"A\\\\\\\"","type":"mention_name","user_id":42}],"offset_basis":"original source UTF-16 code units","text":"A\\\\\\\""}')
        self.assertTrue(source.target().sanitized)
        for raw in (target('x'*2000, [entity('Mention', 0, 2000)]),
            target('x'*4097, [entity('MentionName', 0, 1, user_id=42)]),
            target('\ufdfa'*250, [entity('MentionName', 0, 250, user_id=42)]),
            target('abc', [entity('MentionName', 0, 3, user_id=42)]*33)):
            with self.assertRaises(ValueError): source_from_message(raw, 123, 55)

    def test_exact_preview_and_entity_count_caps_accept_edge_then_refuse_overflow(self):
        # Independently encoded one-span record supplies only the fixed overhead.
        expected = MARKER + 'Formatted target: {"entities":[{"length":1,"offset":0,"text":"x","type":"mention"}],"offset_basis":"original source UTF-16 code units","text":""}'
        text = 'x' * (4096-len(expected))
        source = self.accepted(target(text, [entity('Mention', 0, 1)]))
        self.assertEqual(source.target().text, expected[:-2]+text+'"}')
        self.assertEqual(len(source.target().text), 4096)
        with self.assertRaises(ValueError): source_from_message(target(text+'x', [entity('Mention', 0, 1)]), 123, 55)
        entities = [entity('MentionName', offset, 1, user_id=42) for offset in range(32)]
        source = self.accepted(target('x'*32, entities))
        self.assertEqual(len(json.loads(source.target().text.split('Formatted target: ', 1)[1])['entities']), 32)
        with self.assertRaises(ValueError): source_from_message(target('x'*33, entities+[entity('MentionName', 32, 1, user_id=42)]), 123, 55)

    def test_old_versions_cannot_be_forged_to_authorize_identity_or_v7_media(self):
        self.accepted(target())
        value = json.loads(CANONICAL)
        for version in (1, 2, 3, 4, 5, 6, True, '7', 8):
            value['version'] = version
            with self.subTest(version=version), self.assertRaises(ValueError): ReplySource(serialize(value))
        for raw in old_forms():
            value = json.loads(source_from_message(raw, 123, 55).projection_json); value['version'] = 7
            with self.assertRaises(ValueError): ReplySource(serialize(value))

    def test_v1_through_v6_keep_independent_canonical_bytes_and_authority(self):
        expected_caps = [(), ('reply_media_targets',), ('reply_formatted_targets',),
            ('reply_media_targets', 'reply_formatted_targets'), ('reply_formatted_targets', 'reply_lexical_targets'),
            ('reply_media_targets', 'reply_formatted_targets', 'reply_lexical_targets')]
        for version, raw, caps in zip(range(1, 7), old_forms(), expected_caps):
            source = source_from_message(raw, 123, 55)
            literal = SHELL_PREFIX + serialize(old_expected_content(version)) + SHELL_SUFFIX + str(version) + '}'
            with self.subTest(version=version):
                self.assertEqual(source.projection_json, literal)
                self.assertEqual(source.required_capabilities, caps)

    def test_identity_media_requires_v8_and_wrong_source_anchor_stays_rejected(self):
        for kind in ('document', 'photo', 'voice_note'):
            for identity in (entity('Mention'), entity('MentionName', user_id=42)):
                with self.subTest(kind=kind):
                    source = source_from_message(target(content=media_content(kind, 'abc', [identity])), 123, 55)
                    value = json.loads(source.projection_json)
                    self.assertEqual(value['version'], 8)
                    self.assertEqual(source.required_capabilities,
                        ('reply_media_targets', 'reply_formatted_targets', 'reply_identity_targets'))
                    for version in (2, 4, 6, 7):
                        with self.assertRaises(ValueError): ReplySource(serialize({**value, 'version': version}))
        for raw in (target(chat_id=124), target(id=56)):
            with self.assertRaises(ValueError): source_from_message(raw, 123, 55)

    def test_trusted_policy_identity_optin_is_known_and_default_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700); path = root/'runtime.toml'
            self.assertNotIn('reply_identity_targets', load_runtime_policy(path).enabled_capabilities)
            path.write_text('config_version=1\nenabled_capabilities=["send","reply_text_send","reply_formatted_targets","reply_identity_targets"]\n')
            path.chmod(0o600)
            self.assertEqual(load_runtime_policy(path).enabled_capabilities,
                ('reply_formatted_targets', 'reply_identity_targets', 'reply_text_send', 'send'))


class IdentityRaw:
    """Fresh fake of raw provider transport, never native initialization."""
    def __init__(self):
        self.sent = []; self.events = deque(); self.source = target()
        self.mode = 'sent'; self.hook = None; self.account = 7

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
        elif kind == 'getMessageProperties': value = {'@type': 'messageProperties', 'can_be_replied': True}
        elif kind == 'sendMessage':
            inp = request['input_message_content']
            content = {'@type': 'messageText', 'text': copy.deepcopy(inp['text'])} if inp['@type'] == 'inputMessageText' else media_content(text=inp['caption']['text'])
            pre = self.outgoing(content, sending_state={'@type': 'messageSendingStatePending', 'sending_id': request['options']['sending_id']})
            self.events.append({**pre, '@extra': request['@extra']})
            final = self.outgoing(content, 701, sending_state=None)
            if self.mode == 'wrong': final['reply_to']['message_id'] = 56
            self.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': final})
            return
        else: raise AssertionError(kind)
        self.events.append({**value, '@extra': request['@extra']})

    def receive(self, timeout):
        return self.events.popleft() if self.events else None

    def close(self): pass


class IdentityLifecycle:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.raw = IdentityRaw(); self.client = TDLibClient(raw=self.raw)
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
    def restore(self):
        self.broker._policy = POLICY; self.raw.source = target(); self.raw.mode = 'sent'; self.raw.hook = None
        self.broker._approval_prompt = self.approve

    def test_identity_preview_reaches_owner_and_plain_original_route_sends_once(self):
        preview = self.prepare(); did = preview['draft']['draft_id']
        self.assertEqual(preview['reply_target']['text'], DISPLAY)
        self.assertEqual(preview['reply_target']['source_sha256'], hashlib.sha256(CANONICAL.encode()).hexdigest())
        self.assertEqual(self.broker._drafts.source_required_capabilities(did, owner=DraftOwner(CLIENT, 7)), CAPS)
        self.assertEqual(self.dispatch(self.operations[0], {'draft_id': did})['reply'], preview)
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))
        self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual(self.approvals[0]['reply_artifact_preview' if self.artifact_path else 'reply_preview'], preview)
        wire = self.sends()[0]
        self.assertEqual(wire['chat_id'], 123)
        self.assertEqual(wire['reply_to'], {'@type': 'inputMessageReplyToMessage', 'message_id': 55,
            'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''})
        self.assertIsNone(wire['topic_id']); self.assertIsNone(wire['reply_markup'])
        self.assertEqual(wire['input_message_content']['caption' if self.artifact_path else 'text']['entities'], [])
        self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual((len(self.sends()), len(self.approvals)), (1, 1))
        self.assertTrue(all(request['@type'] in ('getAuthorizationState', 'getMe', 'getChat', 'getMessage', 'getMessageProperties', 'sendMessage') for request in self.raw.sent))

    def test_missing_each_conjunct_refuses_every_lifecycle_and_send(self):
        for lexical in (False, True):
            for missing in MIXED_CAPS if lexical else CAPS:
                self.restore()
                if lexical: self.raw.source['content']['text']['entities'].append(entity('Hashtag', 12, 4))
                allowed = RuntimePolicy(enabled_capabilities=tuple(cap for cap in POLICY.enabled_capabilities if cap != missing))
                self.broker._policy = allowed
                self.assertEqual(self.dispatch(self.preparation, self.prepare_payload())['status'], 'unavailable')
                self.broker._policy = POLICY; did = self.prepare()['draft']['draft_id']; self.broker._policy = allowed
                for op, extra in ((self.operations[0], {}), (self.operations[1], self.revision), (self.operations[2], {})):
                    self.assertEqual(self.dispatch(op, {'draft_id': did, **extra})['status'], 'unavailable')
                self.assertEqual(self.send(did)['status'], 'expired')
                self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_bad_provider_preview_or_outgoing_entity_never_mutates_registry(self):
        before = self.broker._drafts.known_ids()
        bads = [target('x'*2000, [entity('Mention', 0, 2000)]), target(entities=[entity('MentionName', user_id=True)]), target(id=56)]
        for source in bads:
            self.raw.source = source
            self.assertEqual(self.dispatch(self.preparation, self.prepare_payload())['status'], 'unavailable')
            self.assertEqual(self.broker._drafts.known_ids(), before)
        self.raw.source = target()
        from pydantic import ValidationError
        with self.assertRaises(ValidationError): self.dispatch(self.preparation, {**self.prepare_payload(), 'entities': [entity('Mention')]})
        self.assertEqual(self.broker._drafts.known_ids(), before)
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_id_only_drift_before_owner_and_after_owner_never_sends(self):
        for phase in ('before', 'after'):
            self.restore(); preview = self.prepare(); did = preview['draft']['draft_id']
            if phase == 'before': self.raw.source['content']['text']['entities'][1]['type']['user_id'] = 43
            else:
                def approve(**facts):
                    self.approvals.append(facts); self.raw.source['content']['text']['entities'][1]['type']['user_id'] = 43; return True
                self.broker._approval_prompt = approve
            self.assertEqual(self.send(did)['status'], 'failed')
            self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_failed')
        self.assertEqual(len(self.sends()), 0)

    def test_id_edit_during_final_provider_read_never_registers_or_sends(self):
        did = self.prepare()['draft']['draft_id']
        def hook(request):
            if request['@type'] == 'getMessage': self.raw.source['content']['text']['entities'][1]['type']['user_id'] = 43
        self.raw.hook = hook
        self.assertEqual(self.send(did)['status'], 'failed')
        self.assertEqual(len(self.sends()), 0)
        self.assertIsNone(self.client._send_observations.snapshot(did))

    def test_revision_refresh_renew_preview_and_invalidate_old_approval(self):
        original = self.prepare(); did = original['draft']['draft_id']
        revised = self.dispatch(self.operations[1], {'draft_id': did, **self.revision})['reply']
        self.assertEqual(revised['reply_target'], original['reply_target'])
        self.assertNotEqual(revised['preview_sha256'], original['preview_sha256'])
        self.assertEqual(self.send(did)['status'], 'expired')
        self.raw.source['content']['text']['entities'][1]['type']['user_id'] = 43
        refreshed = self.dispatch(self.operations[2], {'draft_id': revised['draft']['draft_id']})['reply']
        self.assertNotEqual(refreshed['reply_target']['source_sha256'], original['reply_target']['source_sha256'])
        self.assertIn('"user_id":43', refreshed['reply_target']['text'])
        self.assertEqual(self.send(revised['draft']['draft_id'])['status'], 'expired')
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_refresh_requires_old_identity_authority_and_new_lexical_authority(self):
        did = self.prepare()['draft']['draft_id']; self.raw.source = target('abc', [entity('Bold')])
        self.broker._policy = RuntimePolicy(enabled_capabilities=BASE + ('reply_formatted_targets',))
        self.assertEqual(self.dispatch(self.operations[2], {'draft_id': did})['status'], 'unavailable')
        self.broker._policy = POLICY
        styled = self.dispatch(self.operations[2], {'draft_id': did})['reply']; self.raw.source = target()
        self.raw.source['content']['text']['entities'].append(entity('Hashtag', 12, 4))
        self.broker._policy = RuntimePolicy(enabled_capabilities=BASE + CAPS)
        self.assertEqual(self.dispatch(self.operations[2], {'draft_id': styled['draft']['draft_id']})['status'], 'unavailable')
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_owner_denial_false_approval_and_foreign_owner_have_no_transport(self):
        preview = self.prepare(); did = preview['draft']['draft_id']
        self.assertEqual(self.dispatch(self.send_operation, {'draft_id': did, 'approved': False})['status'], 'not_approved')
        for op, extra in ((self.operations[0], {}), (self.operations[1], self.revision), (self.operations[2], {})):
            self.assertEqual(self.dispatch(op, {'draft_id': did, **extra}, FOREIGN)['status'], 'unavailable')
        self.broker._approval_prompt = lambda **facts: False
        self.assertEqual(self.send(did)['status'], 'not_approved')
        self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual(len(self.sends()), 0)

    def test_authority_drift_at_owner_properties_and_registration_stops_transport(self):
        for phase in ('owner', 'properties', 'registration'):
            for missing in MIXED_CAPS:
                self.restore(); self.raw.source['content']['text']['entities'].append(entity('Hashtag', 12, 4))
                did = self.prepare()['draft']['draft_id']
                denied = RuntimePolicy(enabled_capabilities=tuple(cap for cap in POLICY.enabled_capabilities if cap != missing))
                if phase == 'owner':
                    def approve(**facts): self.approvals.append(facts); self.broker._policy = denied; return True
                    self.broker._approval_prompt = approve
                elif phase == 'properties':
                    self.raw.hook = lambda request: setattr(self.broker, '_policy', denied) if request['@type'] == 'getMessageProperties' else None
                if phase == 'registration':
                    original = self.client._send_observations.register
                    def register(*args, **kw): original(*args, **kw); self.broker._policy = denied
                    with patch.object(self.client._send_observations, 'register', side_effect=register): result = self.send(did)
                else: result = self.send(did)
                self.assertNotEqual(result['status'], 'sent')
                self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual(len(self.sends()), 0)

    def test_unknown_terminal_replay_and_disabled_cap_never_resend(self):
        self.raw.mode = 'wrong'; did = self.prepare()['draft']['draft_id']
        self.assertEqual(self.send(did)['status'], 'outcome_unknown')
        for missing in CAPS:
            self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(cap for cap in POLICY.enabled_capabilities if cap != missing))
            self.assertEqual(self.send(did)['status'], 'expired')
            self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['status'], 'outcome_unknown')
        self.broker._policy = POLICY
        self.assertEqual(self.send(did)['status'], 'outcome_unknown')
        self.assertEqual((len(self.sends()), len(self.approvals)), (1, 1))


class IdentityTextLifecycleTests(IdentityLifecycle, unittest.TestCase):
    artifact_path = False


class IdentityArtifactLifecycleTests(IdentityLifecycle, unittest.TestCase):
    artifact_path = True
