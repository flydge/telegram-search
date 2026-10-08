"""F6 typed predicates across request, raw evidence, native and framed IPC."""
from __future__ import annotations
import copy
import inspect
import time
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from telegram_search_mcp import schemas
from telegram_search_mcp.exact_search_reader import ExactSearchReader
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.tdjson import MessageNotFound, ForumUnsupported, ForbiddenTDLibRequest
from test_exact_search_reader import SearchProvider, page
from test_exact_search_boundary import response
from test_message_reader import message
from test_forum_reader import topic
from test_forum_tdjson import ready_client

USER = {'kind': 'user', 'id': 17}
FORUM = {'kind': 'forum', 'id': 1}

def request(**changes):
    return schemas.SearchMessagesRequest(target=7, query='selected', mode='latest', **changes)

def row(mid, *, sender=17, outgoing=True, tid=1):
    return message(mid, sender_id={'@type': 'messageSenderUser', 'user_id': sender},
                   is_outgoing=outgoing, topic_id={'@type': 'messageTopicForum', 'forum_topic_id': tid})

class TypedProvider(SearchProvider):
    def __init__(self, messages):
        super().__init__(messages)
        self.current_topic = topic(1)
        self.forum_error = None
    def resolve_forum_chat(self, chat):
        self.calls.append(('forum', chat))
        if self.forum_error: raise self.forum_error
        return {'@type': 'chat', 'id': chat}
    def get_forum_topic(self, chat, tid):
        self.calls.append(('topic', chat, tid))
        return copy.deepcopy(self.current_topic)
    def search_chat_messages(self, chat, query, *, from_message_id, limit, sender=None, topic=None):
        self.calls.append(('filters', sender.model_dump() if sender else None, topic.model_dump() if topic else None))
        return super().search_chat_messages(chat, query, from_message_id=from_message_id, limit=limit)
    def get_sender_identity(self, value):
        kind = 'user' if value['@type'] == 'messageSenderUser' else 'chat'
        return {'kind': kind, 'id': value['user_id' if kind == 'user' else 'chat_id'], 'display_name': 'Synthetic'}
    def close(self): pass

def reader(provider):
    return ExactSearchReader(client=provider, client_id='synthetic', broker_generation='synthetic',
        now=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc))

class TypedTestCase(unittest.TestCase):
    def setUp(self):
        self.assertIn('sender', schemas.SearchMessagesRequest.model_fields, 'typed predicates are not implemented')

class TypedRequestTests(TypedTestCase):
    def test_typed_fields_roundtrip_and_null_is_absence(self):
        r = request(sender=USER, direction='outgoing', topic=FORUM)
        self.assertEqual(r.model_dump(mode='json')['sender'], USER)
        self.assertEqual(r.topic.id, 1)
        self.assertEqual(request().model_dump(), request(sender=None, direction=None, topic=None).model_dump())
        for sender in ({'kind':'user','id':2**53-1},{'kind':'chat','id':-(2**53-1)}):
            self.assertEqual(request(sender=sender).sender.id, sender['id'])
    def test_input_predicates_reject_coercions_unknown_shapes_and_ranges(self):
        cases = [{'sender':x} for x in ('{"kind":"user","id":17}', [], 17,
            {'kind':'self','id':17}, {'kind':'user','id':True}, {'kind':'user','id':'17'},
            {'kind':'user','id':17.0},{'kind':'user','id':0},{'kind':'user','id':-1},
            {'kind':'chat','id':0},{'kind':'chat','id':2**53},{'kind':'chat','id':-(2**53)},
            {'kind':'user','id':17,'name':'x'},{'id':17})]
        cases += [{'direction':x} for x in (True,'OUTGOING',17,{'kind':'outgoing'})]
        cases += [{'topic':x} for x in ('{"kind":"forum","id":1}',
            {'kind':'thread','id':1},{'kind':'forum','id':True},{'kind':'forum','id':'1'},
            {'kind':'forum','id':0},{'kind':'forum','id':2**31},{'kind':'forum','id':1,'extra':0})]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError): request(**changes)

