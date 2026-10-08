"""Styled media captions keep complete evidence and conjunctive authority.

Synthetic provider/owner boundary only; parser, Broker, registry and observation
reducers are real. Literal records below are independent of production helpers.
"""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.outgoing_drafts import DraftError, DraftOwner
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from test_text_replies import ReplyFixture, target, outgoing, CLIENT, FOREIGN, ANCHOR
from test_artifact_replies import ArtifactSendFixture, content_fixture

MARKER = '[untrusted Telegram evidence] '
CAPS = ('reply_media_targets', 'reply_formatted_targets')
BASE = ('send', 'reply_text_send', 'reply_artifact_send')
POLICY = RuntimePolicy(enabled_capabilities=BASE + CAPS)
IDENTITY = 'c2720445a45267813688ff73fa188aa060c1b661aefaf1650d42f690697b5ab3'
STYLES = ('Bold','Italic','Underline','Strikethrough','Spoiler','Code','Pre','PreCode','BlockQuote','ExpandableBlockQuote')
LABELS = ('bold','italic','underline','strikethrough','spoiler','code','pre','pre_code','block_quote','expandable_block_quote')


def span(kind='Bold', offset=0, length=5, language='python'):
    typ={'@type':'textEntityType'+kind}
    if kind=='PreCode':typ['language']=language
    return {'@type':'textEntity','offset':offset,'length':length,'type':typ}


def raw_caption(kind='document', text='A😀BC', entities=None):
    content=content_fixture(kind,text)
    content['caption']['entities']=[span()] if entities is None else entities
    return target(content=content)


