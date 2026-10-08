from __future__ import annotations

import unittest

from telegram_search_mcp.tdjson import ForbiddenTDLibRequest, TDLibError
from test_forum_tdjson import ready_client
from test_message_reader import message


class TopicHistoryNativeTests(unittest.TestCase):
    def operation(self, client):
        value = getattr(client, 'get_forum_topic_history', None)
        self.assertTrue(callable(value), 'bounded native forum-history adapter missing')
        return value

    def test_exact_native_shape_preserves_forum_id_and_message_boundary(self):
        for total in (-1, 1, 999):
            page = {'@type':'messages','total_count':total,'messages':[message(11)]}
            client, raw = ready_client({'getForumTopicHistory':[page]})
            result = self.operation(client)(-1007, 9, from_message_id=12582912, limit=2)
            self.assertEqual(result, {**page, '@extra':raw.sent[-1]['@extra']})
            self.assertEqual({k:v for k,v in raw.sent[-1].items() if k!='@extra'},
                {'@type':'getForumTopicHistory','chat_id':-1007,'forum_topic_id':9,
                 'from_message_id':12582912,'offset':0,'limit':2})

    def test_invalid_values_never_reach_native_transport(self):
        base = dict(chat_id=-1007, forum_topic_id=9, from_message_id=0, limit=20)
        for key, values in {'chat_id':[True,0,'7',2**53], 'forum_topic_id':[True,0,'9',2**31],
                            'from_message_id':[True,-1,'0',2**53], 'limit':[True,0,21,'2']}.items():
            for value in values:
                client, raw = ready_client({})
                with self.assertRaises(ValueError):self.operation(client)(**{**base,key:value})
                self.assertEqual(len(raw.sent),1)

    def test_native_allowlist_is_exact_and_rejects_read_receipts(self):
        base = {'@type':'getForumTopicHistory','chat_id':-1007,'forum_topic_id':9,
                'from_message_id':0,'offset':0,'limit':20}
        mutations = [{**base,'offset':-1},{**base,'offset':False},{**base,'only_local':False},
                     {**base,'forum_topic_id':True},{**base,'limit':21},{**base,'chat_id':0},
                     {**base,'from_message_id':-1},{'@type':'getForumTopicHistory'},
                     {'@type':'viewMessages','chat_id':-1007,'message_ids':[11],'force_read':False}]
        for payload in mutations:
            client, raw = ready_client({})
            with self.assertRaises(ForbiddenTDLibRequest):client._call(payload)
            self.assertEqual(len(raw.sent),1)

    def test_envelope_guard_does_not_infer_completion_or_truncate_overreturn(self):
        base = {'@type':'messages','total_count':-1,'messages':[message(30),message(20),message(10)]}
        client, _ = ready_client({'getForumTopicHistory':[base]})
        self.assertEqual(len(self.operation(client)(7,9,limit=1)['messages']),3)
        for change in ({'@type':'private marker'},{'total_count':True},{'total_count':-2},
                       {'total_count':2**31},{'messages':[None]},{'messages':[{}]},
                       {'messages':[message(11)]*201}):
            client, _ = ready_client({'getForumTopicHistory':[{**base,**change}]})
            with self.assertRaises(TDLibError) as caught:self.operation(client)(7,9)
            self.assertNotIn('private marker',str(caught.exception))
