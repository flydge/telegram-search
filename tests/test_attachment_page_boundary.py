from __future__ import annotations
import copy
import hashlib
import json
import secrets
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from mcp import Client
from telegram_search_mcp.attachment_page_models import ReadAttachmentPageRequest, ReadAttachmentPageResponse
from telegram_search_mcp.broker_client import BrokerClient, BrokerCompatibilityError
from telegram_search_mcp.broker_protocol import receive_request, send_frame, PROTOCOL_VERSION, MAX_RESPONSE_BYTES
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import contract_descriptor
from telegram_search_mcp.server import build_server, _inline_attachment_page_image

POLICY = RuntimePolicy(enabled_capabilities=('attachment_pages',))
GEN = 'broker_'+'a'*32
ART = 'artifact_'+'0'*32+'_'+'b'*64+'_10'

def response(*, start=0, scope=None, more=True):
    scope=copy.deepcopy(scope) if scope else dict(artifact_id=ART,artifact_sha256='b'*64,artifact_bytes=10,
        extraction_fingerprint='c'*64,extractor_version=1,broker_generation=GEN,source_anchor=None,
        kind='pdf',selected_pages=[6,2],total_pages=8,all_pages_selected=False,max_chars=2,
        render_pages=False,expires_at=(datetime.now(timezone.utc)+timedelta(seconds=299)).isoformat(),
        text_coverage='selected_pdf_text')
    return dict(contract_version=1,status='page' if more else 'complete',scope=scope,text='ab' if more else 'c',
        text_start=start,text_end=start+(2 if more else 1),images=[],previews_complete=True,
        scope_complete=not more,has_more=more,next_cursor='attachment_'+secrets.token_hex(32) if more else None,
        detail='untrusted artifact text')

