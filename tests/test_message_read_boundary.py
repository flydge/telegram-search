from __future__ import annotations
import tempfile
import time
import unittest
from pathlib import Path
from mcp import Client
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError
from telegram_search_mcp.server import build_server
from telegram_search_mcp.schemas import ReadMessagesRequest
from test_message_reader import Provider, message

POLICY = RuntimePolicy(enabled_capabilities=('artifacts','read','read_messages','send'))


class ReadBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_read_is_opt_in_and_strict_on_public_surface(self):
        for policy, enabled in ((RuntimePolicy(), False), (POLICY, True)):
            provider = Provider({11: message()})
            class Service:
                def close(self): pass
                def read_messages(self, request):
                    from telegram_search_mcp.message_reader import read_messages
                    return read_messages(provider, request)
            async with Client(build_server(service_factory=Service, policy=policy)) as consumer:
                listing = await consumer.list_tools()
                tool = next((t for t in listing.tools if t.name == 'read_messages'), None)
                self.assertIsNotNone(tool, 'selected-read tool is missing')
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.input_schema['additionalProperties'])
                self.assertEqual(tool.input_schema['properties']['anchors']['maxItems'],20)
                result = await consumer.call_tool('read_messages', {'anchors':[{'chat_id':7,'message_id':11}]})
                if enabled:
                    self.assertEqual(result.structured_content['status'],'complete')
                    invalid = await consumer.call_tool('read_messages', {'anchors':[{'chat_id':7,'message_id':11}], 'path':'/private'})
                    self.assertTrue(invalid.is_error)
                else:
                    self.assertTrue(result.is_error)
                    self.assertEqual(provider.calls, [])

    async def test_broker_enforces_capability_before_provider_and_dispatches_enabled_read(self):
        for policy, enabled in ((RuntimePolicy(),False),(POLICY,True)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); provider = Provider({11:message()})
                broker = Broker(socket_path=root/'broker.sock', artifact_store=ArtifactStore(cache_dir=root/'cache'),
                                client_factory=lambda:provider, policy=policy)
                self.addCleanup(broker._executor.shutdown)
                request = {'operation':'read_messages','client_id':'client_'+'a'*24,
                           'payload':{'anchors':[{'chat_id':7,'message_id':11}]},
                           'deadline':time.monotonic()+10, 'broker_generation':broker._generation}
                if enabled:
                    result = broker._dispatch(request)
                    self.assertEqual(result['results'][0]['message']['text']['value'], 'Full selected text')
                else:
                    with self.assertRaises(CompatibilityError):
                        broker._dispatch(request)
                    self.assertEqual(provider.calls, [])

    async def test_proxy_does_not_accept_response_for_different_anchor(self):
        self.assertTrue(hasattr(BrokerClient,'read_messages'), 'selected read proxy is missing')
        from telegram_search_mcp.message_reader import read_messages
        wrong = read_messages(Provider({12:message(12)}), ReadMessagesRequest(anchors=[{'chat_id':7,'message_id':12}]))
        class WrongProxy(BrokerClient):
            def _request(self, *args, **kwargs):
                return wrong.model_dump(mode='json')
        proxy = WrongProxy(socket_path=Path('/unused'),policy=POLICY)
        result = proxy.read_messages(ReadMessagesRequest(anchors=[{'chat_id':7,'message_id':11}]))
        self.assertEqual(result.status,'error')
        self.assertEqual(result.results[0].anchor.message_id,11)
        self.assertIsNone(result.results[0].message)
