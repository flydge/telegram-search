from __future__ import annotations

import unittest
from collections import deque
from typing import Any

from telegram_search_mcp import tdjson
from telegram_search_mcp.tdjson import (
    AuthorizationBlocked, ForbiddenTDLibRequest, MessageNotFound, TDLibClient, TDLibError,
)


class ScriptedRaw:
    """Replace only the external transport; keep validation and correlation real."""

    def __init__(self, replies: dict[str, list[dict[str, Any]]]) -> None:
        self.replies = {key: deque(values) for key, values in replies.items()}
        self.pending: deque[dict[str, Any]] = deque()
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        response = dict(self.replies[payload['@type']].popleft())
        response['@extra'] = payload['@extra']
        self.pending.append(response)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        del timeout
        return self.pending.popleft() if self.pending else None

    def close(self) -> None:
        pass


def ready_client(replies: dict[str, list[dict[str, Any]]]) -> tuple[TDLibClient, ScriptedRaw]:
    raw = ScriptedRaw({'getAuthorizationState': [{'@type': 'authorizationStateReady'}], **replies})
    client = TDLibClient(raw=raw)
    client.ensure_ready()
    return client, raw


def group_chat() -> dict[str, Any]:
    return {'@type': 'chat', 'id': -1007, 'title': 'Synthetic forum',
            'type': {'@type': 'chatTypeSupergroup', 'supergroup_id': 7, 'is_channel': False}}


def forum_page() -> dict[str, Any]:
    return {'@type': 'forumTopics', 'total_count': 999, 'topics': [],
            'next_offset_date': 0, 'next_offset_message_id': 0, 'next_offset_forum_topic_id': 0}


