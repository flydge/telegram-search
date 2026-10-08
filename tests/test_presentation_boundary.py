"""Risk tests for the selected PPTX public surface and its trusted opt-in."""
from __future__ import annotations

import base64
import importlib
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from mcp import Client
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError, fingerprint, schema_document
from telegram_search_mcp.server import build_server
from test_attachment_page_broker import Provider

ART = 'artifact_' + '0'*32 + '_' + 'a'*64 + '_10'


class PresentationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_off_and_strict_raw_sdk_inputs_prevent_dispatch(self):
        class Service:
            def __init__(self): self.calls = []
            def close(self): pass
            def read_presentation(self, request):
                from telegram_search_mcp.presentation_models import ReadPresentationResponse
                self.calls.append(request)
                return ReadPresentationResponse(status='unsupported')
        for enabled in (False, True):
            service = Service()
            policy = RuntimePolicy(enabled_capabilities=('presentations',)) if enabled else RuntimePolicy()
            async with Client(build_server(service_factory=lambda: service, policy=policy)) as consumer:
                tools = (await consumer.list_tools()).tools
                self.assertIn('read_presentation', [t.name for t in tools])
                tool = next(t for t in tools if t.name == 'read_presentation')
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
                self.assertFalse(tool.input_schema['additionalProperties'])
                result = await consumer.call_tool('read_presentation', {'artifact_id': ART})
                self.assertEqual(bool(result.is_error), not enabled)
                before = len(service.calls)
                for bad in ({'slides': '[1]'}, {'slides': [True]}, {'slides': [1.0]},
                            {'slides': [0]}, {'slides': [129]}, {'slides': [1, 1]},
                            {'slides': []}, {'slides': [1,2,3,4,5,6]},
                            {'include_notes': 1}, {'include_notes': 'true'},
                            {'cursor': 'unused'}, {'unknown': True}):
                    result = await consumer.call_tool('read_presentation', {'artifact_id': ART, **bad})
                    self.assertTrue(result.is_error, repr(bad))
                self.assertEqual(len(service.calls), before)

    async def test_existing_twenty_eight_tool_schemas_and_annotations_are_unchanged(self):
        expected = json.loads((Path(__file__).parent/'fixtures/legacy_0_16_tool_hashes.json').read_text())
        actual = {row['name']: fingerprint(row) for row in schema_document(build_server())['tools']}
        self.assertEqual({name: actual[name] for name in expected}, expected)


class PresentationBrokerBoundaryTests(unittest.TestCase):
    def test_inflight_policy_and_generation_drift_are_rechecked(self):
        from telegram_search_mcp.presentation_models import ReadPresentationResponse
        for drift in ('policy','generation'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                broker=Broker(socket_path=root/'broker.sock',client_factory=Provider,
                    artifact_store=ArtifactStore(cache_dir=root/'cache'),
                    policy=RuntimePolicy(enabled_capabilities=('presentations',)))
                self.addCleanup(broker._executor.shutdown)
                def change(reader,request,**kwargs):
                    if drift=='policy':broker._policy=RuntimePolicy()
                    else:broker._generation='broker_'+'d'*32
                    return ReadPresentationResponse(status='unsupported')
                with patch('telegram_search_mcp.presentations.PresentationReader.read',new=change):
                    with self.assertRaises(CompatibilityError):
                        broker._dispatch({'operation':'read_presentation','payload':{'artifact_id':ART},
                            'client_id':'client_'+'a'*24,'broker_generation':broker._generation,
                            'deadline':time.monotonic()+30})

    def test_local_pptx_metadata_and_broker_capability(self):
        self.assertIn('read_presentation', OPERATIONS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = Broker(socket_path=root/'broker.sock', client_factory=Provider,
                artifact_store=ArtifactStore(cache_dir=root/'cache'), policy=RuntimePolicy())
            self.addCleanup(broker._executor.shutdown)
            def dispatch(op, payload):
                return broker._dispatch({'operation': op, 'payload': payload,
                    'client_id': 'client_'+'a'*24, 'broker_generation': broker._generation,
                    'deadline': time.monotonic()+30})
            made = dispatch('create_local_artifact', {'file_name': 'synthetic.pptx',
                'content_base64': base64.b64encode(b'not-a-presentation').decode()})
            self.assertEqual(made['status'], 'complete')
            metadata = broker._attachment_metadata(made['artifact_id'])
            self.assertEqual(metadata[2], 'application/vnd.openxmlformats-officedocument.presentationml.presentation')
            with self.assertRaises(CompatibilityError):
                dispatch('read_presentation', {'artifact_id': made['artifact_id']})
            broker._policy = RuntimePolicy(enabled_capabilities=('artifacts','presentations'))
            response = dispatch('read_presentation', {'artifact_id': made['artifact_id']})
            self.assertIn(response['status'], {'error', 'unsupported'})
            self.assertIsNone(response['scope'])
            self.assertEqual(response['slides'], [])


if __name__ == '__main__': unittest.main()
