from __future__ import annotations

import copy
import importlib
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from pydantic import ValidationError
from telegram_search_mcp import schemas
from telegram_search_mcp.tdjson import MessageNotFound, TDLibError, TDLibDeadlineExceeded
from test_message_reader import Provider, message


class HistoryProvider(Provider):
    def __init__(self, messages, *, inclusive=False):
        super().__init__(messages)
        self.inclusive = inclusive
        self.account_id = 17
        self.pages = None
        self.hydration = {}

    def get_account_id(self):
        return self.account_id

    def get_chat_history(self, chat_id, *, from_message_id, limit):
        self.calls.append(('history', chat_id, from_message_id, limit))
        if self.pages is not None:
            return copy.deepcopy(self.pages.pop(0))
        values = [m for i, m in sorted(self.messages.items(), reverse=True)
                  if not isinstance(m, Exception) and
                  (not from_message_id or i < from_message_id or (self.inclusive and i == from_message_id))]
        return copy.deepcopy(values[:limit])

    def get_message(self, chat_id, message_id):
        if message_id in self.hydration:
            value = self.hydration[message_id]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        return super().get_message(chat_id, message_id)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(schemas, 'ReadHistoryRequest'), 'F3 history contract is missing')
        self.mod = importlib.import_module('telegram_search_mcp.history_reader')
        self.now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.tick = [10.0]

    def reader(self, provider, **kw):
        return self.mod.HistoryReader(client=provider, client_id='client_synthetic',
            broker_generation='broker_synthetic', clock=lambda:self.tick[0], now=lambda:self.now, **kw)

    def request(self, **kw):
        return schemas.ReadHistoryRequest(target=7, mode='latest', **kw)

    def interval(self, **kw):
        return schemas.ReadHistoryRequest(target=7, mode='interval',
            date_from='2023-11-14T22:13:20Z', date_to='2023-11-14T22:13:22Z', **kw)

    def ids(self, response):
        return [r.anchor.message_id for r in response.results]

    def test_strict_explicit_mode_dates_and_numeric_target(self):
        invalid = [dict(target=7), dict(target='@synthetic',mode='latest'),
                   dict(target=True,mode='latest'), dict(target=0,mode='latest'),
                   dict(target=7,mode='latest',limit=True), dict(target=7,mode='latest',limit=21),
                   dict(target=7,mode='latest',date_from='2023-01-01T00:00:00Z'),
                   dict(target=7,mode='interval'),
                   dict(target=7,mode='interval',date_from='2023-01-01T00:00:00',date_to='2024-01-01T00:00:00Z'),
                   dict(target=7,mode='interval',date_from='2023-01-01T00:00:00Z',date_to='2023-01-01T00:00:00Z'),
                   dict(target=7,mode='latest',cursor='guess'), dict(target=7,mode='latest',path='/private')]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                schemas.ReadHistoryRequest(**value)

    def test_head_same_timestamp_pages_and_insertions_never_duplicate(self):
        for inclusive in (False, True):
            with self.subTest(inclusive=inclusive):
                p = HistoryProvider({i:message(i) for i in (40,30,20,10)},inclusive=inclusive)
                reader = self.reader(p)
                first = reader.read(self.request(limit=2))
                self.assertEqual(self.ids(first),[40,30])
                self.assertEqual(first.scope.upper_message_id,40)
                self.assertEqual(first.scope.date_to,self.now)
                self.assertTrue(first.page_complete)
                self.assertFalse(first.scope_complete)
                p.messages[50] = message(50)
                second = reader.read(self.request(limit=2,cursor=first.next_cursor))
                self.assertEqual(self.ids(second),[20,10])
                self.assertEqual(second.scope.upper_message_id,40)
                end = reader.read(self.request(limit=2,cursor=second.next_cursor))
                self.assertEqual(end.status,'partial')
                self.assertFalse(end.scope_complete)
                self.assertIsNone(end.next_cursor)
                self.assertIsNone(end.has_more)
                self.assertIn(end.stop_reason,('provider_end_unverified','provider_nonprogress'))

    def test_half_open_dates_use_hydration_and_not_id_timestamp_monotonicity(self):
        p=HistoryProvider({50:message(50,date=1700000000),40:message(40,date=1699999999),
                           30:message(30,date=1700000002),20:message(20,date=1700000001)})
        p.hydration[50]=message(50,date=1700000002)
        result=self.reader(p).read(self.interval(limit=20))
        self.assertEqual(self.ids(result),[20])
        self.assertFalse(result.scope_complete)
        self.assertFalse(result.page_complete)

    def test_deleted_and_edited_pending_rows_are_rehydrated(self):
        p=HistoryProvider({i:message(i) for i in (40,30,20,10)})
        reader=self.reader(p)
        first=reader.read(self.request(limit=1))
        p.hydration[30]=MessageNotFound('private provider message')
        second=reader.read(self.request(limit=1,cursor=first.next_cursor))
        self.assertEqual(self.ids(second),[30])
        self.assertEqual(second.results[0].status,'not_found')
        self.assertIsNone(second.results[0].message)
        self.assertFalse(second.page_complete)
        p.hydration[20]=message(20,edit_date=1700000100,content={'@type':'messageText','text':{'text':'edited synthetic'}})
        third=reader.read(self.request(limit=1,cursor=second.next_cursor))
        self.assertEqual(self.ids(third),[20])
        self.assertEqual(third.results[0].message.text.value,'edited synthetic')
        self.assertTrue(third.results[0].message.is_edited)
        self.assertNotIn('private provider message',second.model_dump_json())

    def test_cursor_binding_replay_expiry_and_capacity(self):
        p=HistoryProvider({i:message(i) for i in range(1,50)})
        reader=self.reader(p)
        first=reader.read(self.request(limit=1))
        mismatch=reader.read(self.request(limit=2,cursor=first.next_cursor))
        self.assertEqual(mismatch.status,'invalid_cursor')
        next_page=reader.read(self.request(limit=1,cursor=first.next_cursor))
        self.assertEqual(self.ids(next_page),[48])
        self.assertEqual(reader.read(self.request(limit=1,cursor=first.next_cursor)).status,'invalid_cursor')
        other=self.reader(p)
        self.assertEqual(other.read(self.request(limit=1,cursor=next_page.next_cursor)).status,'invalid_cursor')
        p.account_id=18
        self.assertEqual(reader.read(self.request(limit=1,cursor=next_page.next_cursor)).status,'invalid_cursor')
        p.account_id=17
        cursors=[reader.read(self.request(limit=1)).next_cursor for _ in range(4)]
        self.assertTrue(all(cursors))
        self.assertEqual(reader.read(self.request(limit=1)).status,'capacity_exhausted')
        self.tick[0]=311
        self.assertEqual(reader.read(self.request(limit=1,cursor=cursors[0])).status,'invalid_cursor')
        self.assertEqual(reader.read(self.request(limit=1)).status,'page')

    def test_invalid_page_and_hydrated_identity_never_leak_content(self):
        for bad in [message(12,chat_id=999), message(True), message(12,date=True)]:
            p=HistoryProvider({12:message(12)});p.pages=[[bad]]
            result=self.reader(p).read(self.request())
            self.assertEqual(result.status,'error')
            self.assertEqual(result.results,[])
            self.assertNotIn('Full selected text',result.model_dump_json())
        p=HistoryProvider({12:message(12)})
        p.hydration[12]=message(12,chat_id=999)
        result=self.reader(p).read(self.request())
        self.assertFalse(result.scope_complete)
        self.assertNotIn('Full selected text',result.model_dump_json())

    def test_empty_and_same_only_pages_are_unverified_end(self):
        for pages in [[],[message(30)]]:
            p=HistoryProvider({30:message(30)})
            p.pages=[[message(30)],pages]
            reader=self.reader(p);first=reader.read(self.request(limit=1))
            end=reader.read(self.request(limit=1,cursor=first.next_cursor))
            self.assertEqual(end.status,'partial')
            self.assertFalse(end.scope_complete)
            self.assertIsNone(end.next_cursor)

    def test_latest_scope_budget_and_call_deadline_never_become_complete(self):
        p=HistoryProvider({i:message(i) for i in range(1,151)})
        reader=self.reader(p);cursor=None;seen=[]
        for _ in range(5):
            page=reader.read(self.request(limit=20,cursor=cursor));seen.extend(self.ids(page));cursor=page.next_cursor
        self.assertEqual(seen,list(range(150,50,-1)))
        self.assertEqual(page.status,'limit_reached')
        self.assertEqual(page.stop_reason,'scope_budget_exhausted')
        self.assertIsNone(cursor)
        self.assertFalse(page.scope_complete)
        result=reader.read(self.request(),deadline=0)
        self.assertEqual(result.status,'partial')
        self.assertEqual(result.stop_reason,'call_budget_exhausted')

    def test_text_budget_and_unsupported_content_are_explicit(self):
        p=HistoryProvider({i:message(i,content={'@type':'messageText','text':{'text':'x'*20001}}) for i in range(1,8)})
        result=self.reader(p).read(self.request(limit=7))
        self.assertLessEqual(sum(len(r.message.text.value) for r in result.results if r.message and r.message.text),100000)
        self.assertFalse(result.page_complete)
        self.assertFalse(result.scope_complete)

    def test_simultaneous_same_cursor_is_consumed_at_most_once(self):
        p=HistoryProvider({i:message(i) for i in (40,30,20,10)})
        reader=self.reader(p);first=reader.read(self.request(limit=1))
        entered=threading.Event();release=threading.Event();original=p.get_account_id
        def slow():
            entered.set(); release.wait(2); return original()
        p.get_account_id=slow;out=[]
        thread=threading.Thread(target=lambda:out.append(reader.read(self.request(limit=1,cursor=first.next_cursor))))
        thread.start();self.assertTrue(entered.wait(1))
        duplicate=reader.read(self.request(limit=1,cursor=first.next_cursor))
        release.set();thread.join(3)
        self.assertEqual(duplicate.status,'invalid_cursor')
        self.assertEqual(self.ids(out[0]),[30])

    def test_last_sender_deadline_is_explicit_call_budget(self):
        p=HistoryProvider({30:message(30),20:message(20)})
        def timeout(sender):raise TDLibDeadlineExceeded('private timeout details')
        p.get_sender_identity=timeout
        result=self.reader(p).read(self.request(limit=1))
        self.assertEqual(result.status,'partial')
        self.assertEqual(result.stop_reason,'call_budget_exhausted')
        self.assertIsNone(result.next_cursor)
        self.assertNotIn('private timeout details',result.model_dump_json())

    def test_inflight_capacity_and_close_discard_state(self):
        p=HistoryProvider({30:message(30),20:message(20)})
        reader=self.reader(p);gate=threading.Barrier(5);release=threading.Event();out=[]
        def slow():gate.wait(timeout=3);release.wait(3);return 17
        p.get_account_id=slow
        workers=[threading.Thread(target=lambda:out.append(reader.read(self.request(limit=1)))) for _ in range(4)]
        for worker in workers:worker.start()
        gate.wait(timeout=3)
        self.assertEqual(reader.read(self.request(limit=1)).status,'capacity_exhausted')
        reader.close();release.set()
        for worker in workers:worker.join(3)
        self.assertEqual(len(out),4)
        self.assertTrue(all(x.status=='partial' and x.next_cursor is None for x in out))

    def test_expiry_during_active_page_removes_continuation(self):
        p=HistoryProvider({30:message(30),20:message(20)})
        reader=self.reader(p)
        original=p.get_message
        def later(*args):self.tick[0]=311;return original(*args)
        p.get_message=later
        result=reader.read(self.request(limit=1))
        self.assertEqual(result.stop_reason,'invalid_cursor')
        self.assertIsNone(result.next_cursor)

    def test_clock_exhaustion_after_hydration_is_not_page_limit(self):
        p=HistoryProvider({30:message(30),20:message(20)})
        tick=[10.0];original=p.get_message
        def later(*args):tick[0]=50.0;return original(*args)
        p.get_message=later
        with patch.object(self.mod.time,'monotonic',side_effect=lambda:tick[0]):
            result=self.reader(p).read(self.request(limit=1),deadline=40)
        self.assertEqual(result.stop_reason,'call_budget_exhausted')
        self.assertFalse(result.page_complete)
        self.assertIsNone(result.next_cursor)