class ForumNativeTests(unittest.TestCase):
    def operation(self, client: TDLibClient, name: str):
        operation = getattr(client, name, None)
        self.assertTrue(callable(operation), f'{name} native adapter is missing')
        return operation

    def assert_invalid_provider(self, operation, *args, **kwargs) -> None:
        with self.assertRaises(TDLibError) as caught:
            operation(*args, **kwargs)
        unsupported = getattr(tdjson, 'ForumUnsupported', ())
        self.assertNotIsInstance(caught.exception, unsupported)
        self.assertNotIn('private marker', str(caught.exception))

    def test_resolves_exact_forum_supergroup_without_projecting_chat(self) -> None:
        chat = group_chat()
        chat['untrusted'] = {'private marker': 'retained raw'}
        client, raw = ready_client({'getChat': [chat], 'getSupergroup': [
            {'@type': 'supergroup', 'id': 7, 'is_forum': True}]})
        result = self.operation(client, 'resolve_forum_chat')(-1007)
        self.assertEqual(result['untrusted'], {'private marker': 'retained raw'})
        self.assertEqual([{k: v for k, v in request.items() if k != '@extra'} for request in raw.sent[1:]], [
            {'@type': 'getChat', 'chat_id': -1007}, {'@type': 'getSupergroup', 'supergroup_id': 7}])

    def test_resolves_exact_bot_private_chat_with_topics(self) -> None:
        client, raw = ready_client({'getChat': [{'@type': 'chat', 'id': 19, 'title': 'Bot',
            'type': {'@type': 'chatTypePrivate', 'user_id': 19}}], 'getUser': [
            {'@type': 'user', 'id': 19, 'type': {'@type': 'userTypeBot', 'has_topics': True}}]})
        self.assertEqual(self.operation(client, 'resolve_forum_chat')(19)['id'], 19)
        self.assertEqual({k: v for k, v in raw.sent[-1].items() if k != '@extra'},
                         {'@type': 'getUser', 'user_id': 19})

    def test_rejects_valid_nonforum_chats_with_distinct_exception(self) -> None:
        variants = [
            ({'@type': 'chatTypeSecret', 'secret_chat_id': 4}, {}),
            ({'@type': 'chatTypeBasicGroup', 'basic_group_id': 4}, {}),
            ({'@type': 'chatTypeSupergroup', 'supergroup_id': 7, 'is_channel': True}, {}),
            ({'@type': 'chatTypeSupergroup', 'supergroup_id': 7, 'is_channel': False},
             {'getSupergroup': [{'@type': 'supergroup', 'id': 7, 'is_forum': False}]}),
            ({'@type': 'chatTypePrivate', 'user_id': 19},
             {'getUser': [{'@type': 'user', 'id': 19, 'type': {'@type': 'userTypeRegular'}}]}),
            ({'@type': 'chatTypePrivate', 'user_id': 19},
             {'getUser': [{'@type': 'user', 'id': 19, 'type': {'@type': 'userTypeBot', 'has_topics': False}}]}),
        ]
        for kind, replies in variants:
            with self.subTest(kind=kind, replies=replies):
                chat = {'@type': 'chat', 'id': -1007, 'type': kind}
                client, _ = ready_client({'getChat': [chat], **replies})
                operation = self.operation(client, 'resolve_forum_chat')
                unsupported = getattr(tdjson, 'ForumUnsupported', None)
                self.assertIsNotNone(unsupported)
                with self.assertRaises(unsupported):
                    operation(-1007)

    def test_chat_identity_and_type_are_strict(self) -> None:
        variants = [
            {'@type': 'chat', 'id': -1008, 'type': group_chat()['type']},
            {'@type': 'chat', 'id': True, 'type': group_chat()['type']},
            {'@type': 'private marker', 'id': -1007, 'type': group_chat()['type']},
            {'@type': 'chat', 'id': -1007},
            {'@type': 'chat', 'id': -1007, 'type': 'private marker'},
            {'@type': 'chat', 'id': -1007, 'type': {'@type': 'private marker'}},
            {'@type': 'chat', 'id': -1007, 'type': {'@type': 'chatTypeSupergroup', 'supergroup_id': True, 'is_channel': False}},
            {'@type': 'chat', 'id': -1007, 'type': {'@type': 'chatTypeSupergroup', 'supergroup_id': 0, 'is_channel': False}},
            {'@type': 'chat', 'id': -1007, 'type': {'@type': 'chatTypeSupergroup', 'supergroup_id': 2**53, 'is_channel': False}},
            {'@type': 'chat', 'id': -1007, 'type': {'@type': 'chatTypeSupergroup', 'supergroup_id': 7, 'is_channel': 0}},
            {'@type': 'chat', 'id': -1007, 'type': {'@type': 'chatTypePrivate', 'user_id': True}},
            {'@type': 'chat', 'id': -1007, 'type': {'@type': 'chatTypePrivate', 'user_id': 0}},
            {'@type': 'chat', 'id': -1007, 'type': {'@type': 'chatTypePrivate', 'user_id': 2**53}},
            {'@type': 'chat', 'id': -1007, 'type': {'@type': 'chatTypeSecret', 'secret_chat_id': True}},
            {'@type': 'chat', 'id': -1007, 'type': {'@type': 'chatTypeBasicGroup', 'basic_group_id': 0}},
        ]
        for response in variants:
            with self.subTest(response=response):
                client, raw = ready_client({'getChat': [response]})
                self.assert_invalid_provider(self.operation(client, 'resolve_forum_chat'), -1007)
                self.assertEqual(len(raw.sent), 2)

    def test_capability_identity_and_boolean_are_strict(self) -> None:
        for kind, bad_responses in [
            ('group', [
                {'@type': 'supergroup', 'id': 8, 'is_forum': True},
                {'@type': 'supergroup', 'id': True, 'is_forum': True},
                {'@type': 'private marker', 'id': 7, 'is_forum': True},
                {'@type': 'supergroup', 'id': 7},
                {'@type': 'supergroup', 'id': 7, 'is_forum': 1},
                {'@type': 'supergroup', 'id': 7, 'is_forum': 'true'},
            ]),
            ('bot', [
                {'@type': 'user', 'id': 20, 'type': {'@type': 'userTypeBot', 'has_topics': True}},
                {'@type': 'user', 'id': True, 'type': {'@type': 'userTypeBot', 'has_topics': True}},
                {'@type': 'private marker', 'id': 19, 'type': {'@type': 'userTypeBot', 'has_topics': True}},
                {'@type': 'user', 'id': 19, 'type': 'private marker'},
                {'@type': 'user', 'id': 19, 'type': {'@type': 'private marker'}},
                {'@type': 'user', 'id': 19, 'type': {'@type': ['private marker']}},
                {'@type': 'user', 'id': 19, 'type': {'@type': 'userTypeBot'}},
                {'@type': 'user', 'id': 19, 'type': {'@type': 'userTypeBot', 'has_topics': 1}},
                {'@type': 'user', 'id': 19, 'type': {'@type': 'userTypeBot', 'has_topics': 'true'}},
            ]),
        ]:
            for response in bad_responses:
                with self.subTest(kind=kind, response=response):
                    chat = group_chat() if kind == 'group' else {'@type': 'chat', 'id': -1007,
                        'type': {'@type': 'chatTypePrivate', 'user_id': 19}}
                    client, _ = ready_client({'getChat': [chat],
                        'getSupergroup' if kind == 'group' else 'getUser': [response]})
                    self.assert_invalid_provider(self.operation(client, 'resolve_forum_chat'), -1007)

    def test_invalid_chat_ids_never_reach_transport(self) -> None:
        for identifier in [True, False, 0, 1.5, '-1007', -(2**53), 2**53, None]:
            with self.subTest(identifier=identifier):
                client, raw = ready_client({})
                with self.assertRaises(ValueError):
                    self.operation(client, 'resolve_forum_chat')(identifier)
                self.assertEqual(len(raw.sent), 1)

    def test_listing_has_exact_default_shape_and_preserves_provider_page(self) -> None:
        page = forum_page()
        page['topics'] = [{'@type': 'forumTopic', 'last_message': {'private marker': 'raw'}}]
        client, raw = ready_client({'getForumTopics': [page]})
        result = self.operation(client, 'get_forum_topics')(-1007)
        self.assertEqual(result['total_count'], 999)
        self.assertEqual(result['topics'][0]['last_message'], {'private marker': 'raw'})
        self.assertEqual({k: v for k, v in raw.sent[-1].items() if k != '@extra'}, {
            '@type': 'getForumTopics', 'chat_id': -1007, 'query': '', 'offset_date': 0,
            'offset_message_id': 0, 'offset_forum_topic_id': 0, 'limit': 20})

    def test_offset_triple_is_forwarded_unchanged_at_valid_bounds(self) -> None:
        client, raw = ready_client({'getForumTopics': [forum_page()]})
        self.operation(client, 'get_forum_topics')(-(2**53 - 1), offset_date=2**31 - 1,
            offset_message_id=2**53 - 1, offset_forum_topic_id=2**31 - 1, limit=1)
        self.assertEqual({k: v for k, v in raw.sent[-1].items() if k != '@extra'}, {
            '@type': 'getForumTopics', 'chat_id': -(2**53 - 1), 'query': '',
            'offset_date': 2**31 - 1, 'offset_message_id': 2**53 - 1,
            'offset_forum_topic_id': 2**31 - 1, 'limit': 1})

    def test_invalid_listing_offsets_and_limits_never_reach_transport(self) -> None:
        cases = [(key, value) for key, values in {
            'offset_date': [True, -1, 2**31, '0', 1.0],
            'offset_message_id': [True, -1, 2**53, '0', 1.0],
            'offset_forum_topic_id': [True, -1, 2**31, '0', 1.0],
            'limit': [True, 0, -1, 21, '20', 20.0],
        }.items() for value in values]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                client, raw = ready_client({})
                with self.assertRaises(ValueError):
                    self.operation(client, 'get_forum_topics')(-1007, **{key: value})
                self.assertEqual(len(raw.sent), 1)

    def test_invalid_listing_chat_ids_never_reach_transport(self) -> None:
        for value in [True, 0, 2**53, -(2**53), '19']:
            with self.subTest(value=value):
                client, raw = ready_client({})
                with self.assertRaises(ValueError):
                    self.operation(client, 'get_forum_topics')(value)
                self.assertEqual(len(raw.sent), 1)

    def test_listing_rejects_arbitrary_arguments(self) -> None:
        client, raw = ready_client({})
        for kwargs in [{'query': 'private marker'}, {'topic_id': 2}, {'only_local': True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(TypeError):
                self.operation(client, 'get_forum_topics')(-1007, **kwargs)
        self.assertEqual(len(raw.sent), 1)

    def test_listing_rejects_malformed_envelope_and_local_page_overflow(self) -> None:
        variants = [{'@type': 'private marker'}, {**forum_page(), 'topics': None},
            {**forum_page(), 'topics': [{}]}, {**forum_page(), 'topics': ['private marker']},
            {**forum_page(), 'topics': [{'@type': 'message'}]},
            {**forum_page(), 'topics': [{'@type': 'forumTopic'}] * 201}]
        for response in variants:
            with self.subTest(response=response):
                client, _ = ready_client({'getForumTopics': [response]})
                self.assert_invalid_provider(self.operation(client, 'get_forum_topics'), -1007, limit=1)

    def test_listing_preserves_three_topics_for_requested_limit_two(self) -> None:
        page = {**forum_page(), 'topics': [
            {'@type': 'forumTopic', 'info': {'forum_topic_id': 1}},
            {'@type': 'forumTopic', 'info': {'forum_topic_id': 2}},
            {'@type': 'forumTopic', 'info': {'forum_topic_id': 3}},
        ], 'next_offset_date': 1700000001, 'next_offset_message_id': 1048576,
            'next_offset_forum_topic_id': 3}
        client, raw = ready_client({'getForumTopics': [page]})
        try:
            result = self.operation(client, 'get_forum_topics')(-1007, limit=2)
        except TDLibError:
            self.fail('bounded native over-return was incorrectly rejected')
        self.assertEqual([topic['info']['forum_topic_id'] for topic in result['topics']], [1, 2, 3])
        self.assertEqual((result['next_offset_date'], result['next_offset_message_id'],
                          result['next_offset_forum_topic_id']), (1700000001, 1048576, 3))
        self.assertEqual(raw.sent[-1]['limit'], 2)

    def test_listing_accepts_exactly_two_hundred_topics_without_slicing(self) -> None:
        topics = [{'@type': 'forumTopic', 'info': {'forum_topic_id': identifier}}
                  for identifier in range(1, 201)]
        client, raw = ready_client({'getForumTopics': [{**forum_page(), 'topics': topics}]})
        try:
            result = self.operation(client, 'get_forum_topics')(-1007, limit=1)
        except TDLibError:
            self.fail('exact local candidate bound was incorrectly rejected')
        self.assertEqual(len(result['topics']), 200)
        self.assertEqual(result['topics'][-1]['info']['forum_topic_id'], 200)
        self.assertEqual(raw.sent[-1]['limit'], 1)

    def test_listing_rejects_two_hundred_one_before_inspecting_entries(self) -> None:
        class UninspectableTopic(dict):
            def get(self, key, default=None):
                raise AssertionError('overbudget topic entry was inspected')

        client, _ = ready_client({'getForumTopics': [
            {**forum_page(), 'topics': [UninspectableTopic()] * 201}]})
        self.assert_invalid_provider(self.operation(client, 'get_forum_topics'), -1007, limit=20)

    def test_listing_requires_valid_complete_next_triple(self) -> None:
        for key, invalid in [
            ('next_offset_date', [True, -1, 2**31, '0', None]),
            ('next_offset_message_id', [True, -1, 2**53, '0', None]),
            ('next_offset_forum_topic_id', [True, -1, 2**31, '0', None]),
        ]:
            for value in invalid:
                with self.subTest(key=key, value=value):
                    client, _ = ready_client({'getForumTopics': [{**forum_page(), key: value}]})
                    self.assert_invalid_provider(self.operation(client, 'get_forum_topics'), -1007)
            with self.subTest(key=key, missing=True):
                response = forum_page()
                del response[key]
                client, _ = ready_client({'getForumTopics': [response]})
                self.assert_invalid_provider(self.operation(client, 'get_forum_topics'), -1007)

    def test_listing_preserves_zero_and_repeated_offsets_without_completeness_claim(self) -> None:
        page = forum_page()
        client, _ = ready_client({'getForumTopics': [page, page]})
        operation = self.operation(client, 'get_forum_topics')
        first, second = operation(-1007), operation(-1007)
        self.assertEqual([first[key] for key in ('next_offset_date', 'next_offset_message_id',
                                                'next_offset_forum_topic_id')],
                         [second[key] for key in ('next_offset_date', 'next_offset_message_id',
                                                 'next_offset_forum_topic_id')])
        self.assertNotIn('complete', first)
        self.assertEqual(first['next_offset_forum_topic_id'], 0)

    def test_topic_lookup_has_exact_shape_and_returns_raw_topic(self) -> None:
        client, raw = ready_client({'getForumTopic': [{'@type': 'forumTopic',
            'last_message': {'private marker': 'raw'}}]})
        result = self.operation(client, 'get_forum_topic')(-1007, 1)
        self.assertEqual(result['last_message'], {'private marker': 'raw'})
        self.assertEqual({k: v for k, v in raw.sent[-1].items() if k != '@extra'},
            {'@type': 'getForumTopic', 'chat_id': -1007, 'forum_topic_id': 1})

    def test_topic_id_is_strict_positive_int32(self) -> None:
        for identifier in [True, False, 0, -1, 2**31, 1.0, '1', None]:
            with self.subTest(identifier=identifier):
                client, raw = ready_client({})
                with self.assertRaises(ValueError):
                    self.operation(client, 'get_forum_topic')(-1007, identifier)
                self.assertEqual(len(raw.sent), 1)
        client, _ = ready_client({'getForumTopic': [{'@type': 'forumTopic'}]})
        self.assertIsNotNone(self.operation(client, 'get_forum_topic')(2**53 - 1, 2**31 - 1))

    def test_invalid_lookup_chat_ids_never_reach_transport(self) -> None:
        for identifier in [True, 0, 2**53, -(2**53), '-1007']:
            with self.subTest(identifier=identifier):
                client, raw = ready_client({})
                with self.assertRaises(ValueError):
                    self.operation(client, 'get_forum_topic')(identifier, 1)
                self.assertEqual(len(raw.sent), 1)

    def test_native_null_translates_only_correlated_404_to_absent_topic(self) -> None:
        client, raw = ready_client({'getForumTopic': [
            {'@type': 'error', 'code': 404, 'message': 'private marker'}]})
        self.assertIsNone(self.operation(client, 'get_forum_topic')(-1007, 2))
        self.assertEqual(len(raw.sent), 2)
        client, _ = ready_client({'getMessage': [{'@type': 'error', 'code': 404, 'message': 'private marker'}]})
        with self.assertRaises(MessageNotFound):
            client.get_message(-1007, 1048576)

    def test_lookup_rejects_unpinned_null_objects_and_wrong_outer_type(self) -> None:
        for response in [{'@type': 'null'}, {}, {'@type': 'forumTopics'}, {'@type': 'message'},
                         {'@type': 'error', 'code': 400, 'message': 'private marker'}]:
            with self.subTest(response=response):
                client, _ = ready_client({'getForumTopic': [response]})
                self.assert_invalid_provider(self.operation(client, 'get_forum_topic'), -1007, 2)

    def test_forum_requests_preserve_authorization_gate(self) -> None:
        for name, args in [('resolve_forum_chat', (-1007,)), ('get_forum_topics', (-1007,)),
                           ('get_forum_topic', (-1007, 1))]:
            with self.subTest(name=name):
                raw = ScriptedRaw({})
                client = TDLibClient(raw=raw)
                with self.assertRaises(AuthorizationBlocked):
                    self.operation(client, name)(*args)
                self.assertEqual(raw.sent, [])

    def test_low_level_forum_requests_cannot_escape_closed_shapes(self) -> None:
        valid = [
            {'@type': 'getSupergroup', 'supergroup_id': 7},
            {'@type': 'getForumTopic', 'chat_id': -1007, 'forum_topic_id': 1},
            {'@type': 'getForumTopics', 'chat_id': -1007, 'query': '', 'offset_date': 0,
             'offset_message_id': 0, 'offset_forum_topic_id': 0, 'limit': 20},
        ]
        mutations = [
            {**valid[0], 'supergroup_id': True}, {**valid[0], 'supergroup_id': 0},
            {**valid[0], 'supergroup_id': 2**53}, {**valid[0], 'extra': 0},
            {**valid[1], 'forum_topic_id': True}, {**valid[1], 'forum_topic_id': 0},
            {**valid[1], 'forum_topic_id': 2**31}, {**valid[1], 'chat_id': True},
            {**valid[1], 'extra': 0}, {**valid[2], 'query': 'private marker'},
            {**valid[2], 'offset_date': True}, {**valid[2], 'offset_date': -1},
            {**valid[2], 'offset_message_id': 2**53}, {**valid[2], 'offset_forum_topic_id': 2**31},
            {**valid[2], 'limit': True}, {**valid[2], 'limit': 21}, {**valid[2], 'limit': 0},
            {**valid[2], 'chat_id': 0}, {**valid[2], 'extra': 0},
        ]
        for payload in mutations:
            with self.subTest(payload=payload):
                client, raw = ready_client({})
                with self.assertRaises(ForbiddenTDLibRequest):
                    client._call(payload)
                self.assertEqual(len(raw.sent), 1)

    def test_history_and_other_topic_families_remain_outside_allowlist(self) -> None:
        for request_type in ['getForumTopicHistory', 'getMessageThreadHistory',
                             'getSavedMessagesTopicHistory', 'getDirectMessagesChatTopicHistory']:
            with self.subTest(request_type=request_type):
                client, raw = ready_client({})
                with self.assertRaises(ForbiddenTDLibRequest):
                    client._call({'@type': request_type})
                self.assertEqual(len(raw.sent), 1)


if __name__ == '__main__':
    unittest.main()
