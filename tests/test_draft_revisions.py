"""F11b: stale previews cannot authorize a replaced immutable draft."""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.config import RuntimePolicy, ConfigurationError, load_runtime_policy
from telegram_search_mcp.contract import OPERATION_CAPABILITIES, contract_descriptor, require_compatible, CompatibilityError
from telegram_search_mcp.outgoing_drafts import DraftError, OutgoingDraftRegistry
import test_draft_boundary as boundary_fixture
import test_outgoing_drafts as registry_fixture
from test_draft_boundary import A, B, UNKNOWN
from test_outgoing_drafts import OWNER


class RevisionBoundaryTests(unittest.TestCase):
    setUp = boundary_fixture.DraftBoundaryTests.setUp
    dispatch = boundary_fixture.DraftBoundaryTests.dispatch
    prepare = boundary_fixture.DraftBoundaryTests.prepare

    def revise(self, did, operation='update_draft', **fields):
        self.assertIn(operation, OPERATION_CAPABILITIES, 'revision operation must be public')
        response = self.dispatch(operation, {'draft_id': did, **fields})
        self.assertEqual(response['status'], 'revised')
        self.assertEqual(response['previous_draft_id'], did)
        self.assertNotEqual(response['draft']['draft_id'], did)
        return response['draft']

    def test_update_normalizes_new_preview_and_old_send_cannot_reuse_approval(self):
        old = self.prepare('original')
        revised = self.revise(old, text='Ｆｉｎａｌ\r\ntext')
        self.assertEqual(revised['text'], 'Final\ntext')
        self.assertEqual(revised['sha256'], hashlib.sha256(b'Final\ntext').hexdigest())
        self.assertEqual(self.dispatch('get_draft', {'draft_id': old})['status'], 'unavailable')
        self.assertEqual([d['draft_id'] for d in self.dispatch('list_drafts', {})['drafts']], [revised['draft_id']])
        with patch.object(self.broker, '_approval_prompt', side_effect=AssertionError('stale ID prompted')):
            self.assertNotEqual(self.dispatch('send_prepared_text', {'draft_id': old, 'approved': True})['status'], 'sent')
        self.assertEqual(self.sender.sent, [])
        sent = self.dispatch('send_prepared_text', {'draft_id': revised['draft_id'], 'approved': True})
        self.assertEqual(sent['status'], 'sent')
        self.assertEqual(self.sender.sent, [(123, 'Final\ntext')])

    def test_update_or_refresh_while_approval_open_prevents_old_send(self):
        for operation in ('update_draft', 'refresh_draft'):
            with self.subTest(operation=operation):
                did = self.prepare('before')
                changes = {'text': 'after'} if operation == 'update_draft' else {}
                new = []
                def approve(**preview):
                    self.assertEqual(preview['text'], 'before')
                    new.append(self.revise(did, operation, **changes))
                    return True
                self.broker._approval_prompt = approve
                sent = self.dispatch('send_prepared_text', {'draft_id': did, 'approved': True})
                self.assertNotEqual(sent['status'], 'sent')
                self.assertEqual(self.sender.sent, [])
                self.assertEqual(self.dispatch('get_draft', {'draft_id': new[0]['draft_id']})['status'], 'pending')

    def test_foreign_account_unknown_and_stale_revisions_share_denial(self):
        did = self.prepare()
        self.assertIn('refresh_draft', OPERATION_CAPABILITIES)
        for op, fields in [('refresh_draft', {}), ('update_draft', {'text': 'new'})]:
            unknown = self.dispatch(op, {'draft_id': UNKNOWN, **fields})
            for client, account in [(B, 7), (A, 8)]:
                self.sender.account = account
                self.assertEqual(self.dispatch(op, {'draft_id': did, **fields}, client), unknown)
            self.sender.account = 7
        revised = self.revise(did, text='new')
        self.assertEqual(self.dispatch('refresh_draft', {'draft_id': did}),
                         self.dispatch('refresh_draft', {'draft_id': UNKNOWN}))
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(revised['account_id'], 7)

    def test_refresh_rehydrates_title_and_changed_account_cannot_commit(self):
        did = self.prepare('stable')
        self.sender.resolve_target = lambda recipient: {'title': 'Current title'}
        revised = self.revise(did, 'refresh_draft')
        self.assertEqual((revised['text'], revised['recipient'], revised['recipient_title']), ('stable', 123, 'Current title'))
        def drift(recipient):
            self.sender.account = 8
            return {'title': 'drifted'}
        self.sender.resolve_target = drift
        denied = self.dispatch('refresh_draft', {'draft_id': revised['draft_id']})
        self.assertEqual(denied['status'], 'unavailable')
        self.sender.account = 7
        self.assertEqual(self.dispatch('get_draft', {'draft_id': revised['draft_id']})['status'], 'pending')

    def test_text_or_caption_only_and_schema_injection_is_rejected(self):
        did = self.prepare()
        self.assertIn('update_draft', OPERATION_CAPABILITIES)
        for fields in ({}, {'text': None}, {'text': 'x', 'caption': 'y'}, {'text': 42},
                       {'text': 'x', 'recipient': 9}, {'text': 'x', 'approved': True},
                       {'text': 'x', 'account_id': 7}, {'text': 'x', 'client_id': A},
                       {'text': 'x', 'expires_at': '2099-01-01'}, {'text': 'x', 'artifact_id': 'x'}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                self.dispatch('update_draft', {'draft_id': did, **fields})
        for fields in ({'caption': 'wrong kind'}, {'text': '   '}, {'text': '\x00'}):
            self.assertEqual(self.dispatch('update_draft', {'draft_id': did, **fields})['status'], 'unavailable')
            self.assertEqual(self.dispatch('get_draft', {'draft_id': did})['status'], 'pending')

    def test_caption_change_preserves_exact_artifact_and_tamper_cannot_refresh(self):
        source = self.root/'source'; source.write_bytes(b'bytes to preserve')
        artifact = self.store.store(source)
        prepared = self.dispatch('prepare_artifact_send', {'artifact_id': artifact.artifact_id,
            'recipient': 123, 'display_name': 'report.txt', 'mime_type': 'text/plain', 'caption': 'old'})
        revised = self.revise(prepared['draft_id'], caption='')
        self.assertEqual((revised['artifact_id'], revised['sha256'], revised['size_bytes'], revised['caption']),
                         (artifact.artifact_id, artifact.sha256, 17, ''))
        artifact.path.write_bytes(b'new bytes')
        self.assertEqual(self.dispatch('refresh_draft', {'draft_id': revised['draft_id']})['status'], 'unavailable')
        self.assertEqual(self.dispatch('get_draft', {'draft_id': revised['draft_id']})['status'], 'unavailable')

    def test_cancelled_claimed_and_terminal_drafts_cannot_be_revised(self):
        self.assertIn('refresh_draft', OPERATION_CAPABILITIES)
        cancelled = self.prepare(); self.dispatch('cancel_draft', {'draft_id': cancelled})
        self.assertEqual(self.dispatch('refresh_draft', {'draft_id': cancelled})['status'], 'unavailable')
        did = self.prepare('sent once')
        def send(recipient, text, *, attempt_id=None):
            self.assertEqual(self.dispatch('update_draft', {'draft_id': did, 'text': 'wrong'})['status'], 'unavailable')
            self.sender.sent.append((recipient, text)); return 701
        self.sender.send_text_message = send
        self.assertEqual(self.dispatch('send_prepared_text', {'draft_id': did, 'approved': True})['status'], 'sent')
        self.assertEqual(self.dispatch('refresh_draft', {'draft_id': did})['status'], 'unavailable')
        self.assertEqual(self.dispatch('send_prepared_text', {'draft_id': did, 'approved': True})['message_id'], 701)
        self.assertEqual(self.sender.sent, [(123, 'sent once')])

    def test_broker_uses_configured_ttl_and_disabled_send_denies_revisions(self):
        self.assertIn('refresh_draft', OPERATION_CAPABILITIES)
        from telegram_search_mcp.broker import Broker
        broker = Broker(socket_path=self.root/'other.sock', lock_path=self.root/'other.lock',
                        artifact_store=self.store, client_factory=lambda:self.sender,
                        policy=RuntimePolicy(enabled_capabilities=('send',), max_draft_ttl_seconds=3))
        self.addCleanup(broker.shutdown)
        self.broker = broker
        import time
        before = time.time(); did = self.prepare(); expiry = broker._drafts.peek(did, owner=OWNER).expires_at
        self.assertGreaterEqual(expiry, before+3); self.assertLess(expiry, before+4)
        broker._policy = RuntimePolicy(enabled_capabilities=())
        with self.assertRaises(CompatibilityError):self.dispatch('refresh_draft', {'draft_id': did})


class RegistryRevisionTests(unittest.TestCase):
    setUp = registry_fixture.OutgoingDraftTests.setUp
    prepare = registry_fixture.OutgoingDraftTests.prepare

    def revise(self, registry, did, **fields):
        self.assertTrue(hasattr(registry, 'revise'), 'registry must provide atomic revision transition')
        return registry.revise(did, owner=OWNER, recipient_title='Recipient', **fields)

    def test_renewal_is_finite_and_never_revives_an_expired_revision(self):
        old = self.registry.prepare_text(owner=OWNER, recipient=123, text='text')
        self.clock.now += 899
        new = self.revise(self.registry, old.draft_id)
        self.assertEqual(new.expires_at, 1700001799.0)
        self.clock.now = new.expires_at
        with self.assertRaises(DraftError):self.revise(self.registry, new.draft_id)

    def test_refresh_caps_original_artifact_deadline_even_after_mtime_extension(self):
        self.assertTrue(hasattr(self.registry, 'revise'))
        self.registry = OutgoingDraftRegistry(self.store, clock=self.clock, max_ttl_seconds=86400)
        old = self.prepare()
        self.assertEqual(old.expires_at, self.artifact.expires_at)
        self.clock.now += 100
        os.utime(self.artifact.path, (self.clock.now, self.clock.now))
        revised = self.revise(self.registry, old.draft_id)
        self.assertEqual(revised.expires_at, self.artifact.expires_at)
        self.clock.now = self.artifact.expires_at
        with self.assertRaises(DraftError):self.revise(self.registry, revised.draft_id)

    def test_configured_short_ttl_bounds_initial_and_revised_text(self):
        self.assertTrue(hasattr(self.registry, 'revise'))
        registry = OutgoingDraftRegistry(self.store, clock=self.clock, max_ttl_seconds=30)
        old = registry.prepare_text(owner=OWNER, recipient=123, text='text')
        self.assertEqual(old.expires_at, 1700000030.0)
        self.clock.now += 20
        revised = self.revise(registry, old.draft_id, text='new')
        self.assertEqual(revised.expires_at, 1700000050.0)

    def test_competing_revision_claim_or_cancel_has_only_one_winner(self):
        self.assertTrue(hasattr(self.registry, 'revise'))
        for competitor in ('revise', 'claim', 'cancel'):
            old = self.prepare(); start = threading.Barrier(3)
            def revise():
                start.wait()
                try:return self.registry.revise(old.draft_id, owner=OWNER, recipient_title='Recipient', caption='new')
                except DraftError:return None
            def compete():
                start.wait()
                try:
                    if competitor == 'revise':return self.registry.revise(old.draft_id, owner=OWNER, recipient_title='Recipient')
                    if competitor == 'claim':return self.registry.claim(old.draft_id, owner=OWNER, approved=True)
                    self.registry.cancel(old.draft_id, owner=OWNER); return True
                except DraftError:return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(revise); b=pool.submit(compete); start.wait(); results=[a.result(),b.result()]
            self.assertEqual(sum(result is not None for result in results), 1, competitor)

    def test_capacity_or_id_collision_preserves_original(self):
        self.assertTrue(hasattr(self.registry, 'revise'))
        limited = OutgoingDraftRegistry(self.store, clock=self.clock, capacity=1)
        old = limited.prepare_text(owner=OWNER, recipient=123, text='text')
        with self.assertRaises(DraftError):self.revise(limited, old.draft_id)
        self.assertEqual(limited.peek(old.draft_id, owner=OWNER), old)
        draft = self.prepare()
        import uuid
        with patch('telegram_search_mcp.outgoing_drafts.uuid.uuid4', return_value=uuid.UUID(draft.draft_id[6:])):
            with self.assertRaises(DraftError):self.revise(self.registry, draft.draft_id)
        self.assertEqual(self.registry.peek(draft.draft_id, owner=OWNER), draft)

    def test_artifact_hash_crossing_deadline_cannot_return_preview_or_claim(self):
        lookup = self.store.lookup
        for operation in ('peek', 'claim'):
            with self.subTest(operation=operation):
                old = self.prepare()
                def slow_lookup(artifact_id):
                    result = lookup(artifact_id)
                    self.clock.now = old.expires_at
                    return result
                with patch.object(self.store, 'lookup', side_effect=slow_lookup):
                    with self.assertRaises(DraftError):
                        if operation == 'peek':self.registry.peek(old.draft_id, owner=OWNER)
                        else:self.registry.claim(old.draft_id, owner=OWNER, approved=True)


class RevisionPolicyTests(unittest.TestCase):
    def test_maximum_ttl_is_strict_bounded_trusted_and_fingerprinted(self):
        self.assertIn('max_draft_ttl_seconds', RuntimePolicy.__dataclass_fields__)
        for value in (0, -1, 86401, True, '900', 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):RuntimePolicy(max_draft_ttl_seconds=value)
        default = contract_descriptor(RuntimePolicy())
        changed = contract_descriptor(RuntimePolicy(max_draft_ttl_seconds=30))
        with self.assertRaises(CompatibilityError):require_compatible(changed, default)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);root.chmod(0o700); path=root/'runtime.toml'
            path.write_text('config_version=1\nenabled_capabilities=["send"]\nmax_draft_ttl_seconds=30\n');path.chmod(0o600)
            policy=load_runtime_policy(path); self.assertEqual(policy.max_draft_ttl_seconds,30)
            path.write_text('config_version=1\nenabled_capabilities=["send"]\nmax_draft_ttl_seconds=31\n')
            with self.assertRaises(ConfigurationError):policy.require_current()
            path.write_text('config_version=1\nenabled_capabilities=[]\nmax_draft_ttl_seconds=true\n')
            with self.assertRaises(ConfigurationError):load_runtime_policy(path)


