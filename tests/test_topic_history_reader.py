from __future__ import annotations

import copy
import importlib
import importlib.util
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from telegram_search_mcp import schemas
from telegram_search_mcp.tdjson import AuthorizationBlocked, ForumUnsupported, MessageNotFound, TDLibDeadlineExceeded, TDLibError
from test_history_reader import HistoryProvider
from test_message_reader import message
from test_forum_reader import topic


def forum_message(mid, **changes):
    return message(mid, topic_id={'@type':'messageTopicForum','forum_topic_id':9}, **changes)


class TopicHistoryProvider(HistoryProvider):
    def __init__(self, messages, **kwargs):
        super().__init__(messages, **kwargs)
        self.topic = topic(9)
        self.native_limit = None
    def resolve_forum_chat(self, chat_id):
        self.calls.append(('forum',chat_id))
        return {'@type':'chat','id':chat_id}
    def get_forum_topic(self, chat_id, topic_id):
        self.calls.append(('topic',chat_id,topic_id))
        if isinstance(self.topic, Exception):raise self.topic
        return copy.deepcopy(self.topic)
    def get_forum_topic_history(self, chat_id, forum_topic_id, *, from_message_id=0, limit=20):
        self.calls.append(('topic_history',chat_id,forum_topic_id,from_message_id,limit))
        if self.pages is not None:
            value = self.pages.pop(0)
            if isinstance(value,Exception):raise value
            return copy.deepcopy(value)
        values = [m for mid,m in sorted(self.messages.items(),reverse=True)
                  if not isinstance(m,Exception) and (not from_message_id or mid<from_message_id or
                     (self.inclusive and mid==from_message_id))]
        return envelope(values[:self.native_limit or limit])


def envelope(rows, total=-1):return {'@type':'messages','total_count':total,'messages':rows}


class TopicHistoryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.topic_history_reader'),
                             'F4c bounded topic-history reader missing')
        self.mod=importlib.import_module('telegram_search_mcp.topic_history_reader')
        self.now=datetime(2024,1,1,tzinfo=timezone.utc);self.tick=[10.0]
    def reader(self,p):return self.mod.TopicHistoryReader(client=p,client_id='synthetic_client',
        broker_generation='synthetic_broker',clock=lambda:self.tick[0],now=lambda:self.now)
    def request(self,**kw):return schemas.ReadTopicHistoryRequest(**{
        'target':7,'topic':{'kind':'forum','id':9},'mode':'latest',**kw})
    def interval(self,**kw):return self.request(mode='interval',date_from='2023-11-14T22:13:20Z',
        date_to='2023-11-14T22:13:22Z',**kw)
    def ids(self,r):return [x.anchor.message_id for x in r.results]

    def test_membership_buffered_pagination_insertion_and_fixed_scope(self):
        for inclusive in (False,True):
            p=TopicHistoryProvider({i:forum_message(i) for i in (40,30,20,10)},inclusive=inclusive)
            r=self.reader(p);a=r.read(self.request(limit=2))
            self.assertEqual(self.ids(a),[40,30]);self.assertTrue(a.page_complete)
            self.assertEqual((a.scanned_candidates,a.processed_candidates,a.provider_pages),(4,2,1))
            p.messages[50]=forum_message(50)
            b=r.read(self.request(limit=2,cursor=a.next_cursor))
            self.assertEqual(self.ids(b),[20,10]);self.assertEqual(a.scope,b.scope)
            self.assertEqual(b.scope.upper_message_id,40);self.assertEqual(b.scope.date_to,self.now)
            self.assertEqual((b.scanned_candidates,b.processed_candidates,b.provider_pages),(4,4,1))
            end=r.read(self.request(limit=2,cursor=b.next_cursor))
            self.assertIsNone(end.next_cursor);self.assertFalse(end.scope_complete);self.assertIsNone(end.has_more)
            self.assertIn(end.stop_reason,('provider_nonprogress','provider_end_unverified'))
            calls=[x for x in p.calls if x[0]=='topic_history']
            self.assertEqual([x[3] for x in calls],[0,10])
            self.assertEqual(len([x for x in p.calls if x[0]=='topic']),3)
            for item in a.results+b.results:self.assertEqual(item.message.topic.model_dump(),{'kind':'forum','id':9})
            self.assertNotIn('PRIVATE',a.model_dump_json());self.assertNotIn('Full selected text',repr(r._scans))
            self.assertNotIn('Topic ',repr(r._scans))

    def test_whole_observed_page_validated_before_any_hydration(self):
        malformed=[None,{},message(20),message(20,topic_id={'@type':'messageTopicThread','message_thread_id':9}),
            message(20,topic_id={'@type':'messageTopicForum','forum_topic_id':10}),
            message(20,topic_id={'@type':'messageTopicForum','forum_topic_id':True}),
            forum_message(20,chat_id=8),forum_message(20,date=True),forum_message(40),forum_message(0)]
        for bad in malformed:
            p=TopicHistoryProvider({30:forum_message(30)});p.pages=[envelope([forum_message(30),bad])]
            result=self.reader(p).read(self.request())
            self.assertEqual(result.status,'error');self.assertEqual(result.results,[])
            self.assertFalse(any(x[0] in ('message','sender') for x in p.calls))

    def test_hydration_membership_and_identity_cannot_leak(self):
        for bad in (message(30),message(30,topic_id={'@type':'messageTopicForum','forum_topic_id':10}),
                    forum_message(30,chat_id=8),forum_message(31)):
            p=TopicHistoryProvider({30:forum_message(30)});p.hydration[30]=bad
            result=self.reader(p).read(self.request())
            self.assertEqual(result.status,'error');self.assertEqual(result.results,[])
            self.assertNotIn('Full selected text',result.model_dump_json())
            self.assertFalse(any(x[0]=='sender' for x in p.calls))

    def test_topic_revalidated_on_buffered_call_unavailable_closed_hidden_and_malformed(self):
        p=TopicHistoryProvider({30:forum_message(30),20:forum_message(20)})
        p.topic=topic(9,is_closed=True,is_hidden=True)
        r=self.reader(p);a=r.read(self.request(limit=1))
        self.assertEqual(self.ids(a),[30]);self.assertTrue(a.topic.metadata.is_closed);self.assertTrue(a.topic.metadata.is_hidden)
        p.topic=None;b=r.read(self.request(limit=1,cursor=a.next_cursor))
        self.assertEqual(b.stop_reason,'topic_unavailable');self.assertEqual(b.results,[]);self.assertIsNone(b.next_cursor)
        self.assertEqual(b.topic.status,'not_found');self.assertEqual(len([x for x in p.calls if x[0]=='message']),1)
        for bad in (topic(10),topic(9,chat=8),topic(9,is_closed=1)):
            p=TopicHistoryProvider({30:forum_message(30)});p.topic=bad
            result=self.reader(p).read(self.request())
            self.assertEqual(result.stop_reason,'invalid_provider_topic');self.assertEqual(result.results,[])
            self.assertFalse(any(x[0]=='topic_history' for x in p.calls))

    def test_dates_edited_deleted_and_service_results(self):
        p=TopicHistoryProvider({50:forum_message(50,date=1700000000),40:forum_message(40,date=1699999999),
            30:forum_message(30,date=1700000002),20:forum_message(20,date=1700000001)})
        p.hydration[50]=forum_message(50,date=1700000002)
        result=self.reader(p).read(self.interval());self.assertEqual(self.ids(result),[20])
        self.assertFalse(result.scope_complete)
        p=TopicHistoryProvider({30:forum_message(30),20:forum_message(20),10:forum_message(10)})
        r=self.reader(p);a=r.read(self.request(limit=1));p.hydration[20]=MessageNotFound('private marker')
        b=r.read(self.request(limit=1,cursor=a.next_cursor));self.assertEqual(b.results[0].status,'not_found')
        p.hydration[10]=forum_message(10,content={'@type':'messageForumTopicCreated','name':'private marker'})
        c=r.read(self.request(limit=1,cursor=b.next_cursor));self.assertEqual(c.results[0].status,'unsupported')
        self.assertFalse(c.page_complete);self.assertNotIn('private marker',b.model_dump_json()+c.model_dump_json())

    def test_scope_candidate_cap_drains_accepted_buffer_without_new_fetch(self):
        p=TopicHistoryProvider({i:forum_message(i) for i in range(1,201)});p.native_limit=200
        r=self.reader(p);cursor=None;seen=[]
        for index in range(10):
            out=r.read(self.request(limit=20,cursor=cursor));seen+=self.ids(out);cursor=out.next_cursor
            self.assertEqual(out.scanned_candidates,200);self.assertEqual(out.provider_pages,1)
        self.assertEqual(seen,list(range(200,0,-1)));self.assertIsNone(cursor)
        self.assertEqual(out.stop_reason,'scope_budget_exhausted');self.assertEqual(out.processed_candidates,200)

    def test_native_attempt_cap_drains_pending_and_over_budget_page_never_truncates(self):
        p=TopicHistoryProvider({i:forum_message(i) for i in range(1,30)})
        p.pages=[envelope([forum_message(i)]) for i in range(29,20,-1)]+[envelope([forum_message(20),forum_message(19)])]
        r=self.reader(p);cursor=None
        for _ in range(10):
            out=r.read(self.request(limit=1,cursor=cursor));cursor=out.next_cursor
        self.assertEqual((out.provider_pages,out.scanned_candidates,out.processed_candidates),(10,11,10))
        last=r.read(self.request(limit=1,cursor=cursor));self.assertEqual(self.ids(last),[19])
        self.assertEqual(last.stop_reason,'scope_budget_exhausted');self.assertIsNone(last.next_cursor)
        p=TopicHistoryProvider({i:forum_message(i) for i in range(1,250)})
        p.pages=[envelope([forum_message(i) for i in range(249,59,-1)]),envelope([forum_message(i) for i in range(59,39,-1)])]
        r=self.reader(p);cursor=None
        for _ in range(10):
            out=r.read(self.request(limit=20,cursor=cursor));cursor=out.next_cursor
        self.assertEqual(out.stop_reason,'scope_budget_exhausted');self.assertIsNone(cursor)
        self.assertEqual(out.scanned_candidates,190);self.assertEqual(out.processed_candidates,190)
        self.assertEqual(self.ids(out),list(range(69,59,-1)))

    def test_empty_unknown_count_nonprogress_oversize_and_raw_error_are_honest(self):
        for raw,reason in ((envelope([]),'provider_end_unverified'),(envelope([forum_message(1)]*201),'invalid_provider_page'),
                           (TDLibError('private marker'),'provider_error')):
            p=TopicHistoryProvider({});p.pages=[raw];out=self.reader(p).read(self.request())
            self.assertEqual(out.stop_reason,reason);self.assertFalse(out.scope_complete);self.assertIsNone(out.next_cursor)
            self.assertNotIn('private marker',out.model_dump_json())
        p=TopicHistoryProvider({30:forum_message(30)});p.pages=[envelope([forum_message(30)]),envelope([forum_message(30)])]
        r=self.reader(p);a=r.read(self.request(limit=1));b=r.read(self.request(limit=1,cursor=a.next_cursor))
        self.assertEqual(b.stop_reason,'provider_nonprogress');self.assertEqual(b.results,[])

    def test_cursor_scope_account_replay_expiry_capacity_and_close(self):
        p=TopicHistoryProvider({30:forum_message(30),20:forum_message(20)})
        r=self.reader(p);a=r.read(self.request(limit=1))
        for changed in ({'limit':2},{'target':8},{'topic':{'kind':'forum','id':10}}):
            self.assertEqual(r.read(self.request(limit=changed.pop('limit',1),cursor=a.next_cursor,**changed)).status,'invalid_cursor')
        b=r.read(self.request(limit=1,cursor=a.next_cursor));self.assertEqual(self.ids(b),[20])
        self.assertEqual(r.read(self.request(limit=1,cursor=a.next_cursor)).status,'invalid_cursor')
        self.assertEqual(self.reader(p).read(self.request(limit=1,cursor=b.next_cursor)).status,'invalid_cursor')
        p.account_id=18;self.assertEqual(r.read(self.request(limit=1,cursor=b.next_cursor)).status,'invalid_cursor')
        p.account_id=17;cursors=[r.read(self.request(limit=1)).next_cursor for _ in range(4)]
        self.assertTrue(all(cursors));self.assertEqual(r.read(self.request()).status,'capacity_exhausted')
        self.tick[0]=311;self.assertEqual(r.read(self.request(limit=1,cursor=cursors[0])).status,'invalid_cursor')
        a=r.read(self.request(limit=1));r.close();self.assertEqual(r.read(self.request(limit=1,cursor=a.next_cursor)).status,'invalid_cursor')

    def test_atomic_cursor_claim_and_inflight_close(self):
        p=TopicHistoryProvider({30:forum_message(30),20:forum_message(20)})
        r=self.reader(p);a=r.read(self.request(limit=1));entered=threading.Event();release=threading.Event()
        def slow():entered.set();release.wait(3);return 17
        p.get_account_id=slow;out=[]
        worker=threading.Thread(target=lambda:out.append(r.read(self.request(limit=1,cursor=a.next_cursor))))
        worker.start();self.assertTrue(entered.wait(1))
        self.assertEqual(r.read(self.request(limit=1,cursor=a.next_cursor)).status,'invalid_cursor')
        r.close();release.set();worker.join(3)
        self.assertIsNone(out[0].next_cursor);self.assertEqual(out[0].stop_reason,'invalid_cursor')

    def test_text_and_processing_call_caps_continue_only_with_progress(self):
        p=TopicHistoryProvider({i:forum_message(i,content={'@type':'messageText','text':{'text':'x'*20001}}) for i in range(1,9)})
        r=self.reader(p);out=r.read(self.request(limit=8))
        self.assertEqual(len(out.results),5);self.assertEqual(out.stop_reason,'call_budget_exhausted')
        self.assertIsNotNone(out.next_cursor);self.assertFalse(out.page_complete)
        self.assertEqual(sum(len(x.message.text.value) for x in out.results),100000)
        p=TopicHistoryProvider({i:forum_message(i,date=1699999999) for i in range(1,201)});p.native_limit=200
        r=self.reader(p);out=r.read(self.interval())
        self.assertEqual(out.processed_candidates,100);self.assertIsNotNone(out.next_cursor);self.assertEqual(out.results,[])
        end=r.read(self.interval(cursor=out.next_cursor));self.assertEqual(end.processed_candidates,200)
        self.assertEqual(end.stop_reason,'scope_budget_exhausted');self.assertIsNone(end.next_cursor)

    def test_deadline_auth_forum_loss_and_fixed_ttl_inflight_discard_cursor(self):
        for error,reason in ((AuthorizationBlocked('private marker'),'authorization_unavailable'),
                             (ForumUnsupported('private marker'),'unsupported_forum'),
                             (TDLibDeadlineExceeded('private marker'),'call_budget_exhausted')):
            p=TopicHistoryProvider({30:forum_message(30)});p.topic=error
            out=self.reader(p).read(self.request());self.assertEqual(out.stop_reason,reason);self.assertIsNone(out.next_cursor)
        p=TopicHistoryProvider({30:forum_message(30)})
        r=self.reader(p);original=p.get_message
        def expires(*args):self.tick[0]=311;return original(*args)
        p.get_message=expires;out=r.read(self.request(limit=1));self.assertEqual(out.stop_reason,'invalid_cursor')
        self.assertIsNone(out.next_cursor)
        p=TopicHistoryProvider({30:forum_message(30)});tick=[10.0];original=p.get_message
        def late(*args):tick[0]=50.0;return original(*args)
        p.get_message=late
        with patch.object(self.mod.time,'monotonic',side_effect=lambda:tick[0]):out=self.reader(p).read(self.request(limit=1),deadline=40)
        self.assertEqual(out.stop_reason,'call_budget_exhausted');self.assertEqual(out.results,[])

    def test_sender_auth_loss_stops_before_body_and_cursor(self):
        p=TopicHistoryProvider({30:forum_message(30),20:forum_message(20)})
        def denied(sender):raise AuthorizationBlocked('private marker')
        p.get_sender_identity=denied
        out=self.reader(p).read(self.request(limit=1))
        self.assertEqual(out.status,'blocked');self.assertEqual(out.stop_reason,'authorization_unavailable')
        self.assertEqual(out.results,[]);self.assertIsNone(out.next_cursor)
        self.assertNotIn('private marker',out.model_dump_json())

    def test_late_missing_message_response_stops_without_over_budget_evidence(self):
        p=TopicHistoryProvider({30:forum_message(30)});tick=[10.0]
        def late(*args):tick[0]=50.0;raise MessageNotFound('private marker')
        p.get_message=late
        with patch.object(self.mod.time,'monotonic',side_effect=lambda:tick[0]):
            out=self.reader(p).read(self.request(limit=1),deadline=40)
        self.assertEqual(out.stop_reason,'call_budget_exhausted');self.assertEqual(out.results,[])
        self.assertIsNone(out.next_cursor)