class HistoryNativeBoundaryTests(unittest.TestCase):
    def test_account_identity_rejects_boolean_and_out_of_range(self):
        from telegram_search_mcp.tdjson import TDLibClient
        from test_tdjson import ScriptedRaw
        for account_id in (True,2**53):
            raw=ScriptedRaw({'getAuthorizationState':[{'@type':'authorizationStateReady'}],
                             'getMe':[{'@type':'user','id':account_id}]})
            client=TDLibClient(raw=raw);client.ensure_ready()
            with self.assertRaises(TDLibError):client.get_account_id()

    def test_native_history_request_and_malformed_page_rejected(self):
        from telegram_search_mcp.tdjson import TDLibClient
        from test_tdjson import ScriptedRaw
        raw=ScriptedRaw({'getAuthorizationState':[{'@type':'authorizationStateReady'}],
                         'getChatHistory':[{'@type':'messages','total_count':-1,'messages':[message(11)]}]})
        client=TDLibClient(raw=raw);client.ensure_ready()
        self.assertEqual(client.get_chat_history(7,from_message_id=12,limit=2)[0]['id'],11)
        self.assertEqual({k:v for k,v in raw.sent[-1].items() if k!='@extra'},
            {'@type':'getChatHistory','chat_id':7,'from_message_id':12,'offset':0,'limit':2,'only_local':False})
        raw=ScriptedRaw({'getAuthorizationState':[{'@type':'authorizationStateReady'}],
                         'getChatHistory':[{'@type':'messages','total_count':-1,'messages':[None]}]})
        client=TDLibClient(raw=raw);client.ensure_ready()
        with self.assertRaises(TDLibError):client.get_chat_history(7,from_message_id=12,limit=2)