class RevisionProxyTests(unittest.TestCase):
    def test_proxy_rejects_unrelated_old_id_same_new_id_and_injected_content(self):
        from telegram_search_mcp.broker_client import BrokerClient
        from telegram_search_mcp.draft_models import UpdateDraftRequest, RefreshDraftRequest
        from test_draft_models import PREVIEW, DID
        new_id = 'draft_'+'b'*32
        good = {'status': 'revised', 'previous_draft_id': DID,
                'draft': {**PREVIEW, 'draft_id': new_id},
                'detail': 'new immutable revision; prior draft invalidated; fresh approval required'}
        proxy = BrokerClient(socket_path=Path('/unused-synthetic.sock'), policy=RuntimePolicy(), restart_callback=lambda:None)
        self.addCleanup(proxy.close)
        for method, request in [(proxy.update_draft, UpdateDraftRequest(draft_id=DID, text='exact')),
                                (proxy.refresh_draft, RefreshDraftRequest(draft_id=DID))]:
            with patch.object(proxy, '_request', return_value=good):
                self.assertEqual(method(request).draft.draft_id, new_id)
            for bad in ({**good, 'previous_draft_id': UNKNOWN}, {**good, 'draft': PREVIEW},
                        {**good, 'status': 'unavailable'}, {**good, 'draft': None},
                        {**good, 'draft': {**good['draft'], 'artifact_path': '/private/secret'}},
                        {**good, 'draft': {**good['draft'], 'approval_required': False}}):
                with self.subTest(bad=bad), patch.object(proxy, '_request', return_value=bad):
                    result = method(request).model_dump(mode='json')
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertIsNone(result['draft']); self.assertIsNone(result['previous_draft_id'])

    def test_sdk_closed_schema_rejects_extra_and_invalid_fields_before_service(self):
        import asyncio
        from mcp import Client
        from telegram_search_mcp.server import build_server
        reached = []
        class RejectUnexpectedService:
            def update_draft(self, request):
                reached.append(request); raise AssertionError('invalid request reached service')
            def refresh_draft(self, request):
                reached.append(request); raise AssertionError('invalid request reached service')
        server = build_server(service_factory=RejectUnexpectedService, policy=RuntimePolicy())
        async def exercise():
            async with Client(server) as client:
                tools = {t.name:t for t in (await client.list_tools()).tools}
                for name in ('update_draft','refresh_draft'):
                    self.assertFalse(tools[name].input_schema['additionalProperties'])
                for name, payload in [('update_draft', {'draft_id':UNKNOWN}),
                    ('update_draft', {'draft_id':UNKNOWN, 'text':'x', 'caption':'y'}),
                    ('update_draft', {'draft_id':UNKNOWN, 'text':'x', 'owner':A}),
                    ('update_draft', {'draft_id':UNKNOWN, 'caption':42}),
                    ('refresh_draft', {'draft_id':UNKNOWN, 'approved':True})]:
                    result = await client.call_tool(name, payload)
                    self.assertTrue(result.is_error)
                    self.assertEqual(reached, [], 'invalid input must fail before service invocation')
        asyncio.run(exercise())
