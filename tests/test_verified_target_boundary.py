from __future__ import annotations
import copy
import json
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from mcp import Client
from pydantic import ValidationError
from telegram_search_mcp import schemas
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient, BrokerCompatibilityError
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError, schema_document, fingerprint, contract_descriptor
from telegram_search_mcp.broker_protocol import (receive_request, send_frame, PROTOCOL_VERSION, MAX_RESPONSE_BYTES)
from telegram_search_mcp.search_service import SearchService
from telegram_search_mcp.server import build_server
from test_verified_targets import TargetProvider

POLICY=RuntimePolicy(enabled_capabilities=('verified_targets',))
GEN='broker_'+'a'*32

class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_schema_capability_and_selected_numeric_path(self):
        for policy, enabled in [(RuntimePolicy(),False),(POLICY,True)]:
            provider=TargetProvider();service=SearchService(client=provider,owns_client=False)
            async with Client(build_server(service_factory=lambda:service,policy=policy)) as consumer:
                tools={t.name:t for t in (await consumer.list_tools()).tools}
                self.assertIn('verify_target',list(tools),'verification MCP tool is missing')
                self.assertIn('read_target_messages',list(tools))
                for name in ['verify_target','read_target_messages']:
                    self.assertFalse(tools[name].input_schema['additionalProperties'])
                    self.assertTrue(tools[name].annotations.read_only_hint)
                result=await consumer.call_tool('verify_target',{'target':7})
                if not enabled:
                    self.assertTrue(result.is_error);self.assertEqual(provider.calls,[])
                else:
                    self.assertFalse(result.is_error)
                    issued=result.structured_content
                    read=await consumer.call_tool('read_target_messages',{'target_handle':issued['target_handle'],'message_ids':[12,11]})
                    self.assertEqual(read.structured_content['status'],'complete')
                    self.assertEqual([r['anchor']['message_id'] for r in read.structured_content['messages']['results']],[12,11])
                    before=len(provider.calls)
                    for arguments in [{'target':'7'},{'target':'@synthetic'},{'target':True},{'target':7,'selected':True}]:
                        invalid=await consumer.call_tool('verify_target',arguments)
                        self.assertTrue(invalid.is_error)
                    duplicate=await consumer.call_tool('read_target_messages',{'target_handle':issued['target_handle'],'message_ids':[11,11]})
                    self.assertTrue(duplicate.is_error)
                    self.assertEqual(len(provider.calls),before)
    async def test_sdk_json_looking_message_ids_are_rejected_before_dispatch(self):
        provider=TargetProvider();service=SearchService(client=provider,owns_client=False)
        results=[]
        async with Client(build_server(service_factory=lambda:service,policy=POLICY)) as consumer:
            issued=(await consumer.call_tool('verify_target',{'target':7})).structured_content
            before=len(provider.calls)
            for ids in ['[11]','[11,12]']:
                results.append(await consumer.call_tool('read_target_messages',{'target_handle':issued['target_handle'],'message_ids':ids}))
        self.assertTrue(all(r.is_error for r in results),'SDK coerced a quoted JSON array and dispatched content')
        self.assertEqual(len(provider.calls),before)
    async def test_broker_independent_client_and_capability_binding(self):
        self.assertTrue(hasattr(BrokerClient,'verify_target'),'verified target proxy is missing')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);provider=TargetProvider()
            broker=Broker(socket_path=root/'broker.sock',artifact_store=ArtifactStore(cache_dir=root/'cache'),client_factory=lambda:provider,policy=POLICY)
            self.addCleanup(broker._executor.shutdown)
            def dispatch(op,payload,client='client_'+'a'*24):
                return broker._dispatch({'operation':op,'payload':payload,'client_id':client,'deadline':time.monotonic()+10,'broker_generation':broker._generation})
            issued=dispatch('verify_target',{'target':7})
            provider.calls.clear()
            foreign=dispatch('read_target_messages',{'target_handle':issued['target_handle'],'message_ids':[11]},'client_'+'b'*24)
            self.assertEqual(foreign['status'],'invalid_handle');self.assertEqual(provider.calls,[])
            read=dispatch('read_target_messages',{'target_handle':issued['target_handle'],'message_ids':[11]})
            self.assertEqual(read['status'],'complete')
            dispatch('release_client',{})
            provider.calls.clear()
            self.assertEqual(dispatch('read_target_messages',{'target_handle':issued['target_handle'],'message_ids':[11]})['status'],'invalid_handle')
            self.assertEqual(provider.calls,[])
            broker._policy=RuntimePolicy()
            with self.assertRaises(CompatibilityError):dispatch('verify_target',{'target':7})
            self.assertEqual(provider.calls,[])
    async def test_raw_ipc_release_during_second_message_discards_prior_row(self):
        entered=threading.Event();release=threading.Event();provider=TargetProvider()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            broker=Broker(socket_path=root/'broker.sock',lock_path=root/'broker.lock',artifact_store=ArtifactStore(cache_dir=root/'cache'),client_factory=lambda:provider,policy=POLICY)
            thread=threading.Thread(target=broker.serve_forever);thread.start()
            proxy=BrokerClient(socket_path=root/'broker.sock',policy=POLICY,restart_callback=lambda:None)
            control=BrokerClient(socket_path=root/'broker.sock',policy=POLICY,client_id=proxy.client_id,restart_callback=lambda:None)
            results=[]
            try:
                self.assertTrue(broker.wait_until_ready(timeout=2))
                issued=proxy.verify_target(schemas.VerifyTargetRequest(target=7))
                original=provider.get_message
                def paused(chat,mid):
                    if mid==12:entered.set();release.wait(3)
                    return original(chat,mid)
                provider.get_message=paused
                worker=threading.Thread(target=lambda:results.append(proxy.read_target_messages(schemas.ReadTargetMessagesRequest(target_handle=issued.target_handle,message_ids=[11,12]))))
                worker.start();self.assertTrue(entered.wait(2))
                self.assertEqual(control._request('release_client',{}),{'released':True})
                release.set();worker.join(3);self.assertFalse(worker.is_alive())
                self.assertEqual(results[0].status,'invalid_handle')
                self.assertIsNone(results[0].messages);self.assertIsNone(results[0].target)
            finally:
                release.set();proxy.close();control.close();broker.shutdown();thread.join(3)
    async def test_real_framed_ipc_reuses_handle_without_username_lookup(self):
        self.assertTrue(hasattr(BrokerClient,'verify_target'),'verified target proxy is missing')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);provider=TargetProvider()
            broker=Broker(socket_path=root/'broker.sock',lock_path=root/'broker.lock',artifact_store=ArtifactStore(cache_dir=root/'cache'),client_factory=lambda:provider,policy=POLICY)
            thread=threading.Thread(target=broker.serve_forever);thread.start()
            proxy=BrokerClient(socket_path=root/'broker.sock',policy=POLICY,restart_callback=lambda:None)
            try:
                self.assertTrue(broker.wait_until_ready(timeout=2))
                issued=proxy.verify_target(schemas.VerifyTargetRequest(target=7))
                self.assertEqual(issued.status,'verified')
                for _ in range(2):
                    response=proxy.read_target_messages(schemas.ReadTargetMessagesRequest(target_handle=issued.target_handle,message_ids=[12,11]))
                    self.assertEqual(response.status,'complete')
                    self.assertEqual(response.expires_at,issued.expires_at)
                self.assertTrue(all(c[1]==7 for c in provider.calls if c[0]=='chat'))
            finally:
                proxy.close();broker.shutdown();thread.join(3);self.assertFalse(thread.is_alive())

