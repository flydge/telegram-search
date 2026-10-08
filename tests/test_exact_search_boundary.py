"""Synthetic F5 boundary evidence; no private Telegram fixtures."""
from __future__ import annotations

import copy
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp import schemas
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.message_reader import read_messages
from test_message_reader import Provider, message


def response(mid=30, cursor='search_' + 'a' * 64, scanned=3, processed=1, pages=1):
    provider = Provider({mid: message(mid)})
    item = read_messages(provider, schemas.ReadMessagesRequest(anchors=[{'chat_id':7,'message_id':mid}])).results[0].model_dump(mode='json')
    item['status'] = 'match'
    wall = datetime.fromtimestamp(time.time(), timezone.utc)
    return {'status': 'page' if cursor else 'partial', 'scope': {
        'target':7, 'query':'needle', 'mode':'latest', 'date_from':None,
        'date_to':wall.isoformat(), 'upper_message_id':30,
        'order':'message_id_desc', 'candidate_limit':200, 'provider_page_limit':10,
        'page_limit':1, 'expires_at':(wall+timedelta(seconds=300)).isoformat()},
        'results':[item], 'page_complete':True, 'scope_complete':False, 'has_more':None,
        'next_cursor':cursor, 'stop_reason':'page_limit' if cursor else 'provider_end_unverified',
        'scanned_candidates':scanned, 'processed_candidates':processed, 'provider_pages':pages,
        'provider_coverage':'tdlib_lexical_unverified'}