class TypedReaderTests(TypedTestCase):
    def test_predicates_and_general_topic_are_locally_checked_before_projection(self):
        p = TypedProvider({40:row(40),30:row(30,sender=18),20:row(20,outgoing=False),10:row(10,tid=2)})
        out = reader(p).read(request(sender=USER,direction='outgoing',topic=FORUM))
        self.assertEqual([r.anchor.message_id for r in out.results],[40])
        self.assertEqual(out.scope.sender.model_dump(),USER)
        self.assertEqual(out.scope.direction,'outgoing'); self.assertEqual(out.scope.topic.id,1)
        self.assertEqual(out.processed_candidates,4)
        self.assertEqual(len([c for c in p.calls if c[0]=='message']),4)
        self.assertEqual([c for c in p.calls if c[0]=='filters'],[('filters',USER,FORUM)])
        self.assertFalse(out.scope_complete); self.assertIsNone(out.has_more)
    def test_matching_and_nonmatching_races_do_not_leak_bodies(self):
        cases = [(row(40),row(40,sender=18),'evidence_changed'),
                 (row(40,sender=18),row(40,sender=18),None),
                 (row(40,sender=18),row(40),'match'),
                 (row(40),MessageNotFound('private'),'not_found'),
                 (row(40,sender=18),MessageNotFound('private'),None)]
        for observed,current,want in cases:
            p=TypedProvider({40:observed});p.hydration[40]=current
            out=reader(p).read(request(sender=USER))
            self.assertEqual([r.status for r in out.results],[] if want is None else [want])
            if want in {'evidence_changed','not_found'}: self.assertIsNone(out.results[0].message)
        p=TypedProvider({40:row(40,sender=18)})
        changed=row(40); changed['content']['text']['text']='changed'
        p.hydration[40]=changed
        self.assertEqual(reader(p).read(request(sender=USER)).results[0].status,'evidence_changed')
    def test_same_text_direction_and_topic_changes_are_evidence_changed(self):
        for filters,changed in [({'direction':'outgoing'},row(40,outgoing=False)),
                                ({'topic':FORUM},row(40,tid=2))]:
            p=TypedProvider({40:row(40)});p.hydration[40]=changed
            out=reader(p).read(request(**filters))
            self.assertEqual(out.results[0].status,'evidence_changed');self.assertIsNone(out.results[0].message)
    def test_whole_page_filter_metadata_is_checked_before_hydration(self):
        cases=[({'sender':USER},{'sender_id':None}),({'sender':USER},{'sender_id':{'@type':'messageSenderUser','user_id':True}}),
               ({'direction':'outgoing'},{'is_outgoing':1}),({'direction':'outgoing'},{'is_outgoing':None}),
               ({'topic':FORUM},{'topic_id':{'@type':'messageTopicForum','forum_topic_id':True}}),
               ({'topic':FORUM},{'topic_id':{'@type':'unknown','id':1}}),
               ({'topic':FORUM},{'topic_id':{'@type':'messageTopicThread','message_thread_id':0}})]
        for filters,changes in cases:
            bad=row(20);bad.update(changes)
            p=TypedProvider({40:row(40),20:bad})
            out=reader(p).read(request(**filters))
            self.assertEqual(out.stop_reason,'invalid_provider_page');self.assertEqual(out.results,[])
            self.assertFalse(any(c[0]=='message' for c in p.calls))
    def test_null_ordinary_and_other_valid_typed_topics_are_nonmatches(self):
        for value in (None, {'@type':'messageTopicThread','message_thread_id':9},
                      {'@type':'messageTopicSavedMessages','saved_messages_topic_id':0},
                      {'@type':'messageTopicDirectMessages','direct_messages_chat_topic_id':-5}):
            raw=row(40); raw['topic_id']=value
            p=TypedProvider({40:raw});out=reader(p).read(request(topic=FORUM))
            self.assertEqual(out.results,[]);self.assertEqual(out.processed_candidates,1)
            self.assertEqual(out.stop_reason,'provider_end_unverified')
        raw=row(40);del raw['topic_id']
        self.assertEqual(reader(TypedProvider({40:raw})).read(request(topic=FORUM)).results,[])
    def test_topic_is_checked_on_each_continuation_and_unavailable_never_searches(self):
        p=TypedProvider({40:row(40),30:row(30)});r=reader(p)
        a=r.read(request(topic=FORUM,limit=1))
        p.current_topic=None
        b=r.read(request(topic=FORUM,limit=1,cursor=a.next_cursor))
        self.assertEqual(b.stop_reason,'topic_unavailable');self.assertEqual(b.results,[])
        self.assertEqual(len([c for c in p.calls if c[0]=='topic']),2)
        self.assertEqual(len([c for c in p.calls if c[0]=='search']),1)
        for value,want in ((topic(2),'invalid_provider_topic'),(topic(1,is_closed=True,is_hidden=True),'provider_end_unverified')):
            p=TypedProvider({40:row(40)});p.current_topic=value
            self.assertEqual(reader(p).read(request(topic=FORUM)).stop_reason,want)
        p=TypedProvider({40:row(40)});p.forum_error=ForumUnsupported('private')
        self.assertEqual(reader(p).read(request(topic=FORUM)).stop_reason,'unsupported_forum')
        self.assertFalse(any(c[0]=='search' for c in p.calls))
    def test_each_filter_binds_single_use_cursor_and_pending_retains_only_observations(self):
        p=TypedProvider({40:row(40),30:row(30)});r=reader(p)
        base=request(sender=USER,direction='outgoing',topic=FORUM,limit=1)
        a=r.read(base)
        for key,value in [('sender',{'kind':'user','id':18}),('direction','incoming'),('topic',{'kind':'forum','id':2})]:
            bad=base.model_dump();bad.update({key:value,'cursor':a.next_cursor})
            self.assertEqual(r.read(schemas.SearchMessagesRequest(**bad)).status,'invalid_cursor')
        self.assertNotIn('Full selected text',repr(r._scans));self.assertNotIn('Synthetic',repr(r._scans))
        self.assertEqual(r.read(base.model_copy(update={'cursor':a.next_cursor})).results[0].anchor.message_id,30)
    def test_filtered_rows_consume_call_budget_and_can_return_empty_resumable_page(self):
        p=TypedProvider({i:row(i,sender=18) for i in range(1,201)})
        p.pages=[page([row(i,sender=18) for i in range(200-n*20,180-n*20,-1)],180-n*20) for n in range(10)]
        r=reader(p);a=r.read(request(sender=USER))
        self.assertEqual(a.results,[]);self.assertEqual(a.processed_candidates,100)
        self.assertEqual(a.status,'page');self.assertIsNotNone(a.next_cursor)
        b=r.read(request(sender=USER,cursor=a.next_cursor))
        self.assertEqual(b.processed_candidates,200);self.assertEqual(b.results,[])
        self.assertIsNone(b.next_cursor);self.assertFalse(b.scope_complete)
    def test_caller_mutation_cannot_change_frozen_nested_predicates(self):
        p=TypedProvider({40:row(40),30:row(30)})
        r=reader(p);base=request(sender=USER,topic=FORUM,limit=1)
        first=r.read(base)
        base.sender.id=18; base.topic.id=2
        second=r.read(request(sender=USER,topic=FORUM,limit=1,cursor=first.next_cursor))
        self.assertEqual(second.scope.sender.id,17);self.assertEqual(second.scope.topic.id,1)
        self.assertEqual(second.scope,first.scope)
        self.assertEqual([item.anchor.message_id for item in second.results],[30])

    def test_duplicate_processed_observations_are_hydrated_without_reprojection(self):
        p=TypedProvider({40:row(40),30:row(30),20:row(20)})
        p.pages=[page([row(40),row(30)],30),page([row(30),row(20)],0)]
        out=reader(p).read(request(sender=USER))
        self.assertEqual([r.anchor.message_id for r in out.results],[40,30,20])
        self.assertEqual([c[2] for c in p.calls if c[0]=='message'],[40,30,30,20])
        self.assertEqual(out.processed_candidates,4)

    def test_no_predicate_can_hide_malformed_metadata_of_another_requested_predicate(self):
        bad=row(40,sender=18);bad['is_outgoing']='true'
        out=reader(TypedProvider({40:bad})).read(request(sender=USER,direction='outgoing'))
        self.assertEqual(out.stop_reason,'invalid_provider_page');self.assertEqual(out.results,[])

    def test_current_matching_hydration_with_malformed_metadata_fails_closed(self):
        for key,value in [('sender_id',{'@type':'messageSenderUser','user_id':0}),('is_outgoing',None),
                          ('topic_id',{'@type':'messageTopicForum','forum_topic_id':0})]:
            p=TypedProvider({40:row(40)});bad=row(40);bad[key]=value;p.hydration[40]=bad
            out=reader(p).read(request(sender=USER,direction='outgoing',topic=FORUM))
            self.assertEqual(out.results,[]);self.assertEqual(out.stop_reason,'invalid_provider_page')

    def test_broadcast_sender_lane_broadening_is_filtered_locally(self):
        raw=row(40);raw['sender_id']={'@type':'messageSenderChat','chat_id':7}
        p=TypedProvider({40:raw,30:row(30)})
        out=reader(p).read(request(sender={'kind':'chat','id':7}))
        self.assertEqual([r.anchor.message_id for r in out.results],[40])

