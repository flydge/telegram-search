from __future__ import annotations

import copy
import importlib
import importlib.util
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from telegram_search_mcp import schemas
from telegram_search_mcp.tdjson import AuthorizationBlocked, MessageNotFound, TDLibDeadlineExceeded
from test_history_reader import HistoryProvider
from test_message_reader import message


def page(rows, offset=0, total=-1):
    return {'@type': 'foundChatMessages', 'messages': rows, 'next_from_message_id': offset, 'total_count': total}


class SearchProvider(HistoryProvider):
    def search_chat_messages(self, chat_id, query, *, from_message_id, limit):
        self.calls.append(('search', chat_id, query, from_message_id, limit))
        if self.pages is not None:
            value = self.pages.pop(0)
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        rows = [m for i, m in sorted(self.messages.items(), reverse=True)
                if not isinstance(m, Exception) and (not from_message_id or i <= from_message_id)]
        rows = rows[:limit]
        return page(copy.deepcopy(rows), rows[-1]['id'] if len(rows) == limit else 0)


class ExactSearchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.exact_search_reader'),
                             'F5 exact paginated search reader missing')
        self.mod = importlib.import_module('telegram_search_mcp.exact_search_reader')
        self.now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.tick = [10.0]

    def reader(self, provider):
        return self.mod.ExactSearchReader(client=provider, client_id='client_synthetic',
            broker_generation='broker_synthetic', clock=lambda: self.tick[0], now=lambda: self.now)

    def request(self, **changes):
        return schemas.SearchMessagesRequest(**{'target': 7, 'query': 'selected', 'mode': 'latest', **changes})

    def ids(self, response):
        return [r.anchor.message_id for r in response.results]

    def test_after_twentieth_frozen_head_replay_and_provider_end_never_claim_recall(self):
        p = SearchProvider({i: message(i) for i in range(1, 46)})
        r = self.reader(p)
        a = r.read(self.request())
        self.assertEqual(self.ids(a), list(range(45, 25, -1)))
        self.assertTrue(a.page_complete)
        p.messages[100] = message(100)
        b = r.read(self.request(cursor=a.next_cursor))
        self.assertEqual(self.ids(b), list(range(25, 5, -1)))
        self.assertEqual(a.scope, b.scope)
        c = r.read(self.request(cursor=b.next_cursor))
        self.assertEqual(self.ids(c), [5, 4, 3, 2, 1])
        self.assertEqual(c.stop_reason, 'provider_end_unverified')
        self.assertTrue(c.page_complete)
        self.assertFalse(c.scope_complete)
        self.assertIsNone(c.has_more)
        self.assertIsNone(c.next_cursor)
        self.assertEqual(r.read(self.request(cursor=a.next_cursor)).status, 'invalid_cursor')
        self.assertEqual([x[3] for x in p.calls if x[0] == 'search'], [0, 26, 7])

    def test_returned_offset_not_last_public_message_and_empty_advancing_page(self):
        p = SearchProvider({50: message(50), 20: message(20)})
        p.pages = [page([message(50)], 40), page([], 30), page([message(20)], 0)]
        out = self.reader(p).read(self.request())
        self.assertEqual(self.ids(out), [50, 20])
        self.assertEqual([x[3] for x in p.calls if x[0] == 'search'], [0, 40, 30])
        self.assertEqual((out.scanned_candidates, out.processed_candidates, out.provider_pages), (2, 2, 3))
        self.assertFalse(out.scope_complete)

    def test_pending_is_rehydrated_and_only_digests_persist(self):
        p = SearchProvider({i: message(i) for i in (40, 30, 20)})
        r = self.reader(p)
        a = r.read(self.request(limit=1))
        self.assertEqual(self.ids(a), [40])
        self.assertNotIn('Full selected text', repr(r._scans))
        self.assertNotIn('Synthetic Reader', repr(r._scans))
        p.hydration[30] = message(30, edit_date=1700000001)
        b = r.read(self.request(limit=1, cursor=a.next_cursor))
        self.assertEqual(b.results[0].status, 'evidence_changed')
        self.assertIsNone(b.results[0].message)
        p.hydration[20] = MessageNotFound('private detail')
        c = r.read(self.request(limit=1, cursor=b.next_cursor))
        self.assertEqual(c.results[0].status, 'not_found')
        self.assertFalse(c.page_complete)
        self.assertEqual(len([x for x in p.calls if x[0] == 'search']), 1)
        self.assertNotIn('private detail', c.model_dump_json())

    def test_raw_pre_normalization_and_end_of_text_edit_detected(self):
        for original, changed in [('Ａ', 'A'), ('before\x00after', 'beforeafter'), ('x' * 30000 + 'a', 'x' * 30000 + 'b')]:
            a = message(40); a['content']['text']['text'] = original
            b = copy.deepcopy(a); b['content']['text']['text'] = changed
            p = SearchProvider({40: a}); p.hydration[40] = b
            out = self.reader(p).read(self.request())
            self.assertEqual(out.results[0].status, 'evidence_changed')
            self.assertIsNone(out.results[0].message)

    def test_provider_lexical_matches_need_not_be_local_substrings(self):
        p = SearchProvider({40: message(40)})
        out = self.reader(p).read(self.request(query='provider stemmed query'))
        self.assertEqual(out.results[0].status, 'match')
        self.assertEqual(out.provider_coverage, 'tdlib_lexical_unverified')

    def test_repeat_offset_drains_unique_evidence_then_stops_without_refetch(self):
        p = SearchProvider({i: message(i) for i in (40, 30, 20)})
        p.pages = [page([message(40), message(30)], 30), page([message(30), message(20)], 30)]
        r = self.reader(p)
        a = r.read(self.request(limit=1))
        b = r.read(self.request(limit=1, cursor=a.next_cursor))
        c = r.read(self.request(limit=1, cursor=b.next_cursor))
        self.assertEqual(self.ids(a) + self.ids(b) + self.ids(c), [40, 30, 20])
        self.assertEqual(c.stop_reason, 'provider_nonprogress')
        self.assertIsNone(c.next_cursor)
        self.assertEqual(c.scanned_candidates, 4)
        self.assertEqual(len([x for x in p.calls if x[0] == 'search']), 2)

    def test_all_rows_validate_before_hydration_and_unsafe_envelopes_fail_closed(self):
        invalid = [None, message(20, chat_id=8), message(True), message(20, date=True),
                   message(20, edit_date=-1), message(40), message(0)]
        for bad in invalid:
            p = SearchProvider({40: message(40)}); p.pages = [page([message(40), bad])]
            out = self.reader(p).read(self.request())
            self.assertEqual(out.stop_reason, 'invalid_provider_page')
            self.assertEqual(out.results, [])
            self.assertFalse(any(x[0] in ('message', 'sender') for x in p.calls))
        for changes in ({'@type': 'messages'}, {'next_from_message_id': True}, {'next_from_message_id': -1},
                        {'total_count': True}, {'messages': [message(i) for i in range(30, 0, -1)]}):
            p = SearchProvider({}); p.pages = [{**page([]), **changes}]
            self.assertEqual(self.reader(p).read(self.request()).stop_reason, 'invalid_provider_page')

    def test_hydrated_wrong_chat_id_cannot_disclose_body(self):
        for changes in ({'chat_id': 8}, {'id': 41}, {'date': True}):
            p = SearchProvider({40: message(40)}); p.hydration[40] = message(40, **changes)
            out = self.reader(p).read(self.request())
            self.assertEqual(out.results, [])
            self.assertEqual(out.stop_reason, 'invalid_provider_page')

    def test_half_open_dates_frozen_at_start_and_nonmonotone_dates_do_not_end_scan(self):
        p = SearchProvider({40: message(40, date=1700000000), 30: message(30, date=1699999999),
            20: message(20, date=1700000002), 10: message(10, date=1700000001), 1: message(1, date=0)})
        out = self.reader(p).read(self.request(mode='interval', date_from='2023-11-14T22:13:20Z',
            date_to='2023-11-14T22:13:22Z'))
        self.assertEqual(self.ids(out), [40, 10])

    def test_scope_budget_counts_duplicate_rows_and_empty_provider_attempts(self):
        p = SearchProvider({i: message(i) for i in range(1, 230)})
        r = self.reader(p); cursor = None; result_ids = []
        for _ in range(20):
            out = r.read(self.request(limit=1, cursor=cursor)); result_ids += self.ids(out); cursor = out.next_cursor
            if not cursor: break
        # Tiny public pages must consume one native buffer before issuing another search.
        self.assertEqual(len([x for x in p.calls if x[0] == 'search']), 1)
        for _ in range(210):
            if not cursor: break
            out = r.read(self.request(limit=1, cursor=cursor)); result_ids += self.ids(out); cursor = out.next_cursor
        self.assertEqual((out.scanned_candidates, out.processed_candidates, out.provider_pages), (200, 200, 10))
        self.assertEqual(out.stop_reason, 'scope_budget_exhausted')
        self.assertEqual(len(set(result_ids)), 191)
        p = SearchProvider({}); p.pages = [page([], x) for x in range(100, 0, -10)]
        out = self.reader(p).read(self.request())
        self.assertEqual((out.provider_pages, out.scanned_candidates), (10, 0))
        self.assertEqual(out.stop_reason, 'scope_budget_exhausted')

    def test_fixed_expiry_binding_capacity_account_and_close(self):
        p = SearchProvider({i: message(i) for i in (40, 30, 20)})
        r = self.reader(p); a = r.read(self.request(limit=1))
        self.assertEqual(r.read(self.request(limit=1, query='changed', cursor=a.next_cursor)).status, 'invalid_cursor')
        self.tick[0] += 200
        b = r.read(self.request(limit=1, cursor=a.next_cursor))
        self.tick[0] += 100
        self.assertEqual(r.read(self.request(limit=1, cursor=b.next_cursor)).status, 'invalid_cursor')
        r = self.reader(p); a = r.read(self.request(limit=1)); p.account_id = 18
        self.assertEqual(r.read(self.request(limit=1, cursor=a.next_cursor)).status, 'invalid_cursor')
        r = self.reader(p)
        for _ in range(4): self.assertIsNotNone(r.read(self.request(limit=1)).next_cursor)
        self.assertEqual(r.read(self.request(limit=1)).status, 'capacity_exhausted')
        r.close(); self.assertEqual(r.read(self.request()).status, 'invalid_cursor')

    def test_deadline_after_unavailable_and_authorization_during_sender_fail_closed(self):
        p = SearchProvider({40: message(40)})
        def vanished(*args):
            p.time[0] = 500.0
            raise MessageNotFound('private')
        p.time = [100.0]; p.get_message = vanished
        with patch.object(self.mod.time, 'monotonic', side_effect=lambda: p.time[0]):
            out = self.reader(p).read(self.request())
        self.assertEqual(out.results, [])
        self.assertEqual(out.stop_reason, 'call_budget_exhausted')
        p = SearchProvider({40: message(40)})
        p.get_sender_identity = lambda *args: (_ for _ in ()).throw(AuthorizationBlocked('private'))
        out = self.reader(p).read(self.request())
        self.assertEqual(out.status, 'blocked'); self.assertEqual(out.results, [])

    def test_output_text_is_bounded_and_unsupported_has_no_match_claim(self):
        values = {i: message(i) for i in range(1, 21)}
        for raw in values.values(): raw['content']['text']['text'] = 'x' * 30000
        out = self.reader(SearchProvider(values)).read(self.request())
        self.assertLessEqual(sum(len(x.message.text.value) for x in out.results if x.message and x.message.text), 100000)
        self.assertFalse(out.page_complete)
        for raw in (message(1, content={'@type': 'messageUnsupported'}), message(1)):
            if raw['content']['@type'] == 'messageText': raw['content']['text']['text'] = 'x' * 65537
            out = self.reader(SearchProvider({1: raw})).read(self.request())
            self.assertEqual(out.results[0].status, 'unsupported')
            self.assertIsNone(out.results[0].message)