class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(BrokerClient,'verify_target'),'verified target proxy is missing')
        self.provider=TargetProvider();self.service=SearchService(client=self.provider,owns_client=False)
        self.responses=[];self.operations=[]
        owner=self
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                owner.operations.append(op)
                self._target_operation_state.generation=GEN
                self._target_generation=GEN
                if owner.responses:return copy.deepcopy(owner.responses.pop(0))
                if op=='verify_target':return owner.service.verify_target(schemas.VerifyTargetRequest(**payload)).model_dump(mode='json')
                if op=='read_target_messages':return owner.service.read_target_messages(schemas.ReadTargetMessagesRequest(**payload)).model_dump(mode='json')
                return {'released':True}
        self.proxy=Proxy(socket_path=Path('/unused'),policy=POLICY)
    def issue(self):return self.proxy.verify_target(schemas.VerifyTargetRequest(target=7))
    def read(self,token,ids=(11,)):return self.proxy.read_target_messages(schemas.ReadTargetMessagesRequest(target_handle=token,message_ids=list(ids)))
    def test_unknown_expired_foreign_and_closed_zero_predispatch(self):
        self.assertEqual(self.read('target_'+'b'*64).status,'invalid_handle');self.assertEqual(self.operations,[])
        issued=self.issue();self.operations.clear()
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=time.monotonic()+300):
            self.assertEqual(self.read(issued.target_handle).status,'invalid_handle')
        self.assertEqual(self.operations,[])
        other=BrokerClient(socket_path=Path('/unused'),policy=POLICY,connector=lambda path:self.fail('foreign handle dispatched'))
        self.assertEqual(other.read_target_messages(schemas.ReadTargetMessagesRequest(target_handle=issued.target_handle,message_ids=[11])).status,'invalid_handle')
        self.proxy.close();self.operations.clear()
        self.assertEqual(self.read(issued.target_handle).status,'invalid_handle');self.assertEqual(self.operations,[])
    def test_rejects_wrong_target_token_expiry_and_order_in_response(self):
        for field in ['target','target_handle','expires_at','order','source','sender','text_count','sender_flag','role']:
            issued=self.issue()
            raw=self.service.read_target_messages(schemas.ReadTargetMessagesRequest(target_handle=issued.target_handle,message_ids=[12,11])).model_dump(mode='json')
            if field=='target':raw['target']['chat_id']=8
            if field=='target_handle':raw['target_handle']='target_'+'c'*64
            if field=='expires_at':raw['expires_at']=(datetime.now(timezone.utc)+timedelta(seconds=400)).isoformat()
            if field=='order':raw['messages']['results'].reverse()
            if field=='source':raw['messages']['results'][0]['message']['source']['evidence_anchor']['message_id']=9
            if field=='sender':raw['messages']['results'][0]['message']['sender']['id']=-17
            if field=='text_count':raw['messages']['results'][0]['message']['text']['original_characters']=0
            if field=='sender_flag':raw['messages']['results'][0]['message']['sender']['display_name_truncated']=True
            if field=='role':raw['messages']['results'][0]['message']['text_role']='caption'
            self.responses=[raw]
            result=self.read(issued.target_handle,(12,11))
            with self.subTest(field=field):
                self.assertEqual(result.status,'error',field)
                self.assertIsNone(result.messages);self.assertIsNone(result.target)
    def test_issuance_reply_must_be_bounded_current_and_unique(self):
        valid=self.issue().model_dump(mode='json')
        for field,value in [('target_handle','target_'+'0'*64),('target',{'chat_id':8,'title':'Wrong','chat_type':'private','untrusted':True}),('expires_at',(datetime.now(timezone.utc)+timedelta(seconds=400)).isoformat())]:
            raw=copy.deepcopy(valid);raw[field]=value
            if field=='target_handle':raw['target_handle']='bad'
            self.responses=[raw]
            self.assertEqual(self.issue().status,'error')
        self.responses=[valid]
        self.assertEqual(self.issue().status,'error')
    def test_proxy_four_active_and_sixteen_live_caps(self):
        issued=[self.issue() for _ in range(16)]
        self.assertTrue(all(r.status=='verified' for r in issued))
        before=len(self.operations)
        self.assertEqual(self.issue().status,'capacity');self.assertEqual(len(self.operations),before)
        self.assertEqual(self.read(issued[0].target_handle).status,'complete')
    def test_default_off_checked_without_dispatch(self):
        self.proxy._policy=RuntimePolicy()
        with self.assertRaises(BrokerCompatibilityError):self.issue()
        self.assertEqual(self.operations,[])

