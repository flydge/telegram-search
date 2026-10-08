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
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError
from telegram_search_mcp import schemas
from telegram_search_mcp.search_service import SearchService
from telegram_search_mcp.server import build_server
from test_forum_reader import ForumProvider, page


def response(ids=(1,), cursor='topic_'+'a'*64, scanned=1, pages=1):
    return {'status':'page' if cursor else 'partial','scope':{'target':7,'limit':1,
        'candidate_limit':200,'provider_page_limit':10,
        'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=300)).isoformat()},
        'results':[{'chat_id':7,'topic':{'kind':'forum','id':i},'status':'complete',
            'metadata':{'name':{'value':'Topic','untrusted':True,'sanitized':False,'truncated':False},
                'creation_date_utc':'2023-11-14T22:13:20Z','is_general':i==1,'is_closed':False,'is_hidden':False},
            'coverage_complete':True,'issues':[]} for i in ids],
        'page_complete':bool(ids),'scope_complete':False,'has_more':None,'next_cursor':cursor,
        'stop_reason':'page_limit' if cursor else 'provider_nonprogress',
        'scanned_candidates':scanned,'provider_pages':pages}


class ForumBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def request(self, **kw): return schemas.ListTopicsRequest(target=7,limit=1,**kw)
    def proxy(self, values):
        class Proxy(BrokerClient):
            def _request(self, op, payload):
                if op=='release_client':return {}
                return values.pop(0)
        return Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('list_topics',)))

    async def test_public_topic_listing_strict_opt_in_and_inventory(self):
        self.assertTrue(hasattr(SearchService,'list_topics'),'topic service entry point missing')
        for enabled in (False,True):
            p=ForumProvider([page([1,2,3]),page([4],(20,200,4))])
            service=SearchService(client=p,owns_client=False)
            policy=RuntimePolicy(enabled_capabilities=('list_topics',)) if enabled else RuntimePolicy()
            async with Client(build_server(service_factory=lambda:service,policy=policy)) as consumer:
                tools=(await consumer.list_tools()).tools
                self.assertEqual(len(tools),39)
                tool=next(t for t in tools if t.name=='list_topics')
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.idempotent_hint)
                self.assertFalse(tool.input_schema['additionalProperties'])
                result=await consumer.call_tool('list_topics',{'target':7,'limit':1})
                if not enabled:
                    self.assertTrue(result.is_error);self.assertEqual(p.calls,[]);continue
                body=result.structured_content
                self.assertEqual(body['results'][0]['topic']['id'],1)
                self.assertEqual((body['scanned_candidates'],body['provider_pages']),(3,1))
                for change in ({'target':True},{'target':'7'},{'target':0},{'limit':True},
                               {'limit':'1'},{'limit':21},{'cursor':'guess'},{'query':'private'}):
                    self.assertTrue((await consumer.call_tool('list_topics',{'target':7,'limit':1,**change})).is_error)
                follow=await consumer.call_tool('list_topics',{'target':7,'limit':1,'cursor':body['next_cursor']})
                self.assertEqual(follow.structured_content['results'][0]['topic']['id'],2)
                self.assertEqual((follow.structured_content['scanned_candidates'],follow.structured_content['provider_pages']),(3,1))
                drained=await consumer.call_tool('list_topics',{'target':7,'limit':1,'cursor':follow.structured_content['next_cursor']})
                self.assertEqual(drained.structured_content['results'][0]['topic']['id'],3)
                self.assertEqual((drained.structured_content['scanned_candidates'],drained.structured_content['provider_pages']),(3,1))
                advanced=await consumer.call_tool('list_topics',{'target':7,'limit':1,'cursor':drained.structured_content['next_cursor']})
                self.assertEqual(advanced.structured_content['results'][0]['topic']['id'],4)
                self.assertEqual((advanced.structured_content['scanned_candidates'],advanced.structured_content['provider_pages']),(4,2))
                replay=await consumer.call_tool('list_topics',{'target':7,'limit':1,'cursor':body['next_cursor']})
                self.assertEqual(replay.structured_content['status'],'invalid_cursor')

    async def test_proxy_rejects_foreign_target_limit_and_result_count(self):
        self.assertTrue(hasattr(BrokerClient,'list_topics'),'topic proxy missing')
        candidates=[]
        for field,value in (('target',8),('limit',2),('candidate_limit',201),('provider_page_limit',11)):
            bad=response();bad['scope'][field]=value;candidates.append(bad)
        bad=response();bad['results'][0]['chat_id']=8;candidates.append(bad)
        bad=response(ids=(1,2),scanned=2);candidates.append(bad)
        bad=response(pages=2);candidates.append(bad)
        for bad in candidates:
            result=self.proxy([bad]).list_topics(self.request())
            self.assertEqual(result.status,'error');self.assertEqual(result.results,[])

    async def test_proxy_frozen_scope_disjoint_ids_monotone_counters_and_token_progress(self):
        first=response()
        valid=response(ids=(2,),cursor='topic_'+'b'*64,scanned=2,pages=2)
        valid['scope']=copy.deepcopy(first['scope'])
        variants=[]
        for field,value in (('expires_at','2099-01-01T00:00:00Z'),('limit',2)):
            bad=copy.deepcopy(valid);bad['scope'][field]=value;variants.append(bad)
        for field,value in (('scanned_candidates',0),('provider_pages',0),('next_cursor',first['next_cursor'])):
            bad=copy.deepcopy(valid);bad[field]=value;variants.append(bad)
        bad=copy.deepcopy(valid);bad['results']=copy.deepcopy(first['results']);variants.append(bad)
        bad=copy.deepcopy(valid);bad['scanned_candidates']=1;variants.append(bad)
        bad=copy.deepcopy(valid);bad['provider_pages']=1;variants.append(bad)
        bad=copy.deepcopy(valid);bad['provider_pages']=3;variants.append(bad)
        bad=copy.deepcopy(valid);bad['provider_pages']=True;variants.append(bad)
        bad=copy.deepcopy(valid);bad['scope_complete']=0;variants.append(bad)
        for bad in variants:
            proxy=self.proxy([copy.deepcopy(first),bad])
            a=proxy.list_topics(self.request());self.assertEqual(a.status,'page')
            b=proxy.list_topics(self.request(cursor=a.next_cursor))
            self.assertEqual(b.status,'error');self.assertEqual(b.results,[])
        proxy=self.proxy([first,valid])
        a=proxy.list_topics(self.request());b=proxy.list_topics(self.request(cursor=a.next_cursor))
        self.assertEqual(b.status,'page');self.assertEqual([x.topic.id for x in b.results],[2])

    async def test_proxy_retains_fixed_expiry_and_single_use_after_failure(self):
        first=response();second=response(ids=(2,),cursor='topic_'+'b'*64,scanned=2,pages=2)
        second['scope']=copy.deepcopy(first['scope'])
        proxy=self.proxy([first,second])
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=10):
            a=proxy.list_topics(self.request())
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=309):
            b=proxy.list_topics(self.request(cursor=a.next_cursor))
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=311):
            self.assertEqual(proxy.list_topics(self.request(cursor=b.next_cursor)).status,'invalid_cursor')
        proxy=self.proxy([first,{'malformed':'response'}])
        a=proxy.list_topics(self.request())
        self.assertEqual(proxy.list_topics(self.request(cursor=a.next_cursor)).status,'error')
        self.assertEqual(proxy.list_topics(self.request(cursor=a.next_cursor)).status,'invalid_cursor')

    async def test_proxy_checks_full_cursor_and_topic_chain_and_request_binding(self):
        first=response()
        second=response(ids=(2,),cursor='topic_'+'b'*64,scanned=2,pages=2)
        second['scope']=copy.deepcopy(first['scope'])
        third=response(ids=(3,),cursor=first['next_cursor'],scanned=3,pages=3)
        third['scope']=copy.deepcopy(first['scope'])
        for bad in (third, {**copy.deepcopy(third), 'next_cursor':'topic_'+'c'*64,
                           'results':copy.deepcopy(first['results'])}):
            proxy=self.proxy([copy.deepcopy(first),copy.deepcopy(second),bad])
            a=proxy.list_topics(self.request());b=proxy.list_topics(self.request(cursor=a.next_cursor))
            self.assertEqual(proxy.list_topics(self.request(cursor=b.next_cursor)).status,'error')
        proxy=self.proxy([first,second])
        a=proxy.list_topics(self.request())
        for altered in (schemas.ListTopicsRequest(target=8,limit=1,cursor=a.next_cursor),
                        schemas.ListTopicsRequest(target=7,limit=2,cursor=a.next_cursor)):
            self.assertEqual(proxy.list_topics(altered).status,'invalid_cursor')
        self.assertEqual(proxy.list_topics(self.request(cursor=a.next_cursor)).status,'page')

    async def test_proxy_drains_overreturned_observations_before_next_native_page(self):
        first=response(scanned=3)
        values=[first]
        for tid,scanned,pages in ((2,3,1),(3,3,1),(4,5,2),(5,5,2)):
            value=response(ids=(tid,),cursor='topic_'+format(tid,'x')*64,scanned=scanned,pages=pages)
            value['scope']=copy.deepcopy(first['scope']);values.append(value)
        proxy=self.proxy(values)
        cursor=None;observed=[]
        for scanned,pages in ((3,1),(3,1),(3,1),(5,2),(5,2)):
            result=proxy.list_topics(self.request(cursor=cursor))
            self.assertEqual(result.status,'page')
            self.assertEqual((result.scanned_candidates,result.provider_pages),(scanned,pages))
            observed.extend(x.topic.id for x in result.results);cursor=result.next_cursor
        self.assertEqual(observed,[1,2,3,4,5])

    async def test_proxy_rejects_invented_buffered_topics_and_scans_without_native_page(self):
        first=response(scanned=3)
        second=response(ids=(2,),cursor='topic_'+'b'*64,scanned=3,pages=1)
        third=response(ids=(3,),cursor='topic_'+'c'*64,scanned=3,pages=1)
        for value in (second,third):value['scope']=copy.deepcopy(first['scope'])
        fourth=response(ids=(4,),cursor='topic_'+'d'*64,scanned=3,pages=1)
        fourth['scope']=copy.deepcopy(first['scope'])
        bad=copy.deepcopy(second);bad['scanned_candidates']=4
        for sequence in ([copy.deepcopy(first),bad],[copy.deepcopy(first),second,third,fourth]):
            count=len(sequence);proxy=self.proxy(sequence);cursor=None
            for index in range(count):
                result=proxy.list_topics(self.request(cursor=cursor));cursor=result.next_cursor
                self.assertEqual(result.status,'error' if index==count-1 else 'page')
            self.assertEqual(result.results,[])

    async def test_proxy_cap_continuation_only_allows_remaining_observations(self):
        for stop_on_last in (True,False):
            first=response(cursor='topic_'+'0'*64,scanned=2)
            values=[first]
            for tid in range(2,11):
                value=response(ids=(tid,),cursor='topic_'+format(tid,'x')*64,scanned=tid+1,pages=tid)
                value['scope']=copy.deepcopy(first['scope']);values.append(value)
            final=response(ids=(11,),cursor=None if stop_on_last else 'topic_'+'f'*64,scanned=11,pages=10)
            final['scope']=copy.deepcopy(first['scope']);values.append(final)
            proxy=self.proxy(values);cursor=None
            for _ in range(10):
                result=proxy.list_topics(self.request(cursor=cursor))
                self.assertEqual(result.status,'page');cursor=result.next_cursor
            result=proxy.list_topics(self.request(cursor=cursor))
            self.assertEqual(result.status,'partial' if stop_on_last else 'error')
        first=response(scanned=200);second=response(ids=(2,),cursor='topic_'+'b'*64,scanned=200,pages=1)
        second['scope']=copy.deepcopy(first['scope'])
        proxy=self.proxy([first,second]);a=proxy.list_topics(self.request())
        self.assertEqual(a.status,'page')
        self.assertEqual(proxy.list_topics(self.request(cursor=a.next_cursor)).status,'page')

    async def test_proxy_rejects_native_attempt_after_prior_candidate_cap(self):
        for pages_delta in (0,1):
            first=response(scanned=200)
            second=response(ids=(2,),cursor='topic_'+'b'*64,scanned=200,pages=1+pages_delta)
            second['scope']=copy.deepcopy(first['scope'])
            proxy=self.proxy([first,second]);a=proxy.list_topics(self.request())
            self.assertEqual(a.status,'page')
            b=proxy.list_topics(self.request(cursor=a.next_cursor))
            self.assertEqual(b.status,'page' if pages_delta==0 else 'error')
            self.assertEqual([x.topic.id for x in b.results],[2] if pages_delta==0 else [])

    async def test_schema_requires_provider_page_for_evidence_and_possible_pending_at_cap(self):
        for bad in (response(cursor=None,pages=0),response(scanned=1,pages=10)):
            with self.assertRaises(ValueError):schemas.ListTopicsResponse.model_validate(bad)
        for good in (response(scanned=3),response(scanned=200),response(scanned=2,pages=10)):
            self.assertEqual(schemas.ListTopicsResponse.model_validate(good).status,'page')

    async def test_proxy_atomic_capacity_replay_and_close(self):
        values=[response(cursor='topic_'+str(i)*64) for i in range(1,5)]
        entered=threading.Event();release=threading.Event();slow=[False]
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                if op=='release_client':return {}
                if slow[0]:
                    entered.set();release.wait(3)
                    return response(ids=(2,),cursor='topic_'+'f'*64,scanned=2,pages=2)
                return values.pop(0)
        proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('list_topics',)))
        pages=[proxy.list_topics(self.request()) for _ in range(4)]
        self.assertTrue(all(p.next_cursor for p in pages))
        self.assertEqual(proxy.list_topics(self.request()).status,'capacity_exhausted')
        slow[0]=True;out=[];req=self.request(cursor=pages[0].next_cursor)
        thread=threading.Thread(target=lambda:out.append(proxy.list_topics(req)))
        thread.start();self.assertTrue(entered.wait(1))
        self.assertEqual(proxy.list_topics(req).status,'invalid_cursor')
        proxy.close();release.set();thread.join(3)
        self.assertEqual(out[0].status,'error')
        self.assertEqual(proxy.list_topics(self.request()).status,'invalid_cursor')

    async def test_broker_leased_state_opt_in_client_binding_and_release(self):
        self.assertTrue(hasattr(SearchService,'list_topics'),'topic service entry point missing')
        for enabled in (False,True):
            with tempfile.TemporaryDirectory() as d:
                p=ForumProvider([page([1]),page([2],(20,200,3))])
                policy=RuntimePolicy(enabled_capabilities=('list_topics',)) if enabled else RuntimePolicy()
                b=Broker(socket_path=Path(d)/'s',artifact_store=ArtifactStore(cache_dir=Path(d)/'cache'),
                    client_factory=lambda:p,policy=policy)
                self.addCleanup(b._executor.shutdown)
                def call(client='client_'+'a'*24,op='list_topics',**kw):
                    return b._dispatch({'operation':op,'client_id':client,
                        'payload':{'target':7,'limit':1,**kw} if op=='list_topics' else {},
                        'deadline':time.monotonic()+10,'broker_generation':b._generation})
                if not enabled:
                    with self.assertRaises(CompatibilityError):call()
                    self.assertEqual(p.calls,[]);continue
                first=call()
                self.assertEqual(call(client='client_'+'b'*24,cursor=first['next_cursor'])['status'],'invalid_cursor')
                second=call(cursor=first['next_cursor'])
                self.assertEqual(second['results'][0]['topic']['id'],2)
                call(op='release_client')
                self.assertEqual(call(cursor=second['next_cursor'])['status'],'invalid_cursor')