class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.operations=[];self.responses=[];owner=self
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                owner.operations.append((op,payload))
                self._attachment_operation_state.generation=GEN
                self._attachment_generation=GEN
                return copy.deepcopy(owner.responses.pop(0)) if owner.responses else {'released':True}
        self.proxy=Proxy(socket_path=Path('/unused'),policy=POLICY)
    def read(self,**kw):
        return self.proxy.read_attachment_page(ReadAttachmentPageRequest(artifact_id=ART,pages=[6,2],max_chars=2,render_pages=False,**kw))
    def issue(self):
        self.responses=[response()];return self.read()
    def test_issuance_continuation_and_replay_has_zero_data(self):
        self.assertTrue(hasattr(BrokerClient,'read_attachment_page'),'attachment proxy is missing')
        first=self.issue();self.assertEqual(first.status,'page')
        self.responses=[response(start=2,scope=first.scope.model_dump(mode='json'),more=False)]
        self.assertEqual(self.read(cursor=first.next_cursor).text,'c')
        before=len(self.operations)
        invalid=self.read(cursor=first.next_cursor)
        self.assertEqual(invalid.status,'invalid_cursor');self.assertEqual(invalid.text,'');self.assertIsNone(invalid.scope)
        self.assertEqual(len(self.operations),before)
    def test_foreign_changed_expired_closed_tokens_rejected_before_dispatch(self):
        first=self.issue();before=len(self.operations)
        self.assertEqual(self.read(cursor='attachment_'+'f'*64).status,'invalid_cursor')
        changed=ReadAttachmentPageRequest(artifact_id=ART,pages=[2,6],max_chars=2,render_pages=False,cursor=first.next_cursor)
        self.assertEqual(self.proxy.read_attachment_page(changed).status,'invalid_cursor')
        self.assertEqual(len(self.operations),before)
        other=BrokerClient(socket_path=Path('/unused'),policy=POLICY,connector=lambda _:self.fail('foreign dispatch'))
        self.assertEqual(other.read_attachment_page(ReadAttachmentPageRequest(artifact_id=ART,cursor=first.next_cursor)).status,'invalid_cursor')
        first=self.issue()
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=time.monotonic()+301):
            self.assertEqual(self.read(cursor=first.next_cursor).status,'expired')
        self.proxy.close();before=len(self.operations)
        self.assertEqual(self.read().status,'invalid_cursor');self.assertEqual(len(self.operations),before)
    def test_adversarial_substitutions_never_issue_cursor(self):
        for change in ('artifact','pages','maxchars','render','generation','start','expiry','contract_bool','extractor_bool','failure_content','page_bool','bytes_string','hasmore_int','offset_float'):
            raw=response()
            if change=='artifact':raw['scope']['artifact_id']='artifact_'+'0'*32+'_'+'d'*64+'_10';raw['scope']['artifact_sha256']='d'*64
            if change=='pages':raw['scope']['selected_pages']=[2,6]
            if change=='maxchars':raw['scope']['max_chars']=3
            if change=='render':raw['scope']['render_pages']=True;raw['previews_complete']=False
            if change=='generation':raw['scope']['broker_generation']='broker_'+'d'*32
            if change=='start':raw['text_start']=2;raw['text_end']=4
            if change=='expiry':raw['scope']['expires_at']=(datetime.now(timezone.utc)+timedelta(seconds=401)).isoformat()
            if change=='contract_bool':raw['contract_version']=True
            if change=='extractor_bool':raw['scope']['extractor_version']=True
            if change=='failure_content':raw['status']='error'
            if change=='page_bool':raw['scope']['selected_pages']=[True,2]
            if change=='bytes_string':raw['scope']['artifact_bytes']='10'
            if change=='hasmore_int':raw['has_more']=1
            if change=='offset_float':raw['text_start']=0.0
            self.responses=[raw]
            with self.subTest(change=change):
                rejected=self.read();self.assertEqual(rejected.status,'error');self.assertIsNone(rejected.scope);self.assertEqual(rejected.text,'')
                before=len(self.operations)
                self.assertEqual(self.read(cursor=raw['next_cursor']).status,'invalid_cursor');self.assertEqual(len(self.operations),before)
    def test_initial_selection_resolution_rejects_fabricated_default_and_nonpdf_pages(self):
        raw=response();raw['scope']['selected_pages']=[1,2,3,4,5]
        self.responses=[raw]
        request=ReadAttachmentPageRequest(artifact_id=ART,max_chars=2,render_pages=False)
        self.assertEqual(self.proxy.read_attachment_page(request).status,'page')
        raw=response();self.responses=[raw]
        self.assertEqual(self.proxy.read_attachment_page(request).status,'error')
        raw=response();raw['scope'].update(kind='text',selected_pages=[],total_pages=None,
            all_pages_selected=False,text_coverage='utf8_text')
        self.responses=[raw];self.assertEqual(self.read().status,'error')

    def test_frozen_scope_offset_and_original_expiry(self):
        for change in ('fingerprint','anchor','expiry','offset','cursor_reuse'):
            first=self.issue();raw=response(start=2,scope=first.scope.model_dump(mode='json'))
            if change=='fingerprint':raw['scope']['extraction_fingerprint']='d'*64
            if change=='anchor':raw['scope']['source_anchor']={'chat_id':7,'message_id':11}
            if change=='expiry':raw['scope']['expires_at']=(datetime.now(timezone.utc)+timedelta(seconds=250)).isoformat()
            if change=='offset':raw['text_start']=4;raw['text_end']=6
            if change=='cursor_reuse':raw['next_cursor']=first.next_cursor
            self.responses=[raw]
            with self.subTest(change=change):self.assertEqual(self.read(cursor=first.next_cursor).status,'error')
    def test_single_scope_token_history_has_fixed_call_ceiling(self):
        first=self.issue();previous=first
        for index in range(1,255):
            self.responses=[response(start=index*2,scope=first.scope.model_dump(mode='json'))]
            previous=self.read(cursor=previous.next_cursor);self.assertEqual(previous.status,'page')
        self.responses=[response(start=510,scope=first.scope.model_dump(mode='json'))]
        stopped=self.read(cursor=previous.next_cursor)
        self.assertEqual(stopped.status,'limit_reached');self.assertEqual(stopped.text,'');self.assertIsNone(stopped.next_cursor)

    def test_capacity_and_default_off(self):
        issued=[self.issue() for _ in range(16)]
        self.assertTrue(all(r.status=='page' for r in issued));before=len(self.operations)
        self.assertEqual(self.read().status,'capacity_exhausted');self.assertEqual(len(self.operations),before)
        self.proxy._policy=RuntimePolicy()
        with self.assertRaises(BrokerCompatibilityError):self.read()
        self.assertEqual(len(self.operations),before)
    def test_pending_issuance_counts_toward_sixteen_live_sessions(self):
        for _ in range(15):self.assertEqual(self.issue().status,'page')
        entered=threading.Event();release=threading.Event();results=[];calls=[]
        def paused(op,payload):
            calls.append(op);entered.set();release.wait(2)
            self.proxy._attachment_operation_state.generation=GEN
            self.proxy._attachment_generation=GEN
            return response()
        self.proxy._request=paused
        worker=threading.Thread(target=lambda:results.append(self.read()));worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.read().status,'capacity_exhausted');self.assertEqual(len(calls),1)
        finally:release.set();worker.join(2)
        self.assertEqual(results[0].status,'page')

    def test_close_during_inflight_never_publishes(self):
        entered=threading.Event();release=threading.Event();results=[]
        def paused(op,payload):
            entered.set();release.wait(2)
            self.proxy._attachment_operation_state.generation=GEN;self.proxy._attachment_generation=GEN
            return response()
        self.proxy._request=paused
        worker=threading.Thread(target=lambda:results.append(self.read()));worker.start();self.assertTrue(entered.wait(1))
        self.proxy.close();release.set();worker.join(2)
        self.assertEqual(results[0].status,'invalid_cursor');self.assertIsNone(results[0].scope)

class SDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_strict_raw_inputs_and_capability_gate(self):
        class Service:
            calls=[]
            def close(self):pass
            def read_attachment_page(self,request):self.calls.append(request);return ReadAttachmentPageResponse(status='unsupported')
        service=Service()
        for policy,enabled in ((RuntimePolicy(),False),(POLICY,True)):
            async with Client(build_server(service_factory=lambda:service,policy=policy)) as consumer:
                tools=(await consumer.list_tools()).tools
                tool=next(t for t in tools if t.name=='read_attachment_page')
                self.assertTrue(tool.annotations.read_only_hint);self.assertFalse(tool.annotations.idempotent_hint)
                self.assertFalse(tool.input_schema['additionalProperties'])
                valid=await consumer.call_tool('read_attachment_page',{'artifact_id':ART,'pages':[6,2]})
                self.assertEqual(valid.is_error,not enabled)
                before=len(service.calls)
                for bad in ({'pages':'[6,2]'},{'pages':[True]},{'pages':[6.0]},{'pages':[6,6]},
                            {'max_chars':True},{'max_chars':2.0},{'render_pages':'true'},{'other':1}):
                    result=await consumer.call_tool('read_attachment_page',{'artifact_id':ART,**bad})
                    self.assertTrue(result.is_error,str(bad))
                self.assertEqual(len(service.calls),before)
    async def test_inline_preview_hash_failure_preserves_text_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=b'abc';aid='artifact_'+'0'*32+'_'+hashlib.sha256(data).hexdigest()+'_3'
            path=root/aid;path.write_bytes(b'xyz');path.chmod(0o600)
            raw=response(more=False);raw['scope']['render_pages']=True;raw['scope']['selected_pages']=[6];raw['images']=[dict(artifact_id=aid,artifact_path=str(path),mime_type='image/png',page_number=6)];raw['previews_complete']=True
            class Service:
                def close(self):pass
                def read_attachment_page(self,request):return ReadAttachmentPageResponse.model_validate(raw)
            async with Client(build_server(service_factory=Service,policy=POLICY,artifact_root=root)) as consumer:
                result=await consumer.call_tool('read_attachment_page',{'artifact_id':ART,'pages':[6],'max_chars':2})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content['status'],'complete');self.assertEqual(result.structured_content['text'],'c')
                self.assertTrue(result.structured_content['scope_complete']);self.assertFalse(result.structured_content['previews_complete'])
                self.assertEqual([b.type for b in result.content],['text'])

