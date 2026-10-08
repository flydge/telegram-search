from __future__ import annotations

import copy
import importlib
import threading
import time
import unittest
from contextlib import contextmanager

from pydantic import ValidationError
from telegram_search_mcp import schemas
from telegram_search_mcp.tdjson import AuthorizationBlocked, ForumUnsupported, TDLibDeadlineExceeded, TDLibError


def topic(tid, chat=7, **changes):
    info = {'@type':'forumTopicInfo', 'chat_id':chat, 'forum_topic_id':tid,
        'name':'Topic '+str(tid), 'creation_date':1700000000,
        'is_general':tid==1, 'is_closed':False, 'is_hidden':False}
    info.update(changes)
    return {'@type':'forumTopic', 'info':info, 'order':'123',
            'last_message':{'content':{'text':'PRIVATE EMBEDDED MESSAGE'}}, 'draft_message':{'text':'PRIVATE DRAFT'}}


class ForumProvider:
    def __init__(self, pages, hydrated=None):
        self.pages=pages; self.hydrated=hydrated or {}; self.calls=[]; self.account=17
    @contextmanager
    def request_budget(self, deadline): yield
    def ensure_ready(self): self.calls.append(('ready',))
    def get_account_id(self): return self.account
    def resolve_forum_chat(self, chat): self.calls.append(('forum',chat)); return {'@type':'chat','id':chat}
    def get_forum_topics(self, chat, **kwargs):
        self.calls.append(('page',chat,kwargs)); return copy.deepcopy(self.pages.pop(0))
    def get_forum_topic(self, chat, tid):
        self.calls.append(('topic',chat,tid))
        value=self.hydrated.get(tid, topic(tid))
        if isinstance(value,Exception): raise value
        return copy.deepcopy(value)


def page(ids, after=(10,100,2)):
    return {'@type':'forumTopics','total_count':999,'topics':[topic(i) for i in ids],
        'next_offset_date':after[0],'next_offset_message_id':after[1],'next_offset_forum_topic_id':after[2]}


class ForumReaderTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(schemas,'ListTopicsRequest'),'F4 strict topic-list contract is missing')
        self.mod=importlib.import_module('telegram_search_mcp.forum_reader')
    def reader(self, provider, **kwargs):
        return self.mod.ForumTopicReader(client=provider,client_id='client',broker_generation='generation',**kwargs)
    def request(self, **changes):return schemas.ListTopicsRequest(target=7, **changes)

    def test_hydrated_topic_metadata_and_exact_provider_offsets_without_content(self):
        p=ForumProvider([page([1,2]),page([2,3],(20,200,3))],{1:topic(1,is_closed=True),2:None})
        r=self.reader(p);first=r.read(self.request(limit=2))
        self.assertEqual([x.topic.id for x in first.results],[1,2])
        self.assertEqual([x.status for x in first.results],['complete','not_found'])
        self.assertTrue(first.results[0].metadata.is_closed)
        self.assertFalse(first.scope_complete);self.assertFalse(first.page_complete)
        self.assertNotIn('PRIVATE',first.model_dump_json())
        second=r.read(self.request(limit=2,cursor=first.next_cursor))
        self.assertEqual([x.topic.id for x in second.results],[3])
        self.assertEqual(p.calls[-2],('page',7,{'offset_date':10,'offset_message_id':100,'offset_forum_topic_id':2,'limit':2}))
        self.assertTrue(second.page_complete);self.assertIsNone(second.has_more)
        self.assertEqual(second.scope,first.scope)
        self.assertEqual(r.read(self.request(limit=2,cursor=first.next_cursor)).status,'invalid_cursor')

    def test_overreturned_ids_are_drained_before_next_native_page_without_losing_offsets(self):
        p=ForumProvider([page([1,2,3],(42,420,3)),page([3,4],(43,430,4))])
        r=self.reader(p);first=r.read(self.request(limit=2))
        self.assertEqual([x.topic.id for x in first.results],[1,2])
        self.assertEqual((first.scanned_candidates,first.provider_pages),(3,1))
        self.assertNotIn('PRIVATE',repr(r._scans));self.assertNotIn('Topic ',repr(r._scans))
        second=r.read(self.request(limit=2,cursor=first.next_cursor))
        self.assertEqual([x.topic.id for x in second.results],[3])
        self.assertEqual((second.scanned_candidates,second.provider_pages),(3,1))
        self.assertEqual(len([c for c in p.calls if c[0]=='page']),1)
        third=r.read(self.request(limit=2,cursor=second.next_cursor))
        self.assertEqual([x.topic.id for x in third.results],[4])
        self.assertEqual((third.scanned_candidates,third.provider_pages),(5,2))
        self.assertEqual([c for c in p.calls if c[0]=='page'][-1],
            ('page',7,{'offset_date':42,'offset_message_id':420,'offset_forum_topic_id':3,'limit':2}))
        self.assertNotIn('PRIVATE',third.model_dump_json())

    def test_overreturn_deferred_end_and_repeated_offset_preserve_all_observed_ids(self):
        for after in ((0,0,0),(10,100,2)):
            with self.subTest(after=after):
                p=ForumProvider([page([1],(10,100,2)),page([2,3,4],after)])
                r=self.reader(p);first=r.read(self.request(limit=2))
                second=r.read(self.request(limit=2,cursor=first.next_cursor))
                self.assertEqual([x.topic.id for x in second.results],[2,3])
                self.assertIsNotNone(second.next_cursor)
                final=r.read(self.request(limit=2,cursor=second.next_cursor))
                self.assertEqual([x.topic.id for x in final.results],[4])
                self.assertIsNone(final.next_cursor);self.assertFalse(final.scope_complete)
                self.assertIn(final.stop_reason,{'provider_end_unverified','provider_nonprogress'})
                self.assertEqual(len([c for c in p.calls if c[0]=='page']),2)

    def test_full_candidate_budget_drains_without_an_eleventh_or_extra_page(self):
        p=ForumProvider([page(list(range(1,201)),(10,100,200))])
        r=self.reader(p);cursor=None;ids=[]
        for _ in range(10):
            out=r.read(self.request(limit=20,**({'cursor':cursor} if cursor else {})))
            ids.extend(x.topic.id for x in out.results);cursor=out.next_cursor
            self.assertEqual((out.scanned_candidates,out.provider_pages),(200,1))
            self.assertLessEqual(len(out.results),20)
        self.assertEqual(ids,list(range(1,201)))
        self.assertIsNone(cursor);self.assertEqual(out.stop_reason,'scope_budget_exhausted')
        self.assertEqual(len([c for c in p.calls if c[0]=='page']),1)

    def test_tenth_native_page_can_drain_then_stops(self):
        p=ForumProvider([page([i],(i,100+i,i)) for i in range(1,10)]+[page([10,11],(10,110,11))])
        r=self.reader(p);cursor=None;ids=[]
        for _ in range(11):
            out=r.read(self.request(limit=1,**({'cursor':cursor} if cursor else {})))
            ids.extend(x.topic.id for x in out.results);cursor=out.next_cursor
        self.assertEqual(ids,list(range(1,12)))
        self.assertEqual(out.provider_pages,10);self.assertIsNone(cursor)
        self.assertEqual(out.stop_reason,'scope_budget_exhausted')
        self.assertEqual(len([c for c in p.calls if c[0]=='page']),10)

    def test_overremaining_and_oversized_pages_stop_before_hydration_without_truncation(self):
        p=ForumProvider([page(list(range(1,200)),(10,100,199)),page([200,201],(11,110,201))])
        r=self.reader(p);cursor=None
        for _ in range(11):
            out=r.read(self.request(limit=20,**({'cursor':cursor} if cursor else {})))
            cursor=out.next_cursor
        self.assertEqual(out.results,[]);self.assertIsNone(cursor)
        self.assertEqual(out.stop_reason,'scope_budget_exhausted')
        self.assertEqual(out.scanned_candidates,199)
        self.assertNotIn(('topic',7,200),p.calls)
        p=ForumProvider([page(list(range(1,202)))])
        out=self.reader(p).read(self.request())
        self.assertEqual(out.stop_reason,'invalid_provider_page')
        self.assertFalse(any(c[0]=='topic' for c in p.calls))

    def test_buffered_calls_recheck_authorization_and_expiry_without_hydrating(self):
        for mode in ('authorization','expiry','account'):
            with self.subTest(mode=mode):
                clock=[0.0];p=ForumProvider([page([1,2,3])]);r=self.reader(p,clock=lambda:clock[0])
                first=r.read(self.request(limit=1))
                self.assertEqual([x.topic.id for x in first.results],[1])
                if mode=='authorization':
                    def blocked(): raise AuthorizationBlocked('private')
                    p.ensure_ready=blocked
                elif mode=='expiry':clock[0]=301
                else:p.account=99
                out=r.read(self.request(limit=1,cursor=first.next_cursor))
                self.assertEqual(out.results,[]);self.assertIsNone(out.next_cursor)
                self.assertEqual(out.status,'blocked' if mode=='authorization' else 'invalid_cursor')
                self.assertNotIn(('topic',7,2),p.calls)
                self.assertEqual(len([c for c in p.calls if c[0]=='page']),1)

    def test_zero_repeated_empty_and_duplicate_only_pages_never_prove_inventory_complete(self):
        for ids,after in (([],(0,0,0)),([1],(0,0,0))):
            value=self.reader(ForumProvider([page(ids,after)])).read(self.request())
            self.assertFalse(value.scope_complete);self.assertIsNone(value.next_cursor)
        p=ForumProvider([page([1]),page([1],(11,101,3))]);r=self.reader(p)
        first=r.read(self.request());second=r.read(self.request(cursor=first.next_cursor))
        self.assertEqual(second.results,[]);self.assertIsNone(second.next_cursor)
        self.assertEqual(second.stop_reason,'provider_nonprogress')

    def test_wrong_chat_hydration_and_malformed_fields_never_leak_topic(self):
        for bad in (topic(1,chat=99,name='PRIVATE'),topic(2,name='PRIVATE'),topic(1,is_closed='yes')):
            result=self.reader(ForumProvider([page([1])],{1:bad})).read(self.request())
            self.assertEqual(result.results[0].status,'error')
            self.assertIsNone(result.results[0].metadata)
            self.assertNotIn('PRIVATE',result.model_dump_json());self.assertFalse(result.page_complete)

    def test_cursor_scope_expiry_account_capacity_and_close(self):
        clock=[0.0];p=ForumProvider([page([1]) for _ in range(8)])
        r=self.reader(p,clock=lambda:clock[0]);first=r.read(self.request())
        self.assertEqual(r.read(self.request(limit=1,cursor=first.next_cursor)).status,'invalid_cursor')
        p.account=19
        self.assertEqual(r.read(self.request(cursor=first.next_cursor)).status,'invalid_cursor')
        p.account=17;second=r.read(self.request());clock[0]=301
        self.assertEqual(r.read(self.request(cursor=second.next_cursor)).status,'invalid_cursor')
        for _ in range(4):self.assertIsNotNone(r.read(self.request()).next_cursor)
        self.assertEqual(r.read(self.request()).status,'capacity_exhausted')
        r.close();self.assertEqual(r.read(self.request()).status,'invalid_cursor')

    def test_strict_input_and_scope_output(self):
        for bad in ({'target':True},{'target':'7'},{'limit':True},{'limit':21},{'cursor':'guess'},{'query':'*'}):
            with self.subTest(bad=bad),self.assertRaises(ValidationError):schemas.ListTopicsRequest(**({'target':7}|bad))
        value=self.reader(ForumProvider([page([1])])).read(self.request()).model_dump(mode='json')
        value['results'][0]['chat_id']=99
        with self.assertRaises(ValidationError):schemas.ListTopicsResponse.model_validate(value)

    def test_scope_page_cap_and_title_sanitation_are_explicit(self):
        p=ForumProvider([page([i],(i,100+i,i)) for i in range(1,11)],{1:topic(1,name='Ａ\u202e'+'x'*300)})
        r=self.reader(p);cursor=None
        for i in range(10):
            value=r.read(self.request(**({'cursor':cursor} if cursor else {})))
            if i==0:
                text=value.results[0].metadata.name
                self.assertTrue(text.sanitized);self.assertTrue(text.truncated);self.assertEqual(len(text.value),256)
            cursor=value.next_cursor
        self.assertEqual(value.status,'limit_reached');self.assertIsNone(cursor);self.assertFalse(value.scope_complete)

    def test_invalid_page_is_rejected_before_any_topic_hydration(self):
        bad_pages=[]
        for field,value in (('topics',None),('next_offset_date',True),('next_offset_message_id',-1),
                            ('next_offset_forum_topic_id',2**31)):
            bad=page([1]);bad[field]=value;bad_pages.append(bad)
        bad=page([1]);bad['topics'].append(topic(2,chat=99,name='PRIVATE'));bad_pages.append(bad)
        for bad in bad_pages:
            p=ForumProvider([bad]);out=self.reader(p).read(self.request())
            self.assertEqual(out.status,'error');self.assertEqual(out.results,[])
            self.assertIsNone(out.next_cursor);self.assertNotIn('PRIVATE',out.model_dump_json())
            self.assertFalse(any(c[0]=='topic' for c in p.calls))

    def test_partial_hydration_stop_reports_all_observed_ids_and_does_not_continue(self):
        for error,status,reason in ((AuthorizationBlocked('private'),'blocked','authorization_unavailable'),
                                   (TDLibDeadlineExceeded('private'),'partial','call_budget_exhausted'),
                                   (TDLibError('private'),'error','provider_error')):
            p=ForumProvider([page([1,2,3])],{2:error})
            out=self.reader(p).read(self.request())
            self.assertEqual([r.topic.id for r in out.results],[1,2,3])
            self.assertEqual([r.status for r in out.results],['complete','error','error'])
            self.assertEqual(out.status,status);self.assertEqual(out.stop_reason,reason)
            self.assertFalse(out.page_complete);self.assertIsNone(out.next_cursor)
            self.assertNotIn(('topic',7,3),p.calls);self.assertNotIn('private',out.model_dump_json())

    def test_capability_and_initial_deadline_stop_before_listing(self):
        p=ForumProvider([])
        result=self.reader(p).read(self.request(),deadline=time.monotonic()-1)
        self.assertEqual(result.stop_reason,'call_budget_exhausted');self.assertEqual(p.calls,[])
        def unsupported(chat): raise ForumUnsupported('not a forum')
        p.resolve_forum_chat=unsupported
        result=self.reader(p).read(self.request())
        self.assertEqual(result.status,'unsupported');self.assertEqual(result.stop_reason,'unsupported_forum')
        self.assertFalse(any(c[0]=='page' for c in p.calls))

    def test_reader_atomic_replay_reservation_and_close_during_hydration(self):
        entered=threading.Event();release=threading.Event()
        p=ForumProvider([page([i],(i,100+i,i)) for i in range(1,6)])
        r=self.reader(p);pages=[r.read(self.request()) for _ in range(4)]
        original=p.get_forum_topic
        def slow(chat,tid):
            entered.set();release.wait(3);return original(chat,tid)
        p.get_forum_topic=slow
        out=[];req=self.request(cursor=pages[0].next_cursor)
        thread=threading.Thread(target=lambda:out.append(r.read(req)))
        thread.start();self.assertTrue(entered.wait(1))
        try:
            self.assertEqual(r.read(req).status,'invalid_cursor')
            self.assertEqual(r.read(self.request()).status,'capacity_exhausted')
            r.close()
        finally:
            release.set();thread.join(3)
        self.assertEqual(out[0].stop_reason,'invalid_cursor');self.assertIsNone(out[0].next_cursor)
        self.assertEqual(r.read(self.request()).status,'invalid_cursor')

    def test_response_rejects_false_completeness_and_untrusted_flag_coercion(self):
        good=self.reader(ForumProvider([page([1])])).read(self.request()).model_dump(mode='json')
        changes=[]
        for field,value in (('scope_complete',True),('scope_complete',0),('has_more',False),('page_complete',1)):
            bad=copy.deepcopy(good);bad[field]=value;changes.append(bad)
        for field,value in (('untrusted',False),('untrusted',1),('sanitized','yes')):
            bad=copy.deepcopy(good);bad['results'][0]['metadata']['name'][field]=value;changes.append(bad)
        for bad in changes:
            with self.assertRaises(ValidationError):schemas.ListTopicsResponse.model_validate(bad)
