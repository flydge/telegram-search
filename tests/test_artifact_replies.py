"""Bounded artifact replies, using production draft/provider/observation paths."""
import copy
import hashlib
import unittest
from test_text_replies import ReplyFixture, target, outgoing, CLIENT, ANCHOR
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.send_observations import SendObservations

POLICY = RuntimePolicy(enabled_capabilities=('send','reply_text_send','reply_artifact_send'))

class ArtifactReplyFixture(ReplyFixture):
    def setUp(self):
        super().setUp()
        self.broker._policy = POLICY
        path=self.root/'evidence.txt';path.write_bytes(b'bounded document')
        self.artifact=self.broker._artifact_store.store(path,kind='document')
    def prepare(self):
        result=self.dispatch('prepare_reply_artifact_send',dict(artifact_id=self.artifact.artifact_id,
            recipient=123,display_name='evidence.txt',mime_type='text/plain',caption='Caption',kind='document',reply_to=ANCHOR))
        self.assertEqual(result['status'],'prepared');return result['reply']

class ArtifactReplyTests(ArtifactReplyFixture, unittest.TestCase):
    def test_prepare_and_inspect_complete_snapshot_without_rawsend(self):
        preview=self.prepare()
        self.assertEqual(preview['draft']['sha256'],hashlib.sha256(b'bounded document').hexdigest())
        self.assertEqual(preview['draft']['caption'],'Caption')
        self.assertEqual(preview['reply_target']['anchor'],ANCHOR)
        self.assertEqual(self.dispatch('get_reply_artifact_draft',{'draft_id':preview['draft']['draft_id']})['reply'],preview)
        self.assertEqual(self.sends(),[])
    def test_wrong_caption_cannot_confirm_correlated_document_reply(self):
        observations=SendObservations()
        observations.register('a','e',1,123,'messageDocument',reply_anchor=(123,55),
                              caption_sha256=hashlib.sha256(b'Caption').hexdigest())
        value=outgoing(content=content_fixture('document','WRONG'),
            sending_state={'@type':'messageSendingStatePending','sending_id':1})
        observations.reduce(dict(value,**{'@extra':'e'}))
        observations.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':dict(value,id=701,sending_state=None)})
        self.assertNotEqual(observations.snapshot('a').status,'sent')
        self.assertIsNone(observations.snapshot('a').message_id)

import base64
import json
import time
from unittest.mock import patch
from pydantic import ValidationError
from telegram_search_mcp.reply_artifact_drafts import ReplyArtifactDraftPreview
from telegram_search_mcp.tdjson import MessageSendNotAttempted
from telegram_search_mcp.outgoing_stage import stage_approved_document, retire_staged_document
from telegram_search_mcp.reply_drafts import source_from_message
from test_text_replies import Raw, reply_meta, FOREIGN

def file_fixture():
    return {'@type':'file','id':1,'size':10,'expected_size':10,
        'local':{'@type':'localFile','path':'','can_be_downloaded':True,'can_be_deleted':False,
            'is_downloading_active':False,'is_downloading_completed':False,'download_offset':0,'downloaded_prefix_size':0,'downloaded_size':0},
        'remote':{'@type':'remoteFile','id':'remote','unique_id':'unique','is_uploading_active':False,'is_uploading_completed':True,'uploaded_size':10}}

def content_fixture(kind,caption='Caption',duration=2,waveform='AAAA'):
    fmt={'@type':'formattedText','text':caption,'entities':[]}
    if kind=='document':return {'@type':'messageDocument','caption':fmt,'document':{'@type':'document','file_name':'evidence.txt','mime_type':'text/plain','document':file_fixture()}}
    if kind=='photo':return {'@type':'messagePhoto','caption':fmt,'photo':{'@type':'photo','has_stickers':False,'sizes':[{'@type':'photoSize','type':'i','photo':file_fixture(),'width':2,'height':2,'progressive_sizes':[]}]},'show_caption_above_media':False,'has_spoiler':False,'is_secret':False}
    return {'@type':'messageVoiceNote','caption':fmt,'is_listened':False,'voice_note':{'@type':'voiceNote','duration':duration,'waveform':waveform,'mime_type':'audio/ogg','voice':file_fixture()}}

