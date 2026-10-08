"""An exact reply must survive review, revalidation and provider correlation.

Synthetic raw transport only: all draft, broker and TDLib reducers are production.
"""
from __future__ import annotations
import copy
import hashlib
import inspect
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path

from pydantic import ValidationError
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import OPERATION_CAPABILITIES, CompatibilityError
from telegram_search_mcp.send_observations import SendObservations
from telegram_search_mcp.tdjson import TDLibClient

CLIENT = 'client_' + 'a' * 24
FOREIGN = 'client_' + 'b' * 24
ANCHOR = {'chat_id': 123, 'message_id': 55}
POLICY = RuntimePolicy(enabled_capabilities=('send', 'reply_text_send'))


def target(text='Source full text', **changes):
    return {'@type':'message', 'chat_id':123, 'id':55,
        'sender_id':{'@type':'messageSenderUser','user_id':8},
        'is_outgoing':False, 'is_from_offline':False,'ephemeral_message_id':0,'date':1700000000, 'edit_date':0,
        'self_destruct_in':0.0, 'auto_delete_in':0.0,
        'content':{'@type':'messageText','text':{'@type':'formattedText','text':text,'entities':[]}},
        **changes}


def reply_meta(**changes):
    return {'@type':'messageReplyToMessage','chat_id':123,'message_id':55,
            'checklist_task_id':0,'poll_option_id':'','origin_send_date':0,**changes}


def outgoing(identifier=-10, **changes):
    return {'@type':'message','id':identifier,'chat_id':123,'is_outgoing':True,
        'content':{'@type':'messageText','text':{'@type':'formattedText','text':'Final reply','entities':[]}},
        'reply_to':reply_meta(),**changes}


