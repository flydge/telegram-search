from __future__ import annotations
import copy
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from mcp import Client
from telegram_search_mcp import schemas
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError, schema_document
from telegram_search_mcp.search_service import SearchService
from telegram_search_mcp.server import build_server
from telegram_search_mcp.tdjson import TDLibClient
from test_chat_list_reader import Provider, chat
from test_forum_tdjson import ready_client


def fixture(processed=1, cid=9, cursor='chats_'+'a'*64, scope=None):
    wall=datetime.now(timezone.utc)
    scope=scope or {'selection':'main','selected_lists':['main'],'limit':1,'prefix_limit_per_list':200,
                   'expires_at':(wall+timedelta(seconds=299)).isoformat()}
    return {'status':'page' if cursor else 'prefix_exhausted_unverified','scope':scope,
        'results':[{'chat_id':cid,'kind':'private','title':{'value':'Synthetic','untrusted':True,'sanitized':False,'truncated':False},
                    'observed_list':'main','observed_rank':processed,'current_lists':['main'],
                    'unread_count':2,'unread_mention_count':1,'unread_reaction_count':0,'is_marked_as_unread':False,
                    'hydrated_at':wall.isoformat()}],
        'list_coverage':[{'chat_list':'main','native_state':'observed','observed':3,'processed':processed,
                          'returned':processed,'omitted':0,'pending':3-processed,'issues':{}}],
        'observed_candidates':3,'processed_candidates':processed,'returned_candidates':processed,
        'omitted_candidates':0,'pending_candidates':3-processed,'processed_this_page':1,'returned_this_page':1,
        'omitted_this_page':0,'snapshot_complete':True,'page_complete':True,'scope_complete':False,'has_more':None,
        'next_cursor':cursor,'stop_reason':'page_limit' if cursor else 'prefix_exhausted_unverified'}


class ChatListNativeTests(unittest.TestCase):
    def operation(self,client,name):
        method=getattr(client,name,None);self.assertTrue(callable(method),f'{name} adapter missing');return method
    def test_native_snapshots_preserve_raw_envelope_and_strict_request_shape(self):
        page={'@type':'chats','total_count':500,'chat_ids':[9,3]}
        client,raw=ready_client({'getChats':[page,page],'getChat':[chat(9)]})
        op=self.operation(client,'get_chat_list_snapshot')
        self.assertEqual(op('main',limit=200)['chat_ids'],[9,3]);op('archive',limit=100)
        self.assertEqual(self.operation(client,'get_chat_metadata')(9)['id'],9)
        self.assertEqual([{k:v for k,v in r.items() if k!='@extra'} for r in raw.sent[1:]],
          [{'@type':'getChats','chat_list':{'@type':'chatListMain'},'limit':200},
           {'@type':'getChats','chat_list':{'@type':'chatListArchive'},'limit':100},
           {'@type':'getChat','chat_id':9}])
        for scope,limit in [('both',100),('main',True),('main',201),('main','1'),('*',100)]:
            with self.assertRaises(ValueError):op(scope,limit=limit)
        for cid in (True,0,'9',2**53):
            with self.assertRaises(ValueError):self.operation(client,'get_chat_metadata')(cid)
    def test_raw_adapter_does_not_silently_normalize_bad_provider_envelope(self):
        client,_=ready_client({'getChats':[{'@type':'chats','total_count':'1','chat_ids':[9,True]}]})
        raw=self.operation(client,'get_chat_list_snapshot')('main',limit=200)
        self.assertEqual(raw['chat_ids'],[9,True]);self.assertEqual(raw['total_count'],'1')


class ChatListBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def request(self,**kw):return schemas.ListChatsRequest(scope='main',limit=1,**kw)
    def proxy(self,values):
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                if op=='release_client':return {}
                return values.pop(0)
        return Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('list_chats',)))
    async def test_public_append_strict_scope_and_independent_capability(self):
        self.assertTrue(hasattr(SearchService,'list_chats'),'list_chats service missing')
        for enabled in (False,True):
            p=Provider();service=SearchService(client=p,owns_client=False)
            policy=RuntimePolicy(enabled_capabilities=('list_chats',)) if enabled else RuntimePolicy()
            async with Client(build_server(service_factory=lambda:service,policy=policy)) as consumer:
                tools=(await consumer.list_tools()).tools;self.assertEqual(len(tools),39)
                tool=next(t for t in tools if t.name=='list_chats');self.assertFalse(tool.input_schema['additionalProperties'])
                self.assertTrue(tool.annotations.read_only_hint);self.assertFalse(tool.annotations.idempotent_hint)
                result=await consumer.call_tool('list_chats',{'scope':'main','limit':1})
                if not enabled:
                    self.assertTrue(result.is_error);self.assertEqual(p.calls,[]);continue
                a=result.structured_content;self.assertEqual(a['results'][0]['chat_id'],9)
                for bad in ({},{'scope':'*'},{'scope':True},{'scope':'main','limit':True},
                  {'scope':'main','limit':'1'},{'scope':'main','limit':21},{'scope':'main','path':'/private'},
                  {'scope':'main','cursor':'chats_'+'x'*64}):
                    self.assertTrue((await consumer.call_tool('list_chats',bad)).is_error)
                b=(await consumer.call_tool('list_chats',{'scope':'main','limit':1,'cursor':a['next_cursor']})).structured_content
                self.assertEqual(b['results'][0]['chat_id'],3)
                replay=(await consumer.call_tool('list_chats',{'scope':'main','limit':1,'cursor':a['next_cursor']})).structured_content
                self.assertEqual(replay['status'],'invalid_cursor')
    async def test_proxy_rejects_forged_scope_counter_order_membership_and_replay(self):
        self.assertTrue(hasattr(BrokerClient,'list_chats'),'list_chats proxy missing')
        first=fixture();valid=fixture(processed=2,cid=3,cursor='chats_'+'b'*64,scope=copy.deepcopy(first['scope']))
        variants=[]
        for field,value in [('selection','archive'),('limit',2),('expires_at','2099-01-01T00:00:00Z')]:
            bad=copy.deepcopy(valid);bad['scope'][field]=value;variants.append(bad)
        for field,value in [('unread_count',True),('current_lists',['archive']),('observed_rank',1),('chat_id',9)]:
            bad=copy.deepcopy(valid);bad['results'][0][field]=value;variants.append(bad)
        bad=copy.deepcopy(valid);bad['next_cursor']=first['next_cursor'];variants.append(bad)
        bad=copy.deepcopy(valid);bad['list_coverage'][0].update(observed=4,pending=2)
        bad.update(observed_candidates=4,pending_candidates=2);variants.append(bad)
        bad=copy.deepcopy(valid);bad['list_coverage'][0]['returned']=1;variants.append(bad)
        bad=copy.deepcopy(valid);bad['scope_complete']=False;bad['has_more']=True;variants.append(bad)
        for bad in variants:
            proxy=self.proxy([copy.deepcopy(first),bad]);a=proxy.list_chats(self.request());self.assertEqual(a.status,'page')
            b=proxy.list_chats(self.request(cursor=a.next_cursor));self.assertEqual(b.status,'error');self.assertEqual(b.results,[])
            self.assertEqual(proxy.list_chats(self.request(cursor=a.next_cursor)).status,'invalid_cursor')
        proxy=self.proxy([first,valid]);a=proxy.list_chats(self.request());b=proxy.list_chats(self.request(cursor=a.next_cursor))
        self.assertEqual([r.chat_id for r in b.results],[3])
        self.assertEqual(proxy.list_chats(self.request(cursor=a.next_cursor)).status,'invalid_cursor')
    async def test_proxy_initial_counter_and_expiry_and_page_delta_cannot_be_forged(self):
        self.assertTrue(hasattr(BrokerClient,'list_chats'),'list_chats proxy missing')
        bad=fixture(processed=2);self.assertEqual(self.proxy([bad]).list_chats(self.request()).status,'error')
        bad=fixture();bad['scope']['expires_at']='2099-01-01T00:00:00Z'
        self.assertEqual(self.proxy([bad]).list_chats(self.request()).status,'error')
        first=fixture();second=fixture(processed=2,cid=3,cursor='chats_'+'b'*64,scope=first['scope'])
        proxy=self.proxy([first,second])
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=10):a=proxy.list_chats(self.request())
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=311):
            self.assertEqual(proxy.list_chats(self.request(cursor=a.next_cursor)).status,'invalid_cursor')
        proxy=self.proxy([fixture()]);a=proxy.list_chats(self.request())
        wrong=schemas.ListChatsRequest(scope='archive',limit=1,cursor=a.next_cursor)
        self.assertEqual(proxy.list_chats(wrong).status,'invalid_cursor')
        proxy.close();self.assertEqual(proxy.list_chats(self.request(cursor=a.next_cursor)).status,'invalid_cursor')
    async def test_output_rejects_coerced_contract_and_contradictory_terminal_reason(self):
        for changes in ({'contract_version':True}, {'status':'blocked','next_cursor':None,'stop_reason':'provider_error','page_complete':False},
                        {'status':'error','next_cursor':None,'stop_reason':'page_limit','page_complete':False}):
            bad=fixture();bad.update(changes)
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):schemas.ListChatsResponse.model_validate(bad)
    async def test_proxy_rejects_metadata_timestamp_outside_this_call(self):
        for timestamp in ('2020-01-01T00:00:00Z', (datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()):
            bad=fixture();bad['results'][0]['hydrated_at']=timestamp
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.proxy([bad]).list_chats(self.request()).status,'error')
    async def test_proxy_capacity_includes_pending_scans_and_retains_no_row_metadata(self):
        values=[fixture(cursor='chats_'+format(i,'064x')) for i in range(1,5)]
        proxy=self.proxy(values)
        for _ in range(4):self.assertEqual(proxy.list_chats(self.request()).status,'page')
        self.assertEqual(proxy.list_chats(self.request()).status,'capacity_exhausted')
        retained=repr(proxy._chat_list_states)
        self.assertNotIn('Synthetic',retained);self.assertNotIn('unread_count',retained)
    async def test_output_rows_cannot_borrow_another_lists_returned_count(self):
        bad=fixture()
        bad['scope'].update(selection='both',selected_lists=['main','archive'],limit=2,prefix_limit_per_list=100)
        bad['list_coverage']=[{'chat_list':'main','native_state':'observed','observed':1,'processed':1,
            'returned':0,'omitted':1,'pending':0,'issues':{'secret':1}},
            {'chat_list':'archive','native_state':'observed','observed':2,'processed':1,
            'returned':1,'omitted':0,'pending':1,'issues':{}}]
        bad.update(processed_candidates=2,omitted_candidates=1,pending_candidates=1,
                   processed_this_page=2,omitted_this_page=1,page_complete=False)
        with self.assertRaises(ValueError):schemas.ListChatsResponse.model_validate(bad)
    async def test_proxy_rejects_successful_expired_empty_prefix(self):
        bad=fixture(cursor=None);bad['scope']['expires_at']='1970-01-01T00:00:00Z'
        bad.update(results=[],observed_candidates=0,processed_candidates=0,returned_candidates=0,
                   pending_candidates=0,processed_this_page=0,returned_this_page=0,page_complete=False)
        bad['list_coverage'][0].update(observed=0,processed=0,returned=0,pending=0)
        self.assertEqual(self.proxy([bad]).list_chats(self.request()).status,'error')
        bad.update(status='partial',stop_reason='invalid_cursor')
        self.assertEqual(self.proxy([bad]).list_chats(self.request()).status,'partial')
    async def test_broker_independent_capability_lease_identity_and_generation(self):
        self.assertTrue(hasattr(SearchService,'list_chats'),'list_chats service missing')
        for enabled in (False,True):
            with tempfile.TemporaryDirectory() as d:
                p=Provider();policy=RuntimePolicy(enabled_capabilities=('list_chats',)) if enabled else RuntimePolicy()
                b=Broker(socket_path=Path(d)/'s',artifact_store=ArtifactStore(cache_dir=Path(d)/'cache'),
                         client_factory=lambda:p,policy=policy)
                self.addCleanup(b._executor.shutdown)
                def call(client='client_'+'a'*24,operation='list_chats',**kw):
                    payload={'scope':'main','limit':1,**kw} if operation=='list_chats' else {}
                    return b._dispatch({'operation':operation,'client_id':client,'payload':payload,
                        'deadline':time.monotonic()+10,'broker_generation':b._generation})
                if not enabled:
                    with self.assertRaises(CompatibilityError):call()
                    self.assertEqual(p.calls,[]);continue
                a=call();self.assertEqual(call(client='client_'+'b'*24,cursor=a['next_cursor'])['status'],'invalid_cursor')
                next_page=call(cursor=a['next_cursor']);self.assertEqual(next_page['results'][0]['chat_id'],3)
                call(operation='release_client');self.assertEqual(call(cursor=next_page['next_cursor'])['status'],'invalid_cursor')
                with self.assertRaises(CompatibilityError):
                    b._dispatch({'operation':'list_chats','client_id':'client_'+'a'*24,'payload':{},
                        'deadline':time.monotonic()+10,'broker_generation':'invalid'})

if __name__=='__main__':unittest.main()