class ExactSearchSchemaTests(unittest.TestCase):
    def test_request_strict_text_exact_target_and_half_open_dates(self):
        self.assertTrue(hasattr(schemas, 'SearchMessagesRequest'), 'F5 exact search request is missing')
        args = {'target':7, 'query':'  needle  ', 'mode':'latest'}
        self.assertEqual(schemas.SearchMessagesRequest(**args).query, 'needle')
        for changes in ({'target':True},{'target':'7'},{'target':0},{'target':2**53},
                        {'query':{}},{'query':12},{'query':'*'},{'query':'\x00\u200b'},
                        {'query':' '},{'query':'a'*513},{'limit':True},{'limit':21},
                        {'cursor':'history_'+'a'*64},{'mode':'interval'},
                        {'date_to':'2024-01-01T00:00:00Z'},{'filter':{}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                schemas.SearchMessagesRequest(**{**args, **changes})
        interval = schemas.SearchMessagesRequest(**{**args,'mode':'interval',
            'date_from':'2023-01-01T01:00:00+01:00','date_to':'2024-01-01T00:00:00Z'})
        self.assertEqual(interval.date_from.hour, 0)

    def test_response_honest_outcomes_exact_anchors_and_bounded_progress(self):
        self.assertTrue(hasattr(schemas, 'SearchMessagesResponse'), 'F5 exact search response is missing')
        self.assertEqual(schemas.SearchMessagesResponse.model_validate(response()).results[0].status, 'match')
        variants = []
        for field, value in (('processed_candidates',4),('provider_pages',0),('scope_complete',0),
                             ('has_more',True),('processed_candidates',True),('page_complete',False)):
            bad=response(); bad[field]=value; variants.append(bad)
        bad=response(); bad['results'][0]['status']='evidence_changed'; variants.append(bad)
        bad=response(); bad['results'][0]['anchor']['chat_id']=8; variants.append(bad)
        bad=response(); bad['results'][0]['message']['date_utc']='2027-01-01T00:00:00Z'; variants.append(bad)
        bad=response(scanned=1, processed=1, pages=10); variants.append(bad)
        bad=response(); bad['scope']=None; variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                schemas.SearchMessagesResponse.model_validate(bad)
        changed=response(cursor=None); changed['results'][0].update(status='evidence_changed', coverage_complete=False,
            message=None, issues=['evidence_changed']); changed['page_complete']=False
        self.assertEqual(schemas.SearchMessagesResponse.model_validate(changed).results[0].status, 'evidence_changed')
        partial=response(cursor=None); partial['results'][0].update(status='partial', coverage_complete=False, issues=[])
        partial['page_complete']=False
        with self.assertRaises(ValueError): schemas.SearchMessagesResponse.model_validate(partial)
    def test_match_and_partial_require_supported_lexical_body_and_role(self):
        for status in ('match','partial'):
            for changes in ({'content_kind':'sticker','text_role':'none','text':None},
                            {'content_kind':'video_note','text_role':'caption'},
                            {'content_kind':'text','text_role':'caption'},
                            {'content_kind':'photo','text_role':'text'},
                            {'content_kind':'text','text_role':'none'}, {'text':None}):
                item=response()['results'][0]
                item.update(status=status,coverage_complete=status=='match',
                            issues=[] if status=='match' else ['sender_unavailable'])
                item['message'].update(changes)
                with self.subTest(status=status,changes=changes),self.assertRaises(ValueError):
                    schemas.SearchMessageResult.model_validate(item)
        for kind,role in (('text','text'),('document','caption'),('photo','caption'),
                          ('video','caption'),('audio','caption'),('animation','caption'),('voice_note','caption')):
            item=response()['results'][0];item['message'].update(content_kind=kind,text_role=role)
            self.assertEqual(schemas.SearchMessageResult.model_validate(item).status,'match')

    def test_search_contract_version_rejects_boolean_and_numeric_coercion(self):
        for value in (True,False,1.0,'1',2):
            with self.subTest(value=value),self.assertRaises(ValueError):
                schemas.SearchMessagesResponse(status='error',stop_reason='broker_unavailable',contract_version=value)



class ExactSearchProxyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(BrokerClient, 'search_messages'), 'F5 proxy method is missing')
        wall_patch = patch('telegram_search_mcp.broker_client.time.time', return_value=float(int(time.time())))
        wall_patch.start()
        self.addCleanup(wall_patch.stop)

    def request(self, **kw):
        return schemas.SearchMessagesRequest(target=7, query='needle', mode='latest', limit=1, **kw)

    def proxy(self, values):
        class Proxy(BrokerClient):
            def _request(self, op, payload):
                if op == 'release_client': return {}
                return values.pop(0)
        return Proxy(socket_path=Path('/unused'), policy=RuntimePolicy(enabled_capabilities=('search_messages',)))

    def continuation(self, first, **kw):
        value=response(mid=20, cursor='search_'+'b'*64, processed=2)
        value['scope']=copy.deepcopy(first['scope']); value.update(kw)
        return value

    def test_proxy_rejects_foreign_frozen_scope_current_body_and_terminal_forgery(self):
        variants=[]
        for field, value in (('target',8),('query','other'),('page_limit',2),
                             ('upper_message_id',40),('date_to','2027-01-01T00:00:00Z'),
                             ('expires_at','2099-01-01T00:00:00Z')):
            first=response(); bad=self.continuation(first, status='partial', next_cursor=None, stop_reason='provider_end_unverified')
            bad['scope'][field]=value; variants.append((first,bad))
        first=response(); bad=self.continuation(first); bad['results']=copy.deepcopy(first['results']); variants.append((first,bad))
        first=response(); bad=self.continuation(first); bad['results'][0]['message']['date_utc']='2023-11-14T22:13:20'; variants.append((first,bad))
        for first,bad in variants:
            proxy=self.proxy([first,bad]); a=proxy.search_messages(self.request())
            self.assertEqual(a.status,'page')
            b=proxy.search_messages(self.request(cursor=a.next_cursor))
            self.assertEqual((b.status,b.stop_reason,b.results),('error','broker_unavailable',[]))
        for key,value in (('target',8),('query','other'),('page_limit',2)):
            bad=response(); bad['scope'][key]=value
            self.assertEqual(self.proxy([bad]).search_messages(self.request()).status,'error')

    def test_proxy_observation_counters_bound_deltas_and_pending_only_caps(self):
        first=response()
        for changes in ({'scanned_candidates':2}, {'processed_candidates':1}, {'provider_pages':0},
                        {'scanned_candidates':4}, {'processed_candidates':102,'scanned_candidates':103,'provider_pages':6}):
            proxy=self.proxy([copy.deepcopy(first),self.continuation(first,**changes)])
            a=proxy.search_messages(self.request())
            self.assertEqual(proxy.search_messages(self.request(cursor=a.next_cursor)).status,'error')
        capped=response(scanned=200,pages=10)
        proxy=self.proxy([capped,self.continuation(capped,scanned_candidates=200,provider_pages=10)])
        a=proxy.search_messages(self.request()); b=proxy.search_messages(self.request(cursor=a.next_cursor))
        self.assertEqual(b.status,'page'); self.assertEqual(b.processed_candidates,2)

    def test_proxy_empty_advancing_pages_and_partial_page_complete(self):
        first=response(scanned=0,processed=0,pages=1)
        first['results']=[];first['page_complete']=False
        second=copy.deepcopy(first);second.update(provider_pages=2,next_cursor='search_'+'b'*64)
        proxy=self.proxy([first,second]);a=proxy.search_messages(self.request())
        self.assertEqual(a.status,'page')
        self.assertEqual(proxy.search_messages(self.request(cursor=a.next_cursor)).status,'page')
        self.assertTrue(self.proxy([response(cursor=None)]).search_messages(self.request()).page_complete)

    def test_proxy_single_use_bindings_all_chain_cursor_replay_and_close(self):
        first=response(); second=self.continuation(first)
        third=self.continuation(first,next_cursor=first['next_cursor'],processed_candidates=3)
        proxy=self.proxy([first,second,third]);a=proxy.search_messages(self.request())
        altered=schemas.SearchMessagesRequest(target=7,query='other',mode='latest',limit=1,cursor=a.next_cursor)
        self.assertEqual(proxy.search_messages(altered).status,'invalid_cursor')
        b=proxy.search_messages(self.request(cursor=a.next_cursor))
        self.assertEqual(proxy.search_messages(self.request(cursor=a.next_cursor)).status,'invalid_cursor')
        self.assertEqual(proxy.search_messages(self.request(cursor=b.next_cursor)).status,'error')
        proxy=self.proxy([response()]);a=proxy.search_messages(self.request());proxy.close()
        self.assertEqual(proxy.search_messages(self.request(cursor=a.next_cursor)).status,'invalid_cursor')

    def test_proxy_fixed_monotonic_expiry_including_inflight_terminal(self):
        first=response();terminal=self.continuation(first,status='partial',next_cursor=None,stop_reason='provider_end_unverified')
        clock=[10.0]; values=[first,terminal]
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                value=values.pop(0)
                if payload.get('cursor'):clock[0]=311.0
                return value
        proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('search_messages',)))
        with patch('telegram_search_mcp.broker_client.time.monotonic',side_effect=lambda:clock[0]):
            a=proxy.search_messages(self.request());clock[0]=309.0
            b=proxy.search_messages(self.request(cursor=a.next_cursor))
        self.assertEqual((b.status,b.results),('error',[]))
        expired=response(cursor=None);expired['scope']['expires_at']='2000-01-01T00:00:00Z'
        self.assertEqual(self.proxy([expired]).search_messages(self.request()).status,'error')

    def test_proxy_rejects_broker_claimed_initial_lifetime_above_fixed_ttl(self):
        for cursor in ('search_'+'a'*64,None):
            forged=response(cursor=cursor)
            forged['scope']['expires_at']='2099-01-01T00:00:00Z'
            self.assertEqual(self.proxy([forged]).search_messages(self.request()).status,'error')

    def test_proxy_initial_latest_date_is_bound_to_request_wall_envelope_including_terminal(self):
        for cursor in ('search_'+'a'*64,None):
            for days in (1,-1):
                forged=response(cursor=cursor)
                forged['scope']['date_to']=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat()
                if days==1:
                    forged['results'][0]['message']['date_utc']=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
                result=self.proxy([forged]).search_messages(self.request())
                self.assertEqual((result.status,result.stop_reason,result.results),('error','broker_unavailable',[]))

    def test_proxy_four_active_requests_capacity_and_close_inflight(self):
        import threading
        entered=threading.Event(); release=threading.Event(); outcomes=[]
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                if op=='release_client':return {}
                entered.set();release.wait(5);return response()
        proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('search_messages',)))
        threads=[threading.Thread(target=lambda:outcomes.append(proxy.search_messages(self.request()))) for _ in range(4)]
        for worker in threads:worker.start()
        entered.wait(2)
        for _ in range(200):
            if proxy._search_active==4:break
            __import__('time').sleep(.005)
        self.assertEqual(proxy.search_messages(self.request()).status,'capacity_exhausted')
        proxy.close();release.set()
        for worker in threads:worker.join(3)
        self.assertEqual([r.status for r in outcomes],['error']*4)


class ExactSearchRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_appended_tool_strict_arguments_independent_disabled_capability(self):
        from mcp import Client
        from telegram_search_mcp.server import build_server
        called=[]
        class Service:
            def search_messages(self, request):called.append(request);return schemas.SearchMessagesResponse(status='partial',stop_reason='provider_end_unverified')
            def close(self):pass
        args={'target':7,'query':'needle','mode':'latest','limit':1}
        for enabled in (False,True):
            policy=RuntimePolicy(enabled_capabilities=('search_messages',)) if enabled else RuntimePolicy(enabled_capabilities=('read','read_history'))
            async with Client(build_server(service_factory=Service,policy=policy)) as consumer:
                tools=(await consumer.list_tools()).tools
                self.assertEqual(len(tools),39,'F5 appended tool missing')
                tool=next(t for t in tools if t.name=='search_messages');self.assertEqual(tool.name,'search_messages')
                self.assertTrue(tool.annotations.read_only_hint);self.assertFalse(tool.annotations.idempotent_hint)
                self.assertFalse(tool.input_schema['additionalProperties'])
                result=await consumer.call_tool('search_messages',args)
                self.assertEqual(result.is_error,not enabled)
                if not enabled:self.assertEqual(called,[]);continue
                for extra in ({'target':True},{'target':'7'},{'query':{}},{'query':'*'},{'query':'\u200b'},
                              {'limit':'1'},{'mode':'interval'},{'cursor':'guess'},{'filter':{}},{'path':'/private'}):
                    self.assertTrue((await consumer.call_tool('search_messages',{**args,**extra})).is_error)


class ExactSearchReaderBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_service_sdk_continues_after_twenty_with_current_changed_evidence(self):
        from mcp import Client
        from telegram_search_mcp.search_service import SearchService
        from telegram_search_mcp.server import build_server
        from test_exact_search_reader import SearchProvider
        provider=SearchProvider({i:message(i) for i in range(1,46)})
        service=SearchService(client=provider,owns_client=False)
        self.addCleanup(service.close)
        args={'target':7,'query':'provider lexical','mode':'latest','limit':20}
        async with Client(build_server(service_factory=lambda:service,policy=RuntimePolicy(enabled_capabilities=('search_messages',)))) as consumer:
            a=(await consumer.call_tool('search_messages',args)).structured_content
            self.assertEqual([x['anchor']['message_id'] for x in a['results']],list(range(45,25,-1)))
            provider.hydration[25]=message(25,edit_date=1700000001)
            b=(await consumer.call_tool('search_messages',{**args,'cursor':a['next_cursor']})).structured_content
            self.assertEqual([x['anchor']['message_id'] for x in b['results']],list(range(25,5,-1)))
            self.assertEqual(b['results'][0]['status'],'evidence_changed')
            self.assertIsNone(b['results'][0]['message']);self.assertFalse(b['page_complete'])
            c=(await consumer.call_tool('search_messages',{**args,'cursor':b['next_cursor']})).structured_content
            self.assertEqual(c['stop_reason'],'provider_end_unverified');self.assertFalse(c['scope_complete'])
            self.assertIsNone(c['has_more']);self.assertIsNone(c['next_cursor'])
            replay=(await consumer.call_tool('search_messages',{**args,'cursor':a['next_cursor']})).structured_content
            self.assertEqual(replay['status'],'invalid_cursor')

    async def test_broker_independent_capability_client_lease_release_idle_generation(self):
        import tempfile
        import time
        from telegram_search_mcp.artifact_store import ArtifactStore
        from telegram_search_mcp.broker import Broker
        from telegram_search_mcp.contract import CompatibilityError
        from test_exact_search_reader import SearchProvider
        for enabled in (False,True):
            with tempfile.TemporaryDirectory() as folder:
                provider=SearchProvider({i:message(i) for i in (30,20,10)})
                clock=[10.0]
                policy=RuntimePolicy(enabled_capabilities=('search_messages',)) if enabled else RuntimePolicy(enabled_capabilities=('read','read_history'))
                def make_broker():
                    value=Broker(socket_path=Path(folder)/'s',artifact_store=ArtifactStore(cache_dir=Path(folder)/'cache'),
                        client_factory=lambda:provider,policy=policy,context_clock=lambda:clock[0],context_ttl_seconds=5)
                    self.addCleanup(value._executor.shutdown);return value
                broker=make_broker()
                def call(client='client_'+'a'*24,op='search_messages',**kw):
                    payload={'target':7,'query':'needle','mode':'latest','limit':1,**kw} if op=='search_messages' else {}
                    return broker._dispatch({'operation':op,'client_id':client,'payload':payload,
                        'deadline':time.monotonic()+10,'broker_generation':broker._generation})
                if not enabled:
                    with self.assertRaises(CompatibilityError):call()
                    self.assertEqual(provider.calls,[]);continue
                first=call()
                self.assertEqual(call(client='client_'+'b'*24,cursor=first['next_cursor'])['status'],'invalid_cursor')
                second=call(cursor=first['next_cursor'])
                self.assertEqual(second['results'][0]['anchor']['message_id'],20)
                call(op='release_client')
                self.assertEqual(call(cursor=second['next_cursor'])['status'],'invalid_cursor')
                first=call();clock[0]=16
                self.assertEqual(call(cursor=first['next_cursor'])['status'],'invalid_cursor')
                generation=broker._generation;broker=make_broker()
                with self.assertRaises(CompatibilityError):
                    broker._dispatch({'operation':'search_messages','client_id':'client_'+'a'*24,'payload':{},
                        'deadline':time.monotonic()+10,'broker_generation':generation})

    async def test_real_reader_proxy_rejects_wrong_binding_without_losing_valid_continuation(self):
        from telegram_search_mcp.search_service import SearchService
        from test_exact_search_reader import SearchProvider
        provider=SearchProvider({i:message(i) for i in (30,20,10)})
        service=SearchService(client=provider,owns_client=False)
        self.addCleanup(service.close)
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                if op=='release_client':return {}
                return service.search_messages(schemas.SearchMessagesRequest.model_validate(payload)).model_dump(mode='json')
        proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('search_messages',)))
        request=schemas.SearchMessagesRequest(target=7,query='needle',mode='latest',limit=1)
        a=proxy.search_messages(request);self.assertEqual(a.status,'page')
        self.assertEqual(proxy.search_messages(request.model_copy(update={'query':'different','cursor':a.next_cursor})).status,'invalid_cursor')
        b=proxy.search_messages(request.model_copy(update={'cursor':a.next_cursor}))
        self.assertEqual([x.anchor.message_id for x in b.results],[20])