def serialize(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def facts(kind):
    return {'document':{'file_name':'evidence.txt','mime_type':'text/plain','size':10,'unique_id_sha256':IDENTITY},
            'photo':{'variants':[{'type':'i','width':2,'height':2,'size':10,'unique_id_sha256':IDENTITY}]},
            'voice_note':{'duration':2,'mime_type':'audio/ogg','size':10,'unique_id_sha256':IDENTITY}}[kind]


def projection(kind='document', text='A😀BC', entities=None, version=4):
    return {'version':version,'anchor':{'chat_id':123,'message_id':55},'message':{
        '@type':'message','chat_id':123,'id':55,'sender_id':{'@type':'messageSenderUser','user_id':8},
        'is_outgoing':False,'is_from_offline':False,'ephemeral_message_id':0,'date':1700000000,'edit_date':0,
        'self_destruct_in':0.0,'auto_delete_in':0.0,'sending_state':None,'scheduling_state':None,'topic_id':None,
        'self_destruct_type':None,'ephemeral_content':None,'receiver_id':None,'reply_to':None,'forward_info':None,
        'import_info':None,'reply_markup':None,'content':{'kind':kind,'caption':{'@type':'formattedText',
            'text':text,'entities':[span()] if entities is None else entities},'media':facts(kind)}}}


def display(kind='document', text='A😀BC', entities=None, media=None):
    return MARKER+'Media target: '+serialize({'kind':kind,'caption':{'text':text,
        'offset_basis':'original source UTF-16 code units','entities':[
            {'type':'bold','offset':0,'length':5,'text':'A😀BC'}] if entities is None else entities},
        'media':facts(kind) if media is None else media})


class CaptionSourceTests(unittest.TestCase):
    def accepted(self, raw):
        try:return source_from_message(raw,123,55)
        except ValueError:self.fail('valid styled media caption must be inspectable')

    def capabilities(self, source):
        self.assertTrue(hasattr(source,'required_capabilities'),'source must expose every required capability')
        return source.required_capabilities

    def test_every_media_kind_and_style_preserves_literal_caption_and_stable_facts(self):
        # Flattening styles, dropping media facts, or changing the coordinate basis is a bug.
        for kind in ('document','photo','voice_note'):
            for style,label in zip(STYLES,LABELS):
                with self.subTest(kind=kind,style=style):
                    entities=[span(style)];source=self.accepted(raw_caption(kind,entities=entities))
                    want=serialize(projection(kind,entities=entities))
                    record={'type':label,'offset':0,'length':5,'text':'A😀BC'}
                    if style=='PreCode':record['language']='python'
                    self.assertEqual(source.projection_json,want)
                    self.assertEqual(source.source_sha256,hashlib.sha256(want.encode()).hexdigest())
                    self.assertEqual(source.target().text,display(kind,entities=[record]))
                    self.assertEqual(self.capabilities(source),CAPS);self.assertTrue(source.is_media)
                    self.assertFalse(source.target().sanitized);self.assertFalse(source.target().truncated)
                    self.assertEqual(ReplySource(want),source)

    def test_plural_source_authority_cannot_be_underreported_by_old_singular_accessor(self):
        source=self.accepted(raw_caption())
        self.assertEqual(self.capabilities(source),CAPS)
        with self.assertRaises(ValueError):_ = source.required_capability
        for raw,want in [(target(),()),(raw_caption(entities=[]),('reply_media_targets',))]:
            self.assertEqual(self.capabilities(source_from_message(raw,123,55)),want)
        raw=target('A😀BC');raw['content']['text']['entities']=[span()]
        self.assertEqual(self.capabilities(source_from_message(raw,123,55)),('reply_formatted_targets',))

    def test_nested_nonbmp_spans_and_permutations_use_original_scalar_aligned_utf16(self):
        entities=[span('Underline',3,2),span('Italic',1,2),span('Bold'),span('Spoiler')]
        source=self.accepted(raw_caption(entities=entities))
        ordered=[span('Bold'),span('Spoiler'),span('Italic',1,2),span('Underline',3,2)]
        self.assertEqual(source.projection_json,serialize(projection(entities=ordered)))
        self.assertEqual(source.target().text,display(entities=[
            {'type':'bold','offset':0,'length':5,'text':'A😀BC'},
            {'type':'spoiler','offset':0,'length':5,'text':'A😀BC'},
            {'type':'italic','offset':1,'length':2,'text':'😀'},
            {'type':'underline','offset':3,'length':2,'text':'BC'}]))
        self.assertEqual(source.source_sha256,self.accepted(raw_caption(entities=entities[::-1])).source_sha256)

    def test_sanitation_flags_caption_covered_text_language_and_each_media_string(self):
        raw=raw_caption(text='Ａ\n  😀B',entities=[span('Bold',1,5)])
        source=self.accepted(raw)
        self.assertEqual(source.target().text,display(text='A 😀B',entities=[{'type':'bold','offset':1,'length':5,'text':'😀'}]))
        self.assertTrue(source.target().sanitized)
        raw=raw_caption(text='x😀 y',entities=[span('PreCode',0,5,'Ｐy  thon')])
        self.assertEqual(self.accepted(raw).target().text,display(text='x😀 y',entities=[{'type':'pre_code','offset':0,'length':5,'text':'x😀 y','language':'Py thon'}]))
        self.assertTrue(self.accepted(raw).target().sanitized)
        raw=raw_caption(text='x y',entities=[span('Italic',1,1)])
        self.assertEqual(self.accepted(raw).target().text,display(text='x y',entities=[{'type':'italic','offset':1,'length':1,'text':''}]))
        self.assertTrue(self.accepted(raw).target().sanitized)
        for kind,field,rawvalue,safevalue in [('document','file_name','Ｅvidence.txt','Evidence.txt'),
                ('document','mime_type','ｔext/plain','text/plain'),('photo','type','ｉ','i')]:
            raw=raw_caption(kind);obj=raw['content']['photo']['sizes'][0] if kind=='photo' else raw['content']['document']
            obj[field]=rawvalue;source=self.accepted(raw);media=facts(kind)
            (media['variants'][0] if kind=='photo' else media)[field]=safevalue
            self.assertEqual(source.target().text,display(kind,media=media));self.assertTrue(source.target().sanitized)

    def test_invalid_coordinates_closed_entities_controls_and_overlap_cannot_be_admitted(self):
        self.accepted(raw_caption())
        bad=[span(offset=o,length=n) for o,n in [(2,1),(1,1),(0,2),(-1,1),(0,0),(4,2),(0,2**31),(2**31,1),(True,1),(0,True),(0,1.0)]]
        bad += [{},dict(span(),extra=1),span('PreCode',language='x\n'),span('PreCode',language='a'*65),span('PreCode',language='😀'*33)]
        hidden=span();hidden['type']={'@type':'textEntityTypeTextUrl','url':'https://example.invalid'};bad.append(hidden)
        for entity in bad:
            with self.subTest(entity=repr(entity)[:80]),self.assertRaises(ValueError):source_from_message(raw_caption(entities=[entity]),123,55)
        excluded=[[span(),span()],[span('Bold',0,4),span('Italic',1,4)]]
        excluded += [[span(c),span('Bold')] for c in ('Code','Pre','PreCode')]
        excluded += [[span(a),span(b,1,2)] for a in ('BlockQuote','ExpandableBlockQuote') for b in ('BlockQuote','ExpandableBlockQuote')]
        for entities in excluded:
            with self.subTest(entities=entities),self.assertRaises(ValueError):source_from_message(raw_caption(entities=entities),123,55)
        for entities in ([span('Code',0,1),span('Pre',1,2),span('PreCode',3,2)],
                [span('BlockQuote',0,3),span('ExpandableBlockQuote',3,2)]):
            self.assertEqual(len(json.loads(self.accepted(raw_caption(entities=entities)).projection_json)['message']['content']['caption']['entities']),len(entities))
        for text in ('',' ','\t\n','bad\ud800','bad\u202e','bad\x00','bad\r','x'*1025):
            with self.subTest(text=repr(text)[:30]),self.assertRaises(ValueError):source_from_message(raw_caption(text=text,entities=[span(length=1)]),123,55)
        for entities in (None,{},'[]',True):
            raw=raw_caption();raw['content']['caption']['entities']=entities
            with self.assertRaises(ValueError):source_from_message(raw,123,55)

    def test_caption_1024_and_32_spans_are_admitted_but_overflow_has_no_truncated_preview(self):
        self.assertEqual(len(self.accepted(raw_caption(text='x'*1024,entities=[span(length=1024)])).target().text),2406)
        entities=[span('Bold',i,1) for i in range(32)]
        source=self.accepted(raw_caption(text='a'*33,entities=entities))
        self.assertEqual(len(json.loads(source.projection_json)['message']['content']['caption']['entities']),32)
        with self.assertRaises(ValueError):source_from_message(raw_caption(text='a'*33,entities=entities+[span('Bold',32,1)]),123,55)
        # Independent final display accounts for every media fact and the marker.
        text='x'*1024;rendered=[{'type':'bold','offset':0,'length':1024,'text':text}]
        media={'variants':[{'type':f'{i:016}','width':16384,'height':16384,'size':2**53-1,'unique_id_sha256':IDENTITY} for i in range(20)]}
        self.assertGreater(len(display('photo',text,rendered,media)),4096)
        raw=raw_caption('photo',text,[span(length=1024)]);item=raw['content']['photo']['sizes'][0];items=[]
        for i in range(20):
            v=copy.deepcopy(item);v.update(type=f'{i:016}',width=16384,height=16384);v['photo']['size']=2**53-1;items.append(v)
        raw['content']['photo']['sizes']=items
        with self.assertRaises(ValueError):source_from_message(raw,123,55)
        with self.assertRaises(ValueError):source_from_message(raw_caption(text='ﷺ'*250,entities=[span(length=250)]),123,55)
        oversize=serialize({'version':4,'padding':'😀'*17000})
        self.assertLess(len(oversize),65536);self.assertGreater(len(oversize.encode()),65536)
        with self.assertRaises(ValueError):ReplySource(oversize)

    def test_entire_display_accepts_exact_marker_inclusive_4096_and_refuses_next_character(self):
        text='x'*900;entities=[span('Bold',0,900),span('Italic',0,900),span('Underline',0,900)]
        raw=raw_caption(text=text,entities=entities);raw['content']['document']['file_name']='n'*44
        records=[{'type':label,'offset':0,'length':900,'text':text} for label in ('bold','italic','underline')]
        media=facts('document');media['file_name']='n'*44
        want=display(text=text,entities=records,media=media)
        self.assertEqual(len(want),4096);self.assertEqual(self.accepted(raw).target().text,want)
        raw['content']['document']['file_name']+='n'
        with self.assertRaises(ValueError):source_from_message(raw,123,55)

    def test_constructor_and_target_independently_refuse_version_boundary_and_entity_order_forgeries(self):
        self.accepted(raw_caption())
        mutations=[lambda v:v.update(version=2),lambda v:v['message']['content']['caption'].update(entities=[]),
            lambda v:v['message']['content']['caption']['entities'][0].update(offset=2),
            lambda v:v['message']['content']['caption']['entities'][0]['type'].update(url='hidden'),
            lambda v:v['message']['content']['media'].update(file_id=1),lambda v:v['message'].update(topic_id={}),
            lambda v:v['message']['content']['media'].update(size=True),lambda v:v['anchor'].update(message_id=True),
            lambda v:v['message']['content']['caption'].update(entities=[span('Italic',1,2),span('Bold')])]
        for mutate in mutations:
            value=projection();mutate(value);encoded=serialize(value)
            with self.subTest(value=repr(value)[:90]):
                with self.assertRaises(ValueError):ReplySource(encoded)
                forged=object.__new__(ReplySource);object.__setattr__(forged,'projection_json',encoded)
                with self.assertRaises(ValueError):forged.target()
        value=projection(entities=[],version=2);value['message']['content']['caption']['entities']=[span()]
        with self.assertRaises(ValueError):ReplySource(serialize(value))

    def test_media_restrictions_and_stable_identity_remain_effective_for_styled_captions(self):
        for kind in ('document','photo','voice_note'):
            original=self.accepted(raw_caption(kind))
            raw=raw_caption(kind);obj=raw['content'][{'document':'document','photo':'photo','voice_note':'voice_note'}[kind]]
            f=obj['sizes'][0]['photo'] if kind=='photo' else obj['document' if kind=='document' else 'voice']
            f['id']=200;f['expected_size']=123;f['local']['path']='/synthetic/cache';f['remote']['id']='new'
            if kind=='voice_note':raw['content']['is_listened']=True;obj['waveform']=''
            if kind=='photo':obj['sizes'][0]['progressive_sizes']=[1,9]
            self.assertEqual(original.source_sha256,self.accepted(raw).source_sha256)
            f['remote']['unique_id']='replacement';self.assertNotEqual(original.source_sha256,self.accepted(raw).source_sha256)
        for kind,key,value in [('photo','width',3),('voice_note','duration',3),('document','file_name','new.txt')]:
            raw=raw_caption(kind);obj=raw['content']['photo']['sizes'][0] if kind=='photo' else raw['content'][kind]
            obj[key]=value;self.assertNotEqual(self.accepted(raw_caption(kind)).source_sha256,self.accepted(raw).source_sha256)
        raw=raw_caption('photo');first=raw['content']['photo']['sizes'][0];second=copy.deepcopy(first);second.update(type='z',width=3)
        raw['content']['photo']['sizes']=[second,first];source=self.accepted(raw)
        raw['content']['photo']['sizes']=[first,second];self.assertEqual(source.source_sha256,self.accepted(raw).source_sha256)
        second=copy.deepcopy(first);second['photo']['size']=11;raw['content']['photo']['sizes']=[first,second]
        with self.assertRaises(ValueError):source_from_message(raw,123,55)
        for kind,key,value in [('photo','has_spoiler',True),('photo','is_secret',True),('photo','show_caption_above_media',True)]:
            raw=raw_caption(kind);raw['content'][key]=value
            with self.assertRaises(ValueError):source_from_message(raw,123,55)
        for duration,mime in [(0,'audio/ogg'),(601,'audio/ogg'),(2,'audio/mpeg')]:
            raw=raw_caption('voice_note');raw['content']['voice_note'].update(duration=duration,mime_type=mime)
            with self.assertRaises(ValueError):source_from_message(raw,123,55)

    def test_literal_v1_v2_v3_canonical_and_display_bytes_and_empty_caption_semantics_do_not_change(self):
        # Old bytes are literal independent expectations; no production serializer or renderer is reused.
        common='"@type":"message","auto_delete_in":0.0,"chat_id":123,'
        suffix=',"date":1700000000,"edit_date":0,"ephemeral_content":null,"ephemeral_message_id":0,"forward_info":null,"id":55,"import_info":null,"is_from_offline":false,"is_outgoing":false,"receiver_id":null,"reply_markup":null,"reply_to":null,"scheduling_state":null,"self_destruct_in":0.0,"self_destruct_type":null,"sender_id":{"@type":"messageSenderUser","user_id":8},"sending_state":null,"topic_id":null}'
        textcontent='"content":{"@type":"messageText","link_preview":null,"link_preview_options":null,"text":{"@type":"formattedText","entities":[],"text":"Source full text"}}'
        want='{"anchor":{"chat_id":123,"message_id":55},"message":{'+common+textcontent+suffix+',"version":1}'
        plain=source_from_message(target(),123,55)
        self.assertEqual(plain.projection_json,want);self.assertEqual(plain.target().text,MARKER+'Source full text')
        styled=target('A😀BC');styled['content']['text']['entities']=[span()]
        textcontent='"content":{"@type":"messageText","link_preview":null,"link_preview_options":null,"text":{"@type":"formattedText","entities":[{"@type":"textEntity","length":5,"offset":0,"type":{"@type":"textEntityTypeBold"}}],"text":"A😀BC"}}'
        want='{"anchor":{"chat_id":123,"message_id":55},"message":{'+common+textcontent+suffix+',"version":3}'
        source=source_from_message(styled,123,55);self.assertEqual(source.projection_json,want)
        self.assertEqual(source.target().text,MARKER+'Formatted target: {"entities":[{"length":5,"offset":0,"text":"A😀BC","type":"bold"}],"offset_basis":"original source UTF-16 code units","text":"A😀BC"}')
        media='"content":{"caption":{"@type":"formattedText","entities":[],"text":"Exact caption"},"kind":"document","media":{"file_name":"evidence.txt","mime_type":"text/plain","size":10,"unique_id_sha256":"'+IDENTITY+'"}}'
        want='{"anchor":{"chat_id":123,"message_id":55},"message":{'+common+media+suffix+',"version":2}'
        source=source_from_message(raw_caption(text='Exact caption',entities=[]),123,55)
        self.assertEqual(source.projection_json,want)
        self.assertEqual(source.target().text,MARKER+'Media target: {"caption":"Exact caption","kind":"document","media":{"file_name":"evidence.txt","mime_type":"text/plain","size":10,"unique_id_sha256":"'+IDENTITY+'"}}')
        for caption in ('',' ','\t\n'):
            source=source_from_message(raw_caption(text=caption,entities=[]),123,55)
            self.assertEqual(json.loads(source.projection_json)['version'],2)
            self.assertEqual(json.loads(source.projection_json)['message']['content']['caption']['text'],caption)


class CaptionLifecycleRisks:
    def setUp(self):
        super().setUp();self.broker._policy=POLICY;self.raw.source=raw_caption()
    @property
    def ops(self):
        return ('get_reply_artifact_draft','update_reply_artifact_draft','refresh_reply_artifact_draft') if self.artifact_path else ('get_reply_draft','update_reply_draft','refresh_reply_draft')
    def extra(self):return {'caption':'new'} if self.artifact_path else {'text':'new'}
    def preparation(self):
        return ('prepare_reply_artifact_send',dict(recipient=123,reply_to=ANCHOR,artifact_id=self.artifact.artifact_id,display_name='evidence.txt',mime_type='text/plain',caption='Caption',kind='document')) if self.artifact_path else ('prepare_reply_text_send',dict(recipient=123,reply_to=ANCHOR,text='Final reply'))
    def classifier(self,did,**kw):
        self.assertTrue(hasattr(self.broker._drafts,'source_required_capabilities'),'registry must classify full conjunctive authority')
        return self.broker._drafts.source_required_capabilities(did,owner=kw.pop('owner',DraftOwner(CLIENT,7)),**kw)

    def test_complete_caption_evidence_reaches_owner_and_outgoing_wire_stays_plain_single_send(self):
        for kind in ('document','photo','voice_note'):
            self.raw.source=raw_caption(kind);preview=self.prepare();did=preview['draft']['draft_id']
            self.assertEqual(preview['reply_target']['text'],display(kind));self.assertEqual(self.classifier(did),CAPS)
            self.assertTrue(self.broker._drafts.is_media_reply(did,owner=DraftOwner(CLIENT,7)))
            with self.assertRaises(DraftError):self.broker._drafts.source_required_capability(did,owner=DraftOwner(CLIENT,7))
            self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(self.send(did)['status'],'sent')
            self.assertEqual(self.approvals[-1]['reply_artifact_preview' if self.artifact_path else 'reply_preview'],preview)
            payload=self.sends()[-1]['input_message_content'];self.assertEqual(payload['caption' if self.artifact_path else 'text']['entities'],[])
            self.assertEqual(self.sends()[-1]['reply_to'],{'@type':'inputMessageReplyToMessage','message_id':55,'quote':None,'checklist_task_id':0,'poll_option_id':''})
        self.assertEqual(len(self.sends()),3)

    def test_each_single_missing_source_capability_blocks_prepare_pending_and_terminal_replay(self):
        op,payload=self.preparation()
        for missing in CAPS:
            policy=RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c!=missing))
            self.broker._policy=policy;self.assertEqual(self.dispatch(op,payload)['status'],'unavailable')
            self.broker._policy=POLICY;did=self.prepare()['draft']['draft_id'];self.broker._policy=policy
            for operation,extra in [(self.ops[0],{}),(self.ops[1],self.extra()),(self.ops[2],{})]:
                self.assertEqual(self.dispatch(operation,dict(draft_id=did,**extra))['status'],'unavailable')
            self.assertEqual(self.send(did)['status'],'expired');self.assertEqual(self.sends(),[])
        for mode,want in [('sent','sent'),('caption' if self.artifact_path else 'wrong','outcome_unknown'),('properties','failed')]:
            self.broker._policy=POLICY;self.raw.mode='sent' if mode=='properties' else mode;self.raw.can_reply=mode!='properties'
            did=self.prepare()['draft']['draft_id'];self.assertEqual(self.send(did)['status'],want);count=len(self.sends())
            for missing in CAPS:
                self.broker._policy=RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c!=missing))
                self.assertEqual(self.send(did)['status'],'expired');self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['status'],want)
                self.assertEqual(len(self.sends()),count)
            self.broker._policy=POLICY;self.assertEqual(self.send(did)['status'],want);self.assertEqual(len(self.sends()),count)

    def test_owner_account_pending_ttl_and_terminal_provider_epoch_monotonic_ttl_fail_closed(self):
        registry=self.broker._drafts;did=self.prepare()['draft']['draft_id']
        for owner in (DraftOwner(FOREIGN,7),DraftOwner(CLIENT,8)):
            with self.assertRaises(DraftError):self.classifier(did,owner=owner)
        for op,extra in [(self.ops[0],{}),(self.ops[1],self.extra()),(self.ops[2],{})]:
            self.assertEqual(self.dispatch(op,dict(draft_id=did,**extra),FOREIGN)['status'],'unavailable')
        self.raw.account=8;self.assertEqual(self.dispatch(self.ops[0],{'draft_id':did})['status'],'unavailable');self.raw.account=7
        expires=registry.peek(did,owner=DraftOwner(CLIENT,7)).expires_at
        with patch.object(registry,'_clock',return_value=expires-.001):self.assertEqual(self.classifier(did),CAPS)
        with patch.object(registry,'_clock',return_value=expires):
            with self.assertRaises(DraftError):self.classifier(did)
            self.assertEqual(self.send(did)['status'],'expired')
        with patch.object(registry,'_attempt_clock',return_value=100):did=self.prepare()['draft']['draft_id'];self.assertEqual(self.send(did)['status'],'sent')
        with patch.object(registry,'_clock',return_value=10**10),patch.object(registry,'_attempt_clock',return_value=999.999):self.assertEqual(self.classifier(did),CAPS)
        for provider,epoch in [(object(),self.client.send_observation_epoch),(self.client,object())]:
            with self.assertRaises(DraftError):self.classifier(did,provider=provider,provider_epoch=epoch)
        epoch=self.client.send_observation_epoch;self.client._send_observation_epoch=object()
        self.assertEqual(self.send(did)['status'],'expired');self.client._send_observation_epoch=epoch
        with patch.object(registry,'_attempt_clock',return_value=1000):
            with self.assertRaises(DraftError):self.classifier(did)
            self.assertEqual(self.send(did)['status'],'expired')
        self.assertEqual(len(self.sends()),1)

    def test_v2_to_v4_and_v4_to_v2_refresh_checks_both_old_and_new_authority(self):
        preview=self.prepare();did=preview['draft']['draft_id']
        revision=self.dispatch(self.ops[1],dict(draft_id=did,**self.extra()))['reply']
        self.assertEqual(revision['reply_target'],preview['reply_target']);self.assertEqual(self.send(did)['status'],'expired')
        did=revision['draft']['draft_id'];self.raw.source=raw_caption(entities=[])
        self.broker._policy=RuntimePolicy(enabled_capabilities=BASE+('reply_media_targets',))
        self.assertEqual(self.dispatch(self.ops[2],{'draft_id':did})['status'],'unavailable')
        self.broker._policy=POLICY;plain=self.dispatch(self.ops[2],{'draft_id':did})['reply'];did=plain['draft']['draft_id']
        self.assertNotEqual(plain['reply_target']['source_sha256'],preview['reply_target']['source_sha256'])
        self.raw.source=raw_caption();self.broker._policy=RuntimePolicy(enabled_capabilities=BASE+('reply_media_targets',))
        self.assertEqual(self.dispatch(self.ops[2],{'draft_id':did})['status'],'unavailable')
        self.broker._policy=POLICY;styled=self.dispatch(self.ops[2],{'draft_id':did})['reply']
        self.assertEqual(styled['reply_target']['text'],display());self.assertEqual(self.sends(),[])

    def test_style_text_media_and_eligibility_drift_during_approval_prevent_transport(self):
        for change in ('range','style','text','identity','metadata','properties','account','title','epoch','provider'):
            self.raw.source=raw_caption();self.raw.can_reply=True;self.raw.account=7;self.raw.title='Recipient'
            did=self.prepare()['draft']['draft_id'];epoch=self.client.send_observation_epoch
            def approve(**kw):
                if change=='range':self.raw.source['content']['caption']['entities'][0]['length']=3
                elif change=='style':self.raw.source['content']['caption']['entities'][0]['type']['@type']='textEntityTypeItalic'
                elif change=='text':self.raw.source['content']['caption']['text']='A😀BD'
                elif change=='identity':self.raw.source['content']['document']['document']['remote']['unique_id']='replacement'
                elif change=='metadata':self.raw.source['content']['document']['file_name']='other.txt'
                elif change=='properties':self.raw.can_reply=False
                elif change=='account':self.raw.account=8
                elif change=='title':self.raw.title='Changed'
                elif change=='epoch':self.client._send_observation_epoch=object()
                else:self.broker._client=type(self.client)(raw=type(self.raw)())
                return True
            self.broker._approval_prompt=approve
            with self.subTest(change=change):self.assertNotEqual(self.send(did)['status'],'sent');self.assertEqual(self.sends(),[])
            self.client._send_observation_epoch=epoch;self.broker._client=self.client

    def test_harmless_caption_order_and_file_progress_drift_do_not_prevent_send(self):
        self.raw.source=raw_caption(entities=[span(),span('Italic',1,2)])
        did=self.prepare()['draft']['draft_id']
        def approve(**kw):
            self.raw.source['content']['caption']['entities'].reverse()
            file=self.raw.source['content']['document']['document'];file['id']=2;file['remote']['id']='volatile';file['local']['downloaded_size']=1
            return True
        self.broker._approval_prompt=approve;self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(len(self.sends()),1)

    def test_send_and_path_and_each_source_capability_revoked_after_approval_prevent_dispatch(self):
        for missing in ('send','reply_artifact_send' if self.artifact_path else 'reply_text_send',*CAPS):
            self.broker._policy=POLICY;did=self.prepare()['draft']['draft_id']
            def approve(**kw):self.broker._policy=RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c!=missing));return True
            self.broker._approval_prompt=approve
            self.assertNotEqual(self.send(did)['status'],'sent');self.assertEqual(self.sends(),[])

    def test_each_source_capability_revocation_and_policy_epoch_errors_after_registration_discard_observation(self):
        path='reply_artifact_send' if self.artifact_path else 'reply_text_send'
        for mode in ('send',path,*CAPS,'raise','epoch'):
            self.broker._policy=POLICY;epoch=self.client.send_observation_epoch;did=self.prepare()['draft']['draft_id']
            original=self.client._send_observations.register
            def register(*args,**kw):
                original(*args,**kw)
                if mode in ('send',path,*CAPS):self.broker._policy=RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c!=mode))
                elif mode=='raise':self.broker._policy=RuntimePolicy(enabled_capabilities=POLICY.enabled_capabilities,source_path=self.root/'missing.toml')
                else:self.client._send_observation_epoch=object()
            with patch.object(self.client._send_observations,'register',side_effect=register):self.assertEqual(self.send(did)['status'],'failed')
            self.broker._policy=POLICY;self.client._send_observation_epoch=epoch
            self.assertEqual(self.sends(),[]);self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
            self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(self.sends(),[])

    def test_unknown_late_exact_recovery_retains_observation_and_never_resends(self):
        self.raw.mode='caption' if self.artifact_path else 'wrong';did=self.prepare()['draft']['draft_id']
        self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertIsNotNone(self.client._send_observations.snapshot(did))
        self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertEqual(len(self.sends()),1)
        final=outgoing(701,sending_state=None,**({'content':content_fixture('document')} if self.artifact_path else {}))
        self.client._reduce_receive_event({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':final})
        self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['status'],'sent')
        self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(len(self.sends()),1)


class CaptionTextTests(CaptionLifecycleRisks,ReplyFixture,unittest.TestCase):
    artifact_path=False


class CaptionArtifactTests(CaptionLifecycleRisks,ArtifactSendFixture,unittest.TestCase):
    artifact_path=True
