from __future__ import annotations

import tempfile
import copy
import time
import threading
import unittest
from pathlib import Path

from mcp import Client
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError
from telegram_search_mcp.schemas import ReadHistoryRequest
from telegram_search_mcp.search_service import SearchService
from telegram_search_mcp.server import build_server
from test_history_reader import HistoryProvider
from test_message_reader import message


class HistoryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_capacity_concurrent_replay_and_close(self):
        p=HistoryProvider({i:message(i) for i in (30,20,10)})
        service=SearchService(client=p,owns_client=False)
        entered=threading.Event();release=threading.Event();slow=[False]
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                if op=='release_client':return {}
                if slow[0]:entered.set();release.wait(3)
                return service.read_history(ReadHistoryRequest.model_validate(payload)).model_dump(mode='json')
        proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('read_history',)))
        request=ReadHistoryRequest(target=7,mode='latest',limit=1)
        pages=[proxy.read_history(request) for _ in range(4)]
        self.assertTrue(all(p.next_cursor for p in pages))
        self.assertEqual(proxy.read_history(request).status,'capacity_exhausted')
        continuation=ReadHistoryRequest(target=7,mode='latest',limit=1,cursor=pages[0].next_cursor)
        slow[0]=True;out=[]
        thread=threading.Thread(target=lambda:out.append(proxy.read_history(continuation)))
        thread.start();self.assertTrue(entered.wait(1))
        self.assertEqual(proxy.read_history(continuation).status,'invalid_cursor')
        proxy.close();release.set();thread.join(3)
        self.assertEqual(out[0].status,'error')
        self.assertEqual(proxy.read_history(request).status,'invalid_cursor')

    async def test_broker_idle_expiry_and_new_generation_invalidate_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            p=HistoryProvider({i:message(i) for i in (30,20,10)})
            clock=[10.0];policy=RuntimePolicy(enabled_capabilities=('read_history',))
            def broker():
                b=Broker(socket_path=Path(d)/'s',artifact_store=ArtifactStore(cache_dir=Path(d)/'cache'),
                    client_factory=lambda:p,policy=policy,context_clock=lambda:clock[0],context_ttl_seconds=5)
                self.addCleanup(b._executor.shutdown);return b
            b=broker()
            def req(cursor=None):return {'operation':'read_history','client_id':'client_'+'a'*24,
                'payload':{'target':7,'mode':'latest','limit':1,'cursor':cursor},
                'deadline':time.monotonic()+10,'broker_generation':b._generation}
            first=b._dispatch(req());old=req(first['next_cursor']);clock[0]=16
            self.assertEqual(b._dispatch(req(first['next_cursor']))['status'],'invalid_cursor')
            new=broker()
            with self.assertRaises(CompatibilityError):new._dispatch(old)
            old['broker_generation']=new._generation
            self.assertEqual(new._dispatch(old)['status'],'invalid_cursor')

    async def test_proxy_rejects_naive_dates_without_leaking_exception(self):
        p=HistoryProvider({30:message(30)})
        response=SearchService(client=p,owns_client=False).read_history(
            ReadHistoryRequest(target=7,mode='latest',limit=1)).model_dump(mode='json')
        class Proxy(BrokerClient):
            def _request(self,*a,**k):return response
        for mutate in ('scope','message'):
            candidate=copy.deepcopy(response)
            if mutate=='scope':candidate['scope']['date_to']='2024-01-01T00:00:00'
            else:candidate['results'][0]['message']['date_utc']='2023-11-14T22:13:20'
            original=response;response=candidate
            proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('read_history',)))
            result=proxy.read_history(ReadHistoryRequest(target=7,mode='latest',limit=1))
            self.assertEqual(result.status,'error');self.assertEqual(result.results,[])
            response=original

    async def test_proxy_continuation_rejects_scope_expansion_and_repeated_page(self):
        p=HistoryProvider({i:message(i) for i in (30,20,10)})
        service=SearchService(client=p,owns_client=False)
        first=service.read_history(ReadHistoryRequest(target=7,mode='latest',limit=1)).model_dump(mode='json')
        second=service.read_history(ReadHistoryRequest(target=7,mode='latest',limit=1,cursor=first['next_cursor'])).model_dump(mode='json')
        variants=[]
        for name,value in [('upper_message_id',40),('date_to','2099-01-01T00:00:00Z'),('expires_at','2099-01-01T00:00:00Z')]:
            altered=copy.deepcopy(second);altered['scope'][name]=value;variants.append(altered)
        variants.append(copy.deepcopy(first))
        for altered in variants:
            with self.subTest(scope=altered['scope']):
                responses=[first,altered]
                class Proxy(BrokerClient):
                    def _request(self,*a,**k):return responses.pop(0)
                proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('read_history',)))
                a=proxy.read_history(ReadHistoryRequest(target=7,mode='latest',limit=1))
                self.assertEqual(a.status,'page')
                b=proxy.read_history(ReadHistoryRequest(target=7,mode='latest',limit=1,cursor=a.next_cursor))
                self.assertEqual(b.status,'error');self.assertEqual(b.results,[])

    async def test_public_tool_is_strict_opt_in_and_consumes_cursor_once(self):
        self.assertTrue(hasattr(SearchService,'read_history'),'history service entry point missing')
        for enabled in (False,True):
            p=HistoryProvider({i:message(i) for i in (30,20,10)})
            policy=RuntimePolicy(enabled_capabilities=('read_history',)) if enabled else RuntimePolicy()
            service=SearchService(client=p,owns_client=False)
            async with Client(build_server(service_factory=lambda:service,policy=policy)) as consumer:
                listing=await consumer.list_tools()
                tool=next((t for t in listing.tools if t.name=='read_history'),None)
                self.assertIsNotNone(tool)
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.idempotent_hint)
                self.assertFalse(tool.input_schema['additionalProperties'])
                result=await consumer.call_tool('read_history',{'target':7,'mode':'latest','limit':1})
                if not enabled:
                    self.assertTrue(result.is_error);self.assertEqual(p.calls,[])
                else:
                    body=result.structured_content
                    self.assertEqual(body['results'][0]['anchor']['message_id'],30)
                    self.assertFalse(body['scope_complete'])
                    for extra in [{'target':True},{'target':'7'},{'limit':True},{'mode':'interval'},
                                  {'date_from':1700000000},{'cursor':'guess'},{'path':'/private'}]:
                        args={'target':7,'mode':'latest','limit':1,**extra}
                        self.assertTrue((await consumer.call_tool('read_history',args)).is_error)
                    next_result=await consumer.call_tool('read_history',{'target':7,'mode':'latest','limit':1,'cursor':body['next_cursor']})
                    self.assertEqual(next_result.structured_content['results'][0]['anchor']['message_id'],20)

    async def test_broker_client_scope_release_and_generation_guards(self):
        self.assertTrue(hasattr(SearchService,'read_history'),'history service entry point missing')
        for enabled in (False,True):
            with tempfile.TemporaryDirectory() as d:
                p=HistoryProvider({i:message(i) for i in (30,20,10)})
                policy=RuntimePolicy(enabled_capabilities=('read_history',)) if enabled else RuntimePolicy()
                b=Broker(socket_path=Path(d)/'s',artifact_store=ArtifactStore(cache_dir=Path(d)/'cache'),
                    client_factory=lambda:p,policy=policy)
                self.addCleanup(b._executor.shutdown)
                def call(client='client_'+'a'*24,op='read_history',**kw):
                    payload={'target':7,'mode':'latest','limit':1,**kw} if op=='read_history' else {}
                    return b._dispatch({'operation':op,'client_id':client,'payload':payload,
                        'deadline':time.monotonic()+10,'broker_generation':b._generation})
                if not enabled:
                    with self.assertRaises(CompatibilityError):call()
                    self.assertEqual(p.calls,[])
                    continue
                first=call();cursor=first['next_cursor']
                self.assertEqual(call(client='client_'+'b'*24,cursor=cursor)['status'],'invalid_cursor')
                second=call(cursor=cursor)
                self.assertEqual(second['results'][0]['anchor']['message_id'],20)
                call(op='release_client')
                self.assertEqual(call(cursor=second['next_cursor'])['status'],'invalid_cursor')

    async def test_proxy_rejects_different_scope_and_foreign_evidence(self):
        self.assertTrue(hasattr(BrokerClient,'read_history'),'history proxy missing')
        p=HistoryProvider({30:message(30)})
        service=SearchService(client=p,owns_client=False)
        response=service.read_history(ReadHistoryRequest(target=7,mode='latest',limit=1)).model_dump(mode='json')
        class Proxy(BrokerClient):
            def _request(self,*a,**k):return response
        proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('read_history',)))
        for req in [ReadHistoryRequest(target=8,mode='latest',limit=1),
                    ReadHistoryRequest(target=7,mode='latest',limit=2),
                    ReadHistoryRequest(target=7,mode='interval',date_from='2023-01-01T00:00:00Z',date_to='2024-01-01T00:00:00Z')]:
            result=proxy.read_history(req)
            self.assertEqual(result.status,'error')
            self.assertEqual(result.results,[])
