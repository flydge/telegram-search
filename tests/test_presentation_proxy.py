"""Public proxy rejects hostile wire scope and concurrent lifecycle drift."""
from __future__ import annotations
import copy
import importlib
import threading
import tempfile
import os
import time
import unittest
from pathlib import Path
from telegram_search_mcp.broker_client import BrokerClient, BrokerCompatibilityError
from telegram_search_mcp.config import RuntimePolicy, load_runtime_policy
from telegram_search_mcp.contract import contract_descriptor

GEN = 'broker_'+'a'*32
ART = 'artifact_'+'0'*32+'_'+'b'*64+'_10'


def response():
    return dict(contract_version=1, status='complete', scope=dict(
        artifact_id=ART, artifact_sha256='b'*64, artifact_bytes=10,
        extraction_fingerprint='c'*64, extractor_version=1, broker_generation=GEN,
        source_anchor=None, catalog=[dict(index=1,hidden=False,has_notes=False),
                                   dict(index=2,hidden=True,has_notes=True)],
        slides=[2,1], include_notes=True),
        slides=[dict(index=2,text='Second 🙂',notes='Private speaker note',unsupported_objects=[]),
                dict(index=1,text='First',notes=None,unsupported_objects=[])],
        selection_complete=True,full_content_complete=False,detail='supported selected text only')


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(BrokerClient,'read_presentation'), 'presentation proxy missing')
        self.m = importlib.import_module('telegram_search_mcp.presentation_models')
        self.policy = RuntimePolicy(enabled_capabilities=('presentations',))
        self.calls=[]; self.raw=response(); owner=self
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                owner.calls.append((op,payload))
                self._presentation_operation_state.generation=GEN
                self._presentation_generation=GEN
                return copy.deepcopy(owner.raw)
        self.proxy=Proxy(socket_path=Path('/unused'),policy=self.policy)

    def req(self):
        return self.m.ReadPresentationRequest(artifact_id=ART,slides=[2,1],include_notes=True)

    def test_selected_order_and_note_opt_in(self):
        got=self.proxy.read_presentation(self.req())
        self.assertEqual(got.status,'complete')
        self.assertEqual([(s.index,s.text,s.notes) for s in got.slides],
                         [(2,'Second 🙂','Private speaker note'),(1,'First',None)])
        self.assertFalse(got.full_content_complete)
        self.assertEqual(self.calls[0][1],dict(artifact_id=ART,slides=[2,1],include_notes=True))

    def test_disabled_closed_invalid_request_stop_before_dispatch(self):
        self.proxy._policy=RuntimePolicy()
        with self.assertRaises(BrokerCompatibilityError): self.proxy.read_presentation(self.req())
        self.assertEqual(self.calls,[])
        self.proxy._policy=self.policy
        bad=self.req().model_copy(update={'slides':[True]})
        self.assertEqual(self.proxy.read_presentation(bad).status,'invalid_selection')
        self.assertEqual(self.calls,[])
        self.proxy.close(); self.calls.clear()
        self.assertEqual(self.proxy.read_presentation(self.req()).status,'expired')
        self.assertEqual(self.calls,[])

    def test_hostile_wire_returns_no_content(self):
        for case in ('artifact','selection','generation','notes_mode','note_missing','note_leak',
                     'catalog','hidden_integer','failure_content','contract_bool','extractor_bool',
                     'selected_bool','complete_claim','order','budget','extra','duplicate_object'):
            raw=response()
            if case=='artifact': raw['scope'].update(artifact_id='artifact_'+'0'*32+'_'+'d'*64+'_10',artifact_sha256='d'*64)
            if case=='selection':raw['scope']['slides']=[1,2];raw['slides'].reverse()
            if case=='generation':raw['scope']['broker_generation']='broker_'+'d'*32
            if case=='notes_mode':raw['scope']['include_notes']=False;raw['slides'][0]['notes']=None
            if case=='note_missing':raw['slides'][0]['notes']=None
            if case=='note_leak':raw['slides'][1]['notes']='unexpected'
            if case=='catalog':raw['scope']['catalog'][1]['index']=1
            if case=='hidden_integer':raw['scope']['catalog'][1]['hidden']=1
            if case=='failure_content':raw['status']='error'
            if case=='contract_bool':raw['contract_version']=True
            if case=='extractor_bool':raw['scope']['extractor_version']=True
            if case=='selected_bool':raw['selection_complete']=1
            if case=='complete_claim':raw['full_content_complete']=True
            if case=='order':raw['slides'].reverse()
            if case=='budget':raw['slides'][0]['text']='x'*20000
            if case=='extra':raw['slides'][0]['path']='/private/not-public'
            if case=='duplicate_object':raw['slides'][0]['unsupported_objects']=[dict(source='slide',kind='picture',count=1)]*2
            self.raw=raw
            with self.subTest(case=case):
                bad=self.proxy.read_presentation(self.req())
                self.assertEqual(bad.status,'error');self.assertIsNone(bad.scope);self.assertEqual(bad.slides,[])

    def test_catalog_cannot_disclose_text_and_requires_catalog_request(self):
        raw=response();raw.update(status='catalog',slides=[]);raw['scope']['slides']=None
        self.raw=raw
        self.assertEqual(self.proxy.read_presentation(self.req()).status,'error')
        got=self.proxy.read_presentation(self.req().model_copy(update={'slides':None}))
        self.assertEqual(got.status,'catalog');self.assertEqual(got.slides,[])

    def test_four_active_calls_and_inflight_close_or_generation_drift(self):
        entered=threading.Condition();release=threading.Event();count=[0];got=[]
        def paused(op,payload):
            if op=='release_client':return {'released':True}
            with entered:count[0]+=1;entered.notify_all()
            release.wait(3)
            self.proxy._presentation_operation_state.generation=GEN
            return response()
        self.proxy._presentation_generation=GEN;self.proxy._request=paused
        workers=[threading.Thread(target=lambda:got.append(self.proxy.read_presentation(self.req()))) for _ in range(4)]
        try:
            for worker in workers:worker.start()
            with entered:self.assertTrue(entered.wait_for(lambda:count[0]==4,2))
            self.assertEqual(self.proxy.read_presentation(self.req()).status,'capacity_exhausted')
            self.assertEqual(count[0],4)
            self.proxy.close()
        finally:
            release.set()
            for worker in workers:worker.join(3)
        self.assertEqual([r.status for r in got],['expired']*4)
        self.assertTrue(all(r.scope is None and not r.slides for r in got))

    def test_new_handshake_during_inflight_call_drops_old_generation(self):
        def changed(op,payload):
            self.proxy._presentation_operation_state.generation=GEN
            self.proxy._presentation_generation='broker_'+'d'*32
            return response()
        self.proxy._request=changed
        got=self.proxy.read_presentation(self.req())
        self.assertEqual(got.status,'expired');self.assertEqual(got.slides,[])

    def test_wire_deadline_is_bounded_before_waiting_for_broker(self):
        class Connection:
            def __enter__(self): return self
            def __exit__(self,*args): pass
        proxy=BrokerClient(socket_path=Path('/unused'),policy=self.policy,connector=lambda _:Connection())
        deadlines=[]
        def exchange(connection,operation,payload,*,deadline,generation):
            deadlines.append(deadline-time.monotonic())
            return dict(contract_descriptor(self.policy),broker_generation=GEN) if operation=='handshake' else response()
        proxy._exchange=exchange
        self.assertEqual(proxy.read_presentation(self.req()).status,'complete')
        self.assertEqual(len(deadlines),2)
        self.assertTrue(all(0 < d <= 30 for d in deadlines),deadlines)

    def test_freeform_wire_detail_cannot_disclose_unrequested_body(self):
        for status in ('complete','error'):
            raw=response() if status=='complete' else {'status':'error'}
            raw['detail']='SYNTHETIC_UNREQUESTED_NOTES'
            self.raw=raw
            got=self.proxy.read_presentation(self.req())
            self.assertNotIn('SYNTHETIC_UNREQUESTED_NOTES',got.model_dump_json())

    def test_policy_revocation_during_request_prevents_returning_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'runtime.toml'
            os.chmod(directory,0o700)
            path.write_text('config_version = 1\nenabled_capabilities = ["presentations"]\n')
            path.chmod(0o600)
            self.proxy._policy=load_runtime_policy(path)
            def revoke(op,payload):
                path.write_text('config_version = 1\nenabled_capabilities = []\n')
                self.proxy._presentation_operation_state.generation=GEN
                self.proxy._presentation_generation=GEN
                return response()
            self.proxy._request=revoke
            got=self.proxy.read_presentation(self.req())
            self.assertEqual(got.slides,[]);self.assertIsNone(got.scope)


if __name__=='__main__':unittest.main()
