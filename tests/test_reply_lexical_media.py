"""V6 lexical captions: real source, Broker, registry and TDJSON boundaries.

Each test names a break in evidence or authority. Provider I/O and owner decisions
are synthetic; expected shells/spans are independently specified literal facts.
The real registration method is observed, never replaced, for its final guard.
"""
import copy
import hashlib
import itertools
import json
import sys
import time
import unittest

from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError
from telegram_search_mcp.outgoing_drafts import DraftError, DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from telegram_search_mcp.reply_media_sources import media_content, render_content, validate_content
from test_text_replies import ReplyFixture, target, outgoing, CLIENT, FOREIGN, ANCHOR
from test_artifact_replies import ArtifactSendFixture, content_fixture

MARKER = '[untrusted Telegram evidence] '
CAPS = ('reply_media_targets', 'reply_formatted_targets', 'reply_lexical_targets')
BASE = ('send', 'reply_text_send', 'reply_artifact_send')
POLICY = RuntimePolicy(enabled_capabilities=BASE + CAPS)
TEXT = '😀 #tag $USD /go'
KINDS = ('document', 'photo', 'voice_note')
LABELS = (('Hashtag', 'hashtag'), ('Cashtag', 'cashtag'), ('BotCommand', 'bot_command'))
STYLES = (('Bold', 'bold'), ('Italic', 'italic'), ('Underline', 'underline'),
          ('Strikethrough', 'strikethrough'), ('Spoiler', 'spoiler'), ('Code', 'code'),
          ('Pre', 'pre'), ('PreCode', 'pre_code'), ('BlockQuote', 'block_quote'),
          ('ExpandableBlockQuote', 'expandable_block_quote'))
