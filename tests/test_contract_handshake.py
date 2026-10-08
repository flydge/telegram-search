"""Fail-closed compatibility checks at actual MCP and local socket boundaries."""
from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from telegram_search_mcp import config
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient, BrokerUnavailable
from telegram_search_mcp.broker_protocol import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, PROTOCOL_VERSION, receive_frame, send_frame
from telegram_search_mcp.schemas import ResolveTargetRequest
from telegram_search_mcp.server import build_server


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_manifest_exposes_versioned_schema_without_opening_provider(self):
        async with Client(build_server(service_factory=lambda: self.fail('provider opened'))) as client:
            result = await client.call_tool('_manifest', {})
        manifest = result.structured_content
        self.assertIn('contract_version', manifest)
        self.assertEqual(manifest['package_version'], manifest['version'])
        self.assertRegex(manifest['schema_fingerprint'], r'^[a-f0-9]{64}$')
        self.assertIsNone(manifest['broker_generation'])
        self.assertEqual(manifest['compatibility']['status'], 'not_checked')
        self.assertIn('read', manifest['enabled_capabilities'])

    async def test_consumer_expected_schema_mismatch_is_distinct_from_runtime_check(self):
        async with Client(build_server(service_factory=lambda: self.fail('provider opened'))) as client:
            result = await client.call_tool('_manifest', {'expected_schema_fingerprint': '0' * 64})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content['compatibility']['status'], 'client_schema_mismatch')

    async def test_detected_consumer_mismatch_blocks_calls_until_matching_recheck(self):
        from telegram_search_mcp.contract import schema_fingerprint
        from telegram_search_mcp.schemas import MessageContextResponse
        class Service:
            calls = 0
            def close(self): pass
            def get_message_context(self, request):
                self.calls += 1
                return MessageContextResponse(status='complete', anchor=request.anchor, messages=[],
                                              coverage_complete=True, detail='synthetic exact context')
        service = Service()
        async with Client(build_server(service_factory=lambda: service)) as client:
            await client.call_tool('_manifest', {'expected_schema_fingerprint': '0' * 64})
            blocked = await client.call_tool('get_message_context', {'anchor': {'chat_id': 7, 'message_id': 8}})
            self.assertTrue(blocked.is_error)
            self.assertEqual(service.calls, 0)
            await client.call_tool('_manifest', {})
            still_blocked = await client.call_tool('get_message_context', {'anchor': {'chat_id': 7, 'message_id': 8}})
            self.assertTrue(still_blocked.is_error)
            await client.call_tool('_manifest', {'expected_schema_fingerprint': schema_fingerprint()})
            accepted = await client.call_tool('get_message_context', {'anchor': {'chat_id': 7, 'message_id': 8}})
            self.assertFalse(accepted.is_error)
            self.assertEqual(service.calls, 1)

    def test_missing_config_preserves_only_legacy_capabilities(self):
        loader = getattr(config, 'load_runtime_policy', None)
        self.assertTrue(callable(loader), 'versioned trusted capability config is missing')
        with tempfile.TemporaryDirectory() as parent:
            policy = loader(Path(parent) / 'absent.toml')
        self.assertEqual(policy.config_version, 1)
        self.assertEqual(set(policy.enabled_capabilities), {'read', 'artifacts', 'send'})

    def test_stale_or_unknown_config_cannot_enable_any_operation(self):
        loader = getattr(config, 'load_runtime_policy', None)
        self.assertTrue(callable(loader), 'versioned trusted capability config is missing')
        for content in ('config_version = 0\nenabled_capabilities = ["read"]\n',
                        'config_version = 1\nenabled_capabilities = ["all"]\n',
                        'config_version = true\nenabled_capabilities = ["read"]\n'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as parent:
                path = Path(parent) / 'runtime.toml'; path.write_text(content); path.chmod(0o600)
                with self.assertRaises(config.ConfigurationError):
                    loader(path)

    def test_missing_policy_in_existing_runtime_root_keeps_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / 'runtime'; root.mkdir(mode=0o755)
            policy = config.load_runtime_policy(root / 'runtime.toml')
            self.assertEqual(set(policy.enabled_capabilities), {'read', 'artifacts', 'send'})
            path = root / 'runtime.toml'
            path.write_text('config_version = 1\nenabled_capabilities = []\n'); path.chmod(0o600)
            with self.assertRaises(config.ConfigurationError):
                config.load_runtime_policy(path)

    def test_symlinked_config_directory_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent); real = root / 'real'; real.mkdir(mode=0o700)
            path = real / 'runtime.toml'
            path.write_text('config_version = 1\nenabled_capabilities = []\n'); path.chmod(0o600)
            alias = root / 'alias'; alias.symlink_to(real)
            with self.assertRaises(config.ConfigurationError):
                config.load_runtime_policy(alias / 'runtime.toml')

    def test_trusted_pin_rejects_stale_loaded_package(self):
        from telegram_search_mcp.contract import contract_descriptor, CompatibilityError
        with self.assertRaisesRegex(CompatibilityError, 'config_stale'):
            contract_descriptor(config.RuntimePolicy(expected_package_version='0.1.0'))

    async def test_fingerprint_matches_actual_sdk_tools_and_changes_with_schema_or_annotations(self):
        from telegram_search_mcp.contract import fingerprint, schema_document, schema_fingerprint
        server = build_server(policy=config.RuntimePolicy())
        document = schema_document(server)
        self.assertEqual(fingerprint(document), schema_fingerprint())
        async with Client(server) as client:
            listing = await client.list_tools()
        observed = {t.name: t for t in listing.tools}
        for t in document['tools']:
            self.assertEqual(t['inputSchema'], observed[t['name']].input_schema)
            self.assertEqual(t['outputSchema'], observed[t['name']].output_schema)
        document['tools'][0]['annotations']['readOnlyHint'] = False
        self.assertNotEqual(fingerprint(document), schema_fingerprint())
        document = schema_document(server)
        document['tools'][0]['inputSchema']['additionalProperties'] = True
        self.assertNotEqual(fingerprint(document), schema_fingerprint())


class BrokerHandshakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tg-contract-'); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'broker.sock'
        self.provider_opened = False

    def start(self, **kwargs):
        def forbidden_provider():
            self.provider_opened = True
            raise AssertionError('compatibility validation opened Telegram')
        from telegram_search_mcp.artifact_store import ArtifactStore
        self.broker = Broker(socket_path=self.path, client_factory=forbidden_provider,
                             artifact_store=ArtifactStore(cache_dir=Path(self.tmp.name) / "cache"), **kwargs)
        self.thread = threading.Thread(target=self.broker.serve_forever, daemon=True); self.thread.start()
        self.assertTrue(self.broker.wait_until_ready(timeout=3))
        self.addCleanup(self.stop)
        return BrokerClient(socket_path=self.path, restart_callback=lambda: None)

    def stop(self):
        self.broker.shutdown(); self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def test_handshake_returns_generation_without_initializing_telegram(self):
        client = self.start()
        handshake = getattr(client, 'handshake', None)
        self.assertTrue(callable(handshake), 'broker/proxy handshake is missing')
        descriptor = handshake()
        self.assertRegex(descriptor['broker_generation'], r'^broker_[a-f0-9]{32}$')
        self.assertFalse(self.provider_opened)

    def test_schema_drift_is_rejected_before_operation_and_has_safe_diagnostics(self):
        client = self.start()
        self.assertTrue(callable(getattr(client, 'handshake', None)), 'handshake is missing')
        import telegram_search_mcp.broker_client as client_module
        original = client_module.contract_descriptor
        wrong = original(client._policy); wrong['schema_fingerprint'] = '0' * 64
        with patch.object(client_module, 'contract_descriptor', return_value=wrong):
            response = client.resolve(ResolveTargetRequest(target='@synthetic'))
            diagnostics = client.diagnostics()
        self.assertEqual(response.status, 'error')
        self.assertEqual(diagnostics['status'], 'schema_mismatch')
        self.assertFalse(self.provider_opened)

    def test_raw_operation_without_handshake_is_denied(self):
        client = self.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(self.path)); connection.settimeout(2)
            envelope = {'version': PROTOCOL_VERSION, 'client_id': client.client_id,
                        'request_id': 'request_' + 'a' * 32, 'operation': 'resolve',
                        'payload': {'target': '@synthetic'}, 'deadline': time.monotonic() + 10,
                        'broker_generation': None}
            send_frame(connection, envelope, max_bytes=MAX_REQUEST_BYTES)
            try:
                response = receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
            except Exception:
                self.fail('missing handshake must produce a clear safe rejection')
        self.assertFalse(response['ok'])
        self.assertEqual(response['error'], 'handshake_required')
        self.assertFalse(self.provider_opened)

    def raw_hello(self, client, **changes):
        from telegram_search_mcp.contract import contract_descriptor
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.connect(str(self.path)); connection.settimeout(2)
        payload = contract_descriptor(client._policy); payload.update(changes)
        hello = {'version': PROTOCOL_VERSION, 'client_id': client.client_id,
                 'request_id': 'request_' + 'a' * 32, 'operation': 'handshake',
                 'payload': payload, 'deadline': time.monotonic() + 10, 'broker_generation': None}
        send_frame(connection, hello, max_bytes=MAX_REQUEST_BYTES)
        return connection, hello, receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)

    def test_mismatching_loaded_package_contract_or_policy_rejects_handshake(self):
        client = self.start()
        for changes, code in (({'package_version': '0.1.0'}, 'package_mismatch'),
                              ({'contract_version': 123}, 'contract_mismatch'),
                              ({'config_fingerprint': '0' * 64}, 'config_mismatch'),
                              ({'unexpected': 'private sentinel'}, 'handshake_invalid')):
            with self.subTest(code=code):
                connection, hello, response = self.raw_hello(client, **changes)
                self.assertFalse(response['ok']); self.assertEqual(response['error'], code)
                connection.close()
        self.assertFalse(self.provider_opened)

    def test_wrong_generation_is_rejected_before_local_artifact_creation(self):
        client = self.start()
        connection, hello, response = self.raw_hello(client)
        self.assertTrue(response['ok'])
        operation = {**hello, 'request_id': 'request_' + 'b' * 32,
                     'operation': 'create_local_artifact', 'payload': {'file_name': 'test.txt', 'content': 'safe fixture'},
                     'broker_generation': 'broker_' + '0' * 32}
        send_frame(connection, operation, max_bytes=MAX_REQUEST_BYTES)
        result = receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
        self.assertEqual(result['error'], 'generation_stale')
        self.assertEqual(self.broker._artifact_metadata, {})

    def test_disabled_capability_is_denied_by_broker_even_with_valid_handshake(self):
        policy = config.RuntimePolicy(enabled_capabilities=('read',))
        client = self.start(policy=policy); client._policy = policy
        connection, hello, response = self.raw_hello(client)
        operation = {**hello, 'request_id': 'request_' + 'b' * 32,
                     'operation': 'create_local_artifact', 'payload': {'file_name': 'test.txt', 'content': 'safe fixture'},
                     'broker_generation': response['result']['broker_generation']}
        send_frame(connection, operation, max_bytes=MAX_REQUEST_BYTES)
        result = receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
        self.assertEqual(result['error'], 'capability_disabled')
        self.assertEqual(self.broker._artifact_metadata, {})

    def test_config_changed_after_handshake_blocks_dispatch(self):
        path = Path(self.tmp.name) / 'runtime.toml'
        path.write_text('config_version = 1\nenabled_capabilities = ["read"]\n'); path.chmod(0o600)
        policy = config.load_runtime_policy(path)
        client = self.start(policy=policy); client._policy = policy
        connection, hello, response = self.raw_hello(client)
        path.write_text('config_version = 1\nenabled_capabilities = []\n')
        operation = {**hello, 'request_id': 'request_' + 'b' * 32, 'operation': 'resolve',
                     'payload': {'target': '@synthetic'}, 'broker_generation': response['result']['broker_generation']}
        send_frame(connection, operation, max_bytes=MAX_REQUEST_BYTES)
        result = receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
        self.assertEqual(result['error'], 'config_stale')
        self.assertFalse(self.provider_opened)

    def test_pre_dispatch_send_rejection_is_not_an_uncertain_attempt(self):
        from telegram_search_mcp.schemas import SendPreparedTextRequest
        client = self.start(); client._policy = config.RuntimePolicy(enabled_capabilities=('read',))
        result = client.send_prepared_text(SendPreparedTextRequest(draft_id='draft_' + 'a' * 32, approved=True))
        self.assertEqual(result.status, 'error')
        self.assertIn('before dispatch', result.detail)
        self.assertFalse(self.provider_opened)

    def test_diagnostic_only_connections_do_not_count_as_broker_errors(self):
        client = self.start()
        self.assertEqual(client.diagnostics()['status'], 'compatible')
        deadline = time.monotonic() + 1
        while self.broker._active_handlers and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.broker._health()['error_count'], 0)
