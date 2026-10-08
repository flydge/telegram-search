"""F11a: exercise the owned draft contract through production dispatch/IPC."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mcp import Client
from pydantic import ValidationError
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError
from telegram_search_mcp.server import build_server
import telegram_search_mcp.outgoing_drafts as drafts

POLICY = RuntimePolicy(enabled_capabilities=('send',))
A = 'client_' + 'a' * 24
B = 'client_' + 'b' * 24
UNKNOWN = 'draft_' + 'f' * 32

class Sender:
    def __init__(self):
        self.send_observation_epoch = object()
        self.account = 7
        self.sent = []
        self.resolved = []
    def ensure_ready(self): pass
    def close(self): pass
    def get_account_id(self): return self.account
    def resolve_target(self, recipient):
        self.resolved.append(recipient)
        return {'@type': 'chat', 'id': recipient, 'title': 'Recipient', 'type': {'@type': 'chatTypePrivate'}}
    def send_text_message(self, recipient, text, *, attempt_id=None):
        self.sent.append((recipient, text))
        return 701

class DraftBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.sender = Sender()
        self.store = ArtifactStore(cache_dir=self.root / 'cache')
        self.broker = Broker(socket_path=self.root/'broker.sock',lock_path=self.root/'broker.lock',
                             artifact_store=self.store,client_factory=lambda:self.sender,
                             policy=POLICY,approval_prompt=lambda **_:True)
        self.addCleanup(self.broker.shutdown)
    def dispatch(self, op, payload, client=A):
        return self.broker._dispatch({'operation':op,'payload':payload,'client_id':client,
                                     'deadline':time.monotonic()+10,'broker_generation':self.broker._generation})
    def prepare(self, text='Secret exact text', client=A):
        result = self.dispatch('prepare_text_send', {'recipient':123,'text':text}, client)
        self.assertEqual(result['status'], 'prepared')
        return result['draft_id']
    def test_foreign_send_and_receipt_replay_are_indistinguishable_from_unknown(self):
        did = self.prepare()
        def without_request_id(response):
            return {key: value for key, value in response.items() if key != 'draft_id'}
        for operation in ['send_prepared_text', 'send_prepared_artifact']:
            for client, account in [(B, 7), (A, 8)]:
                self.sender.account = account
                unknown = self.dispatch(operation, {'draft_id': UNKNOWN, 'approved': True}, client)
                foreign = self.dispatch(operation, {'draft_id': did, 'approved': True}, client)
                self.assertEqual(without_request_id(foreign), without_request_id(unknown))
                self.assertIsNone(foreign['message_id'])
        self.assertEqual(self.sender.sent, [])
        self.sender.account = 7
        self.assertEqual(self.dispatch('send_prepared_text', {'draft_id': did, 'approved': True})['status'], 'sent')
        for operation in ['send_prepared_text', 'send_prepared_artifact']:
            for client, account in [(B, 7), (A, 8)]:
                self.sender.account = account
                unknown = self.dispatch(operation, {'draft_id': UNKNOWN, 'approved': True}, client)
                replay = self.dispatch(operation, {'draft_id': did, 'approved': True}, client)
                self.assertEqual(without_request_id(replay), without_request_id(unknown))
                self.assertIsNone(replay['message_id'])
        self.assertEqual(self.sender.sent, [(123, 'Secret exact text')])

    def test_account_and_policy_drift_during_local_approval_prevent_claim(self):
        did=self.prepare()
        def drift(**_): self.sender.account=8; return True
        self.broker._approval_prompt=drift
        response=self.dispatch('send_prepared_text',{'draft_id':did,'approved':True})
        self.assertNotEqual(response['status'],'sent');self.assertEqual(self.sender.sent,[])
        self.sender.account=7
        self.broker._approval_prompt=lambda **_:True
        self.assertEqual(self.dispatch('get_draft',{'draft_id':did})['status'],'pending')
        def disable(**_): self.broker._policy=RuntimePolicy(enabled_capabilities=()); return True
        self.broker._approval_prompt=disable
        self.dispatch('send_prepared_text',{'draft_id':did,'approved':True})
        self.assertEqual(self.sender.sent,[])
        self.broker._policy=POLICY
        self.assertEqual(self.dispatch('get_draft',{'draft_id':did})['status'],'pending')
    def test_get_list_cancel_isolate_accounts_and_clients_without_content_leaks(self):
        did=self.prepare(); unknown=self.dispatch('get_draft',{'draft_id':UNKNOWN})
        got=self.dispatch('get_draft',{'draft_id':did})
        self.assertEqual(got['status'],'pending')
        self.assertEqual((got['draft']['account_id'],got['draft']['recipient'],got['draft']['recipient_title'],got['draft']['text']),
                         (7,123,'Recipient','Secret exact text'))
        self.assertNotIn('client_id',str(got));self.assertNotIn(str(self.root),str(got))
        for client,account in [(B,7),(A,8)]:
            self.sender.account=account
            self.assertEqual(self.dispatch('get_draft',{'draft_id':did},client),unknown)
            self.assertEqual(self.dispatch('list_drafts',{},client)['drafts'],[])
            cancelled=self.dispatch('cancel_draft',{'draft_id':did},client)
            self.assertEqual(cancelled,self.dispatch('cancel_draft',{'draft_id':UNKNOWN},client))
        self.sender.account=7
        listing=self.dispatch('list_drafts',{})
        self.assertEqual([d['draft_id'] for d in listing['drafts']],[did])
        self.assertEqual(set(listing['drafts'][0]),{'draft_id','account_id','recipient','recipient_title','kind','expires_at','approval_required'})
        result=self.dispatch('cancel_draft',{'draft_id':did})
        self.assertEqual(result['status'],'cancelled');self.assertEqual(self.dispatch('cancel_draft',{'draft_id':did}),result)
        self.assertEqual(self.dispatch('get_draft',{'draft_id':did}),unknown)
        self.assertEqual(self.dispatch('list_drafts',{})['drafts'],[])
        self.assertNotEqual(self.dispatch('send_prepared_text',{'draft_id':did,'approved':True})['status'],'sent')
        self.assertEqual(self.sender.sent,[])
    def test_cancellation_during_approval_wins_without_send(self):
        did=self.prepare()
        def cancel(**_):
            self.assertEqual(self.dispatch('cancel_draft',{'draft_id':did})['status'],'cancelled')
            return True
        self.broker._approval_prompt=cancel
        self.assertNotEqual(self.dispatch('send_prepared_text',{'draft_id':did,'approved':True})['status'],'sent')
        self.assertEqual(self.sender.sent,[])
    def test_terminal_receipt_survives_cancel_and_owner_can_replay(self):
        did=self.prepare()
        sent=self.dispatch('send_prepared_text',{'draft_id':did,'approved':True})
        cancelled=self.dispatch('cancel_draft',{'draft_id':did})
        self.assertEqual(cancelled['status'],'unavailable')
        replay=self.dispatch('send_prepared_text',{'draft_id':did,'approved':True})
        self.assertEqual((replay['status'],replay['message_id']),(sent['status'],sent['message_id']))
        self.assertEqual(len(self.sender.sent),1)
    def test_pagination_bounds_owner_anchor_and_live_state(self):
        ids=sorted(self.prepare(str(n)) for n in range(4))
        page=self.dispatch('list_drafts',{'limit':2})
        self.assertEqual([r['draft_id'] for r in page['drafts']],ids[:2]);self.assertTrue(page['has_more'])
        self.assertEqual(page['next_after_draft_id'],ids[1])
        self.dispatch('cancel_draft',{'draft_id':ids[2]})
        page2=self.dispatch('list_drafts',{'limit':2,'after_draft_id':ids[1]})
        self.assertEqual([r['draft_id'] for r in page2['drafts']],ids[3:]);self.assertFalse(page2['has_more'])
        self.assertIsNone(page2['next_after_draft_id'])
        foreign=self.prepare(client=B)
        self.assertEqual(self.dispatch('list_drafts',{'after_draft_id':foreign}),
                         self.dispatch('list_drafts',{'after_draft_id':UNKNOWN}))
        for limit in [0,51,True,'20']:
            with self.assertRaises(ValidationError):self.dispatch('list_drafts',{'limit':limit})
        for op,payload in [('list_drafts',{'client_id':A}),('get_draft',{'draft_id':ids[0],'account_id':7}),('cancel_draft',{'draft_id':ids[0],'approved':True})]:
            with self.assertRaises(ValidationError):self.dispatch(op,payload)
    def test_expired_anchor_requires_restart_and_artifact_tamper_fails_closed(self):
        now=[1700000000.0]
        self.store=ArtifactStore(cache_dir=self.root/'timed',clock=lambda:now[0])
        self.broker._artifact_store=self.store
        self.broker._drafts=drafts.OutgoingDraftRegistry(self.store,clock=lambda:now[0])
        did=self.prepare();now[0]+=900
        self.assertEqual(self.dispatch('get_draft',{'draft_id':did})['status'],'unavailable')
        self.assertEqual(self.dispatch('list_drafts',{'after_draft_id':did})['status'],'unavailable')
        source=self.root/'file';source.write_bytes(b'original');artifact=self.store.store(source)
        preview=self.dispatch('prepare_artifact_send',{'artifact_id':artifact.artifact_id,'recipient':123,'display_name':'secret.txt','mime_type':'text/plain','caption':'secret caption'})
        did=preview['draft_id'];got=self.dispatch('get_draft',{'draft_id':did})
        self.assertEqual((got['draft']['artifact_id'],got['draft']['caption'],got['draft']['display_name']),
                         (artifact.artifact_id,'secret caption','secret.txt'))
        artifact.path.write_bytes(b'changed')
        self.assertEqual(self.dispatch('get_draft',{'draft_id':did})['status'],'unavailable')
        self.assertEqual(self.dispatch('list_drafts',{})['drafts'],[])
    def test_invalid_native_account_is_rejected_and_cannot_prepare(self):
        for value in [True,0,-1,2**53,'7',None]:
            self.sender.account=value
            self.assertEqual(self.dispatch('prepare_text_send',{'recipient':123,'text':'secret'})['status'],'error')
            self.assertEqual(self.dispatch('list_drafts',{})['status'],'unavailable')
    def test_list_is_bounded_metadata_only_and_does_not_resolve_recipients(self):
        for n in range(51):
            self.prepare(str(n))
        resolved = list(self.sender.resolved)
        with patch.object(self.store, 'lookup', side_effect=AssertionError('listing must not read artifact bytes')):
            page = self.dispatch('list_drafts', {'limit': 50})
        self.assertEqual(len(page['drafts']), 50)
        self.assertTrue(page['has_more'])
        self.assertEqual(page['next_after_draft_id'], page['drafts'][-1]['draft_id'])
        self.assertEqual(self.sender.resolved, resolved)
        self.assertEqual(self.sender.sent, [])

    def test_missing_artifact_can_be_listed_but_get_invalidates_without_preview(self):
        source = self.root / 'original'; source.write_bytes(b'private bytes')
        artifact = self.store.store(source)
        prepared = self.dispatch('prepare_artifact_send', {
            'artifact_id': artifact.artifact_id, 'recipient': 123,
            'display_name': 'secret.txt', 'mime_type': 'text/plain', 'caption': 'private caption'})
        artifact.path.unlink()
        did = prepared['draft_id']
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'][0]['draft_id'], did)
        missing = self.dispatch('get_draft', {'draft_id': did})
        self.assertEqual(missing, self.dispatch('get_draft', {'draft_id': UNKNOWN}))
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'], [])
        self.assertEqual(self.sender.sent, [])

    def test_claimed_send_cannot_be_cancelled_and_preserves_terminal_receipt(self):
        did = self.prepare()
        entered = threading.Event(); resume = threading.Event()
        def paused_send(recipient, text, *, attempt_id=None):
            entered.set()
            if not resume.wait(3):
                raise AssertionError('send continuation was not released')
            self.sender.sent.append((recipient, text))
            return 701
        with patch.object(self.sender, 'send_text_message', side_effect=paused_send):
            with ThreadPoolExecutor(max_workers=1) as pool:
                attempt = pool.submit(self.dispatch, 'send_prepared_text', {'draft_id': did, 'approved': True})
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(self.dispatch('cancel_draft', {'draft_id': did})['status'], 'unavailable')
                    self.assertEqual(self.dispatch('get_draft', {'draft_id': did})['status'], 'unavailable')
                finally:
                    resume.set()
                self.assertEqual(attempt.result()['status'], 'sent')
        self.assertEqual(self.dispatch('send_prepared_text', {'draft_id': did, 'approved': True})['message_id'], 701)
        self.assertEqual(self.sender.sent, [(123, 'Secret exact text')])

    def test_registry_all_accesses_are_owner_bound_including_kind_and_receipt(self):
        owner = drafts.DraftOwner(client_id=A, account_id=7)
        foreign = [drafts.DraftOwner(client_id=B, account_id=7), drafts.DraftOwner(client_id=A, account_id=8)]
        registry = drafts.OutgoingDraftRegistry(self.store)
        did = registry.prepare_text(owner=owner, recipient=123, text='exact').draft_id
        for other in foreign:
            for access in [registry.kind, registry.peek, registry.receipt, registry.cancel]:
                with self.subTest(access=access.__name__, owner=other), self.assertRaises(drafts.DraftError):
                    access(did, owner=other)
            with self.assertRaises(drafts.DraftError):
                registry.claim(did, owner=other, approved=True)
        registry.claim(did, owner=owner, approved=True)
        registry.finish(did, owner=owner, status='sent', message_id=701)
        for other in foreign:
            with self.assertRaises(drafts.DraftError):
                registry.finish(did, owner=other, status='failed')
            with self.assertRaises(drafts.DraftError):
                registry.receipt(did, owner=other)
        self.assertEqual(registry.receipt(did, owner=owner).message_id, 701)

    def test_no_implicit_owner_registry_access_and_atomic_cancel_claim(self):
        self.assertTrue(hasattr(drafts,'DraftOwner'),'immutable draft ownership is missing')
        owner=drafts.DraftOwner(client_id=A,account_id=7)
        registry=drafts.OutgoingDraftRegistry(self.store)
        with self.assertRaises(TypeError):registry.prepare_text(recipient=123,text='secret')
        for _ in range(20):
            did=registry.prepare_text(owner=owner,recipient=123,text='secret').draft_id
            start=threading.Barrier(3)
            def claim():
                start.wait()
                try:registry.claim(did,owner=owner,approved=True);return 'claimed'
                except drafts.DraftError:return 'unavailable'
            def cancel():
                start.wait()
                try:registry.cancel(did,owner=owner);return 'cancelled'
                except drafts.DraftError:return 'unavailable'
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(claim);second=pool.submit(cancel);start.wait()
                results=[first.result(),second.result()]
            self.assertEqual(sum(r in {'claimed','cancelled'} for r in results),1)
            if 'claimed' in results:
                receipt=registry.finish(did,owner=owner,status='outcome_unknown')
                with self.assertRaises(drafts.DraftError):registry.cancel(did,owner=owner)
                self.assertEqual(registry.receipt(did,owner=owner),receipt)

class PublicDraftTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_broker_proxy_and_sdk_surface_reject_foreign_and_invalid_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);sender=Sender()
            broker=Broker(socket_path=root/'broker.sock',lock_path=root/'broker.lock',artifact_store=ArtifactStore(cache_dir=root/'cache'),
                          client_factory=lambda:sender,policy=POLICY,approval_prompt=lambda **_:True)
            thread=threading.Thread(target=broker.serve_forever);thread.start()
            proxy=BrokerClient(socket_path=root/'broker.sock',policy=POLICY,client_id=A,restart_callback=lambda:None)
            foreign=BrokerClient(socket_path=root/'broker.sock',policy=POLICY,client_id=B,restart_callback=lambda:None)
            try:
                self.assertTrue(broker.wait_until_ready(timeout=2))
                async with Client(build_server(service_factory=lambda:proxy,policy=POLICY)) as consumer:
                    tools={t.name:t for t in (await consumer.list_tools()).tools}
                    for name in ['list_drafts','get_draft','cancel_draft']:
                        self.assertIn(name,tools,'owned draft tool is missing')
                        self.assertFalse(tools[name].input_schema['additionalProperties'])
                    prepared=await consumer.call_tool('prepare_text_send',{'recipient':123,'text':'exact'})
                    did=prepared.structured_content['draft_id']
                    got=await consumer.call_tool('get_draft',{'draft_id':did})
                    self.assertEqual(got.structured_content['draft']['text'],'exact')
                    listed=await consumer.call_tool('list_drafts',{'limit':1})
                    self.assertEqual(listed.structured_content['drafts'][0]['draft_id'],did)
                    for name,args in [('list_drafts',{'limit':'1'}),('list_drafts',{'limit':True}),('get_draft',{'draft_id':did,'account_id':7})]:
                        self.assertTrue((await consumer.call_tool(name,args)).is_error)
                    async with Client(build_server(service_factory=lambda:foreign,policy=POLICY)) as other:
                        self.assertEqual((await other.call_tool('get_draft',{'draft_id':did})).structured_content['status'],'unavailable')
                    cancelled=await consumer.call_tool('cancel_draft',{'draft_id':did})
                    self.assertEqual(cancelled.structured_content['status'],'cancelled')
                    self.assertEqual(sender.sent,[])
                async with Client(build_server(service_factory=lambda:proxy,policy=RuntimePolicy(enabled_capabilities=()))) as disabled:
                    self.assertTrue((await disabled.call_tool('list_drafts',{})).is_error)
            finally:
                proxy.close();foreign.close();broker.shutdown();thread.join(3)
