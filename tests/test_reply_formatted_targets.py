"""New style-source risks through the production parser, Broker and reducers.

Raw provider I/O and owner decisions are synthetic. No private source projection
or preview helper is used to derive the independent expected records.
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
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message, EVIDENCE_MARKER
from test_text_replies import ReplyFixture, target, outgoing, CLIENT, FOREIGN, ANCHOR
from test_artifact_replies import ArtifactSendFixture, content_fixture

STYLE_POLICY = RuntimePolicy(enabled_capabilities=('send','reply_text_send','reply_artifact_send','reply_formatted_targets'))
BASE_POLICY = RuntimePolicy(enabled_capabilities=('send','reply_text_send','reply_artifact_send'))
STYLES = ('Bold','Italic','Underline','Strikethrough','Spoiler','Code','Pre','PreCode','BlockQuote','ExpandableBlockQuote')
LABELS = ('bold','italic','underline','strikethrough','spoiler','code','pre','pre_code','block_quote','expandable_block_quote')


def entity(kind='Bold', offset=0, length=5, **changes):
    typ={'@type':'textEntityType'+kind}
    if kind=='PreCode':typ['language']='python'
    return {'@type':'textEntity','offset':offset,'length':length,'type':typ,**changes}


def styled(text='A😀BC', entities=None):
    raw=target(text)
    raw['content']['text']['entities']=[entity()] if entities is None else entities
    return raw


def serialized(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def expected_projection(text='A😀BC', entities=None):
    return {'version':3,'anchor':{'chat_id':123,'message_id':55},'message':{
        '@type':'message','chat_id':123,'id':55,'sender_id':{'@type':'messageSenderUser','user_id':8},
        'is_outgoing':False,'is_from_offline':False,'ephemeral_message_id':0,'date':1700000000,'edit_date':0,
        'self_destruct_in':0.0,'auto_delete_in':0.0,'sending_state':None,'scheduling_state':None,'topic_id':None,
        'self_destruct_type':None,'ephemeral_content':None,'receiver_id':None,'reply_to':None,'forward_info':None,
        'import_info':None,'reply_markup':None,'content':{'@type':'messageText','text':{
            '@type':'formattedText','text':text,'entities':[entity()] if entities is None else entities},
            'link_preview':None,'link_preview_options':None}}}


def display(text, entities):
    return EVIDENCE_MARKER+'Formatted target: '+serialized({'text':text,
        'offset_basis':'original source UTF-16 code units','entities':entities})


class FormattedSourceTests(unittest.TestCase):
    def accepted(self, raw):
        try:
            return source_from_message(raw,123,55)
        except ValueError:
            self.fail('approved style-only source must be admitted')

    def capability(self, source):
        self.assertTrue(hasattr(source,'required_capability'),'source capability accessor is required')
        return source.required_capability

    # These positive assertions catch flattening, wrong code-unit arithmetic,
    # order-dependent identity and lossy display before negative checks can pass.
    def test_each_style_has_exact_raw_projection_and_full_literal_display(self):
        for kind,label in zip(STYLES,LABELS):
            spans=[entity(kind)]
            source=self.accepted(styled(entities=spans))
            expected=serialized(expected_projection(entities=spans))
            rendered={'type':label,'offset':0,'length':5,'text':'A😀BC'}
            if kind=='PreCode':rendered['language']='python'
            with self.subTest(style=kind):
                self.assertEqual(source.projection_json,expected)
                self.assertEqual(source.source_sha256,hashlib.sha256(expected.encode()).hexdigest())
                self.assertFalse(source.is_media)
                self.assertEqual(self.capability(source),'reply_formatted_targets')
                self.assertEqual(source.target().text,display('A😀BC',[rendered]))
                self.assertFalse(source.target().sanitized)
                self.assertEqual(ReplySource(expected),source)
    def test_literal_utf16_nested_adjacent_and_coextensive_order(self):
        spans=[entity('Underline',3,2),entity('Italic',1,2),entity('Bold',0,5),entity('Spoiler',0,5)]
        source=self.accepted(styled(entities=spans))
        want=[entity('Bold',0,5),entity('Spoiler',0,5),entity('Italic',1,2),entity('Underline',3,2)]
        self.assertEqual(source.projection_json,serialized(expected_projection(entities=want)))
        self.assertEqual(source.target().text,display('A😀BC',[
            {'type':'bold','offset':0,'length':5,'text':'A😀BC'},
            {'type':'spoiler','offset':0,'length':5,'text':'A😀BC'},
            {'type':'italic','offset':1,'length':2,'text':'😀'},
            {'type':'underline','offset':3,'length':2,'text':'BC'}]))
        self.assertEqual(source.source_sha256,self.accepted(styled(entities=list(reversed(spans)))).source_sha256)
    def test_sanitation_keeps_original_coordinates_and_independent_span_text(self):
        source=self.accepted(styled('Ａ\n  😀B',[entity('Bold',1,5)]))
        self.assertEqual(source.target().text,display('A 😀B',[{'type':'bold','offset':1,'length':5,'text':'😀'}]))
        self.assertTrue(source.target().sanitized)
        source=self.accepted(styled('x😀 y',[entity('PreCode',0,5,type={'@type':'textEntityTypePreCode','language':'Ｐy  thon'})]))
        self.assertIn('"language":"Py thon"',source.target().text)
        self.assertTrue(source.target().sanitized)
    def test_utf16_split_negative_zero_int32_and_type_bounds_refuse(self):
        for off,length in [(2,1),(1,1),(0,2),(-1,1),(0,0),(4,2),(0,2**31),(2**31,1),(True,1),(0,True),(0,1.0),('0',1)]:
            with self.subTest(offset=off,length=length),self.assertRaises(ValueError):
                source_from_message(styled(entities=[entity(offset=off,length=length)]),123,55)
    def test_unknown_closed_fields_types_languages_and_controls_refuse(self):
        invalid=[{},entity(extra=1),entity(type={'@type':'textEntityTypeBold','language':''}),
            entity(type={'@type':'textEntityTypeTextUrl','url':'https://example.invalid'}),
            entity(type={'@type':'textEntityTypeMention'}),entity(type={'@type':[]}),
            entity(type={'@type':'textEntityTypePreCode'}),entity(type={'@type':'textEntityTypePreCode','language':False}),
            entity(type={'@type':'textEntityTypePreCode','language':'a'*65}),
            entity(type={'@type':'textEntityTypePreCode','language':'😀'*33}),
            entity(type={'@type':'textEntityTypePreCode','language':'x\n'}),
            entity(type={'@type':'textEntityTypePreCode','language':'\ud800'})]
        for span in invalid:
            with self.subTest(span=repr(span)[:100]),self.assertRaises(ValueError):source_from_message(styled(entities=[span]),123,55)
        for value in ('x\ud800','x\u202e','x\x00','x\r',' ','x'*4097):
            with self.subTest(text=repr(value)[:60]),self.assertRaises(ValueError):source_from_message(styled(value,[entity(length=1)]),123,55)
        for value in (None,{},'[]',True):
            raw=styled();raw['content']['text']['entities']=value
            with self.subTest(entities=value),self.assertRaises(ValueError):source_from_message(raw,123,55)
    def test_duplicates_crossings_and_code_quote_exclusions_refuse(self):
        invalid=[[entity(),entity()], [entity('Bold',0,4),entity('Italic',1,4)]]
        for code in ('Code','Pre','PreCode'):
            invalid.extend([[entity(code),entity('Bold')],[entity(code,1,2),entity('Italic',0,5)]])
        for a in ('BlockQuote','ExpandableBlockQuote'):
            for b in ('BlockQuote','ExpandableBlockQuote'):
                invalid.append([entity(a,0,5),entity(b,1,2)])
        for spans in invalid:
            with self.subTest(spans=spans),self.assertRaises(ValueError):source_from_message(styled(entities=spans),123,55)
        for spans in ([entity('Code',0,1),entity('Pre',1,2),entity('PreCode',3,2)],
                      [entity('BlockQuote',0,3),entity('ExpandableBlockQuote',3,2)]):
            self.assertEqual(len(json.loads(self.accepted(styled(entities=spans)).projection_json)['message']['content']['text']['entities']),len(spans))
    def test_32_spans_accept_33_refuse_and_full_display_bound_is_marker_inclusive(self):
        spans=[entity('Bold',i,1) for i in range(32)]
        self.assertEqual(len(json.loads(self.accepted(styled('a'*33,spans)).projection_json)['message']['content']['text']['entities']),32)
        with self.assertRaises(ValueError):source_from_message(styled('a'*33,spans+[entity('Bold',32,1)]),123,55)
        # One full-span entity displays raw text twice, so the final bound is reached
        # well before the separate 4096-character raw source limit.
        text='x'*1961
        self.assertEqual(len(display(text,[{'type':'bold','offset':0,'length':1961,'text':text}])),4096)
        self.assertEqual(len(self.accepted(styled(text,[entity(length=1961)])).target().text),4096)
        with self.assertRaises(ValueError):source_from_message(styled(text+'x',[entity(length=1962)]),123,55)
        with self.assertRaises(ValueError):source_from_message(styled('\ufdfa'*250,[entity(length=250)]),123,55)
    def test_constructor_and_target_rendering_independently_refuse_forged_projection(self):
        mutations=[lambda v:v.update(version=1),lambda v:v.update(extra=1),
            lambda v:v['message']['content']['text']['entities'][0].update(offset=2),
            lambda v:v['message']['content']['text']['entities'][0]['type'].update(url='hidden'),
            lambda v:v['message']['content']['text']['entities'].append(entity()),
            lambda v:v['message'].update(topic_id={}),lambda v:v['anchor'].update(message_id=True)]
        for mutate in mutations:
            value=expected_projection();mutate(value);projection=serialized(value)
            with self.subTest(value=repr(value)[:80]):
                with self.assertRaises(ValueError):ReplySource(projection)
                forged=object.__new__(ReplySource);object.__setattr__(forged,'projection_json',projection)
                with self.assertRaises(ValueError):forged.target()
        reverse=expected_projection(entities=[entity('Italic',1,2),entity('Bold',0,5)])
        with self.assertRaises(ValueError):ReplySource(serialized(reverse))
        with self.assertRaises(ValueError):ReplySource('['*1100+'0'+']'*1100)
        with self.assertRaises(ValueError):ReplySource(' '*65537)
    def test_raw_projection_utf8_bound_precedes_forged_target_render(self):
        value=expected_projection('😀'*4096,[entity('Bold',0,8192)])
        value['message']['content']['text']['entities']=[entity('Bold',i*2,8192-i*2) for i in range(32)]
        self.assertLess(len(serialized(value).encode()),65536)
        # Bytes rather than Python character count determines admission.
        projection=serialized({'version':3,'padding':'😀'*17000})
        self.assertLess(len(projection),65536);self.assertGreater(len(projection.encode()),65536)
        with self.assertRaises(ValueError):ReplySource(projection)
    def test_sanitized_empty_span_and_maximum_language_keep_all_facts(self):
        source=self.accepted(styled('x y',[entity('Italic',1,1)]))
        self.assertEqual(source.target().text,display('x y',[{'type':'italic','offset':1,'length':1,'text':''}]))
        self.assertTrue(source.target().sanitized)
        language='é'*64
        source=self.accepted(styled(entities=[entity('PreCode',type={'@type':'textEntityTypePreCode','language':language})]))
        self.assertIn('"language":"'+language+'"',source.target().text)
        self.assertFalse(source.target().sanitized)
    def test_v3_safety_shell_and_disabled_preview_shape_are_still_closed(self):
        from telegram_search_mcp.reply_drafts import LINK_OPTIONS
        raw=styled();raw['content']['link_preview_options']=dict(LINK_OPTIONS)
        self.assertEqual(self.accepted(raw).required_capability,'reply_formatted_targets')
        for key in ('sending_state','scheduling_state','topic_id','self_destruct_type','ephemeral_content','receiver_id','reply_to','forward_info','import_info','reply_markup'):
            raw=styled();raw[key]={}
            with self.subTest(key=key),self.assertRaises(ValueError):source_from_message(raw,123,55)
        for change in ({'link_preview':{}},{'unknown':None},{'link_preview_options':dict(LINK_OPTIONS,url='hidden')},{'link_preview_options':dict(LINK_OPTIONS,is_disabled=1)}):
            raw=styled();raw['content'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):source_from_message(raw,123,55)

    def test_entity_only_drift_changes_hash_but_order_permutation_does_not(self):
        for before,after in [([entity()],[entity(length=3)]),
            ([entity('PreCode')],[entity('PreCode',type={'@type':'textEntityTypePreCode','language':'rust'})]),
            ([entity('BlockQuote')],[entity('ExpandableBlockQuote')])]:
            self.assertNotEqual(self.accepted(styled(entities=before)).source_sha256,
                                self.accepted(styled(entities=after)).source_sha256)
    def test_v1_v2_canonical_continuity_and_default_off_policy(self):
        from test_reply_media_targets import expected_projection as media_projection
        for kind in ('document','photo','voice_note'):
            source=source_from_message(target(content=content_fixture(kind,'Exact caption')),123,55)
            self.assertEqual(source.projection_json,serialized(media_projection(kind)))
            self.assertTrue(source.is_media);self.assertEqual(self.capability(source),'reply_media_targets')
        plain=expected_projection('Source full text',[]);plain['version']=1
        source=source_from_message(target(),123,55)
        self.assertEqual(source.projection_json,serialized(plain));self.assertIsNone(self.capability(source))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'runtime.toml'
            self.assertNotIn('reply_formatted_targets',load_runtime_policy(path).enabled_capabilities)
            path.write_text('config_version=1\nenabled_capabilities=["send","reply_text_send","reply_formatted_targets"]\n');path.chmod(0o600)
            self.assertIn('reply_formatted_targets',load_runtime_policy(path).enabled_capabilities)


class FormattedLifecycleRisks:
    def setUp(self):
        super().setUp();self.broker._policy=STYLE_POLICY;self.raw.source=styled()
    @property
    def ops(self):
        return ('get_reply_artifact_draft','update_reply_artifact_draft','refresh_reply_artifact_draft') if self.artifact_path else ('get_reply_draft','update_reply_draft','refresh_reply_draft')
    def revision_extra(self):return {'caption':'new'} if self.artifact_path else {'text':'new'}
    def test_complete_preview_owner_evidence_and_plain_exact_single_wire(self):
        preview=self.prepare();did=preview['draft']['draft_id']
        self.assertEqual(preview['reply_target']['text'],display('A😀BC',[{'type':'bold','offset':0,'length':5,'text':'A😀BC'}]))
        self.assertEqual(self.sends(),[]);self.assertEqual(self.send(did)['status'],'sent')
        wire=self.sends()[0];payload=wire['input_message_content']
        self.assertEqual(payload['caption' if self.artifact_path else 'text']['entities'],[])
        self.assertEqual(wire['reply_to'],{'@type':'inputMessageReplyToMessage','message_id':55,'quote':None,'checklist_task_id':0,'poll_option_id':''})
        self.assertEqual(self.approvals[-1]['reply_artifact_preview' if self.artifact_path else 'reply_preview'],preview)
        self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(len(self.sends()),1)
    def test_optout_blocks_prepare_inspection_revision_and_every_terminal_replay(self):
        self.broker._policy=BASE_POLICY
        op='prepare_reply_artifact_send' if self.artifact_path else 'prepare_reply_text_send'
        payload=dict(recipient=123,reply_to=ANCHOR,**(dict(artifact_id=self.artifact.artifact_id,display_name='evidence.txt',mime_type='text/plain',caption='Caption',kind='document') if self.artifact_path else {'text':'Final reply'}))
        self.assertEqual(self.dispatch(op,payload)['status'],'unavailable')
        self.broker._policy=STYLE_POLICY;did=self.prepare()['draft']['draft_id'];self.broker._policy=BASE_POLICY
        for op,extra in [(self.ops[0],{}),(self.ops[1],self.revision_extra()),(self.ops[2],{})]:
            self.assertEqual(self.dispatch(op,dict(draft_id=did,**extra))['status'],'unavailable')
        self.assertEqual(self.send(did)['status'],'expired');self.assertEqual(self.sends(),[])
        for mode,want in [('sent','sent'),('caption' if self.artifact_path else 'wrong','outcome_unknown'),('properties','failed')]:
            self.broker._policy=STYLE_POLICY;self.raw.mode='sent' if mode=='properties' else mode;self.raw.can_reply=mode!='properties'
            did=self.prepare()['draft']['draft_id'];self.assertEqual(self.send(did)['status'],want)
            count=len(self.sends());self.broker._policy=BASE_POLICY
            self.assertEqual(self.send(did)['status'],'expired')
            self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['status'],want)
            self.assertEqual(len(self.sends()),count)
    def test_owner_account_epoch_and_provider_replay_do_not_expose_source(self):
        did=self.prepare()['draft']['draft_id']
        for op,extra in [(self.ops[0],{}),(self.ops[1],self.revision_extra()),(self.ops[2],{})]:
            self.assertEqual(self.dispatch(op,dict(draft_id=did,**extra),FOREIGN)['status'],'unavailable')
        self.raw.account=8;self.assertEqual(self.dispatch(self.ops[0],{'draft_id':did})['status'],'unavailable');self.raw.account=7
        self.assertEqual(self.send(did)['status'],'sent');count=len(self.sends())
        epoch=self.client.send_observation_epoch;self.client._send_observation_epoch=object()
        self.assertEqual(self.send(did)['status'],'expired');self.client._send_observation_epoch=epoch
        old=self.broker._client;self.broker._client=type(self.client)(raw=type(self.raw)())
        self.assertEqual(self.send(did)['status'],'expired');self.broker._client=old
        self.assertEqual(len(self.sends()),count)
    def test_plain_receipt_replay_preserves_existing_provider_epoch_behavior(self):
        self.raw.source=target();did=self.prepare()['draft']['draft_id']
        self.assertEqual(self.send(did)['status'],'sent');epoch=self.client.send_observation_epoch
        self.client._send_observation_epoch=object()
        self.assertEqual(self.send(did)['status'],'sent');self.client._send_observation_epoch=epoch
        old=self.broker._client;self.broker._client=type(self.client)(raw=type(self.raw)())
        self.assertEqual(self.send(did)['status'],'sent');self.broker._client=old
        self.assertEqual(len(self.sends()),1)
    def test_media_receipt_replay_requires_original_provider_and_epoch(self):
        self.raw.source=target(content=content_fixture('document'))
        self.broker._policy=RuntimePolicy(enabled_capabilities=STYLE_POLICY.enabled_capabilities+('reply_media_targets',))
        did=self.prepare()['draft']['draft_id'];self.assertEqual(self.send(did)['status'],'sent')
        epoch=self.client.send_observation_epoch;self.client._send_observation_epoch=object()
        self.assertEqual(self.send(did)['status'],'expired');self.client._send_observation_epoch=epoch
        old=self.broker._client;self.broker._client=type(self.client)(raw=type(self.raw)())
        self.assertEqual(self.send(did)['status'],'expired');self.broker._client=old
        self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(len(self.sends()),1)

    def test_pending_wall_and_terminal_monotonic_exact_expiry(self):
        registry=self.broker._drafts;owner=DraftOwner(CLIENT,7);did=self.prepare()['draft']['draft_id'];expires=registry.peek(did,owner=owner).expires_at
        with patch.object(registry,'_clock',return_value=expires-.001):self.assertEqual(registry.source_required_capability(did,owner=owner),'reply_formatted_targets')
        with patch.object(registry,'_clock',return_value=expires):
            with self.assertRaises(DraftError):registry.source_required_capability(did,owner=owner)
            self.assertEqual(self.send(did)['status'],'expired');self.assertEqual(self.sends(),[])
        for mode,want in [('sent','sent'),('caption' if self.artifact_path else 'wrong','outcome_unknown'),('properties','failed')]:
            self.raw.mode='sent' if mode=='properties' else mode;self.raw.can_reply=mode!='properties'
            with patch.object(registry,'_attempt_clock',return_value=100):did=self.prepare()['draft']['draft_id'];self.assertEqual(self.send(did)['status'],want)
            count=len(self.sends())
            with patch.object(registry,'_clock',return_value=10**10),patch.object(registry,'_attempt_clock',return_value=999.999):
                self.assertEqual(registry.source_required_capability(did,owner=owner),'reply_formatted_targets');self.assertEqual(self.send(did)['status'],want)
            with patch.object(registry,'_attempt_clock',return_value=1000):
                with self.assertRaises(DraftError):registry.source_required_capability(did,owner=owner)
                self.assertEqual(self.send(did)['status'],'expired');self.assertEqual(len(self.sends()),count)
                self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'none')
    def test_update_refresh_both_old_and_new_source_capabilities(self):
        preview=self.prepare();did=preview['draft']['draft_id']
        revised=self.dispatch(self.ops[1],dict(draft_id=did,**self.revision_extra()))['reply']
        self.assertEqual(revised['reply_target'],preview['reply_target']);self.assertNotEqual(revised['preview_sha256'],preview['preview_sha256'])
        self.assertEqual(self.send(did)['status'],'expired')
        did=revised['draft']['draft_id'];self.raw.source=target();self.broker._policy=BASE_POLICY
        self.assertEqual(self.dispatch(self.ops[2],{'draft_id':did})['status'],'unavailable')
        self.broker._policy=STYLE_POLICY;plain=self.dispatch(self.ops[2],{'draft_id':did})['reply'];did=plain['draft']['draft_id']
        self.raw.source=styled();self.broker._policy=BASE_POLICY
        self.assertEqual(self.dispatch(self.ops[2],{'draft_id':did})['status'],'unavailable')
        self.broker._policy=STYLE_POLICY;new=self.dispatch(self.ops[2],{'draft_id':did})['reply']
        self.assertNotEqual(new['reply_target']['source_sha256'],plain['reply_target']['source_sha256']);self.assertEqual(self.sends(),[])
    def test_range_language_quote_and_unsafe_source_drift_during_owner_decision(self):
        for before,after in [(styled(),styled(entities=[entity(length=3)])),
            (styled(entities=[entity('PreCode')]),styled(entities=[entity('PreCode',type={'@type':'textEntityTypePreCode','language':'rust'})])),
            (styled(entities=[entity('BlockQuote')]),styled(entities=[entity('ExpandableBlockQuote')])),
            (styled(),styled(entities=[entity(type={'@type':'textEntityTypeUrl'})])),
            (styled(),{'@type':'error','code':404,'message':'private'})]:
            self.raw.source=before;did=self.prepare()['draft']['draft_id']
            def approve(**kw):self.raw.source=after;return True
            self.broker._approval_prompt=approve
            self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(self.sends(),[])
            self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
    def test_entity_drift_at_fresh_provider_read_before_send_is_refused(self):
        for before,after in [(styled(),styled(entities=[entity(length=3)])),
            (styled(entities=[entity('PreCode')]),styled(entities=[entity('PreCode',type={'@type':'textEntityTypePreCode','language':'rust'})])),
            (styled(entities=[entity('BlockQuote')]),styled(entities=[entity('ExpandableBlockQuote')]))]:
            self.raw.source=before;did=self.prepare()['draft']['draft_id'];original=self.raw.send
            def raw_send(request):
                if request['@type']=='getMessage':self.raw.source=after
                return original(request)
            with patch.object(self.raw,'send',side_effect=raw_send):self.assertEqual(self.send(did)['status'],'failed')
            self.assertEqual(self.sends(),[]);self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
    def test_posttransport_receive_failure_retains_uncertainty_and_no_retry(self):
        self.raw.mode='lost';did=self.prepare()['draft']['draft_id']
        self.assertEqual(self.send(did)['status'],'outcome_unknown')
        self.assertIsNotNone(self.client._send_observations.snapshot(did))
        self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertEqual(len(self.sends()),1)
        self.assertNotEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
    def test_classifier_never_returns_gated_source_for_wrong_owner_account_or_epoch(self):
        registry=self.broker._drafts;owner=DraftOwner(CLIENT,7);did=self.prepare()['draft']['draft_id']
        for wrong in (DraftOwner(FOREIGN,7),DraftOwner(CLIENT,8)):
            with self.assertRaises(DraftError):registry.source_required_capability(did,owner=wrong)
        self.assertEqual(self.send(did)['status'],'sent')
        with self.assertRaises(DraftError):registry.source_required_capability(did,owner=owner,provider=self.client,provider_epoch=object())
        with self.assertRaises(DraftError):registry.source_required_capability(did,owner=owner,provider=object(),provider_epoch=self.client.send_observation_epoch)

    def test_final_guard_revocation_exception_and_epoch_discard_unattempted_observation(self):
        for mode in ('revoke','raise','epoch'):
            self.broker._policy=STYLE_POLICY;epoch=self.client.send_observation_epoch;did=self.prepare()['draft']['draft_id']
            original=self.client._send_observations.register
            def register(*args,**kwargs):
                original(*args,**kwargs)
                if mode=='revoke':self.broker._policy=BASE_POLICY
                elif mode=='raise':self.broker._policy=RuntimePolicy(enabled_capabilities=STYLE_POLICY.enabled_capabilities,source_path=self.root/'missing.toml')
                else:self.client._send_observation_epoch=object()
            with patch.object(self.client._send_observations,'register',side_effect=register):self.assertEqual(self.send(did)['status'],'failed')
            self.broker._policy=STYLE_POLICY;self.client._send_observation_epoch=epoch
            self.assertEqual(self.sends(),[]);self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
            self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(self.sends(),[])
    def test_cancel_or_revision_during_owner_decision_never_dispatches(self):
        for op in ('cancel_draft',self.ops[1]):
            did=self.prepare()['draft']['draft_id']
            def approve(**kw):self.dispatch(op,dict(draft_id=did,**(self.revision_extra() if op==self.ops[1] else {})));return True
            self.broker._approval_prompt=approve
            self.assertEqual(self.send(did)['status'],'expired');self.assertEqual(self.sends(),[])
    def test_posttransport_uncertainty_and_exact_late_recovery_never_resend(self):
        self.raw.mode='caption' if self.artifact_path else 'wrong';did=self.prepare()['draft']['draft_id']
        self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertIsNotNone(self.client._send_observations.snapshot(did))
        self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertEqual(len(self.sends()),1)
        final=outgoing(701,sending_state=None,**({'content':content_fixture('document')} if self.artifact_path else {}))
        self.client._reduce_receive_event({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':final})
        self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['status'],'sent')
        self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(len(self.sends()),1)


class FormattedTextTests(FormattedLifecycleRisks,ReplyFixture,unittest.TestCase):
    artifact_path=False


class FormattedArtifactTests(FormattedLifecycleRisks,ArtifactSendFixture,unittest.TestCase):
    artifact_path=True