class TypedNativeTests(TypedTestCase):
    def test_native_typed_shapes_have_no_direction_argument(self):
        client,raw=ready_client({'searchChatMessages':[page([])]})
        client.search_chat_messages(7,'selected',from_message_id=0,limit=20,sender=schemas.SearchMessagesRequest(
            target=7,query='x',mode='latest',sender=USER).sender,topic=schemas.ForumTopicReference(**FORUM))
        payload={k:v for k,v in raw.sent[-1].items() if k!='@extra'}
        self.assertEqual(payload,{'@type':'searchChatMessages','chat_id':7,'topic_id':{'@type':'messageTopicForum','forum_topic_id':1},
            'query':'selected','sender_id':{'@type':'messageSenderUser','user_id':17},'from_message_id':0,'offset':0,'limit':20,'filter':None})
        client,raw=ready_client({'searchChatMessages':[page([])]})
        sender=request(sender={'kind':'chat','id':7}).sender
        client.search_chat_messages(7,'selected',from_message_id=0,limit=20,sender=sender)
        self.assertEqual(raw.sent[-1]['sender_id'],{'@type':'messageSenderChat','chat_id':7})
    def test_native_allowlist_rejects_untyped_or_extra_filter_fields(self):
        from telegram_search_mcp.tdjson import _validate_bounded_request
        valid={'@type':'searchChatMessages','chat_id':7,'topic_id':{'@type':'messageTopicForum','forum_topic_id':1},
               'query':'selected','sender_id':{'@type':'messageSenderUser','user_id':17},'from_message_id':0,'offset':0,'limit':20,'filter':None}
        _validate_bounded_request(valid)
        for field,value in [('sender_id',{'@type':'messageSenderUser','user_id':True}),
                            ('sender_id',{'@type':'messageSenderChat','chat_id':0}),
                            ('sender_id',{'@type':'messageSenderUser','user_id':17,'extra':0}),
                            ('topic_id',{'@type':'messageTopicThread','message_thread_id':1}),
                            ('topic_id',{'@type':'messageTopicForum','forum_topic_id':0}),
                            ('topic_id',{'@type':'messageTopicForum','forum_topic_id':1,'extra':0}),('direction','outgoing')]:
            with self.subTest(field=field,value=value),self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request({**valid,field:value})