class ArtifactRaw(Raw):
    def send(self,request):
        if request['@type']!='sendMessage':return super().send(request)
        self.sent.append(copy.deepcopy(request))
        kind={'inputMessageDocument':'document','inputMessagePhoto':'photo','inputMessageVoiceNote':'voice_note'}[request['input_message_content']['@type']]
        inp=request['input_message_content'];voice=inp.get('voice_note',{})
        content=content_fixture(kind,inp['caption']['text'],voice.get('duration',2),voice.get('waveform','AAAA'))
        pre=outgoing(content=content,sending_state={'@type':'messageSendingStatePending','sending_id':request['options']['sending_id']})
        self.events.append(dict(pre,**{'@extra':request['@extra']}))
        final=outgoing(701,content=copy.deepcopy(content),sending_state=None)
        if self.mode=='caption':final['content']['caption']['text']='different'
        if self.mode=='dropped':final.pop('reply_to')
        if self.mode=='lost':self.fail_receive=True;return
        self.events.append({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':final})

class ArtifactSendFixture(ArtifactReplyFixture):
    def setUp(self):
        super().setUp()
        self.raw=ArtifactRaw();self.client._raw=self.raw
        manager=patch('telegram_search_mcp.tdjson.STAGING_ROOT',self.root/'stage');manager.start();self.addCleanup(manager.stop)
        self.stage_patch=patch('telegram_search_mcp.broker.stage_approved_document',side_effect=lambda claim:stage_approved_document(claim,root=self.root/'stage'))
        self.stage_patch.start();self.addCleanup(self.stage_patch.stop)
        self.retire_patch=patch('telegram_search_mcp.broker.retire_staged_document',side_effect=lambda path:retire_staged_document(path,root=self.root/'stage'))
        self.retire_patch.start();self.addCleanup(self.retire_patch.stop)
    def send(self,did):return self.dispatch('send_prepared_artifact',{'draft_id':did,'approved':True})

class ArtifactSendTests(ArtifactSendFixture, unittest.TestCase):
    def test_document_reply_full_approval_and_exact_wire(self):
        preview=self.prepare();did=preview['draft']['draft_id']
        self.assertEqual(self.dispatch('send_prepared_artifact',{'draft_id':did,'approved':False})['status'],'not_approved')
        self.assertEqual(self.sends(),[])
        self.assertEqual(self.send(did)['status'],'sent')
        self.assertEqual(self.approvals[0]['reply_artifact_preview'],preview)
        wire=self.sends()[0]
        self.assertEqual(wire['reply_to'],{'@type':'inputMessageReplyToMessage','message_id':55,'quote':None,'checklist_task_id':0,'poll_option_id':''})
        self.assertEqual(wire['input_message_content']['document']['disable_content_type_detection'],True)
        self.assertEqual(self.send(did)['status'],'sent');self.assertEqual(len(self.sends()),1)
    def test_independently_canonical_digest_and_nested_forges(self):
        preview=self.prepare();d=preview['draft'];t=preview['reply_target']
        expected={'domain':'telegram-search-mcp.reply-artifact','version':1,'kind':'document','draft':d,'reply_target':t,
            'topic_id':None,'outgoing_entities':[],'send_options':{'@type':'messageSendOptions','suggested_post_info':None,'disable_notification':False,'from_background':False,'protect_content':False,'allow_paid_broadcast':False,'paid_message_star_count':0,'update_order_of_installed_sticker_sets':False,'scheduling_state':None,'effect_id':0,'only_preview':False},'reply_markup':None,
            'reply_to':{'@type':'inputMessageReplyToMessage','message_id':55,'quote':None,'checklist_task_id':0,'poll_option_id':''},'content_options':{'thumbnail':None,'disable_content_type_detection':True}}
        self.assertEqual(preview['preview_sha256'],hashlib.sha256(json.dumps(expected,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest())
        for key,val in [('mime_type','other/type'),('caption','wrong'),('display_name','other.txt'),('recipient',124),('account_id',8),('expires_at','2040-01-01T00:00:00Z')]:
            bad=copy.deepcopy(preview);bad['draft'][key]=val
            with self.subTest(key=key),self.assertRaises(ValidationError):ReplyArtifactDraftPreview.model_validate(bad)
        for key,val in [('source_sha256','0'*64),('text','[untrusted Telegram evidence] Other'),('sanitized',True)]:
            bad=copy.deepcopy(preview);bad['reply_target'][key]=val
            with self.subTest(key=key),self.assertRaises(ValidationError):ReplyArtifactDraftPreview.model_validate(bad)
    def test_lifecycle_variants_and_foreign_owner_refuse(self):
        preview=self.prepare();did=preview['draft']['draft_id'];self.raw.source=target('edited')
        for op,extra in [('get_draft',{}),('get_reply_draft',{}),('update_draft',{'caption':'x'}),('update_reply_draft',{'text':'x'}),('refresh_draft',{}),('refresh_reply_draft',{})]:
            self.assertEqual(self.dispatch(op,dict(draft_id=did,**extra))['status'],'unavailable')
        self.assertEqual(self.dispatch('get_reply_artifact_draft',{'draft_id':did},client=FOREIGN)['status'],'unavailable')
        updated=self.dispatch('update_reply_artifact_draft',{'draft_id':did,'caption':''})['reply']
        self.assertEqual(updated['reply_target'],preview['reply_target']);self.assertEqual(updated['draft']['caption'],'')
        self.assertEqual(self.dispatch('get_reply_artifact_draft',{'draft_id':did})['status'],'unavailable')
        new=self.dispatch('refresh_reply_artifact_draft',{'draft_id':updated['draft']['draft_id']})['reply']
        self.assertNotEqual(new['reply_target']['source_sha256'],preview['reply_target']['source_sha256'])
        self.assertEqual(new['draft']['artifact_id'],preview['draft']['artifact_id'])
    def test_changed_source_properties_account_title_policy_no_send(self):
        for change in ['source','properties','account','title','policy','provider','epoch']:
            self.raw.source=target();self.raw.account=7;self.raw.title='Recipient';self.raw.can_reply=True;self.broker._policy=POLICY
            did=self.prepare()['draft']['draft_id']
            old_epoch=self.client.send_observation_epoch
            def approve(**kw):
                if change=='source':self.raw.source=target('edited')
                if change=='properties':self.raw.can_reply=False
                if change=='account':self.raw.account=8
                if change=='title':self.raw.title='Changed'
                if change=='policy':self.broker._policy=RuntimePolicy(enabled_capabilities=('send',))
                if change=='provider':self.broker._client=type(self.client)(raw=ArtifactRaw())
                if change=='epoch':self.client._send_observation_epoch=object()
                return True
            self.broker._approval_prompt=approve
            with self.subTest(change=change):self.assertNotEqual(self.send(did)['status'],'sent');self.assertEqual(self.sends(),[])
            self.client._send_observation_epoch=old_epoch
            self.broker._client=self.client
    def test_wrong_caption_dropped_reply_unknown_and_late_exact_recovery(self):
        for mode in ['caption','dropped']:
            self.raw.mode=mode;did=self.prepare()['draft']['draft_id']
            result=self.send(did);self.assertEqual(result['status'],'outcome_unknown');self.assertIsNone(result['message_id'])
            self.client._reduce_receive_event({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':outgoing(701,content=content_fixture('document'),sending_state=None)})
            status=self.dispatch('get_send_status',{'draft_id':did});self.assertEqual(status['status'],'sent');self.assertEqual(status['message_id'],701)
            self.assertEqual(self.send(did)['status'],'sent')
        self.assertEqual(len(self.sends()),2)
    def test_update_cancel_or_decline_during_dialog_no_send(self):
        for op in ['update_reply_artifact_draft','cancel_draft','decline']:
            did=self.prepare()['draft']['draft_id']
            def approve(**kw):
                if op=='decline':return False
                self.dispatch(op,dict(draft_id=did,**({'caption':'new'} if op.startswith('update') else {})));return True
            self.broker._approval_prompt=approve
            self.assertNotEqual(self.send(did)['status'],'sent');self.assertEqual(self.sends(),[])
    def test_guard_setup_registration_and_staging_deadlines_are_local_failed(self):
        for phase in ['guard','setup','registration','staging']:
            did=self.prepare()['draft']['draft_id']
            if phase=='guard':
                original=self.raw.send
                def slow(request):
                    if request['@type']=='getMessageProperties':time.sleep(.04)
                    return original(request)
                manager=patch.object(self.raw,'send',side_effect=slow)
            elif phase=='setup':
                original=self.client._send_observations.sending_id_available
                def slow(*args):time.sleep(.04);return original(*args)
                manager=patch.object(self.client._send_observations,'sending_id_available',side_effect=slow)
            elif phase=='registration':
                original=self.client._send_observations.register
                def slow(*args,**kw):original(*args,**kw);time.sleep(.04)
                manager=patch.object(self.client._send_observations,'register',side_effect=slow)
            else:
                def slow(claim):time.sleep(.04);return stage_approved_document(claim,root=self.root/'stage')
                manager=patch('telegram_search_mcp.broker.stage_approved_document',side_effect=slow)
            from telegram_search_mcp.schemas import SendPreparedArtifactRequest
            with manager:
                result=self.broker._send_prepared_artifact(SendPreparedArtifactRequest(draft_id=did,approved=True),client_id=CLIENT,deadline=time.monotonic()+.02)
            with self.subTest(phase=phase):
                self.assertEqual(result.status,'failed');self.assertEqual(self.sends(),[])
                self.assertIsNone(self.client._send_observations.snapshot(did))
                self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
    def test_registration_exhaustion_and_malformed_staged_payload_local_failure(self):
        for phase in ['capacity','payload']:
            did=self.prepare()['draft']['draft_id']
            if phase=='capacity':
                from telegram_search_mcp.send_observations import ObservationUnavailable
                manager=patch.object(self.client._send_observations,'register',side_effect=ObservationUnavailable('full'))
            else:manager=patch('telegram_search_mcp.broker.stage_approved_document',return_value=self.root/'missing')
            with manager:self.assertEqual(self.send(did)['status'],'failed')
            self.assertEqual(self.sends(),[]);self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
    def test_transport_exception_remains_unknown_and_no_retry(self):
        did=self.prepare()['draft']['draft_id'];original=self.raw.send
        def failing(request):
            original(request)
            if request['@type']=='sendMessage':raise OSError('lost')
        with patch.object(self.raw,'send',side_effect=failing):self.assertEqual(self.send(did)['status'],'outcome_unknown')
        self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertEqual(len(self.sends()),1)

class ArtifactObservationTests(unittest.TestCase):
    def observe(self,kind,content,*,listened=None):
        o=SendObservations();kw={'caption_sha256':hashlib.sha256(b'Caption').hexdigest()}
        if kind=='voice_note':kw.update(voice_duration=2,waveform_sha256=hashlib.sha256(base64.b64decode('AAAA')).hexdigest(),waveform_size=3)
        o.register('a','e',1,123,{'document':'messageDocument','photo':'messagePhoto','voice_note':'messageVoiceNote'}[kind],reply_anchor=(123,55),**kw)
        pre=outgoing(content=content,sending_state={'@type':'messageSendingStatePending','sending_id':1})
        o.reduce(dict(pre,**{'@extra':'e'}));o.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':dict(pre,id=701,sending_state=None)})
        return o.snapshot('a')
    def test_all_three_complete_shapes_confirm_and_only_content_free_expectations_retained(self):
        for kind in ['document','photo','voice_note']:
            result=self.observe(kind,content_fixture(kind));self.assertEqual(result.status,'sent')
            data=repr(result);self.assertNotIn('Caption',data);self.assertNotIn('remote',data);self.assertNotIn('AAAA',data)
        voice=content_fixture('voice_note');voice['is_listened']=True;self.assertEqual(self.observe('voice_note',voice).status,'sent')
    def test_nested_safety_and_bounded_vectors_fail_closed(self):
        for kind in ['document','photo','voice_note']:
            original=content_fixture(kind);bads=[]
            for text,entities in [('wrong',[]),('Caption',[{}]),('\ud800',[])]:
                bad=copy.deepcopy(original);bad['caption'].update(text=text,entities=entities);bads.append(bad)
            bad=copy.deepcopy(original);bad['extra']=None;bads.append(bad)
            inner={'document':'document','photo':'photo','voice_note':'voice_note'}[kind]
            bad=copy.deepcopy(original);bad[inner]['extra']=None;bads.append(bad)
            if kind=='photo':
                for field,value in [('has_stickers',True),('has_stickers',0),('sizes',[{}]*21),('sizes',[])]:
                    bad=copy.deepcopy(original);bad['photo'][field]=value;bads.append(bad)
                for field,value in [('show_caption_above_media',True),('has_spoiler',True),('is_secret',True),('video',{} )]:
                    bad=copy.deepcopy(original);bad[field]=value;bads.append(bad)
                bad=copy.deepcopy(original);del bad['photo']['has_stickers'];bads.append(bad)
                bad=copy.deepcopy(original);bad['photo']['sizes'][0]['progressive_sizes']=[1]*101;bads.append(bad)
            if kind=='voice_note':
                for field,value in [('duration',3),('waveform','AAAB'),('mime_type','audio/wav'),('speech_recognition_result',{})]:
                    bad=copy.deepcopy(original);bad['voice_note'][field]=value;bads.append(bad)
                bad=copy.deepcopy(original);bad['is_listened']=1;bads.append(bad)
            for mutate in [lambda f:f.update(id=True),lambda f:f['local'].update(path='x'*4097),lambda f:f['remote'].update(uploaded_size=-1)]:
                bad=copy.deepcopy(original);f=bad[inner]['document'] if kind=='document' else bad['photo']['sizes'][0]['photo'] if kind=='photo' else bad['voice_note']['voice'];mutate(f);bads.append(bad)
            for bad in bads:
                with self.subTest(kind=kind,bad=bad.get('@type')):
                    result=self.observe(kind,bad);self.assertNotEqual(result.status,'sent');self.assertIsNone(result.message_id)

class ArtifactMediaTests(ArtifactSendFixture, unittest.TestCase):
    def prepare_kind(self,kind):
        path=self.root/('photo.png' if kind=='photo' else 'audio.wav')
        if kind=='photo':
            from PIL import Image
            Image.new('RGB',(2,2),'white').save(path)
        else:path.write_bytes(b'synthetic audio source')
        artifact=self.broker._artifact_store.store(path,kind='media')
        self.input_artifact=artifact
        mime='image/png' if kind=='photo' else 'audio/wav'
        def convert(source,store,**kw):
            from telegram_search_mcp.outgoing_voice import VoicePrepared
            self.assertNotEqual(source.path,artifact.path)
            self.assertEqual(hashlib.sha256(source.path.read_bytes()).hexdigest(),artifact.sha256)
            converted=self.root/'converted.ogg';converted.write_bytes(b'OggS synthetic derivative')
            return VoicePrepared(store.store(converted,kind='media'),source.sha256,2,base64.b64encode(bytes(63)).decode(),True)
        with patch('telegram_search_mcp.outgoing_voice.prepare_voice_note',side_effect=convert):
            result=self.dispatch('prepare_reply_artifact_send',dict(artifact_id=artifact.artifact_id,recipient=123,
                display_name=path.name,mime_type=mime,caption='Caption',kind=kind,reply_to=ANCHOR))
        self.assertEqual(result['status'],'prepared');return result['reply']
    def test_photo_and_voice_prepare_digest_provenance_and_send_exact_shapes(self):
        for kind in ['photo','voice_note']:
            preview=self.prepare_kind(kind);did=preview['draft']['draft_id']
            self.assertEqual(self.send(did)['status'],'sent')
            wire=self.sends()[-1]['input_message_content']
            if kind=='photo':
                self.assertEqual(wire['photo']['added_sticker_file_ids'],[])
                self.assertFalse(wire['has_spoiler']);self.assertFalse(wire['show_caption_above_media'])
            else:
                d=preview['draft'];self.assertNotEqual(d['artifact_id'],self.input_artifact.artifact_id)
                self.assertEqual(d['source_sha256'],self.input_artifact.sha256)
                self.assertEqual((d['display_name'],d['source_display_name'],d['mime_type']),('audio.ogg','audio.wav','audio/ogg'))
                self.assertEqual(wire['voice_note']['waveform'],d['waveform_base64']);self.assertEqual(wire['voice_note']['duration'],2)
    def test_source_mutation_is_refused_or_converter_consumes_verified_snapshot(self):
        from telegram_search_mcp.outgoing_voice import prepare_reply_voice_note, VoicePrepared, VoiceError
        path=self.root/'source.wav';path.write_bytes(b'original source')
        artifact=self.broker._artifact_store.store(path,kind='media')
        artifact.path.write_bytes(b'changed! source')
        with patch('telegram_search_mcp.outgoing_voice.prepare_voice_note') as converter:
            with self.assertRaises(VoiceError):prepare_reply_voice_note(artifact,self.broker._artifact_store,input_mime='audio/wav',input_name='source.wav',budget_check=lambda:None)
            converter.assert_not_called()
        artifact.path.write_bytes(b'original source');snapshots=[]
        def converter(source,store,**kw):
            artifact.path.write_bytes(b'changed! source')
            self.assertEqual(source.path.read_bytes(),b'original source');snapshots.append(source.path)
            out=self.root/'derivative.ogg';out.write_bytes(b'OggS derivative')
            return VoicePrepared(store.store(out,kind='media'),source.sha256,2,base64.b64encode(bytes(63)).decode(),True)
        with patch('telegram_search_mcp.outgoing_voice.prepare_voice_note',side_effect=converter):
            result=prepare_reply_voice_note(artifact,self.broker._artifact_store,input_mime='audio/wav',input_name='source.wav',budget_check=lambda:None)
        self.assertEqual(result.source_sha256,hashlib.sha256(b'original source').hexdigest());self.assertFalse(snapshots[0].exists())
    def test_voice_source_snapshot_cleaned_after_conversion_failure_and_expiry(self):
        from telegram_search_mcp.outgoing_voice import prepare_reply_voice_note,VoiceError
        from dataclasses import replace
        seen=[]
        def converter(source,store,**kw):seen.append(source.path);raise VoiceError('synthetic conversion failed')
        with patch('telegram_search_mcp.outgoing_voice.prepare_voice_note',side_effect=converter):
            with self.assertRaises(VoiceError):prepare_reply_voice_note(self.artifact,self.broker._artifact_store,input_mime='audio/wav',input_name='source.wav',budget_check=lambda:None)
        self.assertFalse(seen[0].exists())
        with self.assertRaises(VoiceError):prepare_reply_voice_note(replace(self.artifact,expires_at=time.time()-1),self.broker._artifact_store,input_mime='audio/wav',input_name='source.wav',budget_check=lambda:None)
    def test_reply_voice_derivative_send_survives_input_expiry_and_revision_ttl_caps(self):
        preview=self.prepare_kind('voice_note');did=preview['draft']['draft_id']
        # Source expiry does not revoke an already verified live outgoing derivative.
        self.input_artifact.path.unlink()
        revised=self.dispatch('update_reply_artifact_draft',{'draft_id':did,'caption':'new'})['reply']
        outgoing=self.broker._artifact_store.lookup(revised['draft']['artifact_id'])
        from datetime import datetime
        self.assertLessEqual(datetime.fromisoformat(revised['draft']['expires_at']).timestamp(),outgoing.expires_at)
        self.assertEqual(self.send(revised['draft']['draft_id'])['status'],'sent')
    def test_photo_invalid_mime_or_bytes_refuses_and_artifact_mutation_cannot_stage(self):
        bad=self.dispatch('prepare_reply_artifact_send',dict(artifact_id=self.artifact.artifact_id,recipient=123,
            display_name='photo.png',mime_type='image/png',caption='',kind='photo',reply_to=ANCHOR))
        self.assertEqual(bad['status'],'unavailable');self.assertEqual(self.sends(),[])
        did=self.prepare()['draft']['draft_id']
        def approve(**kw):self.artifact.path.write_bytes(b'changed document');return True
        self.broker._approval_prompt=approve
        self.assertNotEqual(self.send(did)['status'],'sent');self.assertEqual(self.sends(),[])
    def test_policy_change_after_registration_refuses_before_raw_transport(self):
        did=self.prepare()['draft']['draft_id'];original=self.client._send_observations.register
        def register(*args,**kw):original(*args,**kw);self.broker._policy=RuntimePolicy(enabled_capabilities=('send',))
        with patch.object(self.client._send_observations,'register',side_effect=register):self.assertEqual(self.send(did)['status'],'failed')
        self.assertEqual(self.sends(),[]);self.assertIsNone(self.client._send_observations.snapshot(did))
    def test_direct_provider_expired_lock_budget_and_invalid_voice_payload_no_attempt(self):
        source=source_from_message(target(),123,55);self.client.ensure_ready()
        for kw in [{'kind':'document'},{'kind':'voice_note','duration_seconds':True,'waveform_base64':'AAAA'}]:
            with self.client.request_budget(time.monotonic()-1),self.assertRaises(MessageSendNotAttempted):
                self.client.send_reply_artifact_message(123,self.root/'missing','Caption',reply_source=source,
                    expected_account_id=7,expected_recipient_title='Recipient',attempt_id='invalid',pre_send_guard=lambda:True,**kw)
        self.assertEqual(self.sends(),[])

class ArtifactProxyTests(ArtifactReplyFixture, unittest.TestCase):
    def test_proxy_request_bindings_forged_prepare_get_and_revisions(self):
        from telegram_search_mcp.broker_client import BrokerClient
        from telegram_search_mcp.reply_artifact_drafts import (PrepareReplyArtifactSendRequest,GetReplyArtifactDraftRequest,
            UpdateReplyArtifactDraftRequest,RefreshReplyArtifactDraftRequest,preview_facts)
        from telegram_search_mcp.draft_models import DraftPreview
        from telegram_search_mcp.reply_drafts import ReplyTarget,digest,PREPARED,PENDING,REVISED
        proxy=BrokerClient(socket_path=self.root/'unused.sock',policy=POLICY,restart_callback=lambda:None);self.addCleanup(proxy.close)
        preview=self.prepare();did=preview['draft']['draft_id'];new=copy.deepcopy(preview);new['draft']['draft_id']='draft_'+'c'*32
        def rehash(reply):reply['preview_sha256']=digest(preview_facts(DraftPreview.model_validate(reply['draft']),ReplyTarget.model_validate(reply['reply_target'])))
        rehash(new)
        prep=PrepareReplyArtifactSendRequest(artifact_id=self.artifact.artifact_id,recipient=123,display_name='evidence.txt',mime_type='text/plain',caption='Caption',reply_to=ANCHOR)
        cases=[(proxy.prepare_reply_artifact_send,prep,dict(status='prepared',reply=preview,detail=PREPARED)),
            (proxy.get_reply_artifact_draft,GetReplyArtifactDraftRequest(draft_id=did),dict(status='pending',reply=preview,detail=PENDING)),
            (proxy.update_reply_artifact_draft,UpdateReplyArtifactDraftRequest(draft_id=did,caption='Caption'),dict(status='revised',previous_draft_id=did,reply=new,detail=REVISED)),
            (proxy.refresh_reply_artifact_draft,RefreshReplyArtifactDraftRequest(draft_id=did),dict(status='revised',previous_draft_id=did,reply=new,detail=REVISED))]
        for method,req,envelope in cases:
            with patch.object(proxy,'_request',return_value=envelope):self.assertIsNotNone(method(req).reply)
            bads=[]
            bad=copy.deepcopy(envelope);bad['reply']['reply_target']['source_sha256']='0'*64;bads.append(bad)
            if method==proxy.prepare_reply_artifact_send:
                for key,value in [('caption','other'),('recipient',124),('display_name','other.txt'),('mime_type','other/type')]:
                    bad=copy.deepcopy(envelope);bad['reply']['draft'][key]=value
                    if key=='recipient':bad['reply']['reply_target']['anchor']['chat_id']=124
                    rehash(bad['reply']);bads.append(bad)
            if method==proxy.get_reply_artifact_draft:
                bad=copy.deepcopy(envelope);bad['reply']['draft']['draft_id']='draft_'+'d'*32;rehash(bad['reply']);bads.append(bad)
            if method in [proxy.update_reply_artifact_draft,proxy.refresh_reply_artifact_draft]:
                bad=copy.deepcopy(envelope);bad['previous_draft_id']='draft_'+'d'*32;bads.append(bad)
            for bad in bads:
                with patch.object(proxy,'_request',return_value=bad):self.assertEqual(method(req).status,'unavailable')
    def test_sdk_four_tools_closed_inputs_and_send_capability_gate(self):
        import asyncio
        from mcp import Client
        from telegram_search_mcp.server import build_server
        reached=[]
        class Service:
            def close(self):pass
            def prepare_reply_artifact_send(self,request):reached.append(request);raise AssertionError('blocked input reached service')
            def update_reply_artifact_draft(self,request):reached.append(request);raise AssertionError('blocked input reached service')
        async def exercise():
            server=build_server(service_factory=Service,policy=POLICY)
            async with Client(server) as client:
                tools={t.name:t for t in (await client.list_tools()).tools}
                for name in ['prepare_reply_artifact_send','get_reply_artifact_draft','update_reply_artifact_draft','refresh_reply_artifact_draft']:
                    self.assertFalse(tools[name].input_schema['additionalProperties'])
                args=dict(artifact_id=self.artifact.artifact_id,recipient=123,display_name='evidence.txt',mime_type='text/plain',caption='Caption',reply_to=dict(ANCHOR,hidden=0))
                self.assertTrue((await client.call_tool('prepare_reply_artifact_send',args)).is_error)
                self.assertTrue((await client.call_tool('update_reply_artifact_draft',{'draft_id':'draft_'+'a'*32,'caption':'x','text':'hidden'})).is_error)
            server=build_server(service_factory=Service,policy=RuntimePolicy(enabled_capabilities=('reply_artifact_send',)))
            async with Client(server) as client:
                args['reply_to']=ANCHOR
                self.assertTrue((await client.call_tool('prepare_reply_artifact_send',args)).is_error)
            self.assertEqual(reached,[])
        asyncio.run(exercise())

class ArtifactFinalGuardTests(ArtifactSendFixture, unittest.TestCase):
    def test_dedicated_provider_absence_never_falls_back_to_ordinary(self):
        for unavailable in [None,False]:
            did=self.prepare()['draft']['draft_id']
            with patch.object(self.client,'send_reply_artifact_message',unavailable),patch.object(self.client,'send_document_message') as ordinary:
                self.assertEqual(self.send(did)['status'],'failed');ordinary.assert_not_called()
            self.assertEqual(self.sends(),[])
    def test_initial_provider_read_and_dialog_use_inherited_deadline(self):
        from telegram_search_mcp.schemas import SendPreparedArtifactRequest
        for phase in ['initial-read','dialog']:
            did=self.prepare()['draft']['draft_id']
            if phase=='initial-read':
                original=self.raw.send
                def delay(req):
                    if req['@type']=='getMe':time.sleep(.04)
                    original(req)
                manager=patch.object(self.raw,'send',side_effect=delay)
            else:
                def delay(**kw):time.sleep(.04);return True
                manager=patch.object(self.broker,'_approval_prompt',side_effect=delay)
            with manager:
                response=self.broker._send_prepared_artifact(SendPreparedArtifactRequest(draft_id=did,approved=True),client_id=CLIENT,deadline=time.monotonic()+.02)
            self.assertNotEqual(response.status,'sent');self.assertEqual(self.sends(),[])
            self.assertIsNone(self.client._send_observations.snapshot(did))
    def test_expired_outgoing_artifact_and_draft_cannot_be_revived(self):
        did=self.prepare()['draft']['draft_id'];oldclock=self.broker._drafts._clock
        self.broker._drafts._clock=lambda:oldclock()+3601
        for op,extra in [('get_reply_artifact_draft',{}),('update_reply_artifact_draft',{'caption':'x'}),('refresh_reply_artifact_draft',{})]:
            self.assertEqual(self.dispatch(op,dict(draft_id=did,**extra))['status'],'unavailable')
        self.assertNotEqual(self.send(did)['status'],'sent');self.assertEqual(self.sends(),[])
    def test_definite_provider_failure_receipt_stays_failed_after_conflicting_late_success(self):
        did=self.prepare()['draft']['draft_id'];original=self.raw.send
        def send(req):
            if req['@type']=='sendMessage':
                self.raw.sent.append(copy.deepcopy(req));self.raw.events.append({'@type':'error','code':400,'message':'safe synthetic refusal','@extra':req['@extra']})
            else:original(req)
        with patch.object(self.raw,'send',side_effect=send):self.assertEqual(self.send(did)['status'],'failed')
        self.client._reduce_receive_event({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':outgoing(701,content=content_fixture('document'),sending_state=None)})
        self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(len(self.sends()),1)
    def test_thumbnail_nested_optional_objects_are_typed_and_fail_closed(self):
        fixture=content_fixture('document');fixture['document']['thumbnail']={'@type':'thumbnail','format':{'@type':'thumbnailFormatJpeg'},'width':1,'height':1,'file':file_fixture()}
        fixture['document']['minithumbnail']={'@type':'minithumbnail','width':1,'height':1,'data':'AAAA'}
        helper=ArtifactObservationTests();self.assertEqual(helper.observe('document',fixture).status,'sent')
        for mutate in [lambda c:c['document']['thumbnail']['format'].update({'@type':[]}),
                       lambda c:c['document']['thumbnail'].update(width=0),
                       lambda c:c['document']['minithumbnail'].update(data='!'*8),
                       lambda c:c['document']['thumbnail']['file']['local'].update(hidden=True)]:
            bad=copy.deepcopy(fixture);mutate(bad);self.assertNotEqual(helper.observe('document',bad).status,'sent')

class ArtifactAdmissionTests(ArtifactReplyFixture, unittest.TestCase):
    def test_invalid_utf8_and_extra_fields_cannot_create_draft(self):
        from telegram_search_mcp.reply_artifact_drafts import PrepareReplyArtifactSendRequest,UpdateReplyArtifactDraftRequest
        args=dict(artifact_id=self.artifact.artifact_id,recipient=123,display_name='evidence.txt',mime_type='text/plain',kind='document',reply_to=ANCHOR)
        with self.assertRaises(ValidationError):self.dispatch('prepare_reply_artifact_send',dict(args,caption='\ud800'))
        self.assertEqual(self.broker._drafts.known_ids(),set())
        for extra in [{'text':'x'},{'topic_id':None},{'approved':True}]:
            with self.assertRaises(ValidationError):PrepareReplyArtifactSendRequest.model_validate(dict(args,**extra))
        with self.assertRaises(ValidationError):UpdateReplyArtifactDraftRequest(draft_id='draft_'+'a'*32)
    def test_local_artifact_hashing_crossing_budget_refuses_initial_or_replacement_admission(self):
        original=self.broker._artifact_store.lookup;calls=[0]
        def slow_prepare(*args,**kw):
            calls[0]+=1
            result=original(*args,**kw)
            if calls[0]==2:time.sleep(.04)
            return result
        args=dict(artifact_id=self.artifact.artifact_id,recipient=123,display_name='evidence.txt',mime_type='text/plain',caption='Caption',kind='document',reply_to=ANCHOR)
        with patch.object(self.broker._artifact_store,'lookup',side_effect=slow_prepare):
            result=self.broker._dispatch({'operation':'prepare_reply_artifact_send','payload':args,'client_id':CLIENT,'deadline':time.monotonic()+.02,'broker_generation':self.broker._generation})
        self.assertEqual(result['status'],'unavailable');self.assertEqual(self.broker._drafts.known_ids(),set())
        preview=self.prepare();did=preview['draft']['draft_id'];calls[0]=0
        with patch.object(self.broker._artifact_store,'lookup',side_effect=slow_prepare):
            result=self.broker._dispatch({'operation':'update_reply_artifact_draft','payload':{'draft_id':did,'caption':'changed'},'client_id':CLIENT,'deadline':time.monotonic()+.02,'broker_generation':self.broker._generation})
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(self.dispatch('get_reply_artifact_draft',{'draft_id':did})['reply'],preview)

class ArtifactReviewRegressionTests(ArtifactSendFixture, unittest.TestCase):
    def test_330_second_outer_cap_survives_staging_with_longer_caller_budget(self):
        """No sleeps: time consumed by staging cannot renew the330-second cap."""
        from telegram_search_mcp.schemas import SendPreparedArtifactRequest
        did=self.prepare()['draft']['draft_id'];tick=[1000.0];budgets=[]
        def staging(claim):
            budgets.append(self.client._request_context.deadline)
            path=stage_approved_document(claim,root=self.root/'stage')
            tick[0]=1331.0
            return path
        with patch('telegram_search_mcp.broker.time.monotonic',side_effect=lambda:tick[0]),patch('telegram_search_mcp.broker.stage_approved_document',side_effect=staging):
            result=self.broker._send_prepared_artifact(SendPreparedArtifactRequest(draft_id=did,approved=True),client_id=CLIENT,deadline=1570.0)
        self.assertEqual(budgets,[1330.0])
        self.assertEqual(result.status,'failed')
        self.assertEqual(self.sends(),[])
        self.assertIsNone(self.client._send_observations.snapshot(did))
        self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
        self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(self.sends(),[])
    def test_real_staging_configuration_failures_finish_claim_as_local_failed(self):
        """Private real symlink/non-directory stage roots raise ConfigurationError."""
        for kind in ['symlink','file']:
            with self.subTest(kind=kind):
                did=self.prepare()['draft']['draft_id'];stage=self.root/('unsafe-'+kind)
                if kind=='symlink':stage.symlink_to(self.root,target_is_directory=True)
                else:stage.write_bytes(b'not a directory')
                with patch('telegram_search_mcp.broker.stage_approved_document',side_effect=lambda claim:stage_approved_document(claim,root=stage)):
                    result=self.send(did)
                self.assertEqual(result['status'],'failed')
                self.assertEqual(self.sends(),[])
                self.assertIsNone(self.client._send_observations.snapshot(did))
                self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')
                self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(self.sends(),[])