class MaterializationTests(unittest.TestCase):
    def test_symlinked_cache_root_cannot_expose_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'cache';cache.mkdir(mode=0o700)
            link=root/'link';link.symlink_to(cache,target_is_directory=True)
            data=b'private preview';aid='artifact_'+'0'*32+'_'+hashlib.sha256(data).hexdigest()+'_'+str(len(data))
            path=cache/aid;path.write_bytes(data);path.chmod(0o600)
            self.assertIsNone(_inline_attachment_page_image(link/aid,aid,root=link,max_encoded_bytes=2048))
    def test_materializer_exposes_only_matching_owner_regular_hash_pinned_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=b'preview';aid='artifact_'+'0'*32+'_'+hashlib.sha256(data).hexdigest()+'_'+str(len(data))
            path=root/aid;path.write_bytes(data);path.chmod(0o600)
            self.assertEqual(_inline_attachment_page_image(path,aid,root=root,max_encoded_bytes=2048),'cHJldmlldw==')
            self.assertIsNone(_inline_attachment_page_image(path,aid,root=root,max_encoded_bytes=4))
            self.assertIsNone(_inline_attachment_page_image(path,'artifact_'+'1'*32+'_'+hashlib.sha256(data).hexdigest()+'_7',root=root,max_encoded_bytes=2048))
            path.chmod(0o644)
            self.assertIsNone(_inline_attachment_page_image(path,aid,root=root,max_encoded_bytes=2048))

class WireTests(unittest.TestCase):
    def test_changed_handshake_generation_never_dispatches_continuation(self):
        observed=[];threads=[];generation=[GEN];raw=response()
        def connector(path):
            client_socket,server_socket=socket.socketpair()
            def serve():
                with server_socket:
                    hello=receive_request(server_socket)
                    descriptor={**contract_descriptor(POLICY),'broker_generation':generation[0]}
                    send_frame(server_socket,{'version':PROTOCOL_VERSION,'request_id':hello['request_id'],'ok':True,'result':descriptor},max_bytes=MAX_RESPONSE_BYTES)
                    try:req=receive_request(server_socket)
                    except Exception:return
                    observed.append(req['operation'])
                    send_frame(server_socket,{'version':PROTOCOL_VERSION,'request_id':req['request_id'],'ok':True,'result':raw},max_bytes=MAX_RESPONSE_BYTES)
            thread=threading.Thread(target=serve);thread.start();threads.append(thread)
            return client_socket
        proxy=BrokerClient(socket_path=Path('/unused'),policy=POLICY,connector=connector)
        request=ReadAttachmentPageRequest(artifact_id=ART,pages=[6,2],max_chars=2,render_pages=False)
        first=proxy.read_attachment_page(request);self.assertEqual(first.status,'page')
        generation[0]='broker_'+'d'*32
        second=proxy.read_attachment_page(request.model_copy(update={'cursor':first.next_cursor}))
        self.assertEqual(second.status,'invalid_cursor');self.assertEqual(observed,['read_attachment_page'])
        for thread in threads:thread.join(2);self.assertFalse(thread.is_alive())

class ConcurrentTests(unittest.TestCase):
    def test_four_active_and_pending_reservations_are_counted(self):
        entered=threading.Condition();release=threading.Event();count=[0];results=[]
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                with entered:count[0]+=1;entered.notify_all()
                release.wait(3)
                self._attachment_operation_state.generation=GEN
                with self._attachment_lock:self._attachment_generation=GEN
                return response()
        proxy=Proxy(socket_path=Path('/unused'),policy=POLICY)
        request=ReadAttachmentPageRequest(artifact_id=ART,pages=[6,2],max_chars=2,render_pages=False)
        workers=[threading.Thread(target=lambda:results.append(proxy.read_attachment_page(request))) for _ in range(4)]
        try:
            for worker in workers:worker.start()
            with entered:self.assertTrue(entered.wait_for(lambda:count[0]==4,2))
            self.assertEqual(proxy.read_attachment_page(request).status,'capacity_exhausted')
            self.assertEqual(count[0],4)
        finally:
            release.set()
            for worker in workers:worker.join(3)
        self.assertTrue(all(r.status=='page' for r in results))

if __name__=='__main__':unittest.main()