class TypedBoundaryTests(TypedTestCase):
    def test_response_independently_checks_every_body_predicate(self):
        for field,value in [('sender',{'kind':'user','id':18}),('direction','incoming'),('topic',FORUM)]:
            raw=response();raw['scope'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError): schemas.SearchMessagesResponse.model_validate(raw)
    def test_proxy_checks_requested_filters_against_forged_scope(self):
        wall=float(int(time.time()))
        class Proxy(BrokerClient):
            def _request(self,op,payload): return response()
        for changes in ({'sender':USER},{'direction':'outgoing'},{'topic':FORUM}):
            proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('search_messages',)))
            with patch('telegram_search_mcp.broker_client.time.time',return_value=wall):
                out=proxy.search_messages(schemas.SearchMessagesRequest(target=7,query='needle',mode='latest',limit=1,**changes))
            self.assertEqual(out.stop_reason,'broker_unavailable');self.assertEqual(out.results,[])
    def test_sdk_dispatch_rejects_nested_json_strings_and_preserves_typed_inputs(self):
        import asyncio
        from telegram_search_mcp.server import build_server
        class Service:
            def close(self): pass
            def search_messages(self,r):
                self.seen=r
                return schemas.SearchMessagesResponse(status='error',stop_reason='provider_error')
        service=Service();server=build_server(service_factory=lambda:service, policy=RuntimePolicy(enabled_capabilities=('search_messages',)))
        async def run():
            for args in ({'target':7,'query':'selected','mode':'latest','sender':'{"kind":"user","id":17}'},
                         {'target':7,'query':'selected','mode':'latest','topic':'{"kind":"forum","id":1}'}):
                with self.assertRaises(Exception): await server.call_tool('search_messages',args)
            await server.call_tool('search_messages',{'target':7,'query':'selected','mode':'latest','sender':USER,'direction':'outgoing','topic':FORUM})
            self.assertEqual(service.seen.sender.model_dump(),USER);self.assertEqual(service.seen.topic.id,1)
        asyncio.run(run())
    def test_authenticated_framed_ipc_typed_dispatch_and_continuation(self):
        from telegram_search_mcp.artifact_store import ArtifactStore
        from telegram_search_mcp.broker import Broker
        p=TypedProvider({i:row(i,outgoing=i%2==0) for i in range(1,26)})
        policy=RuntimePolicy(enabled_capabilities=('search_messages',))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);broker=Broker(socket_path=root/'s',lock_path=root/'l',artifact_store=ArtifactStore(cache_dir=root/'cache'),client_factory=lambda:p,policy=policy)
            thread=threading.Thread(target=broker.serve_forever);thread.start()
            proxy=BrokerClient(socket_path=root/'s',policy=policy,restart_callback=lambda:None)
            try:
                self.assertTrue(broker.wait_until_ready(timeout=2))
                req=request(sender=USER,direction='outgoing',topic=FORUM,limit=2)
                first=proxy.search_messages(req);self.assertEqual([r.anchor.message_id for r in first.results],[24,22])
                second=proxy.search_messages(req.model_copy(update={'cursor':first.next_cursor}))
                self.assertEqual([r.anchor.message_id for r in second.results],[20,18]);self.assertEqual(second.scope,first.scope)
                self.assertEqual(proxy.search_messages(req.model_copy(update={'cursor':first.next_cursor})).status,'invalid_cursor')
            finally:
                proxy.close();broker.shutdown();thread.join(timeout=3);self.assertFalse(thread.is_alive())
