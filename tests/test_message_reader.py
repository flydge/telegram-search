from __future__ import annotations

import copy
import importlib
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from pydantic import ValidationError
from telegram_search_mcp import schemas
from telegram_search_mcp.tdjson import MessageNotFound, TDLibError


def message(message_id=11, **changes):
    value = {
        '@type': 'message', 'id': message_id, 'chat_id': 7,
        'sender_id': {'@type': 'messageSenderUser', 'user_id': 17},
        'is_outgoing': True, 'date': 1700000000, 'edit_date': 0,
        'reply_to': None, 'topic_id': None, 'media_album_id': '0',
        'content': {'@type': 'messageText', 'text': {'@type': 'formattedText', 'text': 'Full selected text', 'entities': []}},
    }
    value.update(changes)
    return value


class Provider:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []
        self.deadline = None

    @contextmanager
    def request_budget(self, deadline):
        self.deadline = deadline
        yield

    def ensure_ready(self):
        self.calls.append(('ready',))

    def resolve_target(self, chat_id):
        self.calls.append(('chat', chat_id))
        return {'@type': 'chat', 'id': chat_id, 'type': {'@type': 'chatTypePrivate'}, 'title': 'Synthetic chat'}

    def get_message(self, chat_id, message_id):
        self.calls.append(('message', chat_id, message_id))
        result = self.messages[message_id]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def get_sender_identity(self, sender):
        self.calls.append(('sender', sender['@type']))
        return {'kind': 'user', 'id': 17, 'display_name': 'Synthetic Reader'}


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(schemas, 'ReadMessagesRequest'), 'F2 strict read request is missing')
        self.reader = importlib.import_module('telegram_search_mcp.message_reader')

    def read(self, provider, ids=(11,)):
        req = schemas.ReadMessagesRequest(anchors=[{'chat_id': 7, 'message_id': i} for i in ids])
        return self.reader.read_messages(provider, req)

    def test_full_selected_text_and_sender_direction_exact_source(self):
        provider = Provider({11: message()})
        response = self.read(provider)
        self.assertEqual(response.status, 'complete')
        item = response.results[0]
        self.assertEqual(item.status, 'complete')
        self.assertTrue(item.coverage_complete)
        self.assertEqual(item.message.text.value, 'Full selected text')
        self.assertTrue(item.message.text.untrusted)
        self.assertFalse(item.message.text.sanitized)
        self.assertFalse(item.message.text.truncated)
        self.assertEqual(item.message.sender.id, 17)
        self.assertEqual(item.message.sender.kind, 'user')
        self.assertTrue(item.message.is_outgoing)
        self.assertFalse(item.message.is_edited)
        self.assertEqual(item.message.source.evidence_anchor.model_dump(), {'chat_id': 7, 'message_id': 11})
        self.assertEqual(provider.calls, [('ready',), ('chat', 7), ('message', 7, 11), ('sender', 'messageSenderUser')])
        self.assertIsNotNone(provider.deadline)

    def test_partial_batch_preserves_each_anchor_and_does_not_leak_wrong_chat(self):
        provider = Provider({11: message(), 12: MessageNotFound('private provider detail'),
                             13: message(13, chat_id=999),
                             14: message(14, content={'@type': 'messagePoll', 'poll': {'question': 'not exported'}})})
        response = self.read(provider, (11, 12, 13, 14))
        self.assertEqual(response.status, 'partial')
        self.assertFalse(response.coverage_complete)
        self.assertEqual([r.anchor.message_id for r in response.results], [11, 12, 13, 14])
        self.assertEqual([r.status for r in response.results], ['complete', 'not_found', 'wrong_chat', 'unsupported'])
        self.assertIsNone(response.results[2].message)
        self.assertNotIn('999', response.model_dump_json())
        self.assertNotIn('private provider detail', response.model_dump_json())
        self.assertNotIn('not exported', response.model_dump_json())

    def test_normalization_and_truncation_are_separately_reported(self):
        raw = 'Ａ\u202e  text\nnext'
        provider = Provider({11: message(content={'@type':'messageText','text':{'text':raw}}),
                             12: message(12, content={'@type':'messageDocument','caption':{'text':'😀' * 20001}})})
        result = self.read(provider, (11, 12))
        first, second = result.results
        self.assertEqual(first.message.text.value, 'A text next')
        self.assertTrue(first.message.text.sanitized)
        self.assertFalse(first.message.text.truncated)
        self.assertEqual(second.message.content_kind, 'document')
        self.assertEqual(second.message.text_role, 'caption')
        self.assertEqual(len(second.message.text.value), 20000)
        self.assertTrue(second.message.text.truncated)
        self.assertFalse(second.message.text.sanitized)
        self.assertEqual(second.status, 'partial')

    def test_missing_metadata_and_provider_errors_cannot_be_complete(self):
        malformed = message(); malformed.pop('is_outgoing')
        provider = Provider({11: malformed, 12: TDLibError('provider private payload')})
        result = self.read(provider, (11, 12))
        self.assertFalse(result.coverage_complete)
        self.assertNotEqual(result.results[0].status, 'complete')
        self.assertEqual(result.results[1].status, 'error')
        self.assertNotIn('provider private payload', result.model_dump_json())

    def test_strict_batch_rejects_empty_oversized_duplicate_and_wrong_inputs(self):
        bad = [[], [{'chat_id':7,'message_id':i+1} for i in range(21)],
               [{'chat_id':7,'message_id':11}]*2, [{'chat_id':True,'message_id':11}],
               [{'chat_id':7,'message_id':0}], [{'chat_id':7,'message_id':-1}],
               [{'chat_id':7,'message_id':11,'account':'*'}], [{'chat_id':'7','message_id':11}]]
        for anchors in bad:
            with self.subTest(anchors=anchors), self.assertRaises(ValidationError):
                schemas.ReadMessagesRequest(anchors=anchors)

    def test_pending_send_and_ephemeral_replacement_never_export_regular_content(self):
        provider = Provider({11: message(sending_state={'@type':'messageSendingStatePending','sending_id':0}),
                             12: message(12, ephemeral_content={'@type':'messageContentWithNewFormatting',
                                         'content':{'@type':'messageText','text':{'text':'visible replacement'}}})})
        result = self.read(provider, (11,12))
        self.assertEqual([r.status for r in result.results], ['unsupported','unsupported'])
        self.assertTrue(all(r.message is None for r in result.results))
        self.assertNotIn('Full selected text',result.model_dump_json())

    def test_native_json_omits_nullable_fields_and_signed_topics_remain_exact(self):
        plain = message(); plain.pop('reply_to'); plain.pop('topic_id')
        provider = Provider({11: plain, 12: message(12, topic_id={'@type':'messageTopicDirectMessages', 'direct_messages_chat_topic_id':-123})})
        result = self.read(provider, (11,12))
        self.assertEqual(result.status, 'complete')
        self.assertIsNone(result.results[0].message.reply_to)
        self.assertIsNone(result.results[0].message.topic)
        self.assertEqual(result.results[1].message.topic.id, -123)

    def test_unknown_reply_and_scheduled_date_do_not_invent_exact_values(self):
        provider = Provider({11: message(reply_to={'@type':'messageReplyToMessage','chat_id':0,'message_id':0}, date=0)})
        result = self.read(provider)
        self.assertEqual(result.results[0].status, 'partial')
        self.assertIsNone(result.results[0].message.reply_to.chat_id)
        self.assertIsNone(result.results[0].message.date_utc)

    def test_total_text_budget_keeps_results_for_unread_anchors(self):
        provider = Provider({i: message(i, content={'@type':'messageText','text':{'text':'x'*20000}}) for i in range(1,8)})
        result = self.read(provider, tuple(range(1,8)))
        self.assertEqual(len(result.results), 7)
        self.assertLessEqual(sum(len(r.message.text.value) for r in result.results if r.message and r.message.text), 100000)
        self.assertFalse(result.coverage_complete)
        self.assertEqual(result.results[-1].status, 'partial')


