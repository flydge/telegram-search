"""Media source risks through production parser, broker, provider and reducers."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from test_text_replies import ReplyFixture, target, ANCHOR, CLIENT, FOREIGN
from test_artifact_replies import content_fixture, file_fixture, ArtifactSendFixture
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message, EVIDENCE_MARKER

MEDIA_POLICY = RuntimePolicy(enabled_capabilities=('send','reply_text_send','reply_artifact_send','reply_media_targets'))

class MediaReplyFixture(ReplyFixture):
    def setUp(self):
        super().setUp();self.broker._policy=MEDIA_POLICY
        self.raw.source=target(content=content_fixture('document','Exact caption'))

class MediaTextTests(MediaReplyFixture, unittest.TestCase):
    def test_document_target_prepares_complete_evidence_without_send(self):
        result=self.dispatch('prepare_reply_text_send',{'recipient':123,'text':'Final reply','reply_to':ANCHOR})
        self.assertEqual(result['status'],'prepared')
        self.assertIn('Exact caption',result['reply']['reply_target']['text'])
        self.assertEqual(self.sends(),[])
    def test_replaced_identity_after_owner_decision_is_local_failed(self):
        did=self.prepare()['draft']['draft_id']
        def approve(**kw):
            self.raw.source['content']['document']['document']['remote']['unique_id']='replaced';return True
        self.broker._approval_prompt=approve
        self.assertEqual(self.send(did)['status'],'failed')
        self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
        self.assertEqual(self.sends(),[])

IDENTITY='c2720445a45267813688ff73fa188aa060c1b661aefaf1650d42f690697b5ab3'

def expected_projection(kind,caption='Exact caption'):
    facts={'document':{'file_name':'evidence.txt','mime_type':'text/plain','size':10,'unique_id_sha256':IDENTITY},
           'photo':{'variants':[{'type':'i','width':2,'height':2,'size':10,'unique_id_sha256':IDENTITY}]},
           'voice_note':{'duration':2,'mime_type':'audio/ogg','size':10,'unique_id_sha256':IDENTITY}}[kind]
    return {'version':2,'anchor':{'chat_id':123,'message_id':55},'message':{
        '@type':'message','chat_id':123,'id':55,'sender_id':{'@type':'messageSenderUser','user_id':8},
        'is_outgoing':False,'is_from_offline':False,'ephemeral_message_id':0,'date':1700000000,'edit_date':0,
        'self_destruct_in':0.0,'auto_delete_in':0.0,'sending_state':None,'scheduling_state':None,'topic_id':None,
        'self_destruct_type':None,'ephemeral_content':None,'receiver_id':None,'reply_to':None,'forward_info':None,
        'import_info':None,'reply_markup':None,'content':{'kind':kind,'caption':{'@type':'formattedText','text':caption,'entities':[]},'media':facts}}}

def serialized(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)

def media_source(kind='document',caption='Exact caption'):
    return source_from_message(target(content=content_fixture(kind,caption)),123,55)

class MediaSourceTests(unittest.TestCase):
    def test_exact_closed_projection_all_kinds_and_constructor(self):
        for kind in ('document','photo','voice_note'):
            with self.subTest(kind=kind):
                source=media_source(kind)
                expected=serialized(expected_projection(kind))
                self.assertEqual(source.projection_json,expected)
                self.assertEqual(source.source_sha256,hashlib.sha256(expected.encode()).hexdigest())
                self.assertTrue(source.is_media)
                self.assertEqual(ReplySource(expected),source)
                display=source.target().text
                self.assertIn(IDENTITY,display);self.assertIn('"caption":"Exact caption"',display)
                self.assertNotIn('remote',display);self.assertNotIn('local',display)
    def test_v1_canonical_bytes_remain_unchanged(self):
        expected=expected_projection('document');expected['version']=1
        expected['message']['content']={'@type':'messageText','text':{'@type':'formattedText','text':'Source full text','entities':[]},'link_preview':None,'link_preview_options':None}
        source=source_from_message(target(),123,55)
        self.assertEqual(source.projection_json,serialized(expected));self.assertFalse(source.is_media)
        self.assertEqual(source.target().text,EVIDENCE_MARKER+'Source full text')
    def test_empty_captions_raw_normalization_and_delimited_filename(self):
        for kind in ('document','photo','voice_note'):
            self.assertIn('"caption":""',media_source(kind,'').target().text)
        raw=target(content=content_fixture('document','Ａ\n  B'))
        raw['content']['document']['file_name']='name\ncaption: "forged".txt'
        source=source_from_message(raw,123,55);display=source.target()
        self.assertTrue(display.sanitized);self.assertIn('"caption":"A B"',display.text)
        self.assertIn('name caption: \\"forged\\".txt',display.text)
        raw['content']['caption']['text']='A B'
        self.assertNotEqual(source.source_sha256,source_from_message(raw,123,55).source_sha256)
    def test_raw_identity_and_metadata_drift_change_hash_volatile_state_does_not(self):
        for kind in ('document','photo','voice_note'):
            raw=target(content=content_fixture(kind));original=source_from_message(raw,123,55)
            key={'document':'document','photo':'photo','voice_note':'voice_note'}[kind]
            main=raw['content'][key]
            f=main['sizes'][0]['photo'] if kind=='photo' else main['document' if kind=='document' else 'voice']
            f['id']=200;f['expected_size']=123;f['local']['path']='/synthetic/cache';f['local']['downloaded_size']=2
            f['remote']['id']='changed';f['remote']['uploaded_size']=4
            if kind=='voice_note':raw['content']['is_listened']=True;main['waveform']=''
            if kind=='photo':main['sizes'][0]['progressive_sizes']=[1,9]
            self.assertEqual(original.source_sha256,source_from_message(raw,123,55).source_sha256)
            f['remote']['unique_id']='changed'
            self.assertNotEqual(original.source_sha256,source_from_message(raw,123,55).source_sha256)
        for field,value in [('file_name','new.txt'),('mime_type','application/octet-stream')]:
            raw=target(content=content_fixture('document'));raw['content']['document'][field]=value
            self.assertNotEqual(media_source('document','Caption').source_sha256,source_from_message(raw,123,55).source_sha256)
        for kind,key,value in [('photo','width',3),('voice_note','duration',3)]:
            raw=target(content=content_fixture(kind));obj=raw['content']['photo']['sizes'][0] if kind=='photo' else raw['content']['voice_note'];obj[key]=value
            self.assertNotEqual(media_source(kind,'Caption').source_sha256,source_from_message(raw,123,55).source_sha256)
    def test_photo_permutations_and_duplicates_use_exact_key(self):
        raw=target(content=content_fixture('photo'));a=raw['content']['photo']['sizes'][0];b=copy.deepcopy(a);b['type']='z';b['width']=3
        raw['content']['photo']['sizes']=[b,a];first=source_from_message(raw,123,55)
        raw['content']['photo']['sizes']=[a,b];self.assertEqual(first.source_sha256,source_from_message(raw,123,55).source_sha256)
        b=copy.deepcopy(a);b['photo']['size']=11;raw['content']['photo']['sizes']=[a,b]
        with self.assertRaises(ValueError):source_from_message(raw,123,55)
    def test_fail_closed_source_shapes_identity_bounds_and_display_expansion(self):
        mutations=[lambda r:r['content'].update(extra=1),lambda r:r['content']['caption'].update(entities=[{}]),
            lambda r:r['content']['caption'].update(text='x'*1025),lambda r:r['content']['caption'].update(text='bad\ud800'),
            lambda r:r['content']['caption'].update(text='bad\u202e'),lambda r:r['content']['caption'].update(text='ﷺ'*1024),
            lambda r:r['content']['document']['document']['remote'].update(unique_id=''),
            lambda r:r['content']['document']['document'].update(size=0),lambda r:r['content']['document']['document'].update(id=True),
            lambda r:r['content']['document']['document']['local'].update(extra=1),lambda r:r['content']['document'].update(file_name='é'*128),
            lambda r:r['content']['document'].update(mime_type='x'*128),lambda r:r.pop('auto_delete_in'),
            lambda r:r.update(date=True),lambda r:r.update(sender_id={'@type':'messageSenderUser','user_id':False})]
        for mutate in mutations:
            raw=target(content=content_fixture('document'));mutate(raw)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):source_from_message(raw,123,55)
        for key in ('has_spoiler','is_secret','show_caption_above_media','video'):
            raw=target(content=content_fixture('photo'));raw['content'][key]=True
            with self.subTest(key=key),self.assertRaises(ValueError):source_from_message(raw,123,55)
        for count in (0,21):
            raw=target(content=content_fixture('photo'));raw['content']['photo']['sizes']*=count
            with self.assertRaises(ValueError):source_from_message(raw,123,55)
        for value in ([0]*101,[2**31],[-1],[True]):
            raw=target(content=content_fixture('photo'));raw['content']['photo']['sizes'][0]['progressive_sizes']=value
            with self.assertRaises(ValueError):source_from_message(raw,123,55)
        for duration,wave,mime in [(0,'','audio/ogg'),(601,'','audio/ogg'),(True,'','audio/ogg'),(2,'!','audio/ogg'),(2,'AAAA'*34,'audio/ogg'),(2,'','audio/mpeg')]:
            raw=target(content=content_fixture('voice_note',duration=duration,waveform=wave));raw['content']['voice_note']['mime_type']=mime
            with self.assertRaises(ValueError):source_from_message(raw,123,55)
        raw=target(content=content_fixture('voice_note',waveform=''));source_from_message(raw,123,55)
    def test_thumbnail_closed_shapes_bounded_blobs_and_zero_sizes(self):
        raw=target(content=content_fixture('document'));doc=raw['content']['document']
        thumb={'@type':'thumbnail','format':{'@type':'thumbnailFormatJpeg'},'width':1,'height':1,'file':file_fixture()};thumb['file']['size']=0
        doc['thumbnail']=thumb;doc['minithumbnail']={'@type':'minithumbnail','width':1,'height':1,'data':''}
        self.assertEqual(source_from_message(raw,123,55).source_sha256,media_source('document','Caption').source_sha256)
        for mutate in [lambda d:d['thumbnail']['format'].update(extra=True),lambda d:d['thumbnail'].update(width=0),lambda d:d['minithumbnail'].update(data='!'*8),lambda d:d['minithumbnail'].update(data='AAAA'*21846)]:
            bad=copy.deepcopy(raw);mutate(bad['content']['document'])
            with self.assertRaises(ValueError):source_from_message(bad,123,55)
    def test_constructor_rejects_forged_closed_projections_before_parsing_or_display(self):
        for mutate in [lambda v:v.update(extra=1),lambda v:v.update(version=True),lambda v:v['anchor'].update(message_id=True),
            lambda v:v['message'].update(extra=1),lambda v:v['message']['content'].update(kind='video'),
            lambda v:v['message']['content']['media'].update(size=True),lambda v:v['message']['content']['media'].update(unique_id_sha256='A'*64),
            lambda v:v['message']['content']['media'].update(file_id=1),lambda v:v['message']['content']['caption'].update(entities=[{}])]:
            value=expected_projection('document');mutate(value)
            with self.assertRaises(ValueError):ReplySource(serialized(value))
        with self.assertRaises(ValueError):ReplySource(' '*65537)
        with self.assertRaises(ValueError):ReplySource(json.dumps(expected_projection('document')))
        raw=target(content=content_fixture('document'));source=source_from_message(raw,123,55);raw['content']['document']['file_name']='mutated'
        self.assertEqual(source.projection_json,serialized(expected_projection('document','Caption')))

class MediaLifecycleTests(MediaReplyFixture, unittest.TestCase):
    def test_conditional_capability_off_pending_and_terminal_replay_status_send_only(self):
        from telegram_search_mcp.contract import CompatibilityError
        for mode in ('sent','wrong'):
            self.broker._policy=MEDIA_POLICY;self.raw.mode=mode;did=self.prepare()['draft']['draft_id']
            self.assertEqual(self.send(did)['status'],'sent' if mode=='sent' else 'outcome_unknown')
            self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_text_send'))
            self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['status'],'sent' if mode=='sent' else 'outcome_unknown')
            self.assertEqual(self.send(did)['status'],'expired')
        self.broker._policy=MEDIA_POLICY;did=self.prepare()['draft']['draft_id']
        self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_text_send'))
        for op,extra in [('get_reply_draft',{}),('update_reply_draft',{'text':'new'}),('refresh_reply_draft',{})]:
            self.assertEqual(self.dispatch(op,dict(draft_id=did,**extra))['status'],'unavailable')
        self.assertEqual(self.dispatch('prepare_reply_text_send',{'recipient':123,'text':'x','reply_to':ANCHOR})['status'],'unavailable')
        self.raw.source=target();self.assertEqual(self.dispatch('prepare_reply_text_send',{'recipient':123,'text':'x','reply_to':ANCHOR})['status'],'prepared')
    def test_refresh_checks_old_and_new_media_and_revision_ownership(self):
        did=self.prepare()['draft']['draft_id']
        self.assertEqual(self.dispatch('get_reply_draft',{'draft_id':did},client=FOREIGN)['status'],'unavailable')
        old=self.dispatch('get_reply_draft',{'draft_id':did})['reply']
        revised=self.dispatch('update_reply_draft',{'draft_id':did,'text':'New reply'})['reply']
        self.assertEqual(old['reply_target'],revised['reply_target']);self.assertNotEqual(did,revised['draft']['draft_id'])
        self.raw.source=target();self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_text_send'))
        rid=revised['draft']['draft_id'];self.assertEqual(self.dispatch('refresh_reply_draft',{'draft_id':rid})['status'],'unavailable')
        self.broker._policy=MEDIA_POLICY;plain=self.dispatch('refresh_reply_draft',{'draft_id':rid})['reply']
        self.assertEqual(plain['reply_target']['text'],EVIDENCE_MARKER+'Source full text')
        self.raw.source=target(content=content_fixture('photo'));self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_text_send'))
        pid=plain['draft']['draft_id'];self.assertEqual(self.dispatch('refresh_reply_draft',{'draft_id':pid})['status'],'unavailable')
        self.broker._policy=MEDIA_POLICY;self.assertEqual(self.dispatch('refresh_reply_draft',{'draft_id':pid})['status'],'revised')
    def test_all_media_kinds_send_exact_text_with_volatile_mutations_and_no_retry(self):
        for kind in ('document','photo','voice_note'):
            self.raw.source=target(content=content_fixture(kind));did=self.prepare()['draft']['draft_id']
            self.assertEqual(self.dispatch('send_prepared_text',{'draft_id':did,'approved':False})['status'],'not_approved')
            self.assertEqual(len(self.sends()),('document','photo','voice_note').index(kind))
            if kind=='voice_note':self.raw.source['content']['is_listened']=True
            self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(self.send(did)['status'],'sent')
            self.assertEqual(self.sends()[-1]['reply_to']['message_id'],55)
        self.assertEqual(len(self.sends()),3)
    def test_media_revocation_or_raising_guard_after_registration_discards_observation(self):
        for mode in ('revoke','raise'):
            self.broker._policy=MEDIA_POLICY;did=self.prepare()['draft']['draft_id']
            original=self.client._send_observations.register
            def register(*args,**kw):
                original(*args,**kw)
                if mode=='revoke':self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_text_send'))
                else:self.broker._policy=RuntimePolicy(enabled_capabilities=MEDIA_POLICY.enabled_capabilities,source_path=self.root/'missing.toml')
            with patch.object(self.client._send_observations,'register',side_effect=register):
                self.assertEqual(self.send(did)['status'],'failed')
            self.assertEqual(self.sends(),[])
            self.broker._policy=MEDIA_POLICY
            self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
            self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(self.sends(),[])

class MediaArtifactTests(ArtifactSendFixture, unittest.TestCase):
    def setUp(self):super().setUp();self.broker._policy=MEDIA_POLICY
    def test_each_media_target_document_reply_owner_evidence_and_single_send(self):
        for kind in ('document','photo','voice_note'):
            self.raw.source=target(content=content_fixture(kind,''));preview=self.prepare();did=preview['draft']['draft_id']
            self.assertIn('"caption":""',preview['reply_target']['text'])
            self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(self.send(did)['status'],'sent')
            self.assertEqual(self.approvals[-1]['reply_artifact_preview'],preview)
        self.assertEqual(len(self.sends()),3)
    def test_media_receipt_replay_and_pending_inspection_require_optin(self):
        for mode in ('sent','caption'):
            self.broker._policy=MEDIA_POLICY;self.raw.mode=mode;self.raw.source=target(content=content_fixture('document'))
            did=self.prepare()['draft']['draft_id'];self.send(did)
            self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_artifact_send'))
            self.assertEqual(self.send(did)['status'],'expired')
            self.assertIn(self.dispatch('get_send_status',{'draft_id':did})['status'],('sent','outcome_unknown'))
        self.broker._policy=MEDIA_POLICY;did=self.prepare()['draft']['draft_id']
        self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_artifact_send'))
        for op,extra in [('get_reply_artifact_draft',{}),('update_reply_artifact_draft',{'caption':'new'}),('refresh_reply_artifact_draft',{})]:
            self.assertEqual(self.dispatch(op,dict(draft_id=did,**extra))['status'],'unavailable')
    def test_artifact_revocation_and_raising_guard_after_registration_no_transport(self):
        for mode in ('revoke','raise'):
            self.broker._policy=MEDIA_POLICY;self.raw.source=target(content=content_fixture('document'));did=self.prepare()['draft']['draft_id']
            original=self.client._send_observations.register
            def register(*args,**kw):
                original(*args,**kw)
                if mode=='revoke':self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_artifact_send'))
                else:self.broker._policy=RuntimePolicy(enabled_capabilities=MEDIA_POLICY.enabled_capabilities,source_path=self.root/'missing.toml')
            with patch.object(self.client._send_observations,'register',side_effect=register):self.assertEqual(self.send(did)['status'],'failed')
            self.broker._policy=MEDIA_POLICY;self.assertEqual(self.sends(),[])
            self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
            self.assertIsNone(self.client._send_observations.snapshot(did))
            self.assertEqual(self.send(did)['status'],'failed')
    def test_target_replacement_during_approval_refuses_artifact_send(self):
        self.raw.source=target(content=content_fixture('document'));did=self.prepare()['draft']['draft_id']
        def approve(**kw):self.raw.source['content']['document']['document']['remote']['unique_id']='new';return True
        self.broker._approval_prompt=approve
        self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(self.sends(),[])

class AdditionalMediaSourceTests(unittest.TestCase):
    def test_full_media_display_overflow_and_constructor_photo_forgery_refuse(self):
        raw=target(content=content_fixture('photo','x'*1024));sizes=[]
        for i in range(20):
            size=copy.deepcopy(raw['content']['photo']['sizes'][0]);size['type']=f'{i:016}';size['width']=16384;size['height']=16384;size['photo']['size']=2**53-1;sizes.append(size)
        raw['content']['photo']['sizes']=sizes
        with self.assertRaises(ValueError):source_from_message(raw,123,55)
        for mutate in [lambda v:v['message']['content']['media']['variants'].append(copy.deepcopy(v['message']['content']['media']['variants'][0])),
            lambda v:v['message']['content']['media']['variants'][0].update(width=True),
            lambda v:v['message']['content']['media']['variants'][0].update(size=0),
            lambda v:v['message']['content']['media']['variants'][0].update(unique_id_sha256='short')]:
            value=expected_projection('photo');mutate(value)
            with self.assertRaises(ValueError):ReplySource(serialized(value))
    def test_all_required_safety_scalars_and_shell_drift(self):
        source=media_source('document','Caption')
        for key in ('is_outgoing','is_from_offline','ephemeral_message_id','date','edit_date','self_destruct_in','auto_delete_in','sender_id'):
            raw=target(content=content_fixture('document'));raw.pop(key)
            with self.subTest(key=key),self.assertRaises(ValueError):source_from_message(raw,123,55)
        for key,val in [('date',1700000001),('edit_date',1),('is_outgoing',True),('sender_id',{'@type':'messageSenderChat','chat_id':124})]:
            raw=target(content=content_fixture('document'));raw[key]=val
            self.assertNotEqual(source.source_sha256,source_from_message(raw,123,55).source_sha256)
        for key in ('sending_state','scheduling_state','topic_id','self_destruct_type','ephemeral_content','receiver_id','reply_to','forward_info','import_info','reply_markup'):
            raw=target(content=content_fixture('document'));raw[key]={}
            with self.subTest(key=key),self.assertRaises(ValueError):source_from_message(raw,123,55)

class AdditionalMediaTextTests(MediaReplyFixture, unittest.TestCase):
    def test_owner_dialog_properties_account_provider_epoch_policy_guards(self):
        from telegram_search_mcp.tdjson import TDLibClient
        from test_text_replies import Raw
        for change in ('properties','account','title','provider','epoch','policy'):
            self.raw.can_reply=True;self.raw.account=7;self.raw.title='Recipient';self.broker._policy=MEDIA_POLICY
            self.broker._client=self.client;epoch=self.client.send_observation_epoch;did=self.prepare()['draft']['draft_id']
            def approve(**kw):
                if change=='properties':self.raw.can_reply=False
                if change=='account':self.raw.account=8
                if change=='title':self.raw.title='Changed'
                if change=='provider':self.broker._client=TDLibClient(raw=Raw())
                if change=='epoch':self.client._send_observation_epoch=object()
                if change=='policy':self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_text_send'))
                return True
            self.broker._approval_prompt=approve
            with self.subTest(change=change):self.assertNotEqual(self.send(did)['status'],'sent');self.assertEqual(self.sends(),[])
            self.client._send_observation_epoch=epoch
        self.broker._client=self.client
    def test_media_registration_deadline_cleanup_and_unknown_late_recovery(self):
        import time
        from telegram_search_mcp.schemas import SendPreparedTextRequest
        from test_text_replies import outgoing
        did=self.prepare()['draft']['draft_id'];original=self.client._send_observations.register
        def register(*a,**kw):original(*a,**kw);time.sleep(.04)
        with patch.object(self.client._send_observations,'register',side_effect=register):
            result=self.broker._send_prepared_text(SendPreparedTextRequest(draft_id=did,approved=True),client_id=CLIENT,deadline=time.monotonic()+.02)
        self.assertEqual(result.status,'failed');self.assertEqual(self.sends(),[])
        self.assertIsNone(self.client._send_observations.snapshot(did))
        self.raw.mode='wrong';did=self.prepare()['draft']['draft_id'];self.assertEqual(self.send(did)['status'],'outcome_unknown')
        self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertEqual(len(self.sends()),1)
        self.client._reduce_receive_event({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':outgoing(701,sending_state=None)})
        self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['status'],'sent');self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(len(self.sends()),1)

class MalformedMediaProjectionTests(unittest.TestCase):
    def test_malformed_sender_type_and_deep_projection_are_value_errors(self):
        raw=target(content=content_fixture('document'));raw['sender_id']['@type']=[]
        with self.assertRaises(ValueError):source_from_message(raw,123,55)
        value=expected_projection('document');value['message']['sender_id']['@type']=[]
        with self.assertRaises(ValueError):ReplySource(serialized(value))
        with self.assertRaises(ValueError):ReplySource('['*1100+'0'+']'*1100)

class AdditionalMediaArtifactTests(ArtifactSendFixture, unittest.TestCase):
    def setUp(self):super().setUp();self.broker._policy=MEDIA_POLICY;self.raw.source=target(content=content_fixture('document'))
    def test_artifact_refresh_both_source_types_and_revision_binding(self):
        preview=self.prepare();did=preview['draft']['draft_id']
        self.assertEqual(self.dispatch('get_reply_artifact_draft',{'draft_id':did},client=FOREIGN)['status'],'unavailable')
        update=self.dispatch('update_reply_artifact_draft',{'draft_id':did,'caption':'New caption'})['reply']
        self.assertEqual(update['reply_target'],preview['reply_target']);self.assertNotEqual(update['draft']['draft_id'],did)
        self.assertEqual(self.send(did)['status'],'expired')
        self.raw.source=target();self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_artifact_send'))
        new=update['draft']['draft_id'];self.assertEqual(self.dispatch('refresh_reply_artifact_draft',{'draft_id':new})['status'],'unavailable')
        self.broker._policy=MEDIA_POLICY;plain=self.dispatch('refresh_reply_artifact_draft',{'draft_id':new})['reply']
        self.assertEqual(plain['draft']['sha256'],preview['draft']['sha256'])
        self.raw.source=target(content=content_fixture('voice_note'));self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_artifact_send'))
        pid=plain['draft']['draft_id'];self.assertEqual(self.dispatch('refresh_reply_artifact_draft',{'draft_id':pid})['status'],'unavailable')
        self.broker._policy=MEDIA_POLICY;self.assertEqual(self.dispatch('refresh_reply_artifact_draft',{'draft_id':pid})['status'],'revised')
    def test_failed_media_receipt_replay_requires_capability_on_both_paths(self):
        for op in ('send_prepared_artifact','send_prepared_text'):
            self.raw.source=target(content=content_fixture('document'));self.broker._policy=MEDIA_POLICY
            if op.endswith('artifact'):did=self.prepare()['draft']['draft_id']
            else:did=self.dispatch('prepare_reply_text_send',{'recipient':123,'text':'Final reply','reply_to':ANCHOR})['reply']['draft']['draft_id']
            def approve(**kw):self.raw.can_reply=False;return True
            self.broker._approval_prompt=approve
            self.assertEqual(self.dispatch(op,{'draft_id':did,'approved':True})['status'],'failed')
            self.broker._policy=RuntimePolicy(enabled_capabilities=('send','reply_text_send','reply_artifact_send'))
            self.assertEqual(self.dispatch(op,{'draft_id':did,'approved':True})['status'],'expired')
            self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed');self.assertEqual(self.sends(),[])
            self.raw.can_reply=True

class MediaPolicyInventoryTests(unittest.TestCase):
    def test_trusted_policy_accepts_media_optin_without_enabling_it_by_default(self):
        import tempfile
        from pathlib import Path
        from telegram_search_mcp.config import load_runtime_policy
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'runtime.toml'
            self.assertEqual(load_runtime_policy(path).enabled_capabilities,('artifacts','read','send'))
            path.write_text('config_version=1\nenabled_capabilities=["send","reply_text_send","reply_media_targets"]\n');path.chmod(0o600)
            policy=load_runtime_policy(path)
            self.assertIn('reply_media_targets',policy.enabled_capabilities)

class MediaArtifactLateEvidenceTests(ArtifactSendFixture, unittest.TestCase):
    def test_unknown_media_target_artifact_recovers_exact_late_evidence_without_resend(self):
        from test_text_replies import outgoing
        self.broker._policy=MEDIA_POLICY;self.raw.source=target(content=content_fixture('photo'));self.raw.mode='caption'
        did=self.prepare()['draft']['draft_id'];self.assertEqual(self.send(did)['status'],'outcome_unknown')
        self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertEqual(len(self.sends()),1)
        self.client._reduce_receive_event({'@type':'updateMessageSendSucceeded','old_message_id':-10,
            'message':outgoing(701,content=content_fixture('document'),sending_state=None)})
        self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['status'],'sent')
        self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(len(self.sends()),1)

class MediaReplyExpiryTests(MediaReplyFixture, unittest.TestCase):
    def test_pending_media_query_rejects_exact_expiry_without_pruning(self):
        from telegram_search_mcp.outgoing_drafts import DraftOwner, DraftError
        did=self.prepare()['draft']['draft_id'];registry=self.broker._drafts;owner=DraftOwner(CLIENT,7)
        expires=registry.peek(did,owner=owner).expires_at
        with patch.object(registry,'_clock',return_value=expires-.001):
            self.assertTrue(registry.is_media_reply(did,owner=owner))
        with patch.object(registry,'_clock',return_value=expires):
            with self.assertRaises(DraftError):registry.is_media_reply(did,owner=owner)
            self.assertEqual(self.send(did)['status'],'expired');self.assertEqual(self.sends(),[])
    def test_terminal_media_receipts_reject_exact_attempt_retention_boundary(self):
        from telegram_search_mcp.outgoing_drafts import DraftOwner, DraftError
        registry=self.broker._drafts;owner=DraftOwner(CLIENT,7)
        for mode,expected in [('sent','sent'),('wrong','outcome_unknown'),('properties','failed')]:
            self.raw.mode='sent' if mode=='properties' else mode;self.raw.can_reply=mode!='properties'
            with patch.object(registry,'_attempt_clock',return_value=100):
                did=self.prepare()['draft']['draft_id'];self.assertEqual(self.send(did)['status'],expected)
            count=len(self.sends());self.broker._policy=MEDIA_POLICY
            with patch.object(registry,'_attempt_clock',return_value=999.999):
                self.assertTrue(registry.is_media_reply(did,owner=owner))
                self.assertEqual(self.send(did)['status'],expected)
            with patch.object(registry,'_attempt_clock',return_value=1000):
                with self.assertRaises(DraftError):registry.is_media_reply(did,owner=owner)
                self.assertEqual(self.send(did)['status'],'expired')
                self.broker._policy=RuntimePolicy(enabled_capabilities=('send',))
                status=self.dispatch('get_send_status',{'draft_id':did})
                self.assertEqual(status['evidence'],'none');self.assertEqual(len(self.sends()),count)
            self.broker._policy=MEDIA_POLICY
        self.raw.can_reply=True
    def test_nonmedia_discriminator_preserves_existing_expired_query_behavior(self):
        from telegram_search_mcp.outgoing_drafts import DraftOwner
        self.raw.source=target();did=self.prepare()['draft']['draft_id'];registry=self.broker._drafts;owner=DraftOwner(CLIENT,7)
        expires=registry.peek(did,owner=owner).expires_at
        with patch.object(registry,'_clock',return_value=expires):self.assertFalse(registry.is_media_reply(did,owner=owner))
        with patch.object(registry,'_clock',return_value=expires-1),patch.object(registry,'_attempt_clock',return_value=100):
            self.assertEqual(self.send(did)['status'],'sent')
        with patch.object(registry,'_attempt_clock',return_value=1000):
            self.assertFalse(registry.is_media_reply(did,owner=owner))
            self.assertEqual(self.send(did)['status'],'sent')

class MediaArtifactExpiryTests(ArtifactSendFixture, unittest.TestCase):
    def setUp(self):super().setUp();self.broker._policy=MEDIA_POLICY;self.raw.source=target(content=content_fixture('photo'))
    def test_pending_media_artifact_replay_refuses_exact_wall_expiry(self):
        from telegram_search_mcp.outgoing_drafts import DraftOwner, DraftError
        did=self.prepare()['draft']['draft_id'];registry=self.broker._drafts;owner=DraftOwner(CLIENT,7)
        expires=registry.peek(did,owner=owner).expires_at
        with patch.object(registry,'_clock',return_value=expires-.001):self.assertTrue(registry.is_media_reply(did,owner=owner))
        with patch.object(registry,'_clock',return_value=expires):
            with self.assertRaises(DraftError):registry.is_media_reply(did,owner=owner)
            self.assertEqual(self.send(did)['status'],'expired');self.assertEqual(self.sends(),[])
    def test_terminal_media_artifact_replay_uses_attempt_retention_at_exact_boundary(self):
        from telegram_search_mcp.outgoing_drafts import DraftOwner, DraftError
        registry=self.broker._drafts;owner=DraftOwner(CLIENT,7)
        for mode,expected in [('sent','sent'),('caption','outcome_unknown'),('properties','failed')]:
            self.raw.mode='sent' if mode=='properties' else mode;self.raw.can_reply=mode!='properties'
            with patch.object(registry,'_attempt_clock',return_value=100):
                did=self.prepare()['draft']['draft_id'];self.assertEqual(self.send(did)['status'],expected)
            count=len(self.sends())
            with patch.object(registry,'_clock',return_value=10**10),patch.object(registry,'_attempt_clock',return_value=999.999):
                self.assertTrue(registry.is_media_reply(did,owner=owner));self.assertEqual(self.send(did)['status'],expected)
            with patch.object(registry,'_attempt_clock',return_value=1000):
                with self.assertRaises(DraftError):registry.is_media_reply(did,owner=owner)
                self.assertEqual(self.send(did)['status'],'expired');self.assertEqual(len(self.sends()),count)
                self.broker._policy=RuntimePolicy(enabled_capabilities=('send',))
                self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'none')
            self.broker._policy=MEDIA_POLICY
        self.raw.can_reply=True
