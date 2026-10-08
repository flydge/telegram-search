from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mcp import Client
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError, OPERATION_CAPABILITIES
from telegram_search_mcp.reply_reader import read_reply_chain
from telegram_search_mcp.schemas import ReadReplyChainRequest
from telegram_search_mcp.server import build_server
from test_message_reader import Provider, message
from test_reply_chain import reply

POLICY = RuntimePolicy(enabled_capabilities=('read_reply_chain',))
ARGS = {'anchor': {'chat_id': 7, 'message_id': 11}, 'max_depth': 2}


class ReplyBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_opt_in_rejects_raw_coercions_before_provider(self):
        for policy, enabled in ((RuntimePolicy(), False), (POLICY, True)):
            provider = Provider({11: message(reply_to=reply(9)), 9: message(9)})
            class Service:
                def close(self): pass
                def read_reply_chain(self, request): return read_reply_chain(provider, request)
            async with Client(build_server(service_factory=Service, policy=policy)) as consumer:
                listing = await consumer.list_tools()
                tool = next((t for t in listing.tools if t.name == 'read_reply_chain'), None)
                self.assertIsNotNone(tool, 'reply-chain MCP tool missing')
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.input_schema['additionalProperties'])
                self.assertEqual(tool.input_schema['properties']['max_depth']['maximum'], 10)
                result = await consumer.call_tool('read_reply_chain', ARGS)
                if enabled:
                    self.assertEqual(result.structured_content['stop_reason'], 'no_parent')
                    self.assertEqual(len(result.structured_content['results']), 2)
                    provider.calls.clear()
                    for invalid in ({'max_depth': True}, {'max_depth': '2'}, {'max_depth': 2.0},
                            {'max_depth': 0}, {'max_depth': 11}, {'path': '/private'},
                            {'anchor': {'chat_id': 7, 'message_id': 11, 'follow': True}},
                            {'anchor': '{"chat_id":7,"message_id":11}'}):
                        failed = await consumer.call_tool('read_reply_chain', ARGS | invalid)
                        self.assertTrue(failed.is_error, invalid)
                    self.assertEqual(provider.calls, [])
                else:
                    self.assertTrue(result.is_error)
                    self.assertEqual(provider.calls, [])

    async def test_broker_guards_before_provider_and_passes_deadline(self):
        self.assertIn('read_reply_chain', OPERATION_CAPABILITIES)
        for policy, enabled in ((RuntimePolicy(), False), (POLICY, True)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); provider = Provider({11: message()})
                opened = []
                def factory(): opened.append(True); return provider
                broker = Broker(socket_path=root/'broker.sock', artifact_store=ArtifactStore(cache_dir=root/'cache'),
                                client_factory=factory, policy=policy)
                self.addCleanup(broker._executor.shutdown)
                request = {'operation': 'read_reply_chain', 'client_id': 'client_'+'a'*24,
                    'payload': ARGS, 'deadline': time.monotonic()+10, 'broker_generation': broker._generation}
                if enabled:
                    result = broker._dispatch(request)
                    self.assertEqual(result['stop_reason'], 'no_parent')
                    self.assertEqual(provider.deadline, request['deadline'])
                else:
                    with self.assertRaises(CompatibilityError): broker._dispatch(request)
                    self.assertEqual(opened, [])

    async def test_proxy_binds_root_depth_and_rejects_forged_topology(self):
        self.assertTrue(hasattr(BrokerClient, 'read_reply_chain'), 'reply-chain proxy missing')
        request = ReadReplyChainRequest(**ARGS)
        valid = read_reply_chain(Provider({11: message(reply_to=reply(9)), 9: message(9)}), request).model_dump(mode='json')
        for mutation in ('root', 'depth', 'duplicate', 'disconnected', 'foreign', 'claim', 'bool', 'date'):
            data = copy.deepcopy(valid)
            if mutation == 'root': data['anchor']['message_id'] = 22
            elif mutation == 'depth': data['max_depth'] = 10
            elif mutation == 'duplicate': data['results'][1] = copy.deepcopy(data['results'][0])
            elif mutation == 'disconnected': data['results'][0]['message']['reply_to']['message_id'] = 8
            elif mutation == 'foreign': data['results'][1]['anchor']['chat_id'] = 999
            elif mutation == 'claim': data['chain_complete'] = False
            elif mutation == 'bool': data['results'][0]['message']['is_outgoing'] = 'yes'
            else: data['results'][0]['message']['date_utc'] = '2023-11-14T22:13:20'
            class Proxy(BrokerClient):
                def _request(self, *args, **kwargs): return data
            result = Proxy(socket_path=Path('/unused'), policy=POLICY).read_reply_chain(request)
            self.assertEqual(result.status, 'error', mutation)
            self.assertEqual(result.anchor, request.anchor)
            self.assertEqual(result.results, [])

    async def test_socket_operation_uses_matching_contract_and_read_capability(self):
        self.assertTrue(hasattr(BrokerClient, 'read_reply_chain'), 'reply-chain proxy missing')
        for policy, enabled in ((RuntimePolicy(), False), (POLICY, True)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); provider = Provider({11: message(reply_to=reply(9)), 9: message(9)})
                provider.close = lambda: None
                broker = Broker(socket_path=root/'broker.sock', artifact_store=ArtifactStore(cache_dir=root/'cache'),
                                client_factory=lambda: provider, policy=policy)
                thread = threading.Thread(target=broker.serve_forever, daemon=True)
                thread.start()
                try:
                    self.assertTrue(broker.wait_until_ready(timeout=3))
                    proxy = BrokerClient(socket_path=root/'broker.sock', policy=policy, restart_callback=lambda: None)
                    self.assertEqual(proxy.diagnostics()['status'], 'compatible')
                    result = proxy.read_reply_chain(ReadReplyChainRequest(**ARGS))
                    if enabled:
                        self.assertEqual([r.anchor.message_id for r in result.results], [11, 9])
                        self.assertTrue(result.coverage_complete)
                    else:
                        self.assertEqual(result.status, 'error')
                        self.assertEqual(provider.calls, [])
                    proxy.close()
                finally:
                    broker.shutdown(); thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