class WireGenerationTests(unittest.TestCase):
    def test_changed_handshake_generation_invalidates_before_content_dispatch(self):
        self.assertTrue(hasattr(BrokerClient,'verify_target'),'verified target proxy is missing')
        provider=TargetProvider();service=SearchService(client=provider,owns_client=False)
        observed=[];threads=[];generation=[GEN]
        def connector(path):
            client_socket,server_socket=socket.socketpair()
            def serve():
                with server_socket:
                    hello=receive_request(server_socket)
                    descriptor={**contract_descriptor(POLICY),'broker_generation':generation[0]}
                    send_frame(server_socket,{'version':PROTOCOL_VERSION,'request_id':hello['request_id'],'ok':True,'result':descriptor},max_bytes=MAX_RESPONSE_BYTES)
                    try:req=receive_request(server_socket)
                    except Exception:return
                    observed.append(req)
                    self.assertLessEqual(req['deadline']-time.monotonic(),30)
                    result=service.verify_target(schemas.VerifyTargetRequest(**req['payload'])).model_dump(mode='json')
                    send_frame(server_socket,{'version':PROTOCOL_VERSION,'request_id':req['request_id'],'ok':True,'result':result},max_bytes=MAX_RESPONSE_BYTES)
            thread=threading.Thread(target=serve);thread.start();threads.append(thread)
            return client_socket
        proxy=BrokerClient(socket_path=Path('/unused'),policy=POLICY,connector=connector)
        issued=proxy.verify_target(schemas.VerifyTargetRequest(target=7))
        self.assertEqual(issued.status,'verified')
        generation[0]='broker_'+'b'*32
        result=proxy.read_target_messages(schemas.ReadTargetMessagesRequest(target_handle=issued.target_handle,message_ids=[11]))
        self.assertEqual(result.status,'invalid_handle')
        self.assertEqual([r['operation'] for r in observed],['verify_target'])
        for t in threads:t.join(2);self.assertFalse(t.is_alive())

