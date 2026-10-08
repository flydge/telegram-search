from __future__ import annotations

import copy
import importlib
import time
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from telegram_search_mcp import schemas
from telegram_search_mcp.tdjson import AuthorizationBlocked, MessageNotFound, TDLibError
from test_message_reader import Provider, message


def reply(mid, chat_id=7):
    return {'@type': 'messageReplyToMessage', 'chat_id': chat_id, 'message_id': mid,
            'quote': {'text': {'text': 'UNTRUSTED EMBEDDED QUOTE'}},
            'content': {'@type': 'messageText', 'text': {'text': 'STALE EMBEDDED BODY'}}}


class ReplyChainTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(schemas, 'ReadReplyChainRequest'), 'F4 strict reply-chain contract is missing')
        self.reader = importlib.import_module('telegram_search_mcp.reply_reader')

    def read(self, provider, depth=10, **kwargs):
        return self.reader.read_reply_chain(provider,
            schemas.ReadReplyChainRequest(anchor={'chat_id': 7, 'message_id': 11}, max_depth=depth), **kwargs)

    def test_hydrates_each_parent_in_order_and_never_uses_embedded_body(self):
        provider = Provider({11: message(reply_to=reply(9)), 9: message(9, reply_to=reply(3)), 3: message(3)})
        result = self.read(provider)
        self.assertEqual([r.anchor.message_id for r in result.results], [11, 9, 3])
        self.assertEqual(result.stop_reason, 'no_parent')
        self.assertEqual(result.status, 'complete')
        self.assertTrue(result.chain_complete)
        self.assertTrue(result.coverage_complete)
        self.assertNotIn('EMBEDDED', result.model_dump_json())
        self.assertEqual([c for c in provider.calls if c[0] == 'message'], [('message', 7, 11), ('message', 7, 9), ('message', 7, 3)])

    def test_cross_chat_story_and_unknown_reply_do_not_fetch_outside_scope(self):
        for pointer, reason in ((reply(9, 99), 'cross_chat'), (reply(0, 0), 'reply_unavailable'),
                ({'@type': 'messageReplyToStory', 'story_poster_chat_id': 99, 'story_id': 5}, 'story_reply'),
                ({'@type': 'futureReply'}, 'reply_unavailable')):
            with self.subTest(reason=reason):
                provider = Provider({11: message(reply_to=pointer)})
                result = self.read(provider)
                self.assertEqual(result.stop_reason, reason)
                self.assertFalse(result.chain_complete)
                self.assertFalse(result.coverage_complete)
                self.assertEqual([c for c in provider.calls if c[0] == 'message'], [('message', 7, 11)])

    def test_cycle_and_depth_limit_never_fetch_an_extra_node(self):
        provider = Provider({11: message(reply_to=reply(9)), 9: message(9, reply_to=reply(11))})
        result = self.read(provider)
        self.assertEqual(result.stop_reason, 'cycle')
        self.assertEqual(len(result.results), 2)
        self.assertEqual(len([c for c in provider.calls if c[0] == 'message']), 2)
        result = self.read(Provider({11: message(reply_to=reply(9))}), depth=1)
        self.assertEqual(result.stop_reason, 'depth_limit')
        self.assertFalse(result.chain_complete)
        self.assertEqual(self.read(Provider({11: message()}), depth=1).stop_reason, 'no_parent')

    def test_missing_wrong_chat_wrong_id_and_unsupported_parent_have_no_fallback(self):
        for parent, reason, status in ((MessageNotFound('PRIVATE'), 'message_unavailable', 'not_found'),
                (message(9, chat_id=99), 'invalid_provider_message', 'wrong_chat'),
                (message(8), 'invalid_provider_message', 'error'),
                (message(9, content={'@type': 'messagePoll', 'text': 'PRIVATE'}), 'unsupported_content', 'unsupported'),
                (TDLibError('PRIVATE'), 'provider_error', 'error')):
            with self.subTest(reason=reason, status=status):
                result = self.read(Provider({11: message(reply_to=reply(9)), 9: parent}))
                self.assertEqual(result.stop_reason, reason)
                self.assertEqual(result.results[-1].status, status)
                self.assertIsNone(result.results[-1].message)
                self.assertFalse(result.coverage_complete)
                self.assertNotIn('PRIVATE', result.model_dump_json())
                self.assertNotIn('EMBEDDED', result.model_dump_json())

    def test_budget_is_aggregate_and_expired_call_does_not_touch_provider(self):
        provider = Provider({i: message(i, reply_to=reply(i-1), content={'@type': 'messageText', 'text': {'text': 'x'*20000}}) for i in range(1, 12)})
        result = self.read(provider)
        self.assertEqual(result.stop_reason, 'budget_exhausted')
        self.assertEqual(sum(len(r.message.text.value) for r in result.results), 100000)
        self.assertEqual(len(result.results), 5)
        expired = Provider({})
        result = self.read(expired, deadline=time.monotonic()-1)
        self.assertEqual(result.stop_reason, 'budget_exhausted')
        self.assertEqual(expired.calls, [])

    def test_last_sender_deadline_does_not_report_complete_chain(self):
        provider = Provider({11: message()})
        original = provider.get_sender_identity
        clock = [1.0]
        def slow(sender):
            clock[0] = 50.0
            return original(sender)
        provider.get_sender_identity = slow
        with patch.object(self.reader.time, 'monotonic', side_effect=lambda: clock[0]):
            result = self.read(provider)
        self.assertEqual(result.stop_reason, 'budget_exhausted')
        self.assertFalse(result.chain_complete)

    def test_partial_text_can_finish_observed_chain_but_not_content_coverage(self):
        result = self.read(Provider({11: message(content={'@type':'messageText','text':{'text':'x'*20001}})}))
        self.assertTrue(result.chain_complete)
        self.assertFalse(result.coverage_complete)
        self.assertEqual(result.status, 'partial')

    def test_malformed_chat_type_stops_before_message_access(self):
        for kind in (None, [], ['bad'], 'bad', {}, {'@type': 'futureChat'}):
            with self.subTest(kind=kind):
                provider = Provider({11: message()})
                provider.resolve_target = lambda target: {'@type': 'chat', 'id': target, 'type': kind}
                result = self.read(provider)
                self.assertEqual(result.stop_reason, 'invalid_provider_message')
                self.assertFalse(result.coverage_complete)
                self.assertFalse(any(c[0] == 'message' for c in provider.calls))

    def test_native_chat_adapter_rejects_malformed_type_before_hydration(self):
        from telegram_search_mcp.tdjson import TDLibClient
        from test_tdjson import ScriptedRaw
        for kind in (['bad'], 'bad', None, {}):
            with self.subTest(kind=kind):
                raw = ScriptedRaw({'getAuthorizationState': [{'@type':'authorizationStateReady'}],
                    'getChat': [{'@type':'chat', 'id':7, 'type':kind}]})
                result = self.read(TDLibClient(raw=raw))
                self.assertFalse(result.coverage_complete)
                self.assertEqual(result.status, 'error')
                self.assertNotIn('getMessage', [r['@type'] for r in raw.sent])

    def test_malformed_sender_identity_preserves_chain_with_incomplete_metadata(self):
        for identity in (None, [], 'bad', {}):
            with self.subTest(identity=identity):
                provider = Provider({11: message(reply_to=reply(9)), 9: message(9)})
                provider.get_sender_identity = lambda sender: identity
                result = self.read(provider)
                self.assertEqual([r.anchor.message_id for r in result.results], [11, 9])
                self.assertTrue(result.chain_complete)
                self.assertFalse(result.coverage_complete)
                self.assertTrue(all('sender_unavailable' in r.issues for r in result.results))

    def test_auth_secret_and_intermediate_deadline_never_continue(self):
        provider = Provider({})
        def blocked(): raise AuthorizationBlocked('PRIVATE')
        provider.ensure_ready = blocked
        self.assertEqual(self.read(provider).status, 'blocked')
        self.assertEqual(provider.calls, [])
        provider.resolve_target = lambda target: {'@type': 'chat', 'id': target, 'type': {'@type': 'chatTypeSecret'}}
        provider.ensure_ready = lambda: None
        self.assertEqual(self.read(provider).stop_reason, 'secret_chat')
        self.assertEqual(provider.calls, [])
        for stage in ('ready', 'chat', 'parent'):
            provider = Provider({11: message(reply_to=reply(9)), 9: message(9)})
            clock = [1.0]
            if stage == 'ready':
                provider.ensure_ready = lambda: clock.__setitem__(0, 50.0)
            elif stage == 'chat':
                original = provider.resolve_target
                def resolve(target): clock[0] = 50.0; return original(target)
                provider.resolve_target = resolve
            else:
                original = provider.get_message
                def fetch(chat, mid):
                    if mid == 9: clock[0] = 50.0
                    return original(chat, mid)
                provider.get_message = fetch
            with patch.object(self.reader.time, 'monotonic', side_effect=lambda: clock[0]):
                result = self.read(provider)
            self.assertEqual(result.stop_reason, 'budget_exhausted')
            self.assertFalse(result.chain_complete)
            if stage == 'parent':
                self.assertEqual(result.results[0].status, 'complete')
                self.assertEqual(result.results[-1].anchor.message_id, 9)
            else: self.assertFalse(any(c[0] == 'message' for c in provider.calls))

    def test_tenth_node_is_last_fetch_even_with_eleventh_parent(self):
        provider = Provider({i: message(i, reply_to=reply(i-1)) for i in range(2, 12)})
        result = self.read(provider)
        self.assertEqual(result.stop_reason, 'depth_limit')
        self.assertEqual([r.anchor.message_id for r in result.results], list(range(11, 1, -1)))
        self.assertEqual(len([c for c in provider.calls if c[0] == 'message']), 10)

    def test_strict_inputs_and_chain_response_reject_widening_forgery(self):
        valid = {'anchor': {'chat_id': 7, 'message_id': 11}}
        for extra in ({'max_depth': 0}, {'max_depth': 11}, {'max_depth': True}, {'max_depth': '2'}, {'target': '*'},
                {'anchor': {'chat_id': True, 'message_id': 11}}, {'anchor': {'chat_id': 7, 'message_id': -1}}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                schemas.ReadReplyChainRequest(**(valid | extra))
        response = self.read(Provider({11: message(reply_to=reply(9)), 9: message(9)})).model_dump(mode='json')
        for change in ('swap', 'root', 'depth', 'complete', 'link'):
            wrong = copy.deepcopy(response)
            if change == 'swap': wrong['results'].reverse()
            elif change == 'root': wrong['anchor']['message_id'] = 12
            elif change == 'depth': wrong['max_depth'] = 1
            elif change == 'complete': wrong['chain_complete'] = False
            else: wrong['results'][0]['message']['reply_to']['message_id'] = 8
            with self.subTest(change=change), self.assertRaises(ValidationError):
                schemas.ReadReplyChainResponse.model_validate(wrong)
