from __future__ import annotations

import copy
import tempfile
import threading
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
from telegram_search_mcp.contract import CompatibilityError
from telegram_search_mcp.search_service import SearchService
from telegram_search_mcp.server import build_server
from test_forum_reader import ForumProvider, topic
from test_message_reader import message, Provider
from telegram_search_mcp.message_reader import read_messages


def response(mid=30, cursor='topic_history_'+'a'*64, scanned=3, processed=1, pages=1):
    provider=Provider({mid:message(mid,topic_id={'@type':'messageTopicForum','forum_topic_id':2})})
    item=read_messages(provider,schemas.ReadMessagesRequest(anchors=[{'chat_id':7,'message_id':mid}])).results[0]
    return {'status':'page' if cursor else 'partial', 'scope':{'target':7,'topic':{'kind':'forum','id':2},
        'mode':'latest','date_from':None,'date_to':'2026-01-01T00:00:00Z','upper_message_id':30,
        'order':'message_id_desc','candidate_limit':200,'provider_page_limit':10,'page_limit':1,
        'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=300)).isoformat()},
        'topic':{'chat_id':7,'topic':{'kind':'forum','id':2},'status':'complete','metadata':{
            'name':{'value':'Topic','untrusted':True,'sanitized':False,'truncated':False},
            'creation_date_utc':'2023-11-14T22:13:20Z','is_general':False,'is_closed':True,'is_hidden':False},
            'coverage_complete':True,'issues':[]},'results':[item.model_dump(mode='json')],
        'page_complete':True,'scope_complete':False,'has_more':None,'next_cursor':cursor,
        'stop_reason':'page_limit' if cursor else 'provider_end_unverified','scanned_candidates':scanned,
        'processed_candidates':processed,'provider_pages':pages,'provider_coverage':'tdlib_observed_unverified'}


class TopicHistorySchemaTests(unittest.TestCase):
    def test_request_requires_strict_forum_family_and_half_open_dates(self):
        self.assertTrue(hasattr(schemas,'ReadTopicHistoryRequest'),'F4c strict topic history contract missing')
        args={'target':7,'topic':{'kind':'forum','id':2},'mode':'latest','limit':1}
        self.assertEqual(schemas.ReadTopicHistoryRequest(**args).topic.id,2)
        for changes in ({'target':True},{'target':'7'},{'target':0},{'topic':{'kind':'thread','id':2}},
                        {'topic':{'id':True}},{'topic':{'id':2**31}},{'limit':True},{'limit':21},
                        {'cursor':'history_'+'a'*64},{'date_to':'2024-01-01T00:00:00Z'},
                        {'mode':'interval'},{'provider_options':{}}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                schemas.ReadTopicHistoryRequest(**{**args,**changes})
        interval={**args,'mode':'interval','date_from':'2023-01-01T01:00:00+01:00','date_to':'2024-01-01T00:00:00Z'}
        self.assertEqual(schemas.ReadTopicHistoryRequest(**interval).date_from.hour,0)

    def test_response_rejects_foreign_membership_impossible_counters_and_completeness(self):
        self.assertTrue(hasattr(schemas,'ReadTopicHistoryResponse'),'F4c response contract missing')
        self.assertEqual(schemas.ReadTopicHistoryResponse.model_validate(response()).processed_candidates,1)
        variants=[]
        for field,value in (('processed_candidates',4),('provider_pages',0),('scope_complete',0),
                            ('has_more',True),('processed_candidates',True),('page_complete',False)):
            bad=response();bad[field]=value;variants.append(bad)
        bad=response();bad['results'][0]['message']['topic']={'kind':'thread','id':2};variants.append(bad)
        bad=response();bad['topic']['topic']['id']=3;variants.append(bad)
        bad=response(scanned=1,processed=1,pages=10);variants.append(bad)
        bad=response();bad['scope']=None;variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad),self.assertRaises(ValueError):schemas.ReadTopicHistoryResponse.model_validate(bad)


class HistoryForumProvider(ForumProvider):
    def __init__(self):
        super().__init__([])
        self.messages={i:message(i,topic_id={'@type':'messageTopicForum','forum_topic_id':2}) for i in (30,20,10)}
    def get_forum_topic_history(self, chat_id, forum_topic_id, *, from_message_id=0, limit=20):
        self.calls.append(('history',chat_id,forum_topic_id,from_message_id,limit))
        rows=[self.messages[i] for i in (30,20,10) if not from_message_id or i<=from_message_id]
        return {'@type':'messages','total_count':-1,'messages':copy.deepcopy(rows[:limit])}
    def get_message(self, chat_id, mid):
        self.calls.append(('message',chat_id,mid));return copy.deepcopy(self.messages[mid])
    def get_sender_identity(self, sender):
        self.calls.append(('sender',));return {'kind':'user','id':17,'display_name':'Reader'}


class TopicHistoryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def request(self, **kw):
        return schemas.ReadTopicHistoryRequest(target=7,topic={'kind':'forum','id':2},mode='latest',limit=1,**kw)
    def proxy(self, values):
        class Proxy(BrokerClient):
            def _request(self, op, payload):
                if op=='release_client':return {}
                return values.pop(0)
        return Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('read_topic_history',)))
    def continuation(self, first, **kw):
        value=response(mid=20,cursor='topic_history_'+'b'*64,scanned=3,processed=2,pages=1)
        value['scope']=copy.deepcopy(first['scope']);value.update(kw);return value

    async def test_proxy_rejects_foreign_scope_metadata_and_body_before_return(self):
        self.assertTrue(hasattr(BrokerClient,'read_topic_history'),'topic history proxy missing')
        variants=[]
        for field,value in (('target',8),('topic',{'kind':'forum','id':3}),('mode','interval'),('page_limit',2),
                            ('date_from','2023-01-01T00:00:00Z'),('candidate_limit',201)):
            bad=response();bad['scope'][field]=value;variants.append(bad)
        bad=response();bad['topic']['chat_id']=8;variants.append(bad)
        bad=response();bad['results'][0]['message']['topic']={'kind':'forum','id':3};variants.append(bad)
        bad=response();bad['results'][0]['message']['date_utc']='2023-11-14T22:13:20';variants.append(bad)
        for bad in variants:
            result=self.proxy([bad]).read_topic_history(self.request())
            self.assertEqual(result.status,'error');self.assertEqual(result.results,[])

    async def test_proxy_rejects_frozen_scope_and_full_chain_id_or_cursor_replay(self):
        self.assertTrue(hasattr(BrokerClient,'read_topic_history'),'topic history proxy missing')
        first=response();second=self.continuation(first)
        variants=[]
        for field,value in (('upper_message_id',40),('date_to','2027-01-01T00:00:00Z'),('expires_at','2099-01-01T00:00:00Z')):
            bad=self.continuation(first);bad['scope'][field]=value;variants.append(bad)
        bad=self.continuation(first);bad['results']=copy.deepcopy(first['results']);variants.append(bad)
        bad=self.continuation(first,next_cursor=first['next_cursor']);variants.append(bad)
        for bad in variants:
            proxy=self.proxy([copy.deepcopy(first),bad]);a=proxy.read_topic_history(self.request())
            self.assertEqual(a.status,'page')
            self.assertEqual(proxy.read_topic_history(self.request(cursor=a.next_cursor)).status,'error')
        third=self.continuation(first,next_cursor=first['next_cursor'],processed_candidates=3)
        proxy=self.proxy([first,second,third]);a=proxy.read_topic_history(self.request())
        b=proxy.read_topic_history(self.request(cursor=a.next_cursor))
        self.assertEqual(b.status,'page')
        self.assertEqual(proxy.read_topic_history(self.request(cursor=b.next_cursor)).status,'error')

    async def test_proxy_buffer_drain_and_monotone_counters_and_emitted_delta(self):
        self.assertTrue(hasattr(BrokerClient,'read_topic_history'),'topic history proxy missing')
        first=response()
        for changes in ({'scanned_candidates':2},{'processed_candidates':0},{'processed_candidates':1},
                        {'provider_pages':0},{'scanned_candidates':4},
                        {'processed_candidates':102,'scanned_candidates':103,'provider_pages':2}):
            second=self.continuation(first,**changes)
            proxy=self.proxy([copy.deepcopy(first),second]);a=proxy.read_topic_history(self.request())
            self.assertEqual(proxy.read_topic_history(self.request(cursor=a.next_cursor)).status,'error')
        proxy=self.proxy([first,self.continuation(first)])
        a=proxy.read_topic_history(self.request());b=proxy.read_topic_history(self.request(cursor=a.next_cursor))
        self.assertEqual(b.status,'page');self.assertEqual((b.scanned_candidates,b.processed_candidates,b.provider_pages),(3,2,1))

    async def test_proxy_cap_allows_only_pending_processing_without_new_native_attempt(self):
        self.assertTrue(hasattr(BrokerClient,'read_topic_history'),'topic history proxy missing')
        for cap in ('scans','pages'):
            first=response(scanned=200 if cap=='scans' else 3,pages=10 if cap=='pages' else 1)
            second=self.continuation(first,scanned_candidates=first['scanned_candidates'],provider_pages=first['provider_pages'])
            proxy=self.proxy([copy.deepcopy(first),copy.deepcopy(second)])
            a=proxy.read_topic_history(self.request());self.assertEqual(a.status,'page')
            self.assertEqual(proxy.read_topic_history(self.request(cursor=a.next_cursor)).status,'page')
            bad=copy.deepcopy(second);bad['provider_pages']+=1
            proxy=self.proxy([copy.deepcopy(first),bad]);a=proxy.read_topic_history(self.request())
            self.assertEqual(proxy.read_topic_history(self.request(cursor=a.next_cursor)).status,'error')
        first=response(scanned=3,pages=10)
        final=self.continuation(first,processed_candidates=3,provider_pages=10)
        proxy=self.proxy([first,final]);a=proxy.read_topic_history(self.request())
        self.assertEqual(proxy.read_topic_history(self.request(cursor=a.next_cursor)).status,'error')

    async def test_proxy_requires_processed_progress_even_when_no_bodies_match(self):
        self.assertTrue(hasattr(BrokerClient,'read_topic_history'),'topic history proxy missing')
        first=response();first['results']=[];first['page_complete']=False
        second=self.continuation(first);second['results']=[];second['page_complete']=False
        proxy=self.proxy([first,second]);a=proxy.read_topic_history(self.request())
        self.assertEqual(a.status,'page')
        self.assertEqual(proxy.read_topic_history(self.request(cursor=a.next_cursor)).status,'page')

    async def test_proxy_single_use_binding_fixed_expiry_close_and_atomic_capacity(self):
        self.assertTrue(hasattr(BrokerClient,'read_topic_history'),'topic history proxy missing')
        first=response();second=self.continuation(first)
        proxy=self.proxy([first,second])
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=10):a=proxy.read_topic_history(self.request())
        altered=schemas.ReadTopicHistoryRequest(target=7,topic={'id':3},mode='latest',limit=1,cursor=a.next_cursor)
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=11):
            self.assertEqual(proxy.read_topic_history(altered).status,'invalid_cursor')
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=309):b=proxy.read_topic_history(self.request(cursor=a.next_cursor))
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=311):
            self.assertEqual(proxy.read_topic_history(self.request(cursor=b.next_cursor)).status,'invalid_cursor')
        proxy=self.proxy([response(),{'raw':'private secret'}]);a=proxy.read_topic_history(self.request())
        error=proxy.read_topic_history(self.request(cursor=a.next_cursor))
        self.assertEqual(error.status,'error');self.assertNotIn('secret',error.model_dump_json())
        self.assertEqual(proxy.read_topic_history(self.request(cursor=a.next_cursor)).status,'invalid_cursor')
        values=[response(cursor='topic_history_'+str(i)*64) for i in range(1,5)]
        entered=threading.Event();release=threading.Event();slow=[False]
        slow_result=self.continuation(values[0])
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                if op=='release_client':return {}
                if slow[0]:entered.set();release.wait(3);return slow_result
                return values.pop(0)
        proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('read_topic_history',)))
        pages=[proxy.read_topic_history(self.request()) for _ in range(4)]
        self.assertTrue(all(p.next_cursor for p in pages))
        self.assertEqual(proxy.read_topic_history(self.request()).status,'capacity_exhausted')
        slow[0]=True;out=[];req=self.request(cursor=pages[0].next_cursor)
        thread=threading.Thread(target=lambda:out.append(proxy.read_topic_history(req)));thread.start()
        self.assertTrue(entered.wait(1));self.assertEqual(proxy.read_topic_history(req).status,'invalid_cursor')
        self.assertEqual(proxy.read_topic_history(self.request()).status,'capacity_exhausted')
        proxy.close();release.set();thread.join(3)
        self.assertEqual(out[0].status,'error');self.assertEqual(proxy.read_topic_history(self.request()).status,'invalid_cursor')

    async def test_public_twenty_first_tool_strict_independent_opt_in_and_cursor(self):
        self.assertTrue(hasattr(SearchService,'read_topic_history'),'topic history service missing')
        for enabled in (False,True):
            provider=HistoryForumProvider();service=SearchService(client=provider,owns_client=False)
            policy=RuntimePolicy(enabled_capabilities=('read_topic_history',)) if enabled else RuntimePolicy(enabled_capabilities=('list_topics','read_history'))
            async with Client(build_server(service_factory=lambda:service,policy=policy)) as consumer:
                tools=(await consumer.list_tools()).tools;self.assertEqual(len(tools),39)
                tool=next(t for t in tools if t.name=='read_topic_history')
                self.assertTrue(tool.annotations.read_only_hint);self.assertFalse(tool.annotations.idempotent_hint)
                self.assertFalse(tool.input_schema['additionalProperties'])
                args={'target':7,'topic':{'kind':'forum','id':2},'mode':'latest','limit':1}
                result=await consumer.call_tool('read_topic_history',args)
                if not enabled:
                    self.assertTrue(result.is_error);self.assertEqual(provider.calls,[]);continue
                body=result.structured_content
                self.assertEqual(body['results'][0]['anchor']['message_id'],30)
                self.assertEqual(body['results'][0]['message']['topic'],{'kind':'forum','id':2})
                for extra in ({'target':True},{'target':'7'},{'topic':{'kind':'thread','id':2}},
                              {'topic':{'id':True}},{'mode':'interval'},{'cursor':'guess'},{'path':'/private'}):
                    self.assertTrue((await consumer.call_tool('read_topic_history',{**args,**extra})).is_error)
                follow=await consumer.call_tool('read_topic_history',{**args,'cursor':body['next_cursor']})
                self.assertEqual(follow.structured_content['results'][0]['anchor']['message_id'],20)
                replay=await consumer.call_tool('read_topic_history',{**args,'cursor':body['next_cursor']})
                self.assertEqual(replay.structured_content['status'],'invalid_cursor')

    async def test_broker_capability_client_lease_release_idle_expiry_generation(self):
        self.assertTrue(hasattr(SearchService,'read_topic_history'),'topic history service missing')
        for enabled in (False,True):
            with tempfile.TemporaryDirectory() as d:
                provider=HistoryForumProvider();clock=[10.0]
                policy=RuntimePolicy(enabled_capabilities=('read_topic_history',)) if enabled else RuntimePolicy(enabled_capabilities=('list_topics','read_history'))
                def broker():
                    value=Broker(socket_path=Path(d)/'s',artifact_store=ArtifactStore(cache_dir=Path(d)/'cache'),
                        client_factory=lambda:provider,policy=policy,context_clock=lambda:clock[0],context_ttl_seconds=5)
                    self.addCleanup(value._executor.shutdown);return value
                b=broker()
                def call(client='client_'+'a'*24,op='read_topic_history',**kw):
                    payload={'target':7,'topic':{'kind':'forum','id':2},'mode':'latest','limit':1,**kw} if op=='read_topic_history' else {}
                    return b._dispatch({'operation':op,'client_id':client,'payload':payload,'deadline':time.monotonic()+10,'broker_generation':b._generation})
                if not enabled:
                    with self.assertRaises(CompatibilityError):call()
                    self.assertEqual(provider.calls,[]);continue
                first=call();self.assertEqual(call(client='client_'+'b'*24,cursor=first['next_cursor'])['status'],'invalid_cursor')
                second=call(cursor=first['next_cursor']);self.assertEqual(second['results'][0]['anchor']['message_id'],20)
                call(op='release_client');self.assertEqual(call(cursor=second['next_cursor'])['status'],'invalid_cursor')
                first=call();clock[0]=16;self.assertEqual(call(cursor=first['next_cursor'])['status'],'invalid_cursor')
                old_generation=b._generation;b=broker()
                with self.assertRaises(CompatibilityError):
                    b._dispatch({'operation':'read_topic_history','client_id':'client_'+'a'*24,
                        'payload':{},'deadline':time.monotonic()+10,'broker_generation':old_generation})

    async def test_proxy_discards_initial_expired_terminal_evidence(self):
        expired=response(cursor=None)
        expired['scope']['expires_at']='2000-01-01T00:00:00Z'
        result=self.proxy([expired]).read_topic_history(self.request())
        self.assertEqual(result.status,'error');self.assertEqual(result.results,[])

    async def test_proxy_discards_terminal_evidence_that_expires_inflight(self):
        first=response();terminal=self.continuation(first,status='partial',next_cursor=None,stop_reason='provider_end_unverified')
        clock=[10.0];values=[first,terminal]
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                value=values.pop(0)
                if payload.get('cursor'):clock[0]=311.0
                return value
        proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('read_topic_history',)))
        with patch('telegram_search_mcp.broker_client.time.monotonic',side_effect=lambda:clock[0]):
            first_page=proxy.read_topic_history(self.request());self.assertEqual(first_page.status,'page')
            clock[0]=309.0
            result=proxy.read_topic_history(self.request(cursor=first_page.next_cursor))
            self.assertEqual(result.status,'error');self.assertEqual(result.results,[])