class ClosedSchemaTests(unittest.TestCase):
    def test_prior_twenty_four_schemas_annotations_and_order_are_preserved(self):
        baseline=json.loads((Path(__file__).parent/'fixtures/legacy_0_13_tool_hashes.json').read_text())
        current=schema_document(build_server(policy=RuntimePolicy()))
        tools={row['name']:row for row in current['tools']}
        self.assertEqual(len(baseline),24)
        for name,digest in baseline.items():
            self.assertEqual(fingerprint(tools[name]),digest,name)
        names=[t.name for t in build_server(policy=RuntimePolicy())._tool_manager.list_tools()]
        self.assertEqual(names[24:27],['verify_target','read_target_messages','read_attachment_page'])
        self.assertEqual(set(names[:24]),set(baseline))

    def test_response_contract_and_untrusted_flags_are_strict_types(self):
        issued=SearchService(client=TargetProvider(),owns_client=False).verify_target(schemas.VerifyTargetRequest(target=7)).model_dump(mode='json')
        for field,value in [('contract_version',True),('contract_version','1')]:
            raw=copy.deepcopy(issued);raw[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValidationError):
                schemas.VerifyTargetResponse.model_validate(raw)
        raw=copy.deepcopy(issued);raw['target']['untrusted']=1
        with self.assertRaises(ValidationError):schemas.VerifyTargetResponse.model_validate(raw)
        service=SearchService(client=TargetProvider(),owns_client=False)
        issued=service.verify_target(schemas.VerifyTargetRequest(target=7))
        response=service.read_target_messages(schemas.ReadTargetMessagesRequest(target_handle=issued.target_handle,message_ids=[11])).model_dump(mode='json')
        for field,value in [('contract_version',True),('coverage_complete',1)]:
            raw=copy.deepcopy(response);raw['messages'][field]=value
            with self.subTest(field=field),self.assertRaises(ValidationError):
                schemas.ReadTargetMessagesResponse.model_validate(raw)
    def test_terminal_wrappers_cannot_include_metadata_or_nested_content(self):
        self.assertTrue(hasattr(schemas,'VerifyTargetResponse'),'closed verification response is missing')
        for cls in [schemas.VerifyTargetResponse,schemas.ReadTargetMessagesResponse]:
            with self.assertRaises(ValidationError):cls(status='error',target_handle='target_'+'a'*64)
            with self.assertRaises(ValidationError):cls(status='error',provider_error='private')
        with self.assertRaises(ValidationError):schemas.ReadTargetMessagesResponse(status='complete')

if __name__=='__main__':unittest.main()