if __name__ == '__main__':
    unittest.main()

class ProviderIdentityTests(unittest.TestCase):
    def test_sender_hydration_verifies_identity_and_emits_exact_request(self):
        from telegram_search_mcp.tdjson import TDLibClient
        from test_tdjson import ScriptedRaw
        self.assertTrue(hasattr(TDLibClient, 'get_sender_identity'), 'verified sender hydration is missing')
        raw = ScriptedRaw({'getAuthorizationState': [{'@type':'authorizationStateReady'}], 'getUser': [{'@type':'user','id':17,'first_name':'Synthetic','last_name':'Reader'}]})
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        self.assertEqual(client.get_sender_identity({'@type':'messageSenderUser','user_id':17}),
                         {'kind':'user','id':17,'display_name':'Synthetic Reader'})
        self.assertEqual({k:v for k,v in raw.sent[1].items() if k != '@extra'}, {'@type':'getUser','user_id':17})
        raw = ScriptedRaw({'getAuthorizationState': [{'@type':'authorizationStateReady'}], 'getChat': [{'@type':'chat','id':999,'title':'wrong private identity'}]})
        client = TDLibClient(raw=raw)
        client.ensure_ready()
        with self.assertRaises(TDLibError):
            client.get_sender_identity({'@type':'messageSenderChat','chat_id':7})
