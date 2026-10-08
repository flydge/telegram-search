from __future__ import annotations
import copy
import importlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pydantic import ValidationError
from unittest.mock import patch
from telegram_search_mcp import schemas
from telegram_search_mcp.search_service import SearchService
from telegram_search_mcp.tdjson import AuthorizationBlocked, MessageNotFound, TDLibError
from test_message_reader import Provider, message

class TargetProvider(Provider):
    def __init__(self):
        super().__init__({i: message(i) for i in range(1, 22)})
        self.account = 17
        self.chat = {'@type':'chat','id':7,'title':'Synthetic chat',
                     'type':{'@type':'chatTypePrivate','user_id':17}}
        self.hook = lambda name: None
    def get_account_id(self):
        self.calls.append(('account',))
        self.hook('account')
        return self.account
    def resolve_target(self, target):
        self.calls.append(('chat', target))
        self.hook('chat')
        assert type(target) is int
        return copy.deepcopy(self.chat)
    def get_message(self, chat_id, message_id):
        self.hook('message')
        return super().get_message(chat_id, message_id)
    def get_sender_identity(self, sender):
        self.hook('sender')
        return super().get_sender_identity(sender)
    def close(self): pass

class VerifiedTargetTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(schemas, 'VerifyTargetRequest'), 'numeric identity verification request is missing')
        self.assertTrue(hasattr(SearchService, 'verify_target'), 'service does not issue target handles')
        self.module = importlib.import_module('telegram_search_mcp.verified_targets')
        self.provider = TargetProvider()
        self.now = 100.0
        self.reader = self.module.VerifiedTargetReader(client=self.provider, client_id='client_a',
            broker_generation='broker_a', clock=lambda:self.now,
            wall_clock=lambda:datetime(2030,1,1,tzinfo=timezone.utc))
    def issue(self):
        return self.reader.verify(schemas.VerifyTargetRequest(target=7))
    def read(self, handle, ids=(11,)):
        return self.reader.read(schemas.ReadTargetMessagesRequest(target_handle=handle, message_ids=list(ids)))
    def terminal(self, result, status):
        self.assertEqual(result.status,status)
        self.assertIsNone(result.target)
        self.assertIsNone(result.target_handle)
        self.assertIsNone(result.expires_at)
        self.assertIsNone(getattr(result,'messages',None))
    def test_fresh_authorized_provider_is_readied_before_first_account_request(self):
        provider=self.provider
        original=provider.get_account_id
        ready=[False]
        def account():
            if not ready[0]:raise AuthorizationBlocked('provider not initialized')
            return original()
        def initialize():
            provider.calls.append(('ready',));ready[0]=True
        provider.get_account_id=account;provider.ensure_ready=initialize
        self.assertEqual(self.issue().status,'verified')
        self.assertEqual(provider.calls[0],('ready',))
    def test_deadline_expiry_before_provider_and_after_sender_never_exports(self):
        import time
        issued=self.issue();self.provider.calls.clear()
        result=self.reader.read(schemas.ReadTargetMessagesRequest(target_handle=issued.target_handle,message_ids=[11]),deadline=time.monotonic()-1)
        self.terminal(result,'error');self.assertEqual(self.provider.calls,[])
        self.reader=self.module.VerifiedTargetReader(client=self.provider,client_id='client_a',broker_generation='broker_a')
        issued=self.issue();started=time.monotonic()
        with patch('telegram_search_mcp.verified_targets.time.monotonic',return_value=started) as timer:
            def lapse(name):
                if name=='sender':timer.return_value=started+31
            self.provider.hook=lapse
            self.terminal(self.read(issued.target_handle),'error')
    def test_native_exact_numeric_hydration_uses_no_search_or_read_receipt_requests(self):
        from telegram_search_mcp.tdjson import TDLibClient
        from test_tdjson import ScriptedRaw
        raw=ScriptedRaw({'getAuthorizationState':[{'@type':'authorizationStateReady'} for _ in range(2)],
            'getMe':[{'@type':'user','id':17} for _ in range(64)],
            'getChat':[copy.deepcopy(self.provider.chat) for _ in range(16)],
            'getMessage':[message(11)],
            'getUser':[{'@type':'user','id':17,'first_name':'Synthetic','last_name':'Reader'}]})
        native=TDLibClient(raw=raw)
        reader=self.module.VerifiedTargetReader(client=native,client_id='client_a',broker_generation='broker_a')
        issued=reader.verify(schemas.VerifyTargetRequest(target=7))
        self.assertEqual(issued.status,'verified')
        result=reader.read(schemas.ReadTargetMessagesRequest(target_handle=issued.target_handle,message_ids=[11]))
        self.assertEqual(result.status,'complete')
        for request in raw.sent:
            shape={k:v for k,v in request.items() if k!='@extra'}
            kind=shape['@type']
            expected={'getAuthorizationState':{'@type':'getAuthorizationState'},'getMe':{'@type':'getMe'},
                'getChat':{'@type':'getChat','chat_id':7},'getMessage':{'@type':'getMessage','chat_id':7,'message_id':11},
                'getUser':{'@type':'getUser','user_id':17}}
            self.assertIn(kind,expected)
            self.assertEqual(shape,expected[kind])
        before=len(raw.sent)
        self.assertEqual(reader.read(schemas.ReadTargetMessagesRequest(target_handle='target_'+'b'*64,message_ids=[11])).status,'invalid_handle')
        self.assertEqual(len(raw.sent),before)
    def test_numeric_only_selection_and_distinct_positive_ids(self):
        for value in [0,True,'7','@synthetic',2**53, -(2**53),7.0]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                schemas.VerifyTargetRequest(target=value)
        handle='target_'+'a'*64
        for ids in [[],[1,1],[0],[True],['1'],[2**53],list(range(1,22))]:
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                schemas.ReadTargetMessagesRequest(target_handle=handle,message_ids=ids)
        with self.assertRaises(ValidationError):
            schemas.VerifyTargetRequest(target=7,selected=True)
        self.assertEqual(self.provider.calls,[])
    def test_issue_then_read_rehydrates_numeric_identity_and_keeps_order(self):
        issued=self.issue()
        self.assertEqual(issued.status,'verified')
        self.assertRegex(issued.target_handle,r'^target_[0-9a-f]{64}$')
        self.assertEqual(issued.target.chat_id,7)
        self.assertEqual(issued.identity_semantics,'fresh_observed_identity_not_atomic_snapshot')
        result=self.read(issued.target_handle,(12,11))
        self.assertEqual(result.status,'complete')
        self.assertEqual([r.anchor.message_id for r in result.messages.results],[12,11])
        self.assertEqual(result.expires_at,issued.expires_at)
        self.assertEqual(result.messages.results[0].message.text.value,'Full selected text')
        self.assertTrue(all(c[1]==7 for c in self.provider.calls if c[0]=='chat'))
        self.assertLessEqual(self.provider.deadline, __import__('time').monotonic()+30)
    def test_expiry_is_non_sliding_and_boundary_invalidates_without_content(self):
        issued=self.issue(); self.now=399.999
        self.assertEqual(self.read(issued.target_handle).status,'complete')
        self.now=400.0; self.provider.calls.clear()
        self.terminal(self.read(issued.target_handle),'invalid_handle')
        self.assertEqual(self.provider.calls,[])
    def test_foreign_unknown_and_closed_handles_never_reach_provider(self):
        issued=self.issue()
        other=self.module.VerifiedTargetReader(client=self.provider,client_id='client_b',broker_generation='broker_a')
        self.provider.calls.clear()
        self.terminal(other.read(schemas.ReadTargetMessagesRequest(target_handle=issued.target_handle,message_ids=[11])),'invalid_handle')
        self.terminal(self.read('target_'+'b'*64),'invalid_handle')
        self.reader.close()
        self.terminal(self.read(issued.target_handle),'invalid_handle')
        self.terminal(self.issue(),'invalid_handle')
        self.assertEqual(self.provider.calls,[])
    def test_wrong_secret_and_malformed_chat_never_issue(self):
        original=copy.deepcopy(self.provider.chat)
        for patch in [{'id':8},{'@type':'user'},{'type':{'@type':'chatTypeSecret','secret_chat_id':1}},
                      {'type':{'@type':'chatTypeSupergroup','supergroup_id':9,'is_channel':'false'}},
                      {'type':{'@type':'chatTypePrivate','user_id':True}}]:
            self.provider.chat={**original,**patch}
            self.terminal(self.issue(),'invalid_target')
        self.assertFalse(any(c[0]=='message' for c in self.provider.calls))
    def test_title_changes_are_display_only_username_is_never_retained_or_used(self):
        self.provider.chat['usernames']={'active_usernames':['old_name']}
        issued=self.issue()
        self.provider.chat['title']='New title'
        self.provider.chat['usernames']={'active_usernames':['reassigned_name']}
        result=self.read(issued.target_handle)
        self.assertEqual(result.status,'complete')
        self.assertEqual(result.target.title,'New title')
        self.assertNotIn('old_name',repr(self.reader._entries))
        self.assertNotIn('reassigned_name',result.model_dump_json())
    def test_account_drift_at_hydration_content_sender_and_final_discards_entire_batch(self):
        for event, occurrence in [('chat',1),('message',2),('sender',2),('account',23)]:
            with self.subTest(event=event):
                self.provider=TargetProvider()
                self.reader=self.module.VerifiedTargetReader(client=self.provider,client_id='client_a',broker_generation='broker_a')
                issued=self.issue(); count=0
                def drift(name):
                    nonlocal count
                    if name==event:
                        count+=1
                        if count==occurrence:self.provider.account=18
                self.provider.hook=drift
                result=self.read(issued.target_handle,(11,12))
                self.terminal(result,'invalid_handle')
                self.assertNotIn('Full selected text',result.model_dump_json())
                self.provider.calls.clear()
                self.terminal(self.read(issued.target_handle),'invalid_handle')
                self.assertEqual(self.provider.calls,[])
    def test_identity_type_drift_discards_prior_rows(self):
        issued=self.issue()
        def change(name):
            if name=='sender':self.provider.chat['type']={'@type':'chatTypeBasicGroup','basic_group_id':9}
        self.provider.hook=change
        self.terminal(self.read(issued.target_handle,(11,12)),'invalid_handle')
        self.assertEqual(len([c for c in self.provider.calls if c[0]=='message']),1)
    def test_provider_transport_failure_in_sender_is_fatal_not_partial_success(self):
        issued=self.issue()
        def fail(name):
            if name=='sender':raise TDLibError('private failure')
        self.provider.hook=fail
        result=self.read(issued.target_handle)
        self.terminal(result,'error')
        self.assertNotIn('private failure',result.model_dump_json())
    def test_close_during_response_construction_cannot_publish_identity_or_content(self):
        for operation, schema_name in [('verify','VerifyTargetResponse'),('read','ReadTargetMessagesResponse')]:
            with self.subTest(operation=operation):
                self.reader=self.module.VerifiedTargetReader(client=self.provider,client_id='client_a',broker_generation='broker_a')
                issued=self.issue() if operation=='read' else None
                original=getattr(self.module,schema_name)
                def closing(*args,**kwargs):
                    response=original(*args,**kwargs)
                    if response.status in {'verified','complete','partial'}:self.reader.close()
                    return response
                with patch.object(self.module,schema_name,side_effect=closing):
                    result=self.issue() if operation=='verify' else self.read(issued.target_handle)
                self.terminal(result,'invalid_handle')
                self.assertEqual(len(self.reader._entries),0)
    def test_transient_malformed_chat_type_midbatch_is_fatal_and_no_later_content(self):
        issued=self.issue();count=0;original=copy.deepcopy(self.provider.chat)
        def malformed(name):
            nonlocal count
            if name=='chat':
                count+=1
                self.provider.chat=copy.deepcopy(original)
                if count==4:self.provider.chat['type']['@type']=[]
        self.provider.hook=malformed
        self.terminal(self.read(issued.target_handle,(11,12,13)),'invalid_handle')
        self.assertEqual([c[2] for c in self.provider.calls if c[0]=='message'],[11])
        self.provider.calls.clear();self.terminal(self.read(issued.target_handle),'invalid_handle')
        self.assertEqual(self.provider.calls,[])
    def test_malformed_account_and_sender_cannot_be_success(self):
        issued=self.issue()
        self.provider.get_sender_identity=lambda sender: {'kind':'user','id':18,'display_name':'Wrong sender'}
        self.terminal(self.read(issued.target_handle),'error')
        self.provider.account=True
        self.terminal(self.issue(),'error')
    def test_wrong_message_identity_is_fatal_for_entire_batch(self):
        issued=self.issue();self.provider.messages[12]=message(12,chat_id=8)
        self.terminal(self.read(issued.target_handle,(11,12)),'invalid_handle')
    def test_missing_message_preserves_partial_coverage_without_retaining_bodies(self):
        issued=self.issue();self.provider.messages[12]=MessageNotFound('private detail')
        result=self.read(issued.target_handle,(11,12))
        self.assertEqual(result.status,'partial')
        self.assertEqual([r.status for r in result.messages.results],['complete','not_found'])
        self.assertNotIn('Full selected text',repr(self.reader._entries))
    def test_capacity_does_not_evict_and_expired_entries_release_capacity(self):
        handles=[self.issue().target_handle for _ in range(16)]
        self.terminal(self.issue(),'capacity')
        self.assertEqual(self.read(handles[0]).status,'complete')
        self.now=400.0
        self.assertEqual(self.issue().status,'verified')
    def test_pending_issue_reserves_capacity_and_close_cannot_resurrect(self):
        for _ in range(15):self.assertEqual(self.issue().status,'verified')
        entered=threading.Event();release=threading.Event()
        def block(name):
            if name=='chat':entered.set();release.wait(3)
        self.provider.hook=block
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=pool.submit(self.issue);self.assertTrue(entered.wait(2))
            self.terminal(self.issue(),'capacity')
            self.reader.close();release.set()
            self.terminal(pending.result(3),'invalid_handle')
        self.assertEqual(len(self.reader._entries),0)
    def test_four_active_operations_bound_and_no_registry_lock_held_during_io(self):
        issued=self.issue();entered=threading.Barrier(5);release=threading.Event()
        def block(name):
            if name=='chat':entered.wait(3);release.wait(3)
        self.provider.hook=block
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(self.read,issued.target_handle) for _ in range(4)]
            entered.wait(3)
            self.terminal(self.read(issued.target_handle),'capacity')
            self.reader.close();release.set()
            for f in futures:self.terminal(f.result(4),'invalid_handle')

if __name__=='__main__':unittest.main()
