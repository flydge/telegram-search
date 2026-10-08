"""Public XLSX capability and compatibility boundaries use literal synthetic data."""
from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
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


class SpreadsheetBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_is_append_only_and_default_off_with_strict_raw_sdk_inputs(self):
        class Service:
            def __init__(self): self.calls = []
            def close(self): pass
            def read_spreadsheet(self, request):
                from telegram_search_mcp.spreadsheet_models import ReadSpreadsheetResponse
                self.calls.append(request)
                return ReadSpreadsheetResponse(status='unsupported')
        for enabled in (False, True):
            service = Service()
            policy = RuntimePolicy(enabled_capabilities=('spreadsheets',)) if enabled else RuntimePolicy()
            async with Client(build_server(service_factory=lambda: service, policy=policy)) as consumer:
                tools = (await consumer.list_tools()).tools
                self.assertEqual(tools[27].name, 'read_spreadsheet')
                tool = tools[27]
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.idempotent_hint)
                self.assertFalse(tool.input_schema['additionalProperties'])
                result = await consumer.call_tool('read_spreadsheet', {'artifact_id': ART})
                self.assertEqual(bool(result.is_error), not enabled)
                before = len(service.calls)
                bad_inputs = [
                    {'selections': '[{"sheet_index":1,"range":"A1"}]'},
                    {'selections': [{'sheet_index': True, 'range': 'A1'}]},
                    {'selections': [{'sheet_index': 1.0, 'range': 'A1'}]},
                    {'selections': [{'sheet_index': 1, 'range': 'A:A'}]},
                    {'selections': [{'sheet_index': 1, 'range': 'B2:A1'}]},
                    {'selections': [{'sheet_index': 1, 'range': 'A1:A10001'}]},
                    {'selections': [{'sheet_index': 1, 'range': 'A1', 'extra': 1}]},
                    {'selections': []}, {'max_cells': True}, {'max_cells': 1.0},
                    {'max_cells': 201}, {'cursor': 'invalid'}, {'unknown': True},
                ]
                for bad in bad_inputs:
                    result = await consumer.call_tool('read_spreadsheet', {'artifact_id': ART, **bad})
                    self.assertTrue(result.is_error, repr(bad))
                self.assertEqual(len(service.calls), before)

    async def test_existing_twenty_seven_tool_schemas_and_annotations_are_unchanged(self):
        expected = json.loads((Path(__file__).parent/'fixtures/legacy_0_15_tool_hashes.json').read_text())
        actual = {row['name']: fingerprint(row) for row in schema_document(build_server())['tools']}
        self.assertEqual({name: actual[name] for name in expected}, expected)


class SpreadsheetBrokerBoundaryTests(unittest.TestCase):
    def test_xlsx_local_artifact_and_broker_capability_are_enforced(self):
        self.assertIn('read_spreadsheet', OPERATIONS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = Broker(socket_path=root/'broker.sock', client_factory=Provider,
                artifact_store=ArtifactStore(cache_dir=root/'cache'), policy=RuntimePolicy())
            self.addCleanup(broker._executor.shutdown)
            def dispatch(op, payload):
                return broker._dispatch({'operation': op, 'payload': payload,
                    'client_id': 'client_'+'a'*24, 'broker_generation': broker._generation,
                    'deadline': time.monotonic()+30})
            made = dispatch('create_local_artifact', {'file_name': 'synthetic.xlsx',
                'content_base64': base64.b64encode(b'not-a-workbook').decode()})
            self.assertEqual(made['status'], 'complete')
            with self.assertRaises(CompatibilityError):
                dispatch('read_spreadsheet', {'artifact_id': made['artifact_id']})
            broker._policy = RuntimePolicy(enabled_capabilities=('artifacts','spreadsheets'))
            response = dispatch('read_spreadsheet', {'artifact_id': made['artifact_id']})
            self.assertIn(response['status'], {'error', 'unsupported'})
            self.assertIsNone(response['scope'])
            self.assertEqual(response['cells'], [])


if __name__ == '__main__': unittest.main()
