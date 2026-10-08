"""Lexical source evidence through real parsers, Broker, registry and TDJSON.

Only raw provider I/O and owner decisions are synthetic. Expected source shells,
span records and displays are literal facts, never production projection output.
"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.config import RuntimePolicy, load_runtime_policy
from telegram_search_mcp.outgoing_drafts import DraftError, DraftOwner
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from telegram_search_mcp.reply_formatted_sources import formatted_text
from test_text_replies import ReplyFixture, target, outgoing, CLIENT, FOREIGN, ANCHOR
from test_artifact_replies import ArtifactSendFixture, content_fixture

MARKER = '[untrusted Telegram evidence] '
TEXT = '😀 #tag $USD /go'
CAPS = ('reply_formatted_targets', 'reply_lexical_targets')
BASE = ('send', 'reply_text_send', 'reply_artifact_send')
POLICY = RuntimePolicy(enabled_capabilities=BASE + CAPS)
KINDS = ('Hashtag', 'Cashtag', 'BotCommand')
STYLE_KINDS = ('Bold', 'Italic', 'Underline', 'Strikethrough', 'Spoiler',
               'Code', 'Pre', 'PreCode', 'BlockQuote', 'ExpandableBlockQuote')


def entity(kind, offset=0, length=2, **metadata):
    return {'@type': 'textEntity', 'offset': offset, 'length': length,
            'type': {'@type': 'textEntityType' + kind, **metadata}}


LEXICAL = [entity('Hashtag', 3, 4), entity('Cashtag', 8, 4), entity('BotCommand', 13, 3)]
RECORDS = [{'type': 'hashtag', 'offset': 3, 'length': 4, 'text': '#tag'},
           {'type': 'cashtag', 'offset': 8, 'length': 4, 'text': '$USD'},
           {'type': 'bot_command', 'offset': 13, 'length': 3, 'text': '/go'}]
DISPLAY = MARKER + 'Formatted target: {"entities":[{"length":4,"offset":3,"text":"#tag","type":"hashtag"},{"length":4,"offset":8,"text":"$USD","type":"cashtag"},{"length":3,"offset":13,"text":"/go","type":"bot_command"}],"offset_basis":"original source UTF-16 code units","text":"😀 #tag $USD /go"}'
# The independent shell includes every stable provider safety fact and nullable field.
SHELL_PREFIX = '{"anchor":{"chat_id":123,"message_id":55},"message":{"@type":"message","auto_delete_in":0.0,"chat_id":123,"content":'
SHELL_SUFFIX = ',"date":1700000000,"edit_date":0,"ephemeral_content":null,"ephemeral_message_id":0,"forward_info":null,"id":55,"import_info":null,"is_from_offline":false,"is_outgoing":false,"receiver_id":null,"reply_markup":null,"reply_to":null,"scheduling_state":null,"self_destruct_in":0.0,"self_destruct_type":null,"sender_id":{"@type":"messageSenderUser","user_id":8},"sending_state":null,"topic_id":null},"version":'
LEXICAL_CONTENT = '{"@type":"messageText","link_preview":null,"link_preview_options":null,"text":{"@type":"formattedText","entities":[{"@type":"textEntity","length":4,"offset":3,"type":{"@type":"textEntityTypeHashtag"}},{"@type":"textEntity","length":4,"offset":8,"type":{"@type":"textEntityTypeCashtag"}},{"@type":"textEntity","length":3,"offset":13,"type":{"@type":"textEntityTypeBotCommand"}}],"text":"😀 #tag $USD /go"}}'
CANONICAL = SHELL_PREFIX + LEXICAL_CONTENT + SHELL_SUFFIX + '5}'
IDENTITY = 'c2720445a45267813688ff73fa188aa060c1b661aefaf1650d42f690697b5ab3'


def lexical(text=TEXT, entities=None):
    raw = target(text)
    raw['content']['text']['entities'] = copy.deepcopy(LEXICAL if entities is None else entities)
    return raw


def serialize(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


class LexicalSourceTests(unittest.TestCase):
    def accepted(self, raw):
        try:
            return source_from_message(raw, 123, 55)
        except ValueError:
            self.fail('approved lexical TEXT source must produce complete evidence')

    def test_three_provider_labels_preserve_literal_v5_shell_spans_and_digest(self):
        # Losing a label, flattening the source or using scalar offsets breaks evidence.
        source = self.accepted(lexical())
        self.assertEqual(source.projection_json, CANONICAL)
        self.assertEqual(source.source_sha256, hashlib.sha256(CANONICAL.encode()).hexdigest())
        self.assertEqual(source.target().text, DISPLAY)
        self.assertEqual(source.required_capabilities, CAPS)
        self.assertFalse(source.is_media)
        self.assertFalse(source.target().sanitized)
        self.assertFalse(source.target().truncated)
        with self.assertRaises(ValueError):
            _ = source.required_capability

    def test_each_lexical_type_can_stand_alone_without_recognizing_grammar(self):
        for kind, label in zip(KINDS, ('hashtag', 'cashtag', 'bot_command')):
            with self.subTest(kind=kind):
                source = self.accepted(lexical('abc', [entity(kind, 0, 3)]))
                self.assertEqual(json.loads(source.projection_json)['version'], 5)
                self.assertEqual(json.loads(source.target().text.split('Formatted target: ', 1)[1]),
                    {'text': 'abc', 'offset_basis': 'original source UTF-16 code units',
                     'entities': [{'type': label, 'offset': 0, 'length': 3, 'text': 'abc'}]})

    def test_mixed_bold_canonical_permutation_and_volatile_provider_facts(self):
        spans = [entity('Bold', 0, 16), *LEXICAL]
        first = self.accepted(lexical(entities=spans))
        shuffled = lexical(entities=list(reversed(spans)))
        shuffled.update(unread_mention=True, views=999)
        other = self.accepted(shuffled)
        self.assertEqual(first.projection_json, other.projection_json)
        self.assertEqual(json.loads(first.target().text.split('Formatted target: ', 1)[1])['entities'],
            [{'type': 'bold', 'offset': 0, 'length': 16, 'text': TEXT}, *RECORDS])
        shuffled['content']['text']['entities'][0]['length'] = 2
        self.assertEqual(first.target().text, other.target().text)

    def test_all_ten_styles_remain_available_disjoint_from_lexical_spans(self):
        for kind in STYLE_KINDS:
            metadata = {'language': 'python'} if kind == 'PreCode' else {}
            with self.subTest(kind=kind):
                source = self.accepted(lexical('#x abc', [entity('Hashtag'), entity(kind, 3, 3, **metadata)]))
                self.assertEqual(source.required_capabilities, CAPS)
                self.assertEqual(json.loads(source.target().text.split('Formatted target: ', 1)[1])['entities'][0],
                    {'type': 'hashtag', 'offset': 0, 'length': 2, 'text': '#x'})

    def test_simple_style_and_lexical_containment_coextensive_and_adjacency(self):
        for style in STYLE_KINDS[:5]:
            for spans in ([entity(style, 0, 4), entity('Hashtag', 1, 2)],
                          [entity('Hashtag', 0, 4), entity(style, 1, 2)],
                          [entity('Hashtag', 0, 4), entity(style, 0, 4)],
                          [entity('Hashtag', 0, 2), entity(style, 2, 2)]):
                with self.subTest(style=style, spans=spans):
                    self.assertEqual(self.accepted(lexical('#abc', spans)).required_capabilities, CAPS)
        self.assertEqual(self.accepted(lexical('#x/y', [entity('Hashtag'), entity('BotCommand', 2, 2)])).required_capabilities, CAPS)

    def test_original_nonbmp_boundaries_and_normalized_display_spans(self):
        source = self.accepted(lexical('ﬃ 😀 #ＴＡＧ', [entity('Bold', 0, 9), entity('Hashtag', 5, 4)]))
        self.assertEqual(json.loads(source.target().text.split('Formatted target: ', 1)[1]),
            {'text': 'ffi 😀 #TAG', 'offset_basis': 'original source UTF-16 code units',
             'entities': [{'type': 'bold', 'offset': 0, 'length': 9, 'text': 'ffi 😀 #TAG'},
                          {'type': 'hashtag', 'offset': 5, 'length': 4, 'text': '#TAG'}]})
        self.assertTrue(source.target().sanitized)
        scalar = self.accepted(lexical('😀', [entity('Hashtag', 0, 2)]))
        self.assertIn('"text":"😀","type":"hashtag"', scalar.target().text)
        for offset, length in ((0, 1), (1, 1), (1, 2)):
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                source_from_message(lexical('😀x', [entity('Hashtag', offset, length)]), 123, 55)

    def test_strict_closed_shapes_int32_coordinates_and_fieldless_lexical_types(self):
        bads = []
        for kind in KINDS:
            for metadata in ({'language': ''}, {'url': 'x'}, {'user_id': 8}, {'unknown': None}):
                bads.append(lexical('#x', [entity(kind, **metadata)]))
        for field, values in (('offset', (True, False, -1, 2**31, '0', 0.0)),
                              ('length', (True, False, 0, -1, 2**31, '2', 2.0))):
            for value in values:
                span = entity('Hashtag'); span[field] = value
                bads.append(lexical('#x', [span]))
        for update in ({'extra': None}, {'@type': 'other'}, {'type': None}, {'type': {'@type': True}},
                       {'type': {'@type': 'textEntityTypeMention'}}, {'type': {'@type': 'textEntityTypeUrl'}},
                       {'type': {'@type': 'textEntityTypeCustomEmoji'}}, {'type': {'@type': 'textEntityTypeDateTime'}}):
            bads.append(lexical('#x', [{**entity('Hashtag'), **update}]))
        for level in ('content', 'text'):
            bad = lexical(); obj = bad['content'] if level == 'content' else bad['content']['text']
            obj['unknown'] = None; bads.append(bad)
        bad = lexical(); bad['content']['text']['entities'] = tuple(LEXICAL); bads.append(bad)
        for raw in bads:
            with self.subTest(raw=repr(raw)[:100]), self.assertRaises(ValueError):
                source_from_message(raw, 123, 55)

    def test_lexical_duplicates_crossings_pairs_quote_and_code_overlaps_fail_closed(self):
        pairs = [[entity('Hashtag', 0, 4), entity('Hashtag', 0, 4)],
                 [entity('Hashtag', 0, 3), entity('Bold', 2, 2)]]
        for left in KINDS:
            for right in KINDS:
                pairs.extend([[entity(left, 0, 4), entity(right, 1, 2)],
                              [entity(left, 0, 4), entity(right, 0, 4)]])
        for other in ('Code', 'Pre', 'PreCode', 'BlockQuote', 'ExpandableBlockQuote'):
            metadata = {'language': ''} if other == 'PreCode' else {}
            pairs.extend([[entity('Hashtag', 0, 4), entity(other, 1, 2, **metadata)],
                          [entity(other, 0, 4, **metadata), entity('Hashtag', 1, 2)]])
        pairs.extend([[entity('Hashtag', 0, 1), entity('BlockQuote', 1, 3), entity('ExpandableBlockQuote', 1, 3)],
                      [entity('Hashtag', 0, 1), entity('Code', 1, 3), entity('Bold', 1, 3)]])
        for spans in pairs:
            with self.subTest(spans=spans), self.assertRaises(ValueError):
                source_from_message(lexical('#abc', spans), 123, 55)

    def test_entity_count_bounds_and_empty_entities_select_old_plain_version(self):
        self.assertEqual(json.loads(source_from_message(lexical('#x', []), 123, 55).projection_json)['version'], 1)
        with self.assertRaises(ValueError):
            formatted_text({'@type': 'formattedText', 'text': '#x', 'entities': []}, lexical=True)
        # 32 adjacent fieldless spans fit the complete display bound.
        spans = [entity('Hashtag', i, 1) for i in range(32)]
        self.assertEqual(self.accepted(lexical('#' * 32, spans)).required_capabilities, CAPS)
        with self.assertRaises(ValueError):
            source_from_message(lexical('#' * 33, spans + [entity('Hashtag', 32, 1)]), 123, 55)

    def test_raw_nonblank_unicode_control_and_display_expansion_bounds(self):
        for text in ('', ' ', '\n\t', '#\r', '#\x00', '#\u202e', '#\ud800', '#' * 4097, '\ufdfa' * 4096):
            with self.subTest(text=repr(text)[:30]), self.assertRaises(ValueError):
                source_from_message(lexical(text, [entity('Hashtag', 0, 1)]), 123, 55)
        source = self.accepted(lexical('#\n\tz', [entity('Hashtag', 0, 4)]))
        self.assertEqual(json.loads(source.target().text.split('Formatted target: ', 1)[1]),
            {'text': '# z', 'offset_basis': 'original source UTF-16 code units',
             'entities': [{'type': 'hashtag', 'offset': 0, 'length': 4, 'text': '# z'}]})
        self.assertTrue(source.target().sanitized)

    def test_complete_marker_inclusive_4096_accepted_and_4097_refused(self):
        # Expected JSON is constructed from independent literal display facts.
        record = {'entities': [{'length': 1, 'offset': 0, 'text': '#', 'type': 'hashtag'}],
                  'offset_basis': 'original source UTF-16 code units', 'text': '#'}
        padding = 4096 - len(MARKER + 'Formatted target: ' + serialize(record))
        text = '#' + 'x' * padding
        record['text'] = text
        want = MARKER + 'Formatted target: ' + serialize(record)
        self.assertEqual(len(want), 4096)
        self.assertEqual(self.accepted(lexical(text, [entity('Hashtag', 0, 1)])).target().text, want)
        with self.assertRaises(ValueError):
            source_from_message(lexical(text + 'x', [entity('Hashtag', 0, 1)]), 123, 55)

    def test_oversized_utf8_projection_refuses_before_json_parse_even_when_forged(self):
        value = json.loads(CANONICAL)
        value['message']['content']['text']['text'] = '😀' * 17000
        oversized = serialize(value)
        self.assertLess(len(oversized), 65536)
        self.assertGreater(len(oversized.encode()), 65536)
        forged = object.__new__(ReplySource)
        object.__setattr__(forged, 'projection_json', oversized)
        with patch('telegram_search_mcp.reply_drafts.json.loads', side_effect=AssertionError('oversized JSON was parsed')):
            with self.assertRaises(ValueError): ReplySource(oversized)
            with self.assertRaises(ValueError): forged.target()
            with self.assertRaises(ValueError): _ = forged.required_capabilities

    def test_forged_v5_old_version_discriminators_and_noncanonical_order_refuse(self):
        self.assertEqual(ReplySource(CANONICAL).target().text, DISPLAY)
        cases = []
        for version in (1, 2, 3, 4, 6, True):
            value = json.loads(CANONICAL); value['version'] = version; cases.append(value)
        value = json.loads(CANONICAL); value['message']['content']['text']['entities'].reverse(); cases.append(value)
        value = json.loads(CANONICAL); value['message']['content']['text']['entities'] = [entity('Bold', 0, 16)]; cases.append(value)
        value = json.loads(CANONICAL); value['message']['content']['text']['entities'] = []; cases.append(value)
        value = json.loads(CANONICAL); value['message']['content']['text']['entities'][0]['type']['url'] = 'x'; cases.append(value)
        for value in cases:
            raw = serialize(value)
            forged = object.__new__(ReplySource); object.__setattr__(forged, 'projection_json', raw)
            with self.subTest(version=value['version']), self.assertRaises(ValueError): ReplySource(raw)
            with self.assertRaises(ValueError): forged.target()
            with self.assertRaises(ValueError): _ = forged.required_capabilities

    def test_default_formatted_parser_mode_still_rejects_all_lexical_types(self):
        for kind in KINDS:
            fmt = lexical('#x', [entity(kind)])['content']['text']
            with self.subTest(kind=kind), self.assertRaises(ValueError): formatted_text(fmt)
            self.assertEqual(formatted_text(fmt, lexical=True), fmt)

    def test_v1_v2_v3_v4_literal_canonical_and_display_bytes_preserved(self):
        plain = '{"@type":"messageText","link_preview":null,"link_preview_options":null,"text":{"@type":"formattedText","entities":[],"text":"A😀BC"}}'
        styled = '{"@type":"messageText","link_preview":null,"link_preview_options":null,"text":{"@type":"formattedText","entities":[{"@type":"textEntity","length":5,"offset":0,"type":{"@type":"textEntityTypeBold"}}],"text":"A😀BC"}}'
        fmt = '{"@type":"formattedText","entities":[{"@type":"textEntity","length":5,"offset":0,"type":{"@type":"textEntityTypeBold"}}],"text":"A😀BC"}'
        facts = '{"file_name":"evidence.txt","mime_type":"text/plain","size":10,"unique_id_sha256":"' + IDENTITY + '"}'
        media_plain = '{"caption":{"@type":"formattedText","entities":[],"text":"A😀BC"},"kind":"document","media":' + facts + '}'
        media_styled = '{"caption":' + fmt + ',"kind":"document","media":' + facts + '}'
        record = '{"entities":[{"length":5,"offset":0,"text":"A😀BC","type":"bold"}],"offset_basis":"original source UTF-16 code units","text":"A😀BC"}'
        a = target('A😀BC'); b = lexical('A😀BC', [entity('Bold', 0, 5)])
        c = target(content=content_fixture('document', 'A😀BC')); d = copy.deepcopy(c)
        d['content']['caption']['entities'] = [entity('Bold', 0, 5)]
        for version, raw, content, display in ((1, a, plain, MARKER + 'A😀BC'),
            (2, c, media_plain, MARKER + 'Media target: {"caption":"A😀BC","kind":"document","media":' + facts + '}'),
            (3, b, styled, MARKER + 'Formatted target: ' + record),
            (4, d, media_styled, MARKER + 'Media target: {"caption":' + record + ',"kind":"document","media":' + facts + '}')):
            with self.subTest(version=version):
                source = source_from_message(raw, 123, 55)
                self.assertEqual(source.projection_json, SHELL_PREFIX + content + SHELL_SUFFIX + str(version) + '}')
                self.assertEqual(source.target().text, display)

    def test_default_media_parser_rejects_lexical_for_every_media_kind_and_type(self):
        from telegram_search_mcp.reply_media_sources import media_content
        for media in ('document', 'photo', 'voice_note'):
            for kind in KINDS:
                raw = target(content=content_fixture(media, '#x'))
                raw['content']['caption']['entities'] = [entity(kind)]
                with self.subTest(media=media, kind=kind), self.assertRaises(ValueError): media_content(raw['content'])

    def test_raw_text_range_kind_and_shell_edits_change_identity(self):
        base = self.accepted(lexical()).source_sha256
        variants = []
        raw = lexical(); raw['content']['text']['text'] = '😀 #Tag $USD /go'; variants.append(raw)
        raw = lexical(); raw['content']['text']['entities'][0]['length'] = 3; variants.append(raw)
        raw = lexical(); raw['content']['text']['entities'][0]['type']['@type'] = 'textEntityTypeCashtag'; variants.append(raw)
        raw = lexical(); raw['edit_date'] = 1; variants.append(raw)
        for raw in variants:
            self.assertNotEqual(self.accepted(raw).source_sha256, base)

    def test_policy_accepts_lexical_optin_but_default_keeps_it_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700); path = root / 'runtime.toml'
            self.assertNotIn('reply_lexical_targets', load_runtime_policy(path).enabled_capabilities)
            path.write_text('config_version=1\nenabled_capabilities=["send","reply_text_send","reply_formatted_targets","reply_lexical_targets"]\n')
            path.chmod(0o600)
            self.assertEqual(load_runtime_policy(path).enabled_capabilities,
                             ('reply_formatted_targets', 'reply_lexical_targets', 'reply_text_send', 'send'))


class LexicalLifecycleRisks:
    def setUp(self):
        super().setUp(); self.broker._policy = POLICY; self.raw.source = lexical()

    @property
    def ops(self):
        return ('get_reply_artifact_draft', 'update_reply_artifact_draft', 'refresh_reply_artifact_draft') if self.artifact_path else ('get_reply_draft', 'update_reply_draft', 'refresh_reply_draft')

    def extra(self):
        return {'caption': 'new'} if self.artifact_path else {'text': 'new'}

    def preparation(self):
        return ('prepare_reply_artifact_send', dict(recipient=123, reply_to=ANCHOR, artifact_id=self.artifact.artifact_id,
            display_name='evidence.txt', mime_type='text/plain', caption='Caption', kind='document')) if self.artifact_path else ('prepare_reply_text_send', dict(recipient=123, reply_to=ANCHOR, text='Final reply'))

    def classifier(self, did, **kw):
        return self.broker._drafts.source_required_capabilities(did, owner=kw.pop('owner', DraftOwner(CLIENT, 7)), **kw)

    def restore(self):
        self.broker._policy = POLICY; self.broker._client = self.client
        self.raw.source = lexical(); self.raw.account = 7; self.raw.title = 'Recipient'; self.raw.can_reply = True; self.raw.mode = 'sent'
        self.broker._approval_prompt = lambda **kw: self.approvals.append(kw) or True

    def test_complete_lexical_evidence_reaches_owner_and_one_plain_exact_wire(self):
        preview = self.prepare(); did = preview['draft']['draft_id']
        self.assertEqual(preview['reply_target']['text'], DISPLAY)
        self.assertEqual(preview['reply_target']['source_sha256'], hashlib.sha256(CANONICAL.encode()).hexdigest())
        self.assertEqual(self.classifier(did), CAPS)
        self.assertFalse(self.broker._drafts.is_media_reply(did, owner=DraftOwner(CLIENT, 7)))
        with self.assertRaises(DraftError): self.broker._drafts.source_required_capability(did, owner=DraftOwner(CLIENT, 7))
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))
        self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual(self.approvals[0]['reply_artifact_preview' if self.artifact_path else 'reply_preview'], preview)
        self.assertEqual(self.sends()[0]['input_message_content']['caption' if self.artifact_path else 'text']['entities'], [])
        self.assertEqual(self.sends()[0]['reply_to'], {'@type': 'inputMessageReplyToMessage', 'message_id': 55,
            'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''})
        self.assertIsNone(self.sends()[0]['topic_id'])
        self.assertIsNotNone(self.client._send_observations.snapshot(did))
        self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual((len(self.sends()), len(self.approvals)), (1, 1))

    def test_each_missing_source_capability_refuses_prepare_get_update_refresh_and_send(self):
        op, payload = self.preparation()
        for missing in CAPS:
            denied = RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c != missing))
            self.broker._policy = denied
            self.assertEqual(self.dispatch(op, payload)['status'], 'unavailable')
            self.broker._policy = POLICY; did = self.prepare()['draft']['draft_id']; self.broker._policy = denied
            for operation, extra in ((self.ops[0], {}), (self.ops[1], self.extra()), (self.ops[2], {})):
                self.assertEqual(self.dispatch(operation, dict(draft_id=did, **extra))['status'], 'unavailable')
            self.assertEqual(self.send(did)['status'], 'expired')
            self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_each_missing_cap_refuses_all_terminal_replays_restoration_never_resends(self):
        for mode, want in (('sent', 'sent'), ('caption' if self.artifact_path else 'wrong', 'outcome_unknown'), ('properties', 'failed')):
            self.restore(); self.raw.mode = 'sent' if mode == 'properties' else mode; self.raw.can_reply = mode != 'properties'
            did = self.prepare()['draft']['draft_id']; before = len(self.sends()); decisions = len(self.approvals)
            self.assertEqual(self.send(did)['status'], want)
            self.assertEqual(len(self.sends()) - before, 0 if mode == 'properties' else 1)
            self.assertEqual(len(self.approvals) - decisions, 1)
            count = len(self.sends()); decisions = len(self.approvals)
            for missing in CAPS:
                self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c != missing))
                self.assertEqual(self.send(did)['status'], 'expired')
                self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['status'], want)
            self.broker._policy = POLICY
            self.assertEqual(self.send(did)['status'], want)
            self.assertEqual((len(self.sends()), len(self.approvals)), (count, decisions))

    def test_owner_account_pending_ttl_and_terminal_provider_epoch_monotonic_ttl(self):
        registry = self.broker._drafts; owner = DraftOwner(CLIENT, 7); did = self.prepare()['draft']['draft_id']
        for wrong in (DraftOwner(FOREIGN, 7), DraftOwner(CLIENT, 8)):
            with self.assertRaises(DraftError): self.classifier(did, owner=wrong)
        for operation, extra in ((self.ops[0], {}), (self.ops[1], self.extra()), (self.ops[2], {})):
            self.assertEqual(self.dispatch(operation, dict(draft_id=did, **extra), FOREIGN)['status'], 'unavailable')
        self.raw.account = 8; self.assertEqual(self.dispatch(self.ops[0], {'draft_id': did})['status'], 'unavailable'); self.raw.account = 7
        expires = registry.peek(did, owner=owner).expires_at
        with patch.object(registry, '_clock', return_value=expires - .001): self.assertEqual(self.classifier(did), CAPS)
        with patch.object(registry, '_clock', return_value=expires):
            with self.assertRaises(DraftError): self.classifier(did)
            self.assertEqual(self.send(did)['status'], 'expired')
        with patch.object(registry, '_attempt_clock', return_value=100):
            did = self.prepare()['draft']['draft_id']; self.assertEqual(self.send(did)['status'], 'sent')
        with patch.object(registry, '_clock', return_value=10**10), patch.object(registry, '_attempt_clock', return_value=999.999):
            self.assertEqual(self.classifier(did), CAPS)
        for provider, epoch in ((object(), self.client.send_observation_epoch), (self.client, object())):
            with self.assertRaises(DraftError): self.classifier(did, provider=provider, provider_epoch=epoch)
        epoch = self.client.send_observation_epoch; self.client._send_observation_epoch = object()
        self.assertEqual(self.send(did)['status'], 'expired'); self.client._send_observation_epoch = epoch
        self.broker._client = type(self.client)(raw=type(self.raw)())
        self.assertEqual(self.send(did)['status'], 'expired'); self.broker._client = self.client
        with patch.object(registry, '_attempt_clock', return_value=1000):
            with self.assertRaises(DraftError): self.classifier(did)
            self.assertEqual(self.send(did)['status'], 'expired')
        self.assertEqual((len(self.sends()), len(self.approvals)), (1, 1))

    def test_revision_and_style_lexical_refresh_check_old_and_new_capabilities(self):
        preview = self.prepare(); did = preview['draft']['draft_id']
        revision = self.dispatch(self.ops[1], dict(draft_id=did, **self.extra()))['reply']
        self.assertEqual(revision['reply_target'], preview['reply_target'])
        self.assertNotEqual(revision['preview_sha256'], preview['preview_sha256'])
        self.assertEqual(self.send(did)['status'], 'expired'); did = revision['draft']['draft_id']
        style = lexical(TEXT, [entity('Bold', 0, 16)])
        for missing in CAPS:
            self.raw.source = style
            self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c != missing))
            self.assertEqual(self.dispatch(self.ops[2], {'draft_id': did})['status'], 'unavailable')
        self.broker._policy = POLICY
        styled = self.dispatch(self.ops[2], {'draft_id': did})['reply']
        self.assertNotEqual(styled['reply_target']['source_sha256'], preview['reply_target']['source_sha256'])
        self.assertEqual(self.send(did)['status'], 'expired'); did = styled['draft']['draft_id']
        self.raw.source = lexical()
        for missing in CAPS:
            self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c != missing))
            self.assertEqual(self.dispatch(self.ops[2], {'draft_id': did})['status'], 'unavailable')
        self.broker._policy = POLICY
        refreshed = self.dispatch(self.ops[2], {'draft_id': did})['reply']
        self.assertEqual(refreshed['reply_target']['text'], DISPLAY)
        self.assertNotEqual(refreshed['preview_sha256'], styled['preview_sha256'])
        self.assertEqual(self.send(did)['status'], 'expired')
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_lexical_and_eligibility_drift_during_owner_decision_refuse_transport(self):
        for change in ('text', 'range', 'kind', 'metadata', 'shell', 'properties', 'account', 'title', 'provider', 'epoch'):
            self.restore(); epoch = self.client.send_observation_epoch; did = self.prepare()['draft']['draft_id']
            def approve(**kw):
                self.approvals.append(kw)
                if change == 'text': self.raw.source['content']['text']['text'] = '😀 #Tag $USD /go'
                elif change == 'range': self.raw.source['content']['text']['entities'][0]['length'] = 3
                elif change == 'kind': self.raw.source['content']['text']['entities'][0]['type']['@type'] = 'textEntityTypeCashtag'
                elif change == 'metadata': self.raw.source['content']['text']['entities'][0]['type']['url'] = 'x'
                elif change == 'shell': self.raw.source['edit_date'] = 1
                elif change == 'properties': self.raw.can_reply = False
                elif change == 'account': self.raw.account = 8
                elif change == 'title': self.raw.title = 'Changed'
                elif change == 'provider': self.broker._client = type(self.client)(raw=type(self.raw)())
                else: self.client._send_observation_epoch = object()
                return True
            self.broker._approval_prompt = approve
            with self.subTest(change=change):
                self.assertNotEqual(self.send(did)['status'], 'sent')
                self.assertIsNone(self.client._send_observations.snapshot(did))
            self.client._send_observation_epoch = epoch
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 10))

    def test_fresh_provider_span_kind_text_drift_refuses_without_observation(self):
        for field in ('text', 'length', 'kind'):
            self.restore(); did = self.prepare()['draft']['draft_id']; original = self.raw.send
            def raw_send(request):
                if request['@type'] == 'getMessage':
                    fmt = self.raw.source['content']['text']
                    if field == 'text': fmt['text'] = '😀 #Tag $USD /go'
                    elif field == 'length': fmt['entities'][0]['length'] = 3
                    else: fmt['entities'][0]['type']['@type'] = 'textEntityTypeCashtag'
                return original(request)
            with patch.object(self.raw, 'send', side_effect=raw_send): self.assertEqual(self.send(did)['status'], 'failed')
            self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_failed')
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 3))

    def test_harmless_entity_order_and_volatile_message_drift_allow_one_exact_send(self):
        did = self.prepare()['draft']['draft_id']
        def approve(**kw):
            self.approvals.append(kw); self.raw.source['content']['text']['entities'].reverse()
            self.raw.source.update(views=999, unread_mention=True)
            return True
        self.broker._approval_prompt = approve
        self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual((len(self.sends()), len(self.approvals)), (1, 1))

    def test_send_path_and_each_source_cap_revoked_after_owner_prevent_dispatch(self):
        path = 'reply_artifact_send' if self.artifact_path else 'reply_text_send'
        for missing in ('send', path, *CAPS):
            self.restore(); did = self.prepare()['draft']['draft_id']
            def approve(**kw):
                self.approvals.append(kw)
                self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c != missing))
                return True
            self.broker._approval_prompt = approve
            self.assertNotEqual(self.send(did)['status'], 'sent')
            self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 4))

    def test_final_guard_after_properties_rechecks_each_source_cap_and_epoch(self):
        for change in (*CAPS, 'epoch'):
            self.restore(); epoch = self.client.send_observation_epoch; did = self.prepare()['draft']['draft_id']
            original = self.raw.send
            def raw_send(request):
                original(request)
                if request['@type'] == 'getMessageProperties':
                    if change == 'epoch': self.client._send_observation_epoch = object()
                    else: self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c != change))
            with patch.object(self.raw, 'send', side_effect=raw_send): self.assertEqual(self.send(did)['status'], 'failed')
            self.broker._policy = POLICY; self.client._send_observation_epoch = epoch
            self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_failed')
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 3))

    def test_lexical_media_requires_media_authority_beside_formatting_and_lexical(self):
        op, payload = self.preparation()
        self.broker._policy = POLICY
        for media in ('document', 'photo', 'voice_note'):
            for kind in KINDS:
                self.raw.source = target(content=content_fixture(media, '#x'))
                self.raw.source['content']['caption']['entities'] = [entity(kind)]
                self.assertEqual(self.dispatch(op, payload)['status'], 'unavailable')
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_register_before_wire_each_cap_policy_exception_epoch_revoke_discards_observation(self):
        path = 'reply_artifact_send' if self.artifact_path else 'reply_text_send'
        for mode in ('send', path, *CAPS, 'raise', 'epoch'):
            self.restore(); epoch = self.client.send_observation_epoch; did = self.prepare()['draft']['draft_id']
            original = self.client._send_observations.register
            def register(*args, **kw):
                original(*args, **kw)
                if mode == 'raise': self.broker._policy = RuntimePolicy(enabled_capabilities=POLICY.enabled_capabilities, source_path=self.root / 'missing.toml')
                elif mode == 'epoch': self.client._send_observation_epoch = object()
                else: self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c != mode))
            with patch.object(self.client._send_observations, 'register', side_effect=register): self.assertEqual(self.send(did)['status'], 'failed')
            self.broker._policy = POLICY; self.client._send_observation_epoch = epoch
            self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_failed')
            self.assertEqual(self.send(did)['status'], 'failed')
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 6))

    def test_unknown_and_late_exact_reconciliation_keeps_one_observation_without_resend(self):
        self.raw.mode = 'caption' if self.artifact_path else 'wrong'; did = self.prepare()['draft']['draft_id']
        self.assertEqual(self.send(did)['status'], 'outcome_unknown')
        self.assertIsNotNone(self.client._send_observations.snapshot(did))
        self.assertEqual(self.send(did)['status'], 'outcome_unknown')
        final = outgoing(701, sending_state=None, **({'content': content_fixture('document')} if self.artifact_path else {}))
        self.client._reduce_receive_event({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': final})
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['status'], 'sent')
        self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual((len(self.sends()), len(self.approvals)), (1, 1))

    def test_transport_loss_retains_unknown_and_no_resend(self):
        self.raw.mode = 'lost'; did = self.prepare()['draft']['draft_id']
        self.assertEqual(self.send(did)['status'], 'outcome_unknown')
        self.assertIsNotNone(self.client._send_observations.snapshot(did))
        self.assertEqual(self.send(did)['status'], 'outcome_unknown')
        self.assertEqual((len(self.sends()), len(self.approvals)), (1, 1))


class LexicalTextTests(LexicalLifecycleRisks, ReplyFixture, unittest.TestCase):
    artifact_path = False


class LexicalArtifactTests(LexicalLifecycleRisks, ArtifactSendFixture, unittest.TestCase):
    artifact_path = True