class Raw:
    def __init__(self):
        self.sent=[]; self.events=deque(); self.source=target(); self.account=7
        self.can_reply=True; self.mode='sent'; self.fail_receive=False; self.title='Recipient'
    def send(self, request):
        self.sent.append(copy.deepcopy(request)); kind=request['@type']
        if kind=='getAuthorizationState':value={'@type':'authorizationStateReady'}
        elif kind=='getMe':value={'@type':'user','id':self.account}
        elif kind=='getChat':value={'@type':'chat','id':request['chat_id'],'title':self.title,'type':{'@type':'chatTypePrivate'}}
        elif kind=='getMessage':value=copy.deepcopy(self.source)
        elif kind=='getMessageProperties':value={'@type':'messageProperties','can_be_replied':self.can_reply}
        elif kind=='sendMessage':
            preliminary=outgoing(sending_state={'@type':'messageSendingStatePending','sending_id':request['options']['sending_id']})
            if self.mode=='dropped_preliminary':preliminary.pop('reply_to')
            self.events.append({**preliminary,'@extra':request['@extra']})
            final=outgoing(701,sending_state=None)
            if self.mode=='dropped':final.pop('reply_to')
            elif self.mode=='wrong':final['reply_to']['message_id']=56
            elif self.mode=='lost':self.fail_receive=True; return
            self.events.append({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':final}); return
        else:raise AssertionError(kind)
        self.events.append({**value,'@extra':request['@extra']})
    def receive(self, timeout):
        if self.events:return self.events.popleft()
        if self.fail_receive:self.fail_receive=False;raise OSError('synthetic loss')
        return None
    def close(self):pass


class ReplyFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.raw=Raw();self.client=TDLibClient(raw=self.raw)
        self.approvals=[]
        self.broker=Broker(socket_path=self.root/'broker.sock',artifact_store=ArtifactStore(cache_dir=self.root/'cache'),
            client_factory=lambda:self.client,policy=POLICY,approval_prompt=lambda **kw:self.approvals.append(kw) or True)
        self.addCleanup(self.broker.shutdown)
    def dispatch(self, op, payload, client=CLIENT):
        self.assertIn(op, OPERATION_CAPABILITIES, 'reply operation must be public')
        return self.broker._dispatch({'operation':op,'payload':payload,'client_id':client,
            'deadline':time.monotonic()+10,'broker_generation':self.broker._generation})
    def prepare(self):
        result=self.dispatch('prepare_reply_text_send',{'recipient':123,'text':'Final reply','reply_to':ANCHOR})
        self.assertEqual(result['status'],'prepared');return result['reply']
    def send(self, did):return self.dispatch('send_prepared_text',{'draft_id':did,'approved':True})
    def sends(self):return [x for x in self.raw.sent if x['@type']=='sendMessage']


class ReplyTests(ReplyFixture, unittest.TestCase):
    def test_prepare_full_snapshot_is_immutable_and_never_sends(self):
        preview=self.prepare();did=preview['draft']['draft_id']
        self.assertEqual(preview['reply_target']['anchor'],ANCHOR)
        self.assertEqual(preview['reply_target']['text'],'[untrusted Telegram evidence] Source full text')
        self.assertFalse(preview['reply_target']['truncated']);self.assertFalse(preview['reply_target']['sanitized'])
        self.raw.source=target('Edited after preparation')
        got=self.dispatch('get_reply_draft',{'draft_id':did})
        self.assertEqual(got['reply'],preview);self.assertEqual(self.sends(),[])
        self.assertEqual(self.dispatch('get_draft',{'draft_id':did})['status'],'unavailable')
        self.assertEqual(self.dispatch('update_draft',{'draft_id':did,'text':'other'})['status'],'unavailable')
        self.assertEqual(self.dispatch('refresh_draft',{'draft_id':did})['status'],'unavailable')
    def test_source_digest_distinguishes_raw_equivalent_display(self):
        self.raw.source=target('Ａ  B');a=self.prepare()['reply_target']
        self.raw.source=target('A B');b=self.prepare()['reply_target']
        self.assertEqual(a['text'],b['text']);self.assertNotEqual(a['source_sha256'],b['source_sha256'])
        self.assertTrue(a['sanitized']);self.assertFalse(b['sanitized'])
    def test_update_preserves_snapshot_refresh_rehydrates_and_invalidates_old_ids(self):
        old=self.prepare();old_id=old['draft']['draft_id'];self.raw.source=target('new source')
        update=self.dispatch('update_reply_draft',{'draft_id':old_id,'text':'New text'})
        self.assertEqual(update['status'],'revised');self.assertEqual(update['previous_draft_id'],old_id)
        new=update['reply'];self.assertEqual(new['reply_target'],old['reply_target'])
        self.assertEqual(new['draft']['text'],'New text');self.assertNotEqual(new['preview_sha256'],old['preview_sha256'])
        self.assertEqual(self.dispatch('get_reply_draft',{'draft_id':old_id})['status'],'unavailable')
        refreshed=self.dispatch('refresh_reply_draft',{'draft_id':new['draft']['draft_id']})['reply']
        self.assertEqual(refreshed['reply_target']['anchor'],ANCHOR)
        self.assertEqual(refreshed['reply_target']['text'],'[untrusted Telegram evidence] new source')
        self.assertEqual(self.sends(),[])
    def test_anchor_and_new_tool_inputs_are_strict_closed(self):
        for anchor in [dict(ANCHOR,message_id=True),dict(ANCHOR,message_id='55'),dict(ANCHOR,message_id=0),dict(ANCHOR,message_id=2**53),dict(ANCHOR,extra=1)]:
            with self.subTest(anchor=anchor), self.assertRaises(ValidationError):
                self.dispatch('prepare_reply_text_send',{'recipient':123,'text':'x','reply_to':anchor})
        denied=self.dispatch('prepare_reply_text_send',{'recipient':124,'text':'x','reply_to':ANCHOR})
        self.assertEqual(denied['status'],'unavailable');self.assertIsNone(denied['reply'])
    def test_unsupported_missing_or_malformed_target_never_prepares(self):
        bads=[target(topic_id={'@type':'messageTopicForum','forum_topic_id':1}),target(sending_state={}),
              target(scheduling_state={}),target(reply_to=reply_meta()),target(forward_info={}),target(import_info={}),
              target(ephemeral_content={}),target(self_destruct_type={}),target(auto_delete_in=1),target(self_destruct_in=True),
              target(is_outgoing=1),target(date=True),target(edit_date=-1),target(sender_id={'@type':'messageSenderUser','user_id':True}),
              target('x'*4097),target('bad\u202e'),target('bad\ud800'),target('\ufdfa'*4096),
              target(content={'@type':'messagePhoto'}),target(content={'@type':'messageText','text':{'@type':'formattedText','text':'x','entities':[{}]}})]
        missing=target();missing.pop('auto_delete_in');bads.append(missing)
        for bad in bads:
            with self.subTest(bad=repr(bad)[:100]):
                self.raw.source=bad
                result=self.dispatch('prepare_reply_text_send',{'recipient':123,'text':'x','reply_to':ANCHOR})
                self.assertEqual(result,{'status':'unavailable','reply':None,'detail':'reply draft is unavailable'})
        self.assertEqual(self.sends(),[])
    def test_postapproval_source_drift_is_local_failure_without_raw_send(self):
        for change in [target('changed'),target('Source full text ',edit_date=1),target(topic_id={}),{'@type':'error','code':404,'message':'SECRET'}]:
            with self.subTest(change=repr(change)[:100]):
                self.raw.source=target();preview=self.prepare();did=preview['draft']['draft_id']
                def approve(**kw):self.raw.source=change;return True
                self.broker._approval_prompt=approve
                result=self.send(did);self.assertEqual(result['status'],'failed');self.assertEqual(self.sends(),[])
                status=self.dispatch('get_send_status',{'draft_id':did})
                self.assertEqual(status['evidence'],'local_failed');self.assertIsNone(status['message_id'])
    def test_eligibility_requires_strict_true_after_owner_dialog(self):
        for value in [False,1,'true',None]:
            self.raw.source=target();self.raw.can_reply=True;did=self.prepare()['draft']['draft_id']
            def approve(**kw):self.raw.can_reply=value;return True
            self.broker._approval_prompt=approve
            self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(self.sends(),[])
    def test_reply_owner_prompt_covers_complete_target_and_hashes_and_one_exact_send(self):
        preview=self.prepare();did=preview['draft']['draft_id'];result=self.send(did)
        self.assertEqual((result['status'],result['message_id']),('sent',701))
        request=self.sends()[0]
        self.assertEqual(request['reply_to'],{'@type':'inputMessageReplyToMessage','message_id':55,'quote':None,'checklist_task_id':0,'poll_option_id':''})
        self.assertIsNone(request['topic_id']);self.assertEqual(request['chat_id'],123)
        self.assertEqual(self.approvals[0]['reply_preview'],preview)
        self.assertEqual(self.send(did)['message_id'],701);self.assertEqual(len(self.sends()),1)
    def test_silent_drop_wrong_final_and_late_confirmation_never_retry(self):
        for mode in ['dropped','wrong','dropped_preliminary']:
            with self.subTest(mode=mode):
                self.raw.mode=mode;did=self.prepare()['draft']['draft_id'];count=len(self.sends())
                result=self.send(did);self.assertEqual(result['status'],'outcome_unknown');self.assertIsNone(result['message_id'])
                self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertEqual(len(self.sends()),count+1)
                status=self.dispatch('get_send_status',{'draft_id':did});self.assertNotEqual(status['status'],'sent');self.assertIsNone(status['message_id'])
                if mode!='dropped_preliminary':
                    self.raw.events.append({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':outgoing(702,sending_state=None)})
                    status=self.dispatch('get_send_status',{'draft_id':did});self.assertEqual((status['status'],status['message_id']),('sent',702))
    def test_foreign_and_ordinary_drafts_cannot_be_inspected_as_replies(self):
        did=self.prepare()['draft']['draft_id']
        foreign=self.dispatch('get_reply_draft',{'draft_id':did},FOREIGN)
        self.assertEqual(foreign['status'],'unavailable');self.assertIsNone(foreign['reply'])
        plain=self.dispatch('prepare_text_send',{'recipient':123,'text':'plain'})['draft_id']
        for op,extra in [('get_reply_draft',{}),('update_reply_draft',{'text':'x'}),('refresh_reply_draft',{})]:
            self.assertEqual(self.dispatch(op,{'draft_id':plain,**extra})['status'],'unavailable')
    def test_reply_capability_is_required_before_and_after_owner_dialog(self):
        did=self.prepare()['draft']['draft_id'];self.broker._policy=RuntimePolicy(enabled_capabilities=('send',))
        result=self.send(did);self.assertNotEqual(result['status'],'sent');self.assertEqual(self.sends(),[]);self.assertEqual(self.approvals,[])
        self.broker._policy=POLICY
        def approve(**kw):self.broker._policy=RuntimePolicy(enabled_capabilities=('send',));return True
        self.broker._approval_prompt=approve
        self.assertNotEqual(self.send(did)['status'],'sent');self.assertEqual(self.sends(),[])


class ReplyObservationRiskTests(unittest.TestCase):
    def test_dropped_final_reply_is_never_sent_even_with_exact_send_correlation(self):
        observations=SendObservations()
        kwargs={'text_sha256':hashlib.sha256(b'Final reply').hexdigest()}
        if 'reply_anchor' in inspect.signature(observations.register).parameters:kwargs['reply_anchor']=(123,55)
        observations.register('attempt','extra',1,123,'messageText',**kwargs)
        observations.reduce(outgoing(**{'@extra':'extra','sending_state':{'@type':'messageSendingStatePending','sending_id':1}}))
        final=outgoing(701,sending_state=None);final.pop('reply_to')
        observations.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':final})
        self.assertNotEqual(observations.snapshot('attempt').status,'sent','silent dropped reply is not success')

class ReplyBoundaryChecks(ReplyFixture, unittest.TestCase):
    # Each test protects an additional public boundary or observed local race.
    def test_emitted_ephemeral_and_offline_safety_scalars_cannot_be_missing_or_coerced(self):
        bads=[]
        for key,values in [('ephemeral_message_id',[None,True,1,'0']),('is_from_offline',[None,0,'false'])]:
            for value in values:
                bad=target()
                if value is None:bad.pop(key,None)
                else:bad[key]=value
                bads.append(bad)
        for bad in bads:
            self.raw.source=bad
            result=self.dispatch('prepare_reply_text_send',{'recipient':123,'text':'Final reply','reply_to':ANCHOR})
            self.assertEqual(result['status'],'unavailable');self.assertIsNone(result['reply'])
        self.assertEqual(self.sends(),[])

    def test_missing_reply_provider_never_falls_back_to_plain_send(self):
        did=self.prepare()['draft']['draft_id'];self.client.send_reply_text_message=None
        result=self.send(did);self.assertEqual(result['status'],'failed');self.assertEqual(self.sends(),[])
        self.assertEqual(self.dispatch('get_send_status',{'draft_id':did})['evidence'],'local_failed')

    def test_exact_terminal_failure_cannot_be_weakened_by_late_success(self):
        did=self.prepare()['draft']['draft_id'];original=self.raw.send
        def send(request):
            original(request)
            if request['@type']=='sendMessage':
                error={'@type':'error','code':400,'message':'SYNTHETIC SECRET'}
                self.raw.events[-1]={'@type':'updateMessageSendFailed','old_message_id':-10,'error':error,
                    'message':outgoing(sending_state={'@type':'messageSendingStateFailed','error':error})}
        self.raw.send=send
        self.assertEqual(self.send(did)['status'],'failed')
        self.raw.events.append({'@type':'updateMessageSendSucceeded','old_message_id':-10,'message':outgoing(702,sending_state=None)})
        status=self.dispatch('get_send_status',{'draft_id':did})
        self.assertEqual(status['status'],'failed');self.assertIsNone(status['message_id'])
        self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(len(self.sends()),1)
        self.assertNotIn('SYNTHETIC SECRET',str(status))

    def test_final_guard_rechecks_capability_and_provider_epoch_after_properties(self):
        original=self.raw.send
        for drift in ['capability','epoch','account']:
            self.broker._policy=POLICY;self.raw.account=7;did=self.prepare()['draft']['draft_id']
            def send(request):
                original(request)
                if request['@type']=='getMessageProperties':
                    if drift=='capability':self.broker._policy=RuntimePolicy(enabled_capabilities=('send',))
                    elif drift=='epoch':self.client._send_observation_epoch=object()
                    else:self.raw.account=8
            self.raw.send=send
            result=self.send(did)
            self.assertEqual(result['status'],'failed');self.assertEqual(self.sends(),[])
            self.raw.send=original
    def test_link_preview_options_reject_hidden_custom_url_and_hash_null_vs_disabled(self):
        from telegram_search_mcp.reply_drafts import LINK_OPTIONS
        self.raw.source=target();null=self.prepare()
        self.raw.source['content']['link_preview_options']=dict(LINK_OPTIONS);disabled=self.prepare()
        self.assertNotEqual(null['reply_target']['source_sha256'],disabled['reply_target']['source_sha256'])
        for change in [{'url':'https://example.invalid/hidden'},{'is_disabled':1},{'force_large_media':True},{'extra':0}]:
            self.raw.source=target();self.raw.source['content']['link_preview_options']={**LINK_OPTIONS,**change}
            denied=self.dispatch('prepare_reply_text_send',{'recipient':123,'text':'x','reply_to':ANCHOR})
            self.assertEqual(denied['status'],'unavailable');self.assertIsNone(denied['reply'])
    def test_full_preview_digest_rejects_every_review_fact_tamper(self):
        from telegram_search_mcp.reply_drafts import ReplyDraftPreview
        preview=self.prepare()
        cases=[]
        for field,value in [('draft_id','draft_'+'b'*32),('account_id',9),('recipient_title','Other title'),('expires_at','2099-01-01T00:00:00Z')]:
            bad=copy.deepcopy(preview);bad['draft'][field]=value;cases.append(bad)
        for field,value in [('text','[untrusted Telegram evidence] altered'),('sanitized',True),('source_sha256','0'*64),('truncated',True)]:
            bad=copy.deepcopy(preview);bad['reply_target'][field]=value;cases.append(bad)
        bad=copy.deepcopy(preview);bad['reply_target']['anchor']['message_id']=56;cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad),self.assertRaises(ValidationError):ReplyDraftPreview.model_validate(bad)
    def test_reply_preview_rejects_unnormalized_or_control_outgoing_text_even_with_fresh_digest(self):
        from telegram_search_mcp.reply_drafts import ReplyDraftPreview, digest, preview_facts, ReplyTarget
        from telegram_search_mcp.draft_models import DraftPreview
        preview=self.prepare()
        for value in ['Ｆｉｎａｌ reply','Final\u202ereply','Final\rreply']:
            bad=copy.deepcopy(preview);data=value.encode('utf-8')
            bad['draft'].update(text=value,sha256=hashlib.sha256(data).hexdigest(),size_bytes=len(data))
            bad['preview_sha256']=digest(preview_facts(DraftPreview.model_validate(bad['draft']),ReplyTarget.model_validate(bad['reply_target'])))
            with self.subTest(value=value),self.assertRaises(ValidationError):ReplyDraftPreview.model_validate(bad)
    def test_malformed_output_reply_metadata_never_confirms_or_exposes_id(self):
        for change in [{'quote':{}},{'origin':{}},{'content':{}},{'origin_send_date':True},{'checklist_task_id':False},
                       {'poll_option_id':1},{'chat_id':True},{'message_id':'55'},{'extra':None}]:
            with self.subTest(change=change):
                did=self.prepare()['draft']['draft_id'];original=self.raw.send
                def send(request):
                    original(request)
                    if request['@type']=='sendMessage':self.raw.events[-1]['message']['reply_to'].update(change)
                self.raw.send=send
                result=self.send(did);self.assertEqual(result['status'],'outcome_unknown');self.assertIsNone(result['message_id'])
                self.raw.send=original
    def test_final_hidden_link_preview_url_is_not_plain_reply_evidence(self):
        from telegram_search_mcp.reply_drafts import LINK_OPTIONS
        did=self.prepare()['draft']['draft_id'];original=self.raw.send
        def send(request):
            original(request)
            if request['@type']=='sendMessage':
                self.raw.events[-1]['message']['content']['link_preview_options']={**LINK_OPTIONS,'url':'https://example.invalid/hidden'}
        self.raw.send=send
        result=self.send(did);self.assertEqual(result['status'],'outcome_unknown');self.assertIsNone(result['message_id'])

    def test_final_formatted_entities_are_not_plain_text_reply_evidence(self):
        did=self.prepare()['draft']['draft_id'];original=self.raw.send
        def send(request):
            original(request)
            if request['@type']=='sendMessage':self.raw.events[-1]['message']['content']['text']['entities']=[{'@type':'textEntity'}]
        self.raw.send=send
        result=self.send(did);self.assertEqual(result['status'],'outcome_unknown');self.assertIsNone(result['message_id'])
    def test_update_or_cancel_during_approval_wins_without_send(self):
        for op,extra in [('update_reply_draft',{'text':'changed'}),('cancel_draft',{})]:
            did=self.prepare()['draft']['draft_id']
            def approve(**kw):self.dispatch(op,{'draft_id':did,**extra});return True
            self.broker._approval_prompt=approve
            self.assertNotEqual(self.send(did)['status'],'sent');self.assertEqual(self.sends(),[])
    def test_prompt_decline_retains_pending_snapshot_and_never_sends(self):
        preview=self.prepare();did=preview['draft']['draft_id'];self.broker._approval_prompt=lambda **kw:False
        self.assertEqual(self.send(did)['status'],'not_approved');self.assertEqual(self.sends(),[])
        self.assertEqual(self.dispatch('get_reply_draft',{'draft_id':did})['reply'],preview)


class ReplyProxyAndSDKTests(unittest.TestCase):
    def test_sdk_rejects_reply_when_send_capability_absent_before_service(self):
        import asyncio
        from mcp import Client
        from telegram_search_mcp.server import build_server
        reached=[]
        class Service:
            def close(self):pass
            def prepare_reply_text_send(self, request):reached.append(request);raise AssertionError('disabled send reached service')
        server=build_server(service_factory=Service,policy=RuntimePolicy(enabled_capabilities=('reply_text_send',)))
        async def exercise():
            async with Client(server) as client:
                result=await client.call_tool('prepare_reply_text_send',{'recipient':123,'text':'x','reply_to':ANCHOR})
                self.assertTrue(result.is_error);self.assertEqual(reached,[])
        asyncio.run(exercise())
    def test_sdk_closed_inputs_reject_invalid_anchor_and_unknown_fields_before_service(self):
        import asyncio
        from mcp import Client
        from telegram_search_mcp.server import build_server
        reached=[]
        class Service:
            def close(self):pass
            def prepare_reply_text_send(self, request):reached.append(request);raise AssertionError('invalid reached service')
            def update_reply_draft(self, request):reached.append(request);raise AssertionError('invalid reached service')
            def get_reply_draft(self, request):reached.append(request);raise AssertionError('invalid reached service')
            def refresh_reply_draft(self, request):reached.append(request);raise AssertionError('invalid reached service')
        server=build_server(service_factory=Service,policy=POLICY)
        async def exercise():
            async with Client(server) as client:
                tools={t.name:t for t in (await client.list_tools()).tools}
                for name in ['prepare_reply_text_send','get_reply_draft','update_reply_draft','refresh_reply_draft']:
                    self.assertFalse(tools[name].input_schema['additionalProperties'])
                bads=[('prepare_reply_text_send',{'recipient':123,'text':'x','reply_to':dict(ANCHOR,message_id=True)}),
                    ('prepare_reply_text_send',{'recipient':123,'text':'x','reply_to':dict(ANCHOR,extra=0)}),
                    ('update_reply_draft',{'draft_id':'draft_'+'a'*32,'text':'x','reply_to':ANCHOR}),
                    ('get_reply_draft',{'draft_id':'draft_'+'a'*32,'approved':True}),
                    ('refresh_reply_draft',{'draft_id':'draft_'+'a'*32,'text':'x'})]
                for name,arguments in bads:
                    result=await client.call_tool(name,arguments);self.assertTrue(result.is_error);self.assertEqual(reached,[])
        asyncio.run(exercise())

class ReplyIPCAndProxyTests(ReplyFixture, unittest.TestCase):
    def test_real_unix_ipc_roundtrip_with_same_owner_revisions_and_foreign_denial(self):
        import threading
        from telegram_search_mcp.broker_client import BrokerClient
        from telegram_search_mcp.reply_drafts import PrepareReplyTextSendRequest,GetReplyDraftRequest,UpdateReplyDraftRequest,RefreshReplyDraftRequest
        from telegram_search_mcp.schemas import SendPreparedTextRequest
        thread=threading.Thread(target=self.broker.serve_forever,daemon=True);thread.start()
        proxy=BrokerClient(socket_path=self.root/'broker.sock',policy=POLICY,restart_callback=lambda:None)
        foreign=BrokerClient(socket_path=self.root/'broker.sock',policy=POLICY,restart_callback=lambda:None)
        self.addCleanup(proxy.close);self.addCleanup(foreign.close)
        try:
            preview=proxy.prepare_reply_text_send(PrepareReplyTextSendRequest(recipient=123,text='Final reply',reply_to=ANCHOR)).reply
            self.assertIsNotNone(preview);did=preview.draft.draft_id
            self.assertEqual(proxy.get_reply_draft(GetReplyDraftRequest(draft_id=did)).reply,preview)
            self.assertEqual(foreign.get_reply_draft(GetReplyDraftRequest(draft_id=did)).status,'unavailable')
            new=proxy.update_reply_draft(UpdateReplyDraftRequest(draft_id=did,text='Final reply')).reply
            refreshed=proxy.refresh_reply_draft(RefreshReplyDraftRequest(draft_id=new.draft.draft_id)).reply
            self.assertEqual(proxy.send_prepared_text(SendPreparedTextRequest(draft_id=refreshed.draft.draft_id,approved=True)).status,'sent')
            self.assertEqual(len(self.sends()),1)
        finally:
            proxy.close();foreign.close();self.broker.shutdown();thread.join(3)
        self.assertFalse(thread.is_alive())
    def test_proxy_binds_prepare_get_and_update_responses_to_request(self):
        from unittest.mock import patch
        from telegram_search_mcp.broker_client import BrokerClient
        from telegram_search_mcp.reply_drafts import (PrepareReplyTextSendRequest,GetReplyDraftRequest,UpdateReplyDraftRequest,
            ReplyDraftPreview,ReplyTarget,digest,preview_facts,PREPARED,PENDING,REVISED)
        from telegram_search_mcp.draft_models import DraftPreview
        preview=self.prepare();did=preview['draft']['draft_id']
        proxy=BrokerClient(socket_path=Path('/unused-synthetic.sock'),policy=POLICY,restart_callback=lambda:None);self.addCleanup(proxy.close)
        request=PrepareReplyTextSendRequest(recipient=123,text='Final reply',reply_to=ANCHOR)
        got=GetReplyDraftRequest(draft_id=did)
        update=UpdateReplyDraftRequest(draft_id=did,text='Final reply')
        envelopes=[(proxy.prepare_reply_text_send,request,{'status':'prepared','reply':preview,'detail':PREPARED}),
                   (proxy.get_reply_draft,got,{'status':'pending','reply':preview,'detail':PENDING})]
        revised=copy.deepcopy(preview);revised['draft']['draft_id']='draft_'+'b'*32
        revised['preview_sha256']=digest(preview_facts(DraftPreview.model_validate(revised['draft']),ReplyTarget.model_validate(revised['reply_target'])))
        envelopes.append((proxy.update_reply_draft,update,{'status':'revised','previous_draft_id':did,'reply':revised,'detail':REVISED}))
        for method, req, good in envelopes:
            with patch.object(proxy,'_request',return_value=good):self.assertIsNotNone(method(req).reply)
            bads=[]
            bad=copy.deepcopy(good);bad['reply']['draft']['text']='Unrelated';data=b'Unrelated'
            bad['reply']['draft'].update(sha256=hashlib.sha256(data).hexdigest(),size_bytes=len(data))
            bad['reply']['preview_sha256']=digest(preview_facts(DraftPreview.model_validate(bad['reply']['draft']),ReplyTarget.model_validate(bad['reply']['reply_target'])))
            if method!=proxy.get_reply_draft:bads.append(bad)
            bad=copy.deepcopy(good);bad['reply']['draft']['draft_id']='draft_'+'c'*32
            bad['reply']['preview_sha256']=digest(preview_facts(DraftPreview.model_validate(bad['reply']['draft']),ReplyTarget.model_validate(bad['reply']['reply_target'])))
            if method==proxy.get_reply_draft:bads.append(bad)
            bad=copy.deepcopy(good);bad['reply']['reply_target']['source_sha256']='0'*64;bads.append(bad)
            if method==proxy.update_reply_draft:
                bad=copy.deepcopy(good);bad['previous_draft_id']='draft_'+'c'*32;bads.append(bad)
            for bad in bads:
                with patch.object(proxy,'_request',return_value=bad):
                    denied=method(req);self.assertEqual(denied.status,'unavailable');self.assertIsNone(denied.reply)
    def test_expired_request_budget_refuses_reply_before_any_send_observation(self):
        from telegram_search_mcp.tdjson import MessageSendNotAttempted
        from telegram_search_mcp.reply_drafts import source_from_message
        self.client.ensure_ready();source=source_from_message(target(),123,55)
        with self.client.request_budget(time.monotonic()-1),self.assertRaises(MessageSendNotAttempted):
            self.client.send_reply_text_message(123,'Final reply',reply_source=source,expected_account_id=7,
                expected_recipient_title='Recipient',attempt_id='expired',pre_send_guard=lambda:True)
        self.assertEqual(self.sends(),[]);self.assertIsNone(self.client._send_observations.snapshot('expired'))

class ReplyDeadlineTests(ReplyFixture, unittest.TestCase):
    def test_final_guard_consuming_budget_refuses_reply_without_observation(self):
        from telegram_search_mcp.tdjson import MessageSendNotAttempted
        from telegram_search_mcp.reply_drafts import source_from_message
        self.client.ensure_ready();source=source_from_message(target(),123,55)
        def slow_guard():time.sleep(0.04);return True
        with self.client.request_budget(time.monotonic()+0.02),self.assertRaises(MessageSendNotAttempted):
            self.client.send_reply_text_message(123,'Final reply',reply_source=source,expected_account_id=7,
                expected_recipient_title='Recipient',attempt_id='guard-expired',pre_send_guard=slow_guard)
        self.assertEqual(self.sends(),[])
        self.assertIsNone(self.client._send_observations.snapshot('guard-expired'))

    def test_send_setup_consuming_budget_before_registration_refuses_reply(self):
        from unittest.mock import patch
        from telegram_search_mcp.tdjson import MessageSendNotAttempted
        from telegram_search_mcp.reply_drafts import source_from_message
        self.client.ensure_ready();source=source_from_message(target(),123,55)
        original=self.client._send_observations.sending_id_available
        def slow_setup(sending_id):time.sleep(0.04);return original(sending_id)
        with patch.object(self.client._send_observations,'sending_id_available',side_effect=slow_setup):
            with self.client.request_budget(time.monotonic()+0.02),self.assertRaises(MessageSendNotAttempted):
                self.client.send_reply_text_message(123,'Final reply',reply_source=source,expected_account_id=7,
                    expected_recipient_title='Recipient',attempt_id='setup-expired',pre_send_guard=lambda:True)
        self.assertEqual(self.sends(),[])
        self.assertIsNone(self.client._send_observations.snapshot('setup-expired'))

    def test_registration_crossing_deadline_is_discarded_and_broker_records_local_failure(self):
        from unittest.mock import patch
        from telegram_search_mcp.schemas import SendPreparedTextRequest
        did=self.prepare()['draft']['draft_id']
        original=self.client._send_observations.register
        def slow_register(*args,**kwargs):original(*args,**kwargs);time.sleep(0.04)
        with patch.object(self.client._send_observations,'register',side_effect=slow_register):
            result=self.broker._send_prepared_text(
                SendPreparedTextRequest(draft_id=did,approved=True),
                client_id=CLIENT,deadline=time.monotonic()+0.02)
        self.assertEqual(result.status,'failed');self.assertIsNone(result.message_id)
        self.assertEqual(self.sends(),[]);self.assertIsNone(self.client._send_observations.snapshot(did))
        status=self.dispatch('get_send_status',{'draft_id':did})
        self.assertEqual((status['status'],status['evidence']),('failed','local_failed'))
        self.assertEqual(self.send(did)['status'],'failed');self.assertEqual(self.sends(),[])

    def test_transport_exception_after_attempt_remains_unknown_and_never_retries(self):
        did=self.prepare()['draft']['draft_id'];original=self.raw.send
        def fail_after_attempt(request):
            original(request)
            if request['@type']=='sendMessage':raise OSError('synthetic post-attempt transport loss')
        self.raw.send=fail_after_attempt
        result=self.send(did)
        self.assertEqual(result['status'],'outcome_unknown');self.assertIsNone(result['message_id'])
        self.assertEqual(len(self.sends()),1);self.assertIsNotNone(self.client._send_observations.snapshot(did))
        self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertEqual(len(self.sends()),1)

    def test_budget_expiring_inside_raw_send_keeps_attempt_uncertainty(self):
        from telegram_search_mcp.schemas import SendPreparedTextRequest
        did=self.prepare()['draft']['draft_id'];original=self.raw.send
        def slow_transport(request):
            original(request)
            if request['@type']=='sendMessage':time.sleep(0.04)
        self.raw.send=slow_transport
        result=self.broker._send_prepared_text(SendPreparedTextRequest(draft_id=did,approved=True),
            client_id=CLIENT,deadline=time.monotonic()+0.02)
        self.assertEqual(result.status,'outcome_unknown');self.assertIsNone(result.message_id)
        self.assertEqual(len(self.sends()),1);self.assertIsNotNone(self.client._send_observations.snapshot(did))
        self.assertEqual(self.send(did)['status'],'outcome_unknown');self.assertEqual(len(self.sends()),1)

    def test_provider_read_past_ipc_deadline_cannot_create_reply_draft(self):
        original=self.raw.send
        def send(request):
            if request['@type']=='getMessage':time.sleep(0.03)
            original(request)
        self.raw.send=send
        result=self.broker._dispatch({'operation':'prepare_reply_text_send',
            'payload':{'recipient':123,'text':'Final reply','reply_to':ANCHOR},'client_id':CLIENT,
            'deadline':time.monotonic()+0.01,'broker_generation':self.broker._generation})
        self.assertEqual(result['status'],'unavailable');self.assertEqual(self.sends(),[])
    def test_provider_lock_wait_obeys_budget_and_is_no_attempt(self):
        import threading
        from telegram_search_mcp.tdjson import MessageSendNotAttempted
        from telegram_search_mcp.reply_drafts import source_from_message
        self.client.ensure_ready();source=source_from_message(target(),123,55)
        held=threading.Event();release=threading.Event();outcomes=[]
        def holder():
            with self.client._lock:held.set();release.wait(2)
        thread=threading.Thread(target=holder);thread.start();self.assertTrue(held.wait(1))
        def send():
            try:
                with self.client.request_budget(time.monotonic()+0.02):
                    self.client.send_reply_text_message(123,'Final reply',reply_source=source,expected_account_id=7,
                        expected_recipient_title='Recipient',attempt_id='held',pre_send_guard=lambda:True)
            except Exception as error:outcomes.append(error)
        caller=threading.Thread(target=send);caller.start();caller.join(0.15)
        finished=not caller.is_alive();release.set();thread.join(1);caller.join(1)
        self.assertTrue(finished,'provider lock wait ignored bounded reply deadline')
        self.assertIsInstance(outcomes[0],MessageSendNotAttempted);self.assertEqual(self.sends(),[])

class ReplyAdmissionTests(ReplyFixture, unittest.TestCase):
    def test_disabled_send_permission_never_opens_provider_for_reply_dispatch(self):
        self.broker._policy=RuntimePolicy(enabled_capabilities=('reply_text_send',))
        opened=[]
        self.broker._client_factory=lambda:opened.append(True) or self.client
        try:
            self.dispatch('prepare_reply_text_send',{'recipient':123,'text':'x','reply_to':ANCHOR})
        except CompatibilityError:pass
        self.assertEqual(opened,[]);self.assertEqual(self.raw.sent,[])
    def test_reply_request_options_cannot_coerce_false_to_integer_zero(self):
        from telegram_search_mcp.tdjson import _validate_bounded_request,ForbiddenTDLibRequest
        preview=self.prepare();self.send(preview['draft']['draft_id']);request=copy.deepcopy(self.sends()[0]);request.pop('@extra')
        request['options']['disable_notification']=0
        with self.assertRaises(ForbiddenTDLibRequest):_validate_bounded_request(request)
    def test_reply_proxy_transport_deadline_is_at_most_30_seconds(self):
        import socket
        from unittest.mock import patch
        from telegram_search_mcp.broker_client import BrokerClient
        from telegram_search_mcp.contract import contract_descriptor
        from telegram_search_mcp.reply_drafts import PrepareReplyTextSendRequest
        left,right=socket.socketpair();self.addCleanup(right.close)
        proxy=BrokerClient(socket_path=Path('/unused-synthetic.sock'),policy=POLICY,
            connector=lambda path:left,restart_callback=lambda:None,request_timeout=570);self.addCleanup(proxy.close)
        deadlines=[]
        def exchange(connection,operation,payload,*,deadline,generation):
            deadlines.append(deadline)
            if operation=='handshake':return {**contract_descriptor(POLICY),'broker_generation':'broker_'+'a'*32}
            return {'status':'unavailable','reply':None,'detail':'reply draft is unavailable'}
        before=time.monotonic()
        with patch.object(proxy,'_exchange',side_effect=exchange):
            proxy.prepare_reply_text_send(PrepareReplyTextSendRequest(recipient=123,text='x',reply_to=ANCHOR))
        self.assertLessEqual(max(deadlines),before+30.1)
