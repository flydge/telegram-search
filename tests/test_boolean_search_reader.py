from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from telegram_search_mcp import schemas
from telegram_search_mcp.exact_search_reader import ExactSearchReader
from telegram_search_mcp.tdjson import MessageNotFound, TDLibDeadlineExceeded
from test_exact_search_reader import page
from test_history_reader import HistoryProvider
from test_message_reader import message


def text(mid, body='red blue', **changes):
    return message(mid, content={'@type': 'messageText', 'text': {'text': body, 'entities': []}}, **changes)


class BooleanProvider(HistoryProvider):
    def __init__(self, messages, branches):
        super().__init__(messages)
        self.branches = copy.deepcopy(branches)

    def search_chat_messages(self, chat_id, query, *, from_message_id, limit, **filters):
        self.calls.append(('search', chat_id, query, from_message_id, limit, filters))
        value = self.branches[query].pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def get_message(self, chat_id, message_id):
        self.calls.append(('hydrate', message_id))
        return super().get_message(chat_id, message_id)


class BooleanReaderTests(unittest.TestCase):
    def reader(self, provider):
        return ExactSearchReader(client=provider, client_id='synthetic_client', broker_generation='synthetic_broker',
            now=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc))

    def request(self, query=None, **changes):
        return schemas.SearchMessagesRequest(target=7, mode='latest', query=query or {'any': ['red', 'blue']}, **changes)

    def ids(self, result):
        return [r.anchor.message_id for r in result.results]

    def assert_coverage(self, result):
        self.assertEqual(result.scanned_candidates, sum(b.scanned_candidates for b in result.branch_coverage))
        self.assertEqual(result.processed_candidates, sum(b.processed_candidates for b in result.branch_coverage))
        self.assertEqual(result.provider_pages, sum(b.provider_pages for b in result.branch_coverage))
        self.assertFalse(result.scope_complete)
        self.assertIsNone(result.has_more)

    def test_local_same_message_all_and_none_full_unicode_caption(self):
        values = {40: text(40, 'ＲＥＤ\tBlue'), 30: text(30, 'red'), 20: text(20, 'blue'),
                  10: text(10, 'red blue banned'), 5: message(5, content={'@type': 'messagePhoto',
                  'photo': {'sizes': []}, 'caption': {'text': 'RED blue', 'entities': []}})}
        p = BooleanProvider(values, {'red': [page(list(values.values()))]})
        out = self.reader(p).read(self.request({'all': ['red', 'blue'], 'none': ['banned']}))
        self.assertEqual(self.ids(out), [40, 5])
        self.assertEqual([r.message.text_role for r in out.results], ['text', 'caption'])
        self.assertEqual(out.scope.matching_semantics, 'local_nfkc_casefold_whitespace_substring')
        self.assertEqual([c[2] for c in p.calls if c[0] == 'search'], ['red'])
        self.assertEqual(len([c for c in p.calls if c[0] == 'hydrate']), 5)
        self.assert_coverage(out)

    def test_kway_merge_groups_duplicates_and_primes_before_hydration(self):
        values = {i: text(i) for i in (50, 40, 30, 20)}
        p = BooleanProvider(values, {'red': [page([values[40], values[20]])],
                                    'blue': [page([], 60), page([values[50], values[40], values[30]])]})
        r = self.reader(p)
        first = r.read(self.request(limit=2))
        self.assertEqual(self.ids(first), [50, 40])
        self.assertEqual(first.scope.upper_message_id, 50)
        second = r.read(self.request(limit=2, cursor=first.next_cursor))
        self.assertEqual(self.ids(second), [30, 20])
        self.assertEqual(first.scope, second.scope)
        self.assertEqual([c for c in p.calls if c[0] == 'hydrate'], [('hydrate', 50), ('hydrate', 40), ('hydrate', 30), ('hydrate', 20)])
        self.assertEqual([c[0] for c in p.calls if c[0] in ('search', 'hydrate')][:4], ['search', 'search', 'search', 'hydrate'])
        self.assertEqual((second.scanned_candidates, second.processed_candidates), (5, 5))
        self.assertEqual([b.state for b in second.branch_coverage], ['provider_end_unverified'] * 2)
        self.assert_coverage(second)

    def test_same_id_conflicts_and_exclusion_races_suppress_content(self):
        a, b = text(40), text(40, 'red blue changed')
        p = BooleanProvider({40: a}, {'red': [page([a])], 'blue': [page([b])]})
        out = self.reader(p).read(self.request())
        self.assertEqual(out.results[0].status, 'evidence_changed')
        self.assertIsNone(out.results[0].message)
        for before, after, expected in [('red blue', 'red blue banned', 'evidence_changed'),
                                        ('red blue banned', 'red blue', 'evidence_changed'),
                                        ('red banned', 'blue banned', None)]:
            p = BooleanProvider({40: text(40, before)}, {'red': [page([text(40, before)])]})
            p.hydration[40] = text(40, after)
            out = self.reader(p).read(self.request({'all': ['red'], 'none': ['banned']}))
            self.assertEqual([r.status for r in out.results], [] if expected is None else [expected])
            self.assertTrue(all(r.message is None for r in out.results))

    def test_full_text_exclusion_before_projection_and_unsupported_is_contentless(self):
        values = {40: text(40, 'red ' + 'x' * 24000 + ' banned'),
                  30: text(30, 'x' * 24000 + ' red'), 20: text(20, 'red ' + 'x' * 65536),
                  10: message(10, content={'@type': 'messageUnsupported'})}
        p = BooleanProvider(values, {'red': [page(list(values.values()))]})
        out = self.reader(p).read(self.request({'all': ['red'], 'none': ['banned']}))
        self.assertEqual(self.ids(out), [30, 20, 10])
        self.assertEqual([r.status for r in out.results], ['partial', 'unsupported', 'unsupported'])
        self.assertTrue(out.results[0].message.text.truncated)
        self.assertTrue(all(r.message is None for r in out.results[1:]))

    def test_unavailable_only_previously_matching_in_window(self):
        values = {40: text(40), 30: text(30, 'not relevant'), 20: text(20, date=0)}
        p = BooleanProvider(values, {'red': [page(list(values.values()))]})
        p.hydration = {i: MessageNotFound('private') for i in values}
        out = self.reader(p).read(self.request({'all': ['red']}))
        self.assertEqual(self.ids(out), [40])
        self.assertEqual(out.results[0].status, 'not_found')
        self.assertNotIn('private', out.model_dump_json())

    def test_all_branches_validate_before_body_and_incomplete_priming_has_no_head(self):
        p = BooleanProvider({40: text(40)}, {'red': [page([text(40)])], 'blue': [page([text(30, chat_id=8)])]})
        out = self.reader(p).read(self.request())
        self.assertEqual(out.stop_reason, 'invalid_provider_page')
        self.assertEqual(out.results, [])
        self.assertIsNone(out.scope.upper_message_id)
        self.assertFalse(any(c[0] == 'hydrate' for c in p.calls))
        p = BooleanProvider({40: text(40)}, {'red': [page([text(40)])],
            'blue': [page([], i) for i in range(100, 0, -10)]})
        out = self.reader(p).read(self.request())
        self.assertEqual(out.provider_pages, 10)
        self.assertEqual(out.stop_reason, 'scope_budget_exhausted')
        self.assertEqual(out.results, [])
        self.assertIsNone(out.scope.upper_message_id)
        self.assertIsNone(out.next_cursor)
        self.assertEqual(out.branch_coverage[1].state, 'scope_budget_exhausted')
        self.assert_coverage(out)

    def test_duplicate_refills_hydrate_and_future_heads_never_emit(self):
        a, b, c = text(50), text(40), text(30)
        p = BooleanProvider({50: a, 40: b, 30: c, 100: text(100)},
            {'red': [page([a, b], 40), page([text(100), b, c])], 'blue': [page([])]})
        r = self.reader(p)
        first = r.read(self.request(limit=1))
        second = r.read(self.request(limit=1, cursor=first.next_cursor))
        third = r.read(self.request(limit=1, cursor=second.next_cursor))
        self.assertEqual(self.ids(first) + self.ids(second) + self.ids(third), [50, 40, 30])
        self.assertEqual([c[1] for c in p.calls if c[0] == 'hydrate'], [50, 40, 100, 40, 30])
        self.assertEqual((third.scanned_candidates, third.processed_candidates), (5, 5))

    def test_query_scope_copy_cursor_binding_and_string_compatibility(self):
        values = {i: text(i) for i in (40, 30)}
        p = BooleanProvider(values, {'red': [page(list(values.values()))]})
        r = self.reader(p); req = self.request({'all': ['red']}, limit=1)
        out = r.read(req)
        req.query.all.append('changed')
        out.scope.query.all.append('also changed')
        self.assertNotIn('changed', repr(r._scans))
        self.assertNotIn('red blue', repr(r._scans))
        bad = r.read(self.request({'all': ['blue']}, limit=1, cursor=out.next_cursor))
        self.assertEqual(bad.status, 'invalid_cursor')
        good = r.read(self.request({'all': ['red']}, limit=1, cursor=out.next_cursor))
        self.assertEqual(self.ids(good), [30])
        self.assertEqual(r.read(self.request({'all': ['red']}, limit=1, cursor=out.next_cursor)).status, 'invalid_cursor')
        p = BooleanProvider({40: text(40, 'provider stemming')}, {'unmatched literal': [page([text(40, 'provider stemming')])]})
        old = self.reader(p).read(self.request('unmatched literal'))
        self.assertEqual(old.results[0].status, 'match')
        self.assertEqual(old.branch_coverage, [])

    def test_deadline_interrupts_active_branches_and_never_extends_thirty_seconds(self):
        p = BooleanProvider({40: text(40)}, {'red': [page([text(40)], 30)], 'blue': [TDLibDeadlineExceeded('private')]})
        out = self.reader(p).read(self.request())
        self.assertEqual(out.results, [])
        self.assertEqual(out.stop_reason, 'call_budget_exhausted')
        self.assertEqual([b.state for b in out.branch_coverage], ['interrupted', 'interrupted'])
        self.assertIsNone(out.next_cursor)
        self.assertIsNone(out.scope.upper_message_id)

    def test_shared_observation_budget_and_terminal_nonprogress_never_refetch(self):
        values = {i: text(i, 'nothing') for i in range(250, 0, -1)}
        red = [page([values[j] for j in range(i, i-20, -1)], i-20) for i in range(250, 50, -20)]
        p = BooleanProvider(values, {'red': red, 'blue': [page([])]})
        r = self.reader(p); out = r.read(self.request())
        self.assertEqual(out.stop_reason, 'call_budget_exhausted')
        self.assertEqual(out.processed_candidates, 100)
        self.assertIsNotNone(out.next_cursor)
        end = r.read(self.request(cursor=out.next_cursor))
        self.assertEqual(end.stop_reason, 'scope_budget_exhausted')
        self.assertEqual((end.scanned_candidates, end.processed_candidates, end.provider_pages), (180, 180, 10))
        self.assertEqual(end.branch_coverage[0].state, 'scope_budget_exhausted')
        self.assert_coverage(end)
        p = BooleanProvider({40: text(40), 30: text(30)}, {'red': [page([text(40)], 40), page([text(30)], 40)], 'blue': [page([])]})
        out = self.reader(p).read(self.request())
        self.assertEqual(self.ids(out), [40, 30])
        self.assertEqual(out.branch_coverage[0].state, 'provider_nonprogress')
        self.assertEqual(out.stop_reason, 'provider_nonprogress')

    def test_same_id_group_stays_pending_when_call_budget_has_one_slot(self):
        values = [text(i, 'irrelevant') for i in range(149, 50, -1)] + [text(50)]
        pages = [page(values[i:i+20], values[i+19]['id'] if i < 80 else 0) for i in range(0, 100, 20)]
        p = BooleanProvider({m['id']: m for m in values}, {'red': pages, 'blue': [page([values[-1]])]})
        r = self.reader(p); first = r.read(self.request())
        self.assertEqual(first.results, [])
        self.assertEqual(first.processed_candidates, 99)
        self.assertEqual(first.stop_reason, 'call_budget_exhausted')
        self.assertIsNotNone(first.next_cursor)
        self.assertEqual([b.scanned_candidates - b.processed_candidates for b in first.branch_coverage], [1, 1])
        self.assertFalse(any(c == ('hydrate', 50) for c in p.calls))
        second = r.read(self.request(cursor=first.next_cursor))
        self.assertEqual(self.ids(second), [50])
        self.assertEqual(second.processed_candidates, 101)
        self.assertEqual([c for c in p.calls if c == ('hydrate', 50)], [('hydrate', 50)])
        self.assert_coverage(second)

    def test_typed_membership_always_validated_even_after_local_mismatch(self):
        invalid = text(30, 'irrelevant', is_outgoing='false')
        p = BooleanProvider({40: text(40)}, {'red': [page([text(40), invalid])]})
        out = self.reader(p).read(self.request({'all': ['red']}, direction='outgoing'))
        self.assertEqual(out.results, [])
        self.assertEqual(out.stop_reason, 'invalid_provider_page')
        self.assertFalse(any(c[0] == 'hydrate' for c in p.calls))
        a = text(40)
        p = BooleanProvider({40: a}, {'red': [page([a])]})
        p.hydration[40] = text(40, is_outgoing=False)
        out = self.reader(p).read(self.request({'all': ['red']}, direction='outgoing'))
        self.assertEqual([r.status for r in out.results], ['evidence_changed'])
        self.assertIsNone(out.results[0].message)

    def test_output_budget_and_account_reverification_keep_scope_bounded(self):
        values = {i: text(i, 'red ' + 'x' * 30000) for i in range(10, 0, -1)}
        p = BooleanProvider(values, {'red': [page(list(values.values()))]})
        r = self.reader(p); first = r.read(self.request({'all': ['red']}))
        self.assertEqual(sum(len(x.message.text.value) for x in first.results), 100000)
        self.assertEqual(first.stop_reason, 'call_budget_exhausted')
        self.assertIsNotNone(first.next_cursor)
        self.assertTrue(all(x.status == 'partial' for x in first.results))
        p.account_id = 18
        second = r.read(self.request({'all': ['red']}, cursor=first.next_cursor))
        self.assertEqual(second.status, 'invalid_cursor')
        self.assertEqual(second.results, [])

    def test_frozen_dates_use_current_date_and_do_not_assume_monotonic_timestamps(self):
        values = {50: text(50, date=1700000002), 40: text(40, date=1700000000),
                  30: text(30, date=1699999999), 20: text(20, date=1700000001)}
        p = BooleanProvider(values, {'red': [page(list(values.values()))]})
        request = schemas.SearchMessagesRequest(target=7, mode='interval', query={'all': ['red']},
            date_from='2023-11-14T22:13:20Z', date_to='2023-11-14T22:13:22Z')
        out = self.reader(p).read(request)
        self.assertEqual(self.ids(out), [40, 20])
        self.assertEqual(out.scope.upper_message_id, 50)


if __name__ == '__main__':
    unittest.main()