IDENTITY = 'c2720445a45267813688ff73fa188aa060c1b661aefaf1650d42f690697b5ab3'
PREFIX = '{"anchor":{"chat_id":123,"message_id":55},"message":{"@type":"message","auto_delete_in":0.0,"chat_id":123,"content":'
SUFFIX = ',"date":1700000000,"edit_date":0,"ephemeral_content":null,"ephemeral_message_id":0,"forward_info":null,"id":55,"import_info":null,"is_from_offline":false,"is_outgoing":false,"receiver_id":null,"reply_markup":null,"reply_to":null,"scheduling_state":null,"self_destruct_in":0.0,"self_destruct_type":null,"sender_id":{"@type":"messageSenderUser","user_id":8},"sending_state":null,"topic_id":null},"version":'
FACTS = {
    'document': {'file_name': 'evidence.txt', 'mime_type': 'text/plain', 'size': 10, 'unique_id_sha256': IDENTITY},
    'photo': {'variants': [{'type': 'i', 'width': 2, 'height': 2, 'size': 10, 'unique_id_sha256': IDENTITY}]},
    'voice_note': {'duration': 2, 'mime_type': 'audio/ogg', 'size': 10, 'unique_id_sha256': IDENTITY},
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def entity(kind, offset=0, length=2, **metadata):
    return {'@type': 'textEntity', 'offset': offset, 'length': length,
            'type': {'@type': 'textEntityType' + kind, **metadata}}


SPANS = [entity('Hashtag', 3, 4), entity('Cashtag', 8, 4), entity('BotCommand', 13, 3)]
RECORDS = [{'type': 'hashtag', 'offset': 3, 'length': 4, 'text': '#tag'},
           {'type': 'cashtag', 'offset': 8, 'length': 4, 'text': '$USD'},
           {'type': 'bot_command', 'offset': 13, 'length': 3, 'text': '/go'}]


def caption_source(kind='document', text=TEXT, spans=None):
    raw = target(content=content_fixture(kind, text))
    raw['content']['caption']['entities'] = copy.deepcopy(SPANS if spans is None else spans)
    return raw


def source_bytes(kind='document', text=TEXT, spans=None, version=6):
    # The complete expected shell is literal, independent of projection code.
    content = {'kind': kind, 'caption': {'@type': 'formattedText', 'text': text,
        'entities': copy.deepcopy(SPANS if spans is None else spans)}, 'media': FACTS[kind]}
    return PREFIX + encoded(content) + SUFFIX + str(version) + '}'


def evidence(kind='document', text=TEXT, records=None, facts=None):
    return MARKER + 'Media target: ' + encoded({'kind': kind,
        'caption': {'text': text, 'offset_basis': 'original source UTF-16 code units',
                    'entities': RECORDS if records is None else records},
        'media': FACTS[kind] if facts is None else facts})


def main_file(raw):
    content = raw['content']
    if content['@type'] == 'messageDocument': return content['document']['document']
    if content['@type'] == 'messagePhoto': return content['photo']['sizes'][0]['photo']
    return content['voice_note']['voice']


class LexicalMediaSourceTests(unittest.TestCase):
    def accepted(self, raw):
        try: return source_from_message(raw, 123, 55)
        except ValueError: self.fail('approved lexical media must preserve complete v6 evidence')

    def rejected(self, raw):
        with self.assertRaises(ValueError): source_from_message(raw, 123, 55)

    def test_all_three_kinds_and_labels_have_literal_v6_spans_identity_and_authority(self):
        # Dropping media/labels, scalar offsets, or underreporting authority fails.
        for kind in KINDS:
            source = self.accepted(caption_source(kind))
            want = source_bytes(kind)
            self.assertEqual(source.projection_json, want)
            self.assertEqual(source.source_sha256, hashlib.sha256(want.encode()).hexdigest())
            self.assertEqual(source.target().text, evidence(kind))
            self.assertEqual(source.required_capabilities, CAPS)
            self.assertTrue(source.is_media)
            self.assertFalse(source.target().sanitized)
            self.assertFalse(source.target().truncated)
            self.assertEqual(ReplySource(want).target().text, evidence(kind))
            with self.assertRaises(ValueError): _ = source.required_capability
            for typ, label in LABELS:
                single = self.accepted(caption_source(kind, 'abc', [entity(typ, 0, 3)]))
                self.assertEqual(single.target().text, evidence(kind, 'abc',
                    [{'type': label, 'offset': 0, 'length': 3, 'text': 'abc'}]))

    def test_all_ten_optional_styles_use_existing_overlap_rules_and_language_evidence(self):
        for kind in KINDS:
            for typ, label in STYLES:
                metadata = {'language': 'python'} if typ == 'PreCode' else {}
                spans = [entity('Hashtag'), entity(typ, 3, 3, **metadata)]
                record = {'type': label, 'offset': 3, 'length': 3, 'text': 'abc', **metadata}
                source = self.accepted(caption_source(kind, '#x abc', spans))
                self.assertEqual(source.target().text, evidence(kind, '#x abc',
                    [{'type': 'hashtag', 'offset': 0, 'length': 2, 'text': '#x'}, record]))
                self.assertEqual(source.required_capabilities, CAPS)
        for typ, label in STYLES[:5]:
            for spans in ([entity(typ, 0, 4), entity('Hashtag', 1, 2)],
                          [entity('Hashtag', 0, 4), entity(typ, 1, 2)],
                          [entity('Hashtag', 0, 4), entity(typ, 0, 4)],
                          [entity('Hashtag'), entity(typ, 2, 2)]):
                self.assertEqual(self.accepted(caption_source(text='#abc', spans=spans)).required_capabilities, CAPS)

    def test_nonbmp_original_coordinates_and_complete_sanitized_spans_are_bound(self):
        raw = caption_source(text='ﬃ 😀 #ＴＡＧ', spans=[entity('Bold', 0, 9), entity('Hashtag', 5, 4)])
        source = self.accepted(raw)
        self.assertEqual(source.target().text, evidence(text='ffi 😀 #TAG', records=[
            {'type': 'bold', 'offset': 0, 'length': 9, 'text': 'ffi 😀 #TAG'},
            {'type': 'hashtag', 'offset': 5, 'length': 4, 'text': '#TAG'}]))
        self.assertTrue(source.target().sanitized)
        self.assertEqual(source.projection_json, source_bytes(text='ﬃ 😀 #ＴＡＧ', spans=raw['content']['caption']['entities']))
        normalized = self.accepted(caption_source(text='ffi 😀 #TAG', spans=[entity('Bold', 0, 11), entity('Hashtag', 7, 4)]))
        self.assertNotEqual(source.source_sha256, normalized.source_sha256)
        self.assertIn('"text":"😀","type":"hashtag"', self.accepted(caption_source(text='😀', spans=[entity('Hashtag')])).target().text)
        for offset, length in ((0, 1), (1, 1), (1, 2)):
            self.rejected(caption_source(text='😀x', spans=[entity('Hashtag', offset, length)]))

    def test_caption_span_language_and_media_string_sanitation_flags_without_truncation(self):
        raw = caption_source(text='#\n\tz', spans=[entity('Hashtag', 0, 4), entity('PreCode', 4, 1, language='x')])
        # Use disjoint language-bearing source and preserve original whitespace offsets.
        raw['content']['caption']['text'] = '#\n\tzx'
        raw['content']['caption']['entities'][1]['type']['language'] = 'ｐｙ'
        raw['content']['document']['file_name'] = 'Ｅ  v.txt'
        raw['content']['document']['mime_type'] = 'text/ｐlain'
        source = self.accepted(raw)
        facts = {**FACTS['document'], 'file_name': 'E v.txt', 'mime_type': 'text/plain'}
        self.assertEqual(source.target().text, evidence(text='# zx', records=[
            {'type': 'hashtag', 'offset': 0, 'length': 4, 'text': '# z'},
            {'type': 'pre_code', 'offset': 4, 'length': 1, 'text': 'x', 'language': 'py'}], facts=facts))
        self.assertTrue(source.target().sanitized)
        self.assertFalse(source.target().truncated)
        photo = caption_source('photo'); photo['content']['photo']['sizes'][0]['type'] = 'ｉ'
        source = self.accepted(photo)
        self.assertTrue(source.target().sanitized)
        self.assertIn('"type":"i","unique_id_sha256"', source.target().text)

    def test_default_media_parser_validator_and_renderer_keep_lexical_mode_closed(self):
        for kind in KINDS:
            raw = caption_source(kind)['content']
            with self.assertRaises(ValueError): media_content(raw)
            expected = json.loads(source_bytes(kind))['message']['content']
            with self.assertRaises(ValueError): validate_content(expected, styled=True)
            with self.assertRaises(ValueError): render_content(expected, MARKER, styled=True)
            self.assertEqual(media_content(raw, lexical=True), expected)
            validate_content(expected, lexical=True)
            self.assertEqual(render_content(expected, MARKER, lexical=True), (evidence(kind), False))

    def test_closed_caption_entity_metadata_integer_boundaries_and_unsupported_types_refuse(self):
        bads = []
        for typ, _ in LABELS:
            for metadata in ({'language': ''}, {'url': 'x'}, {'user_id': 8}, {'extra': None}):
                bads.append(caption_source(text='#x', spans=[entity(typ, **metadata)]))
        for field, values in (('offset', (True, False, -1, 2**31, '0', 0.0)),
                              ('length', (True, False, 0, -1, 2**31, '2', 2.0))):
            for value in values:
                span = entity('Hashtag'); span[field] = value
                bads.append(caption_source(text='#x', spans=[span]))
        for update in ({'extra': None}, {'@type': 'other'}, {'type': None}, {'type': {'@type': True}},
                       {'type': {'@type': 'textEntityTypeUrl'}},
                       {'type': {'@type': 'textEntityTypeCustomEmoji'}}, {'type': {'@type': 'textEntityTypeDateTime'}}):
            bads.append(caption_source(text='#x', spans=[{**entity('Hashtag'), **update}]))
        for level in ('content', 'caption'):
            raw = caption_source(); obj = raw['content'] if level == 'content' else raw['content']['caption']
            obj['extra'] = None; bads.append(raw)
        raw = caption_source(); raw['content']['caption']['entities'] = tuple(SPANS); bads.append(raw)
        for text in ('', ' ', '\n\t', '#\r', '#\x00', '#\u202e', '#\ud800'):
            bads.append(caption_source(text=text, spans=[entity('Hashtag', 0, 1)]))
        for raw in bads:
            with self.subTest(raw=repr(raw)[:90]): self.rejected(raw)

    def test_lexical_duplicate_crossing_code_quote_and_lexical_pairs_are_refused(self):
        cases = [[entity('Hashtag'), entity('Hashtag')], [entity('Hashtag', 0, 3), entity('Bold', 2, 2)]]
        for left, _ in LABELS:
            for right, _ in LABELS:
                cases.extend([[entity(left, 0, 4), entity(right, 1, 2)], [entity(left, 0, 4), entity(right, 0, 4)]])
        for typ in ('Code', 'Pre', 'PreCode', 'BlockQuote', 'ExpandableBlockQuote'):
            metadata = {'language': ''} if typ == 'PreCode' else {}
            cases.extend([[entity('Hashtag', 0, 4), entity(typ, 1, 2, **metadata)],
                          [entity(typ, 0, 4, **metadata), entity('Hashtag', 1, 2)]])
        for spans in cases: self.rejected(caption_source(text='#abc', spans=spans))
        self.assertEqual(self.accepted(caption_source(text='#x/y', spans=[entity('Hashtag'), entity('BotCommand', 2, 2)])).required_capabilities, CAPS)

    def test_original_1024_caption_and_32_entity_limits_precede_display_overflow(self):
        text = '#' + 'x' * 1023
        self.assertEqual(self.accepted(caption_source(text=text, spans=[entity('Hashtag', 0, 1)])).target().text,
            evidence(text=text, records=[{'type': 'hashtag', 'offset': 0, 'length': 1, 'text': '#'}]))
        self.rejected(caption_source(text=text + 'x', spans=[entity('Hashtag', 0, 1)]))
        spans = [entity('Hashtag', i, 1) for i in range(32)]
        records = [{'type': 'hashtag', 'offset': i, 'length': 1, 'text': '#'} for i in range(32)]
        self.assertEqual(self.accepted(caption_source(text='#' * 32, spans=spans)).target().text,
            evidence(text='#' * 32, records=records))
        self.rejected(caption_source(text='#' * 33, spans=spans + [entity('Hashtag', 32, 1)]))
        self.rejected(caption_source(text='ﷺ' * 250, spans=[entity('Hashtag', 0, 250)]))

    def test_complete_marker_inclusive_display_accepts_4096_and_refuses_4097(self):
        text = '#' + 'x' * 899
        spans = [entity('Bold', 0, 900), entity('Italic', 0, 900), entity('Hashtag', 0, 900)]
        records = [{'type': label, 'offset': 0, 'length': 900, 'text': text} for label in ('bold', 'hashtag', 'italic')]
        facts = {**FACTS['document'], 'file_name': 'n'}
        padding = 4096 - len(evidence(text=text, records=records, facts=facts))
        facts['file_name'] += 'n' * padding
        want = evidence(text=text, records=records, facts=facts)
        self.assertEqual(len(want), 4096)
        raw = caption_source(text=text, spans=spans); raw['content']['document']['file_name'] = facts['file_name']
        self.assertEqual(self.accepted(raw).target().text, want)
        raw['content']['document']['file_name'] += 'n'; self.rejected(raw)

    def test_v6_requires_lexical_caption_and_forgeries_cannot_cross_old_versions(self):
        good = source_bytes()
        cases = []
        for version in (1, 2, 3, 4, 5, True, 7):
            value = json.loads(good); value['version'] = version; cases.append(value)
        for spans in ([], [entity('Bold', 0, 16)], list(reversed(SPANS))):
            value = json.loads(good); value['message']['content']['caption']['entities'] = spans; cases.append(value)
        for field in ('message', 'anchor', 'content', 'media', 'caption'):
            value = json.loads(good)
            obj = value[field] if field in ('message', 'anchor') else value['message']['content'] if field == 'content' else value['message']['content'][field]
            obj['extra'] = None; cases.append(value)
        for value in cases:
            raw = encoded(value); forged = object.__new__(ReplySource); object.__setattr__(forged, 'projection_json', raw)
            with self.subTest(version=value['version']):
                with self.assertRaises(ValueError): ReplySource(raw)
                with self.assertRaises(ValueError): forged.target()
                with self.assertRaises(ValueError): _ = forged.required_capabilities
                with self.assertRaises(ValueError): _ = forged.required_capability
        self.assertEqual(ReplySource(good).required_capabilities, CAPS)
        text_content = {'@type': 'messageText', 'link_preview': None, 'link_preview_options': None,
            'text': {'@type': 'formattedText', 'text': TEXT, 'entities': SPANS}}
        with self.assertRaises(ValueError): ReplySource(PREFIX + encoded(text_content) + SUFFIX + '6}')

    def test_projection_byte_and_character_limits_refuse_before_json_decoding(self):
        # An observer fails if JSON decoding occurs at all; it does not replace it.
        decoder = json.loads.__code__
        def no_decode(frame, event, arg):
            if event == 'call' and frame.f_code is decoder: self.fail('oversized projection reached JSON decoding')
        for raw in ('x' * 65537, '😀' * 17000):
            forged = object.__new__(ReplySource); object.__setattr__(forged, 'projection_json', raw)
            prior = sys.getprofile(); sys.setprofile(no_decode)
            try:
                with self.assertRaises(ValueError): ReplySource(raw)
                with self.assertRaises(ValueError): forged.target()
                with self.assertRaises(ValueError): _ = forged.required_capabilities
            finally: sys.setprofile(prior)

    def test_stable_media_changes_bind_digest_but_typed_progress_and_order_do_not(self):
        for kind in KINDS:
            raw = caption_source(kind); source = self.accepted(raw); volatile = copy.deepcopy(raw)
            f = main_file(volatile)
            f.update(id=888, expected_size=99)
            f['local'].update(path='/never/read/source', downloaded_size=10, is_downloading_completed=True)
            f['remote'].update(id='different', uploaded_size=99, is_uploading_active=True)
            volatile.update(views=99, unread_mention=True)
            volatile['content']['caption']['entities'].reverse()
            if kind == 'photo': volatile['content']['photo']['sizes'][0]['progressive_sizes'] = [1, 3]
            if kind == 'voice_note': volatile['content'].update(is_listened=True); volatile['content']['voice_note']['waveform'] = 'AAAB'
            self.assertEqual(self.accepted(volatile).projection_json, source.projection_json)
            for change in ('identity', 'size', 'caption', 'range', 'label', 'shell', 'metadata'):
                changed = copy.deepcopy(raw)
                if change == 'identity': main_file(changed)['remote']['unique_id'] = 'replacement'
                elif change == 'size': main_file(changed)['size'] = 11
                elif change == 'caption': changed['content']['caption']['text'] = '😀 #Tag $USD /go'
                elif change == 'range': changed['content']['caption']['entities'][0]['length'] = 3
                elif change == 'label': changed['content']['caption']['entities'][0]['type']['@type'] = 'textEntityTypeCashtag'
                elif change == 'shell': changed['edit_date'] = 1
                elif kind == 'document': changed['content']['document']['file_name'] = 'other.txt'
                elif kind == 'photo': changed['content']['photo']['sizes'][0]['width'] = 3
                else: changed['content']['voice_note']['duration'] = 3
                self.assertNotEqual(self.accepted(changed).source_sha256, source.source_sha256)
            raw['content']['caption']['text'] = 'mutated caller'
            self.assertEqual(source.target().text, evidence(kind))

    def test_media_identity_and_closed_kind_limits_remain_in_force(self):
        for kind in KINDS:
            for change in ('unknown', 'unknown_inner', 'no_identity', 'zero_size', 'file_shape', 'unsafe'):
                raw = caption_source(kind)
                if change == 'unknown': raw['content']['extra'] = None
                elif change == 'unknown_inner': raw['content'][{'document': 'document', 'photo': 'photo', 'voice_note': 'voice_note'}[kind]]['extra'] = None
                elif change == 'no_identity': main_file(raw)['remote']['unique_id'] = ''
                elif change == 'zero_size': main_file(raw)['size'] = 0
                elif change == 'file_shape': main_file(raw)['local']['extra'] = None
                elif kind == 'document': raw['content']['document']['mime_type'] = '\r'
                elif kind == 'photo': raw['content']['has_spoiler'] = True
                else: raw['content']['voice_note']['mime_type'] = 'audio/mp3'
                self.rejected(raw)
        for change in ('video', 'secret', 'stickers', 'duplicate', 'too_many'):
            raw = caption_source('photo'); item = raw['content']['photo']['sizes'][0]
            if change == 'video': raw['content']['video'] = {}
            elif change == 'secret': raw['content']['is_secret'] = True
            elif change == 'stickers': raw['content']['photo']['has_stickers'] = True
            else: raw['content']['photo']['sizes'] = [item] * (21 if change == 'too_many' else 2)
            self.rejected(raw)
        for field, value in (('duration', 601), ('duration', True), ('waveform', '!!'), ('speech_recognition_result', {})):
            raw = caption_source('voice_note'); raw['content']['voice_note'][field] = value; self.rejected(raw)
        raw = caption_source('photo'); raw['content']['@type'] = 'messageVideo'; self.rejected(raw)

    def test_topics_replies_forward_import_markup_link_shells_are_still_refused(self):
        for kind in KINDS:
            for field in ('topic_id', 'reply_to', 'forward_info', 'import_info', 'reply_markup', 'sending_state',
                          'scheduling_state', 'self_destruct_type', 'ephemeral_content', 'receiver_id'):
                raw = caption_source(kind); raw[field] = {}; self.rejected(raw)
            raw = caption_source(kind); raw['content']['link_preview'] = None; self.rejected(raw)
            for field, value in (('self_destruct_in', True), ('auto_delete_in', 1), ('is_outgoing', 1),
                                 ('is_from_offline', 0), ('ephemeral_message_id', 1), ('date', True), ('edit_date', -1)):
                raw = caption_source(kind); raw[field] = value; self.rejected(raw)

    def test_v1_to_v5_literal_bytes_displays_and_authority_stay_unchanged(self):
        plain = '{"@type":"messageText","link_preview":null,"link_preview_options":null,"text":{"@type":"formattedText","entities":[],"text":"A😀BC"}}'
        styled = '{"@type":"messageText","link_preview":null,"link_preview_options":null,"text":{"@type":"formattedText","entities":[{"@type":"textEntity","length":5,"offset":0,"type":{"@type":"textEntityTypeBold"}}],"text":"A😀BC"}}'
        lex = '{"@type":"messageText","link_preview":null,"link_preview_options":null,"text":{"@type":"formattedText","entities":[{"@type":"textEntity","length":2,"offset":0,"type":{"@type":"textEntityTypeHashtag"}}],"text":"#x"}}'
        facts = '{"file_name":"evidence.txt","mime_type":"text/plain","size":10,"unique_id_sha256":"' + IDENTITY + '"}'
        v2 = '{"caption":{"@type":"formattedText","entities":[],"text":"A😀BC"},"kind":"document","media":' + facts + '}'
        v4 = '{"caption":{"@type":"formattedText","entities":[{"@type":"textEntity","length":5,"offset":0,"type":{"@type":"textEntityTypeBold"}}],"text":"A😀BC"},"kind":"document","media":' + facts + '}'
        style_record = '{"entities":[{"length":5,"offset":0,"text":"A😀BC","type":"bold"}],"offset_basis":"original source UTF-16 code units","text":"A😀BC"}'
        a = target('A😀BC'); b = target('A😀BC'); b['content']['text']['entities'] = [entity('Bold', 0, 5)]
        c = caption_source(text='A😀BC', spans=[]); d = caption_source(text='A😀BC', spans=[entity('Bold', 0, 5)])
        e = target('#x'); e['content']['text']['entities'] = [entity('Hashtag')]
        for version, raw, content, display, caps in (
            (1, a, plain, MARKER + 'A😀BC', ()),
            (2, c, v2, MARKER + 'Media target: {"caption":"A😀BC","kind":"document","media":' + facts + '}', CAPS[:1]),
            (3, b, styled, MARKER + 'Formatted target: ' + style_record, CAPS[1:2]),
            (4, d, v4, MARKER + 'Media target: {"caption":' + style_record + ',"kind":"document","media":' + facts + '}', CAPS[:2]),
            (5, e, lex, MARKER + 'Formatted target: {"entities":[{"length":2,"offset":0,"text":"#x","type":"hashtag"}],"offset_basis":"original source UTF-16 code units","text":"#x"}', CAPS[1:])):
            with self.subTest(version=version):
                source = source_from_message(raw, 123, 55); want = PREFIX + content + SUFFIX + str(version) + '}'
                self.assertEqual(source.projection_json, want)
                self.assertEqual(source.source_sha256, hashlib.sha256(want.encode()).hexdigest())
                self.assertEqual(source.target().text, display)
                self.assertEqual(source.required_capabilities, caps)
                self.assertEqual(source.is_media, version in (2, 4))
        self.assertEqual(self.accepted(caption_source(text='', spans=[])).target().text,
            MARKER + 'Media target: {"caption":"","kind":"document","media":' + facts + '}')


class LexicalMediaLifecycleRisks:
    """New v6 lifecycle methods only; fixtures contain no historical test methods."""
    def setUp(self):
        super().setUp()
        self.broker._policy = POLICY
        self.raw.source = caption_source()
        self.wall = time.time(); self.monotonic = 100.0
        self.broker._drafts = OutgoingDraftRegistry(self.broker._artifact_store,
            clock=lambda: self.wall, attempt_clock=lambda: self.monotonic)

    @property
    def path(self): return 'reply_artifact_send' if self.artifact_path else 'reply_text_send'

    @property
    def ops(self):
        return ('get_reply_artifact_draft', 'update_reply_artifact_draft', 'refresh_reply_artifact_draft') if self.artifact_path else ('get_reply_draft', 'update_reply_draft', 'refresh_reply_draft')

    def extra(self): return {'caption': 'new'} if self.artifact_path else {'text': 'new'}

    def preparation(self):
        return ('prepare_reply_artifact_send', dict(recipient=123, reply_to=ANCHOR, artifact_id=self.artifact.artifact_id,
            display_name='evidence.txt', mime_type='text/plain', caption='Caption', kind='document')) if self.artifact_path else ('prepare_reply_text_send', dict(recipient=123, reply_to=ANCHOR, text='Final reply'))

    def classifier(self, did, **kw):
        return self.broker._drafts.source_required_capabilities(did, owner=kw.pop('owner', DraftOwner(CLIENT, 7)), **kw)

    def deny(self, *missing):
        self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c not in missing))

    def restore(self, kind='document'):
        self.broker._policy = POLICY; self.broker._client = self.client
        self.raw.source = caption_source(kind); self.raw.account = 7; self.raw.title = 'Recipient'; self.raw.can_reply = True; self.raw.mode = 'sent'
        self.broker._approval_prompt = lambda **kw: self.approvals.append(kw) or True

    def test_all_source_kinds_reach_full_owner_evidence_and_one_plain_same_chat_wire(self):
        for kind in KINDS:
            self.restore(kind); preview = self.prepare(); did = preview['draft']['draft_id']
            self.assertEqual(preview['reply_target']['text'], evidence(kind))
            self.assertEqual(preview['reply_target']['source_sha256'], hashlib.sha256(source_bytes(kind).encode()).hexdigest())
            self.assertEqual(self.classifier(did), CAPS)
            self.assertTrue(self.broker._drafts.is_media_reply(did, owner=DraftOwner(CLIENT, 7)))
            with self.assertRaises(DraftError): self.broker._drafts.source_required_capability(did, owner=DraftOwner(CLIENT, 7))
            self.assertEqual(self.send(did)['status'], 'sent')
            self.assertEqual(self.approvals[-1]['reply_artifact_preview' if self.artifact_path else 'reply_preview'], preview)
            wire = self.sends()[-1]
            self.assertEqual(wire['input_message_content']['caption' if self.artifact_path else 'text']['entities'], [])
            self.assertEqual(wire['reply_to'], {'@type': 'inputMessageReplyToMessage', 'message_id': 55,
                'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''})
            self.assertIsNone(wire['topic_id']); self.assertIsNone(wire['reply_markup'])
            self.assertIsNotNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual((len(self.sends()), len(self.approvals)), (3, 3))

    def test_all_seven_incomplete_triples_refuse_preparation_inspection_revision_refresh_and_owner(self):
        op, payload = self.preparation()
        for count in range(3):
            for enabled in itertools.combinations(CAPS, count):
                missing = tuple(c for c in CAPS if c not in enabled)
                self.restore(); self.deny(*missing)
                self.assertEqual(self.dispatch(op, payload)['status'], 'unavailable')
                self.restore(); did = self.prepare()['draft']['draft_id']; self.deny(*missing)
                for operation, extra in ((self.ops[0], {}), (self.ops[1], self.extra()), (self.ops[2], {})):
                    self.assertEqual(self.dispatch(operation, dict(draft_id=did, **extra))['status'], 'unavailable')
                self.assertEqual(self.send(did)['status'], 'expired')
                self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_send_and_selected_path_revocation_refuse_before_owner_and_after_owner(self):
        for phase in ('before', 'owner'):
            for missing in ('send', self.path, *CAPS):
                self.restore(); did = self.prepare()['draft']['draft_id']
                if phase == 'before': self.deny(missing)
                else:
                    def approve(**kw): self.approvals.append(kw); self.deny(missing); return True
                    self.broker._approval_prompt = approve
                if phase == 'before' and missing == 'send':
                    with self.assertRaises(CompatibilityError): self.send(did)
                else: self.assertNotEqual(self.send(did)['status'], 'sent')
                self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 5))

    def test_after_provider_properties_each_required_cap_and_epoch_refuse_before_registration(self):
        original = self.raw.send
        for change in ('send', self.path, *CAPS, 'epoch'):
            self.restore(); epoch = self.client.send_observation_epoch; did = self.prepare()['draft']['draft_id']
            def raw_send(request):
                original(request)
                if request['@type'] == 'getMessageProperties':
                    if change == 'epoch': self.client._send_observation_epoch = object()
                    else: self.deny(change)
            self.raw.send = raw_send
            try: self.assertEqual(self.send(did)['status'], 'failed')
            finally: self.raw.send = original; self.client._send_observation_epoch = epoch; self.broker._policy = POLICY
            self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_failed')
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 6))

    def test_real_registration_then_each_cap_policy_error_and_epoch_revoke_discards_unattempted(self):
        registration = type(self.client._send_observations).register.__code__
        for change in ('send', self.path, *CAPS, 'policy_error', 'epoch'):
            self.restore(); epoch = self.client.send_observation_epoch; did = self.prepare()['draft']['draft_id']; observed = []
            def after_registration(frame, event, arg):
                if event == 'return' and frame.f_code is registration:
                    observed.append(self.client._send_observations.snapshot(did))
                    if change == 'epoch': self.client._send_observation_epoch = object()
                    elif change == 'policy_error': self.broker._policy = RuntimePolicy(enabled_capabilities=POLICY.enabled_capabilities, source_path=self.root / 'missing.toml')
                    else: self.deny(change)
            prior = sys.getprofile(); sys.setprofile(after_registration)
            try: self.assertEqual(self.send(did)['status'], 'failed')
            finally: sys.setprofile(prior); self.broker._policy = POLICY; self.client._send_observation_epoch = epoch
            self.assertEqual(len(observed), 1)
            self.assertIsNotNone(observed[0])
            self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_failed')
            self.assertEqual(self.send(did)['status'], 'failed')
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 7))

    def test_revision_preserves_v6_and_refresh_checks_authority_of_old_and_new_sources(self):
        preview = self.prepare(); did = preview['draft']['draft_id']
        revision = self.dispatch(self.ops[1], dict(draft_id=did, **self.extra()))['reply']
        self.assertEqual(revision['reply_target'], preview['reply_target'])
        self.assertNotEqual(revision['preview_sha256'], preview['preview_sha256'])
        self.assertEqual(self.send(did)['status'], 'expired'); did = revision['draft']['draft_id']
        for old_kind, next_source in (('v6', caption_source(text='plain', spans=[])),
                                      ('v2', target('plain')), ('v1', caption_source())):
            self.raw.source = next_source
            for missing in CAPS:
                # v6 -> v2 needs old triple; v1 -> v6 needs new triple.
                if old_kind == 'v2' and missing != 'reply_media_targets': continue
                self.deny(missing)
                self.assertEqual(self.dispatch(self.ops[2], {'draft_id': did})['status'], 'unavailable')
                self.broker._policy = POLICY
            refreshed = self.dispatch(self.ops[2], {'draft_id': did})['reply']
            self.assertEqual(self.send(did)['status'], 'expired')
            did = refreshed['draft']['draft_id']
        self.assertEqual(refreshed['reply_target']['text'], evidence())
        self.assertNotEqual(refreshed['preview_sha256'], preview['preview_sha256'])
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))

    def test_all_terminal_states_require_each_cap_restoration_never_renews_attempt(self):
        for mode, want in (('sent', 'sent'), ('caption' if self.artifact_path else 'wrong', 'outcome_unknown'), ('properties', 'failed')):
            self.restore(); self.raw.mode = 'sent' if mode == 'properties' else mode; self.raw.can_reply = mode != 'properties'
            did = self.prepare()['draft']['draft_id']; before = len(self.sends())
            self.assertEqual(self.send(did)['status'], want)
            self.assertEqual(len(self.sends()) - before, 0 if mode == 'properties' else 1)
            count = len(self.sends()); decisions = len(self.approvals)
            for missing in CAPS:
                self.deny(missing); self.assertEqual(self.send(did)['status'], 'expired')
                self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['status'], want)
            self.broker._policy = POLICY
            self.assertEqual(self.send(did)['status'], want)
            self.assertEqual((len(self.sends()), len(self.approvals)), (count, decisions))

    def test_owner_account_pending_wall_expiry_and_terminal_provider_epoch_retention(self):
        registry = self.broker._drafts; owner = DraftOwner(CLIENT, 7); did = self.prepare()['draft']['draft_id']
        for wrong in (DraftOwner(FOREIGN, 7), DraftOwner(CLIENT, 8)):
            with self.assertRaises(DraftError): self.classifier(did, owner=wrong)
        for operation, extra in ((self.ops[0], {}), (self.ops[1], self.extra()), (self.ops[2], {})):
            self.assertEqual(self.dispatch(operation, dict(draft_id=did, **extra), FOREIGN)['status'], 'unavailable')
        self.raw.account = 8; self.assertEqual(self.dispatch(self.ops[0], {'draft_id': did})['status'], 'unavailable'); self.raw.account = 7
        expires = registry.peek(did, owner=owner).expires_at
        self.wall = expires - .001; self.assertEqual(self.classifier(did), CAPS)
        self.wall = expires
        with self.assertRaises(DraftError): self.classifier(did)
        self.assertEqual(self.send(did)['status'], 'expired')
        self.wall = time.time(); self.monotonic = 100.0
        did = self.prepare()['draft']['draft_id']; self.assertEqual(self.send(did)['status'], 'sent')
        self.wall += 10**7; self.monotonic = 999.999; self.assertEqual(self.classifier(did), CAPS)
        for provider, epoch in ((object(), self.client.send_observation_epoch), (self.client, object())):
            with self.assertRaises(DraftError): self.classifier(did, provider=provider, provider_epoch=epoch)
        epoch = self.client.send_observation_epoch; self.client._send_observation_epoch = object()
        self.assertEqual(self.send(did)['status'], 'expired'); self.client._send_observation_epoch = epoch
        self.broker._client = type(self.client)(raw=type(self.raw)())
        self.assertEqual(self.send(did)['status'], 'expired'); self.broker._client = self.client
        self.monotonic = 1000.0
        with self.assertRaises(DraftError): self.classifier(did)
        self.assertEqual(self.send(did)['status'], 'expired')
        self.assertEqual((len(self.sends()), len(self.approvals)), (1, 1))

    def test_caption_range_label_media_and_shell_drift_during_owner_prevent_transport(self):
        for kind in KINDS:
            for change in ('text', 'range', 'label', 'extra', 'identity', 'size', 'metadata', 'shell'):
                self.restore(kind); did = self.prepare()['draft']['draft_id']
                def approve(**kw):
                    self.approvals.append(kw); raw = self.raw.source
                    if change == 'text': raw['content']['caption']['text'] = '😀 #Tag $USD /go'
                    elif change == 'range': raw['content']['caption']['entities'][0]['length'] = 3
                    elif change == 'label': raw['content']['caption']['entities'][0]['type']['@type'] = 'textEntityTypeCashtag'
                    elif change == 'extra': raw['content']['caption']['entities'][0]['type']['url'] = 'x'
                    elif change == 'identity': main_file(raw)['remote']['unique_id'] = 'replacement'
                    elif change == 'size': main_file(raw)['size'] = 11
                    elif change == 'shell': raw['edit_date'] = 1
                    elif kind == 'document': raw['content']['document']['file_name'] = 'other.txt'
                    elif kind == 'photo': raw['content']['photo']['sizes'][0]['width'] = 3
                    else: raw['content']['voice_note']['duration'] = 3
                    return True
                self.broker._approval_prompt = approve
                self.assertEqual(self.send(did)['status'], 'failed')
                self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 24))

    def test_account_title_properties_provider_epoch_drift_during_owner_refuse(self):
        for change in ('account', 'title', 'properties', 'provider', 'epoch'):
            self.restore(); epoch = self.client.send_observation_epoch; did = self.prepare()['draft']['draft_id']
            def approve(**kw):
                self.approvals.append(kw)
                if change == 'account': self.raw.account = 8
                elif change == 'title': self.raw.title = 'Changed'
                elif change == 'properties': self.raw.can_reply = False
                elif change == 'provider': self.broker._client = type(self.client)(raw=type(self.raw)())
                else: self.client._send_observation_epoch = object()
                return True
            self.broker._approval_prompt = approve
            self.assertNotEqual(self.send(did)['status'], 'sent')
            self.assertIsNone(self.client._send_observations.snapshot(did))
            self.client._send_observation_epoch = epoch
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 5))

    def test_provider_fresh_lexical_drift_refuses_but_volatile_order_progress_changes_send(self):
        original = self.raw.send
        for change in ('caption', 'range', 'label'):
            self.restore(); did = self.prepare()['draft']['draft_id']
            def raw_send(request):
                if request['@type'] == 'getMessage':
                    fmt = self.raw.source['content']['caption']
                    if change == 'caption': fmt['text'] = '😀 #Tag $USD /go'
                    elif change == 'range': fmt['entities'][0]['length'] = 3
                    else: fmt['entities'][0]['type']['@type'] = 'textEntityTypeCashtag'
                original(request)
            self.raw.send = raw_send
            try: self.assertEqual(self.send(did)['status'], 'failed')
            finally: self.raw.send = original
            self.assertIsNone(self.client._send_observations.snapshot(did))
        for kind in KINDS:
            self.restore(kind); did = self.prepare()['draft']['draft_id']
            def approve(**kw):
                self.approvals.append(kw); self.raw.source['content']['caption']['entities'].reverse()
                f = main_file(self.raw.source); f['local'].update(path='/never/read', downloaded_size=10)
                f['remote']['uploaded_size'] = 11; self.raw.source.update(views=99)
                return True
            self.broker._approval_prompt = approve
            self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual((len(self.sends()), len(self.approvals)), (3, 6))

    def test_unknown_and_transport_loss_keep_observation_exact_late_recovery_never_resends(self):
        for mode in ('caption' if self.artifact_path else 'wrong', 'lost'):
            self.restore(); self.raw.mode = mode; did = self.prepare()['draft']['draft_id']
            self.assertEqual(self.send(did)['status'], 'outcome_unknown')
            self.assertIsNotNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.send(did)['status'], 'outcome_unknown')
            final = outgoing(701, sending_state=None, **({'content': content_fixture('document')} if self.artifact_path else {}))
            wrong = copy.deepcopy(final); wrong['reply_to']['message_id'] = 56
            self.client._reduce_receive_event({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': wrong})
            self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['status'], 'outcome_unknown')
            self.client._reduce_receive_event({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': final})
            self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['status'], 'sent')
            self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual((len(self.sends()), len(self.approvals)), (2, 2))

    def test_generic_draft_routes_cannot_expose_or_mutate_lexical_media_evidence(self):
        did = self.prepare()['draft']['draft_id']
        for operation, extra in (('get_draft', {}), ('update_draft', {'caption': 'x'} if self.artifact_path else {'text': 'x'}), ('refresh_draft', {})):
            self.assertEqual(self.dispatch(operation, dict(draft_id=did, **extra))['status'], 'unavailable')
        self.assertEqual(self.dispatch(self.ops[0], {'draft_id': did})['reply']['reply_target']['text'], evidence())
        self.assertEqual((len(self.sends()), len(self.approvals)), (0, 0))


class LexicalMediaTextTests(LexicalMediaLifecycleRisks, ReplyFixture, unittest.TestCase):
    artifact_path = False


class LexicalMediaArtifactTests(LexicalMediaLifecycleRisks, ArtifactSendFixture, unittest.TestCase):
    artifact_path = True
