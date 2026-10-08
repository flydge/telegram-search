from __future__ import annotations

import copy
import importlib
import importlib.util
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone

from telegram_search_mcp import schemas
from telegram_search_mcp.tdjson import AuthorizationBlocked, MessageNotFound, TDLibDeadlineExceeded, TDLibError


def chat(cid, lists=('main',), **changes):
    return {'@type':'chat','id':cid,'title':'Synthetic title',
        'type':{'@type':'chatTypePrivate','user_id':100},
        'chat_lists':[{'@type':'chatListMain' if x=='main' else 'chatListArchive'} for x in lists],
        'positions':[{'list':{'@type':'chatListMain'},'order':'9223372036854775807'}],
        'unread_count':2,'unread_mention_count':1,'unread_reaction_count':0,'is_marked_as_unread':False,
        'last_message':{'text':'NEVER_RETAIN_BODY'},'draft_message':{'text':'NEVER_RETAIN_DRAFT'},**changes}


class Provider:
    def __init__(self, main=(9,3,7), archive=()):
        self.pages={'main':{'@type':'chats','total_count':len(main),'chat_ids':list(main)},
                    'archive':{'@type':'chats','total_count':len(archive),'chat_ids':list(archive)}}
        self.chats={i:chat(i,('main',) if i in main else ('archive',)) for i in (*main,*archive)}
        self.calls=[];self.account=17;self.ready=None;self.hook=None
    def request_budget(self,deadline):return nullcontext()
    def ensure_ready(self):
        if self.ready:raise self.ready
    def get_account_id(self):return self.account
    def get_chat_list_snapshot(self,selection,*,limit):
        self.calls.append(('getChats',selection,limit))
        value=self.pages[selection]
        if isinstance(value,Exception):raise value
        return copy.deepcopy(value)
    def get_chat_metadata(self,cid):
        self.calls.append(('getChat',cid))
        if self.hook:self.hook(cid)
        value=self.chats[cid]
        if isinstance(value,Exception):raise value
        return copy.deepcopy(value)


class ChatListingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.chat_list_reader'),
                             'bounded list_chats reader missing')
        self.mod=importlib.import_module('telegram_search_mcp.chat_list_reader')
        self.tick=[10.];self.now=datetime(2026,1,1,tzinfo=timezone.utc)
    def reader(self,p):return self.mod.ChatListReader(client=p,client_id='synthetic',broker_generation='synthetic',
        clock=lambda:self.tick[0],now=lambda:self.now)
    def request(self,**kw):return schemas.ListChatsRequest(**{'scope':'main',**kw})
    def test_frozen_native_order_limits_single_use_and_no_retained_metadata(self):
        p=Provider();reader=self.reader(p);a=reader.read(self.request(limit=2))
        self.assertEqual([r.chat_id for r in a.results],[9,3]);self.assertEqual(a.observed_candidates,3)
        self.assertEqual(a.pending_candidates,1);self.assertEqual(a.processed_this_page,2)
        self.assertEqual(p.calls,[('getChats','main',200),('getChat',9),('getChat',3)])
        p.pages['main']['chat_ids']=[50,9,3,7];p.chats[7]['unread_count']=5
        b=reader.read(self.request(limit=2,cursor=a.next_cursor))
        self.assertEqual([r.chat_id for r in b.results],[7]);self.assertEqual(b.results[0].unread_count,5)
        self.assertEqual(b.status,'prefix_exhausted_unverified');self.assertIsNone(b.next_cursor)
        self.assertFalse(b.scope_complete);self.assertIsNone(b.has_more);self.assertEqual(a.scope,b.scope)
        self.assertEqual(reader.read(self.request(limit=2,cursor=a.next_cursor)).status,'invalid_cursor')
        self.assertNotIn('Synthetic title',repr(reader._scans));self.assertNotIn('NEVER_RETAIN',repr(reader._scans))
    def test_both_sequential_snapshots_dedup_consume_budget_and_membership_movement(self):
        p=Provider(main=(9,3),archive=(9,7));p.chats[3]['chat_lists']=[{'@type':'chatListArchive'}]
        reader=self.reader(p);a=reader.read(self.request(scope='both',limit=2))
        self.assertEqual([r.chat_id for r in a.results],[9,3]);self.assertEqual(a.results[1].current_lists,['archive'])
        self.assertEqual(p.calls[:2],[('getChats','main',100),('getChats','archive',100)])
        b=reader.read(self.request(scope='both',limit=2,cursor=a.next_cursor))
        self.assertEqual([r.chat_id for r in b.results],[7]);self.assertEqual(b.omitted_candidates,1)
        self.assertEqual(b.list_coverage[1].issues.duplicate_observation,1);self.assertEqual(b.observed_candidates,4)
        self.assertEqual(b.returned_candidates,3);self.assertFalse(b.page_complete)
        p=Provider(main=(9,));p.chats[9]['chat_lists']=[{'@type':'chatListArchive'}]
        r=self.reader(p).read(self.request());self.assertEqual(r.results,[])
        self.assertEqual(r.list_coverage[0].issues.out_of_scope,1)
    def test_native_prefix_is_whole_validated_before_hydration(self):
        invalid=[{},None,{'@type':'chats','total_count':0,'chat_ids':[9,True]},
          {'@type':'chats','total_count':'1','chat_ids':[9]},
          {'@type':'chats','total_count':1,'chat_ids':[9,9]},
          {'@type':'chats','total_count':1,'chat_ids':[9,0]},
          {'@type':'chats','total_count':1,'chat_ids':[9,2**53]},
          {'@type':'chats','total_count':201,'chat_ids':list(range(1,202))}]
        for page in invalid:
            p=Provider();p.pages['main']=page;r=self.reader(p).read(self.request())
            self.assertEqual(r.status,'error');self.assertEqual(r.stop_reason,'invalid_provider_prefix')
            self.assertEqual(r.results,[]);self.assertFalse(any(x[0]=='getChat' for x in p.calls))
    def test_secret_identity_malformed_unread_and_types_omit_without_leak(self):
        cases=[({'id':10},'identity_mismatch'),({'type':{'@type':'chatTypeSecret','secret_chat_id':8}},'secret'),
          ({'unread_count':True},'invalid_metadata'),({'unread_reaction_count':'0'},'invalid_metadata'),
          ({'unread_mention_count':2**31},'invalid_metadata'),({'is_marked_as_unread':0},'invalid_metadata'),
          ({'type':{'@type':'chatTypePrivate','user_id':0}},'invalid_metadata'),
          ({'type':{'@type':'chatTypeSupergroup','supergroup_id':8,'is_channel':1}},'invalid_metadata'),
          ({'chat_lists':[{'@type':'unknown'}]},'invalid_metadata')]
        for changes,issue in cases:
            p=Provider(main=(9,));p.chats[9]=chat(9,title='DO_NOT_DISCLOSE',**changes)
            r=self.reader(p).read(self.request());self.assertEqual(r.results,[])
            self.assertEqual(getattr(r.list_coverage[0].issues,issue),1)
            self.assertNotIn('DO_NOT_DISCLOSE',r.model_dump_json());self.assertNotIn('"chat_id":9',r.model_dump_json())
    def test_folder_memberships_allowed_positions_ignored_and_title_sanitized(self):
        p=Provider(main=(9,));p.chats[9]['chat_lists'].append({'@type':'chatListFolder','chat_folder_id':2})
        p.chats[9]['title']='\u202eHello\n'+('x'*300)
        r=self.reader(p).read(self.request());row=r.results[0]
        self.assertTrue(row.title.sanitized);self.assertTrue(row.title.truncated);self.assertTrue(row.title.untrusted)
        self.assertLessEqual(len(row.title.value),256);self.assertNotIn('\u202e',row.title.value)
        for native,kind in [('chatTypeBasicGroup','basic_group'),('chatTypeSupergroup','supergroup'),('chatTypeSupergroup','channel')]:
            p.chats[9]['type']={'@type':native, 'basic_group_id':5,'supergroup_id':5,'is_channel':kind=='channel'}
            self.assertEqual(self.reader(p).read(self.request()).results[0].kind,kind)
    def test_oversized_native_metadata_is_omitted_before_normalization_or_membership_walk(self):
        for changes in ({'title':'x'*4097}, {'chat_lists':[{'@type':'chatListMain'}]*129}):
            p=Provider(main=(9,));p.chats[9]=chat(9,**changes)
            r=self.reader(p).read(self.request())
            with self.subTest(field=next(iter(changes))):
                self.assertEqual(r.results,[]);self.assertEqual(r.list_coverage[0].issues.invalid_metadata,1)
    def test_title_normalization_expansion_cannot_hide_display_truncation(self):
        p=Provider(main=(9,));p.chats[9]['title']='\ufdfa'*16
        row=self.reader(p).read(self.request()).results[0]
        self.assertTrue(row.title.sanitized);self.assertTrue(row.title.truncated)
        self.assertEqual(len(row.title.value),256)
    def test_cursor_scope_limit_account_client_expiry_capacity_and_close(self):
        p=Provider();reader=self.reader(p);a=reader.read(self.request(limit=1))
        for kw in ({'scope':'archive','limit':1},{'limit':2}):
            self.assertEqual(reader.read(self.request(cursor=a.next_cursor,**kw)).status,'invalid_cursor')
        self.assertEqual(self.reader(p).read(self.request(limit=1,cursor=a.next_cursor)).status,'invalid_cursor')
        p.account=18;self.assertEqual(reader.read(self.request(limit=1,cursor=a.next_cursor)).status,'invalid_cursor')
        reader=self.reader(p);a=reader.read(self.request(limit=1));self.tick[0]=311
        self.assertEqual(reader.read(self.request(limit=1,cursor=a.next_cursor)).status,'invalid_cursor')
        reader=self.reader(p)
        for _ in range(4):self.assertEqual(reader.read(self.request(limit=1)).status,'page')
        self.assertEqual(reader.read(self.request(limit=1)).status,'capacity_exhausted')
        reader.close();self.assertEqual(reader.read(self.request()).status,'invalid_cursor')
    def test_provider_failures_deadline_and_empty_prefix_are_honest(self):
        for error,status,reason in [(TDLibError('PRIVATE'),'error','provider_error'),
          (AuthorizationBlocked('PRIVATE'),'blocked','authorization_unavailable'),
          (TDLibDeadlineExceeded('PRIVATE'),'partial','call_budget_exhausted')]:
            p=Provider();p.chats[3]=error;r=self.reader(p).read(self.request())
            self.assertEqual([x.chat_id for x in r.results],[9]);self.assertEqual(r.status,status)
            self.assertEqual(r.stop_reason,reason);self.assertIsNone(r.next_cursor)
            self.assertEqual(r.processed_candidates,2);self.assertEqual(r.omitted_candidates,1)
            self.assertNotIn('PRIVATE',r.model_dump_json())
        p=Provider(main=());r=self.reader(p).read(self.request())
        self.assertEqual(r.status,'prefix_exhausted_unverified');self.assertFalse(r.page_complete)
        self.assertFalse(r.scope_complete);self.assertIsNone(r.has_more)
        p=Provider();p.chats[3]=MessageNotFound('PRIVATE');r=self.reader(p).read(self.request())
        self.assertEqual([x.chat_id for x in r.results],[9,7]);self.assertEqual(r.list_coverage[0].issues.unavailable,1)
    def test_inflight_calls_count_against_capacity(self):
        p=Provider();reader=self.reader(p)
        for _ in range(3):reader.read(self.request(limit=1))
        entered=threading.Event();release=threading.Event()
        p.hook=lambda cid:(entered.set(),release.wait(2))
        worker=threading.Thread(target=lambda:reader.read(self.request(limit=1)));worker.start();entered.wait(2)
        self.assertEqual(reader.read(self.request(limit=1)).status,'capacity_exhausted')
        release.set();worker.join()
    def test_close_during_native_hydration_stops_work_and_never_returns_success(self):
        for candidates in ((9,), (9,3,7)):
            p=Provider(main=candidates);reader=self.reader(p);p.hook=lambda cid:reader.close()
            r=reader.read(self.request())
            with self.subTest(count=len(candidates)):
                self.assertEqual(r.status,'partial');self.assertEqual(r.stop_reason,'invalid_cursor')
                self.assertEqual(r.results,[]);self.assertIsNone(r.next_cursor)
                self.assertEqual([x for x in p.calls if x[0]=='getChat'],[('getChat',9)])
    def test_account_mismatch_refusal_stays_valid_when_lifetime_expires_during_return(self):
        p=Provider();reader=self.reader(p);first=reader.read(self.request(limit=1));p.account=18
        ticks=iter((10.,10.,10.,10.,311.));reader._clock=lambda:next(ticks)
        r=reader.read(self.request(limit=1,cursor=first.next_cursor))
        self.assertEqual(r.status,'invalid_cursor')
        schemas.ListChatsResponse.model_validate(r.model_dump())


class ChatListingRequestTests(unittest.TestCase):
    def test_request_scope_strict_bounds_and_extra_forbid(self):
        self.assertTrue(hasattr(schemas,'ListChatsRequest'),'strict list_chats request missing')
        for args in ({},{'scope':'*'},{'scope':'MAIN'},{'scope':True},{'scope':'main','limit':True},
          {'scope':'main','limit':'1'},{'scope':'main','limit':0},{'scope':'main','limit':21},
          {'scope':'main','cursor':'path'},{'scope':'main','extra':1}):
            with self.assertRaises(ValueError):schemas.ListChatsRequest.model_validate(args)
        self.assertEqual(schemas.ListChatsRequest(scope='main').limit,20)

if __name__=='__main__':unittest.main()
