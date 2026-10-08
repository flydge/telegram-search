from __future__ import annotations

import copy
import importlib
import importlib.util
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from telegram_search_mcp import schemas
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.tdjson import AuthorizationBlocked, TDLibError
from test_message_reader import message


class SelectedProvider:
    """Native boundary double: same message IDs in different chats remain separate."""
    def __init__(self, targets=(7, 8, 9, 10, 11), count=3):
        self.messages = {(chat, mid): message(mid, chat_id=chat) for chat in targets
                         for mid in range(1, count + 1)}
        self.calls, self.pages, self.chats, self.hydration = [], {}, {}, {}
        self.account = 42
        self.account_hook = None
        self.search_hook = None
        self.chat_hook = None
        self.deadlines = []

    @contextmanager
    def request_budget(self, deadline):
        self.deadlines.append(deadline)
        yield

    def ensure_ready(self):
        self.calls.append(('ready',))

    def get_account_id(self):
        self.calls.append(('account',))
        if self.account_hook:
            self.account_hook(self)
        return self.account

    def resolve_target(self, target):
        self.calls.append(('chat', target))
        if self.chat_hook:
            self.chat_hook(self, target)
        value = self.chats.get(target, {'@type': 'chat', 'id': target,
                    'type': {'@type': 'chatTypePrivate', 'user_id': target}, 'title': 'Not retained'})
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def search_chat_messages(self, target, query, *, from_message_id, limit, **filters):
        assert type(target) is int and target in {key[0] for key in self.messages}
        assert type(query) is str and type(from_message_id) is int and 1 <= limit <= 20
        assert set(filters) <= {'sender'}
        self.calls.append(('search', target, query, from_message_id, limit, filters))
        if self.search_hook:
            self.search_hook(self, target)
        if self.pages.get(target):
            value = self.pages[target].pop(0)
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        rows = [row for (chat, mid), row in sorted(self.messages.items(), reverse=True)
                if chat == target and (not from_message_id or mid < from_message_id)][:limit]
        return {'@type': 'foundChatMessages', 'total_count': -1, 'messages': copy.deepcopy(rows),
                'next_from_message_id': rows[-1]['id'] if len(rows) == limit else 0}

    def get_message(self, target, mid):
        self.calls.append(('message', target, mid))
        return copy.deepcopy(self.hydration.get((target, mid), self.messages[(target, mid)]))

    def get_sender_identity(self, sender):
        return {'kind': 'user', 'id': 17, 'display_name': 'Synthetic Sender'}


class SelectedSearchTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(schemas, 'SearchChatsRequest'), 'F7b selected search request missing')
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.selected_search_reader'),
                             'F7b selected search reader missing')
        self.mod = importlib.import_module('telegram_search_mcp.selected_search_reader')
        self.wall = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.clock = [10.0]

    def request(self, **kw):
        return schemas.SearchChatsRequest(**{'targets': [7, 8], 'query': 'Full',
                                            'mode': 'latest', 'limit': 2, **kw})

    def reader(self, provider):
        reader = self.mod.SelectedSearchReader(client=provider, client_id='owner',
            broker_generation='generation', clock=lambda: self.clock[0], now=lambda: self.wall)
        self.addCleanup(reader.close)
        return reader

    def test_strict_exact_unique_selected_scope(self):
        for changes in ({'targets': []}, {'targets': [7] * 2}, {'targets': [1,2,3,4,5,6]},
                        {'targets': [True]}, {'targets': ['7']}, {'targets': [0]}, {'targets': [2**53]},
                        {'targets': '@any'}, {'query': '*'}, {'query': {}}, {'topic': {'kind':'forum','id':1}},
                        {'limit': True}, {'limit': 21}, {'date_to':'2024-01-01T00:00:00Z'},
                        {'cursor':'search_'+'a'*64}, {'cursor':'history_'+'a'*64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.request(**changes)

    def test_all_targets_verified_before_content_one_chat_per_call_and_frozen_date(self):
        provider = SelectedProvider(count=3)
        reader = self.reader(provider)
        a = reader.read(self.request())
        first_search = next(i for i,c in enumerate(provider.calls) if c[0] == 'search')
        self.assertEqual([c[1] for c in provider.calls[:first_search] if c[0]=='chat'][:2], [7,8])
        self.assertEqual([r.anchor.chat_id for r in a.results], [7,7])
        self.assertEqual([c.state for c in a.coverage], ['active','pending'])
        self.assertEqual(a.scope.date_to, self.wall)
        self.assertTrue(a.next_cursor.startswith('selected_search_'))
        b = reader.read(self.request(cursor=a.next_cursor))
        self.assertEqual([r.anchor.message_id for r in b.results], [1])
        self.assertEqual([c.state for c in b.coverage], ['stopped','pending'])
        self.assertEqual([c[1] for c in provider.calls if c[0]=='search'], [7])
        self.wall = datetime(2024,2,1,tzinfo=timezone.utc)
        c = reader.read(self.request(cursor=b.next_cursor))
        self.assertEqual([r.anchor.chat_id for r in c.results], [8,8])
        self.assertEqual(c.scope.date_to, a.scope.date_to)
        self.assertEqual(c.scope.expires_at, a.scope.expires_at)
        self.assertEqual(c.coverage[0], b.coverage[0])
        self.assertFalse(c.scope_complete)
        self.assertIsNone(c.has_more)
        self.assertEqual(provider.deadlines[0],provider.deadlines[1])
        self.assertEqual(provider.deadlines[2],provider.deadlines[3])

    def test_preflight_wrong_secret_missing_and_unknown_chat_types_stop_before_content(self):
        for value, reason in (({'@type':'chat','id':9,'type':{'@type':'chatTypePrivate'}},'invalid_provider_chat'),
                             ({'@type':'chat','id':8,'type':{'@type':'chatTypeSecret'}},'secret_chat'),
                             ({'@type':'chat','id':8},'invalid_provider_chat'),
                             ({'@type':'chat','id':8,'type':{'@type':'unexpected'}},'invalid_provider_chat')):
            with self.subTest(value=value):
                provider=SelectedProvider(); provider.chats[8]=value
                response=self.reader(provider).read(self.request())
                self.assertIsNone(response.next_cursor); self.assertEqual(response.results, [])
                self.assertEqual(response.stop_reason, reason)
                self.assertEqual([c.state for c in response.coverage], ['pending','stopped'])
                self.assertFalse(any(c[0] in {'search','message'} for c in provider.calls))

    def test_rehydrated_type_change_before_child_blocks_content(self):
        provider=SelectedProvider(); hits=[0]
        def change(p, target):
            if target==7:
                hits[0]+=1
                if hits[0]==2:p.chats[7]={'@type':'chat','id':7,'type':{'@type':'unexpected'}}
        provider.chat_hook=change
        response=self.reader(provider).read(self.request())
        self.assertEqual(response.results, [])
        self.assertEqual(response.stop_reason,'invalid_provider_chat')
        self.assertFalse(any(c[0]=='search' for c in provider.calls))

    def test_five_chats_do_not_consume_existing_single_chat_store(self):
        from telegram_search_mcp.search_service import SearchService
        provider=SelectedProvider(count=3); service=SearchService(client=provider,owns_client=False)
        self.addCleanup(service.close)
        for _ in range(4):
            self.assertIsNotNone(service.search_messages(schemas.SearchMessagesRequest(
                target=7,query='Full',mode='latest',limit=1)).next_cursor)
        self.assertEqual(service.search_messages(schemas.SearchMessagesRequest(
            target=7,query='Full',mode='latest',limit=1)).status,'capacity_exhausted')
        request=self.request(targets=[7,8,9,10,11],limit=20)
        response=service.search_chats(request); selected=[]
        for _ in range(12):
            selected.extend(r.anchor.chat_id for r in response.results)
            if response.next_cursor is None:break
            response=service.search_chats(request.model_copy(update={'cursor':response.next_cursor}))
        self.assertEqual(selected,[7,7,7,8,8,8,9,9,9,10,10,10,11,11,11])
        self.assertEqual([c.state for c in response.coverage],['stopped']*5)

    def test_changed_binding_preserves_legitimate_cursor_and_replay_fails(self):
        reader=self.reader(SelectedProvider(count=5)); a=reader.read(self.request())
        for change in ({'targets':[8,7]}, {'query':'different'}, {'limit':1},
                       {'direction':'incoming'}, {'sender':{'kind':'user','id':18}}):
            self.assertEqual(reader.read(self.request(cursor=a.next_cursor,**change)).status,'invalid_cursor')
        b=reader.read(self.request(cursor=a.next_cursor));self.assertEqual([r.anchor.message_id for r in b.results],[3,2])
        self.assertEqual(reader.read(self.request(cursor=a.next_cursor)).status,'invalid_cursor')

    def test_wrong_chat_candidate_or_hydration_never_returns_foreign_body(self):
        for hydration in (False,True):
            provider=SelectedProvider(count=1)
            if hydration:provider.hydration[(7,1)]=message(1,chat_id=8)
            else:provider.pages[7]=[{'@type':'foundChatMessages','total_count':1,'next_from_message_id':0,
                                    'messages':[message(1,chat_id=8)]}]
            response=self.reader(provider).read(self.request())
            self.assertEqual(response.results,[])
            self.assertEqual(response.coverage[0].stop_reason,'invalid_provider_page')
            self.assertEqual(response.coverage[1].state,'pending')

    def test_chat_failure_independent_but_auth_terminates_group(self):
        for error, has_cursor in ((TDLibError('private error'),True),(AuthorizationBlocked('private auth'),False)):
            provider=SelectedProvider();provider.pages[7]=[error]
            response=self.reader(provider).read(self.request())
            self.assertEqual(response.results,[])
            self.assertEqual(response.next_cursor is not None,has_cursor)
            self.assertEqual(response.coverage[1].state,'pending')
            self.assertNotIn('private ',response.model_dump_json())

    def test_uncertain_transport_failure_closes_group_even_if_child_swallows_sender_timeout(self):
        for phase in ('search','hydrate','sender'):
            for error in (TimeoutError('private timeout'),OSError('private transport')):
                provider=SelectedProvider(count=3)
                if phase=='search':provider.pages[7]=[error]
                elif phase=='hydrate':
                    def fail(target,mid):raise error
                    provider.get_message=fail
                else:
                    def fail(sender):raise error
                    provider.get_sender_identity=fail
                response=self.reader(provider).read(self.request())
                self.assertEqual(response.results,[],(phase,type(error)))
                self.assertIsNone(response.next_cursor,(phase,type(error)))
                self.assertEqual(response.status,'error')
                self.assertEqual(response.coverage[1].state,'pending')

    def test_account_change_between_preflight_and_child_or_after_content_suppresses_bodies(self):
        for when in ('before','after'):
            provider=SelectedProvider();calls=[0]
            def account(p):
                calls[0]+=1
                if when=='before' and calls[0]==2:p.account=43
            provider.account_hook=account
            if when=='after':provider.search_hook=lambda p,target:setattr(p,'account',43)
            response=self.reader(provider).read(self.request())
            self.assertEqual(response.results,[]);self.assertIsNone(response.next_cursor)
            self.assertEqual(response.stop_reason,'account_changed')

    def test_absolute_expiry_suppresses_inflight_bodies_and_closes_child(self):
        provider=SelectedProvider();reader=self.reader(provider)
        a=reader.read(self.request());self.clock[0]=309.0
        provider.search_hook=lambda p,t:self.clock.__setitem__(0,311.0)
        # Force another native page after pending candidates are drained.
        b=reader.read(self.request(cursor=a.next_cursor))
        if b.next_cursor:
            provider.search_hook=lambda p,t:self.clock.__setitem__(0,311.0)
            b=reader.read(self.request(cursor=b.next_cursor))
        self.assertEqual(b.results,[]);self.assertIsNone(b.next_cursor)
        self.assertEqual(b.stop_reason,'invalid_cursor')
        self.assertFalse(reader._scans)

    def test_close_inflight_suppresses_bodies_and_all_owned_children(self):
        provider=SelectedProvider();reader=self.reader(provider)
        entered,release=threading.Event(),threading.Event();out=[]
        def block(p,target):entered.set();release.wait(3)
        provider.search_hook=block
        worker=threading.Thread(target=lambda:out.append(reader.read(self.request())))
        worker.start();self.assertTrue(entered.wait(2));reader.close();release.set();worker.join(3)
        self.assertEqual(len(out),1);self.assertEqual(out[0].results,[])
        self.assertIsNone(out[0].next_cursor);self.assertFalse(reader._scans)

    def test_close_between_child_rebinding_and_dispatch_returns_safe_terminal(self):
        from unittest.mock import patch
        provider=SelectedProvider(count=5);reader=self.reader(provider);armed=[False]
        def get_client(child):return child._test_client
        def set_client(child,value):
            child._test_client=value
            if armed[0]:reader.close()
        with patch.object(self.mod._OwnedExactReader,'_client',property(get_client,set_client),create=True):
            first=reader.read(self.request());armed[0]=True
            result=reader.read(self.request(cursor=first.next_cursor))
        self.assertEqual(result.results,[]);self.assertIsNone(result.next_cursor)
        self.assertEqual(result.stop_reason,'invalid_cursor')

    def test_boolean_coverage_and_typed_filters_preserved_per_chat(self):
        provider=SelectedProvider(count=1); reader=self.reader(provider)
        request=self.request(query={'all':['Full','selected']},sender={'kind':'user','id':17},direction='outgoing')
        a=reader.read(request)
        self.assertEqual(a.scope.matching_semantics,'local_nfkc_casefold_whitespace_substring')
        self.assertEqual([b.query for b in a.coverage[0].branch_coverage],['Full'])
        self.assertEqual(a.results[0].status,'match')
        self.assertEqual(a.coverage[1].branch_coverage,[])

    def test_four_outer_scopes_count_inflight_and_release_owned_children(self):
        provider=SelectedProvider(count=5);reader=self.reader(provider)
        entered,release=threading.Event(),threading.Event();out=[]
        def block(p,target):entered.set();release.wait(3)
        provider.search_hook=block
        workers=[threading.Thread(target=lambda:out.append(reader.read(self.request()))) for _ in range(4)]
        for worker in workers:worker.start()
        self.assertTrue(entered.wait(2))
        for _ in range(200):
            if len(reader._inflight)==4:break
            time.sleep(.005)
        self.assertEqual(reader.read(self.request()).status,'capacity_exhausted')
        release.set()
        for worker in workers:worker.join(3)
        self.assertEqual(len(out),4)
        children=[scan.child for scan in reader._scans.values()]
        self.assertEqual(len(children),4)
        retained=repr([(scan.scope,scan.coverage) for scan in reader._scans.values()])
        self.assertNotIn('Full selected text',retained);self.assertNotIn('Not retained',retained)
        reader.close()
        self.assertTrue(all(child._closed and not child._scans for child in children))

    def test_selected_owned_child_expiry_not_renewed_on_later_chat(self):
        provider=SelectedProvider(count=1);reader=self.reader(provider)
        a=reader.read(self.request(limit=20));self.clock[0]=300.0
        b=reader.read(self.request(limit=20,cursor=a.next_cursor))
        self.assertEqual(b.scope.expires_at,a.scope.expires_at)
        self.assertEqual(b.scope.date_to,a.scope.date_to)
        self.assertIsNone(b.next_cursor)

    def test_initial_verification_deadline_or_authorization_leaves_all_other_lanes_unprocessed(self):
        for failure in (AuthorizationBlocked('secret'),):
            provider=SelectedProvider();provider.chats[8]=failure
            response=self.reader(provider).read(self.request())
            self.assertEqual(response.results,[]);self.assertIsNone(response.next_cursor)
            self.assertEqual([lane.state for lane in response.coverage],['pending','stopped'])
            self.assertFalse(any(call[0]=='search' for call in provider.calls))
        provider=SelectedProvider();response=self.reader(provider).read(self.request(),deadline=time.monotonic()-1)
        self.assertEqual(response.stop_reason,'call_budget_exhausted');self.assertEqual(provider.calls,[])

    def test_unexpected_claimed_failure_disposes_child(self):
        provider=SelectedProvider();reader=self.reader(provider)
        a=reader.read(self.request());child=next(iter(reader._scans.values())).child
        def fail(p):raise RuntimeError('synthetic unexpected')
        provider.account_hook=fail
        with self.assertRaises(RuntimeError):reader.read(self.request(cursor=a.next_cursor))
        self.assertTrue(child._closed);self.assertFalse(child._scans);self.assertFalse(reader._scans)


class SelectedRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_appended_tool_preserves_old_schemas_and_denies_disabled_or_coerced_scope(self):
        from mcp import Client
        from telegram_search_mcp.server import build_server
        self.assertTrue(hasattr(schemas,'SearchChatsResponse'),'F7b tool response missing')
        calls=[]
        class Service:
            def search_chats(self,request):
                calls.append(request)
                return schemas.SearchChatsResponse(status='error',stop_reason='broker_unavailable')
            def close(self):pass
        for enabled in (False,True):
            async with Client(build_server(service_factory=Service,policy=RuntimePolicy(
                    enabled_capabilities=('search_chats',) if enabled else ('read',)))) as consumer:
                tools=(await consumer.list_tools()).tools
                self.assertEqual(len(tools),39)
                tool=next(t for t in tools if t.name=='search_chats')
                self.assertTrue(tool.annotations.read_only_hint);self.assertFalse(tool.annotations.idempotent_hint)
                args={'targets':[7,8],'query':'Full','mode':'latest'}
                result=await consumer.call_tool('search_chats',args)
                self.assertEqual(result.is_error,not enabled)
                for changes in ({'targets':'[7,8]'}, {'targets':['7']}, {'targets':[True]},
                                {'query':'{"all":["Full"]}'}, {'limit':'1'}, {'path':'/private'}, {'topic':{}}):
                    if changes.get('query'):
                        # JSON-looking lexical strings stay strings, not Boolean objects.
                        await consumer.call_tool('search_chats',{**args,**changes})
                        self.assertIsInstance(calls[-1].query,str) if enabled else None
                    else:self.assertTrue((await consumer.call_tool('search_chats',{**args,**changes})).is_error)


class SelectedProxyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(BrokerClient,'search_chats'),'F7b independent proxy missing')

    def request(self, **changes):
        return schemas.SearchChatsRequest(**{'targets':[7,8], 'query':'Full', 'mode':'latest', 'limit':1,**changes})

    def proxy(self, service, alter=lambda raw:raw):
        class Proxy(BrokerClient):
            def _request(self,operation,payload):
                if operation=='release_client':return {}
                return alter(service.search_chats(schemas.SearchChatsRequest.model_validate(payload)).model_dump(mode='json'))
        return Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('search_chats',)))

    def service(self, count=3):
        from telegram_search_mcp.search_service import SearchService
        service=SearchService(client=SelectedProvider(count=count),owns_client=False)
        self.addCleanup(service.close)
        return service

    def test_proxy_continues_exact_lanes_and_resets_descending_id_boundary(self):
        proxy=self.proxy(self.service(count=1));response=proxy.search_chats(self.request());seen=[]
        for _ in range(6):
            seen.extend((r.anchor.chat_id,r.anchor.message_id) for r in response.results)
            if response.next_cursor is None:break
            response=proxy.search_chats(self.request(cursor=response.next_cursor))
        self.assertEqual(seen,[(7,1),(8,1)])
        self.assertEqual([lane.state for lane in response.coverage],['stopped','stopped'])

    def test_proxy_rejects_shape_frozen_scope_cross_chat_prior_lane_and_counter_forgery(self):
        for mutation in ('order','head','date','expiry','foreign','counter','reopen','skip'):
            count=[0]
            def alter(raw):
                count[0]+=1
                if count[0]!=2:return raw
                if mutation=='order':raw['scope']['targets']=[8,7]
                elif mutation=='head':raw['coverage'][0]['upper_message_id']=99
                elif mutation=='date':raw['scope']['date_to']='2029-01-01T00:00:00Z'
                elif mutation=='expiry':raw['scope']['expires_at']='2099-01-01T00:00:00Z'
                elif mutation=='foreign':raw['results'][0]['anchor']['chat_id']=8
                elif mutation=='counter':raw['coverage'][0]['processed_candidates']=1
                elif mutation=='reopen':raw['coverage'][1]['state']='active';raw['coverage'][1]['status']='page'
                elif mutation=='skip':raw['current_index']=1
                return raw
            proxy=self.proxy(self.service(),alter);a=proxy.search_chats(self.request())
            self.assertEqual(a.status,'page')
            b=proxy.search_chats(self.request(cursor=a.next_cursor))
            self.assertEqual((b.status,b.stop_reason,b.results),('error','broker_unavailable',[]),mutation)

    def test_proxy_binding_replay_close_and_original_ttl(self):
        proxy=self.proxy(self.service());a=proxy.search_chats(self.request())
        self.assertEqual(proxy.search_chats(self.request(targets=[8,7],cursor=a.next_cursor)).status,'invalid_cursor')
        b=proxy.search_chats(self.request(cursor=a.next_cursor))
        self.assertEqual(proxy.search_chats(self.request(cursor=a.next_cursor)).status,'invalid_cursor')
        proxy.close()
        self.assertEqual(proxy.search_chats(self.request(cursor=b.next_cursor)).status,'invalid_cursor')

    def test_proxy_rejects_invalid_future_scope_even_terminal_and_raw_booleans(self):
        for field in ('date_to','expires_at'):
            def alter(raw):raw['scope'][field]='2099-01-01T00:00:00Z';return raw
            response=self.proxy(self.service(count=1),alter).search_chats(self.request(targets=[7],limit=20))
            self.assertEqual(response.status,'error')
        def alter(raw):raw['contract_version']=True;return raw
        self.assertEqual(self.proxy(self.service(),alter).search_chats(self.request()).status,'error')

    def test_proxy_rejects_group_lifecycle_reason_with_bodies_or_continuation(self):
        for reason in ('account_changed','authorization_unavailable','invalid_cursor'):
            def alter(raw):
                raw['stop_reason']=reason;raw['coverage'][0]['stop_reason']=reason
                return raw
            response=self.proxy(self.service(),alter).search_chats(self.request())
            self.assertEqual((response.status,response.results),('error',[]),reason)

    def test_proxy_close_or_expiry_inflight_suppresses_terminal_content(self):
        from unittest.mock import patch
        clock=[10.0];service=self.service();proxy=None
        def alter(raw):clock[0]=311.0;return raw
        proxy=self.proxy(service,alter)
        with patch('telegram_search_mcp.broker_client.time.monotonic',side_effect=lambda:clock[0]):
            response=proxy.search_chats(self.request())
        self.assertEqual((response.status,response.results),('error',[]))

    def test_initial_terminal_response_cannot_skip_pending_selected_chat_to_return_body(self):
        def skip(raw):
            raw['status']='partial';raw['next_cursor']=None;raw['current_index']=1
            first=raw['coverage'][0];first.update(target=8,state='stopped',status='partial',stop_reason='provider_end_unverified')
            raw['coverage']=[schemas.SelectedChatCoverage(target=7).model_dump(mode='json'),first]
            raw['stop_reason']='provider_end_unverified'
            for result in raw['results']:
                result['anchor']['chat_id']=8
                result['message']['source']['evidence_anchor']['chat_id']=8
            return raw
        response=self.proxy(self.service(),skip).search_chats(self.request())
        self.assertEqual((response.status,response.results),('error',[]))
        service=self.service()
        def close(raw):proxy.close();return raw
        proxy=self.proxy(service,close)
        response=proxy.search_chats(self.request(targets=[7],limit=20))
        self.assertEqual((response.status,response.results),('error',[]))


if __name__=='__main__':unittest.main()
