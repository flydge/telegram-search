"""Boundary risks: weakening any validated field or post-read guard breaks these tests."""
from __future__ import annotations
import copy
import importlib
import os
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pydantic import ValidationError
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.tdjson import AuthorizationBlocked

HASH = 'a' * 64
ARTIFACT = 'artifact_' + 'b' * 32 + '_' + HASH + '_25'
GENERATION = 'broker_' + 'c' * 32
CATALOG = [dict(index=1, hidden=False, has_notes=False), dict(index=2, hidden=True, has_notes=True)]


def scope():
    return dict(artifact_id=ARTIFACT, artifact_sha256=HASH, artifact_bytes=25,
                extraction_fingerprint='d' * 64, extractor_version=1, broker_generation=GENERATION,
                source_anchor=None, catalog=copy.deepcopy(CATALOG), slides=[2, 1], include_notes=True)


def payload():
    return dict(contract_version=1, status='complete', scope=scope(),
                slides=[dict(index=2, text='é🙂', notes='private notes', unsupported_objects=[dict(source='notes', kind='field', count=1)]),
                        dict(index=1, text='public', notes=None, unsupported_objects=[])],
                selection_complete=True, full_content_complete=False,
                detail='supported shape text and opt-in notes; layout, master text, formatting, OCR and other objects omitted')


class PresentationModelsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.presentation_models'), 'presentation contract missing')
        self.m = importlib.import_module('telegram_search_mcp.presentation_models')

    def test_strict_selection_rejects_duplicates_bool_coercion_extra_and_excess(self):
        for changes in ({'slides': []}, {'slides': [1, 1]}, {'slides': [True]}, {'slides': ['1']},
                        {'slides': [0]}, {'slides': [129]}, {'slides': list(range(1, 7))}, {'slides': (1,)},
                        {'include_notes': 1}, {'include_notes': 'false'}, {'cursor': 'unused'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.m.ReadPresentationRequest(artifact_id=ARTIFACT, **changes)
        request = self.m.ReadPresentationRequest(artifact_id=ARTIFACT, slides=[2, 1])
        self.assertEqual(request.slides, [2, 1]); self.assertFalse(request.include_notes)

    def test_success_preserves_order_unicode_hidden_and_note_omission(self):
        result = self.m.ReadPresentationResponse.model_validate(payload())
        self.assertEqual([s.index for s in result.slides], [2, 1])
        self.assertEqual(result.slides[0].text, 'é🙂'); self.assertTrue(result.scope.catalog[1].hidden)
        self.assertIsNone(result.slides[1].notes); self.assertFalse(result.full_content_complete)
        data = payload(); data['scope']['include_notes'] = False; data['slides'][0]['notes'] = None
        data['slides'][0]['unsupported_objects'] = []
        self.assertIsNone(self.m.ReadPresentationResponse.model_validate(data).slides[0].notes)

    def test_rejects_scope_mismatch_note_leaks_and_invented_coverage(self):
        mutations = [lambda d: d['scope'].update(artifact_bytes=24), lambda d: d['scope'].update(artifact_sha256='e'*64),
                     lambda d: d['scope']['catalog'][0].update(index=2), lambda d: d['scope'].update(slides=[1,2]),
                     lambda d: d['scope'].update(slides=[3]), lambda d: d['scope'].update(slides=[2,2]),
                     lambda d: d['scope'].update(include_notes=False), lambda d: d['slides'][1].update(notes='leak'),
                     lambda d: d['slides'][0].update(notes=None), lambda d: d.update(selection_complete=False),
                     lambda d: d.update(full_content_complete=True), lambda d: d.update(full_content_complete=0),
                     lambda d: d.update(contract_version=True), lambda d: d['scope'].update(extractor_version=True),
                     lambda d: d['scope']['catalog'][0].update(title='unselected leak'), lambda d: d['slides'][0].update(extra='leak')]
        for mutate in mutations:
            data = payload(); mutate(data)
            with self.subTest(data=data), self.assertRaises(ValidationError):
                self.m.ReadPresentationResponse.model_validate(data)

    def test_catalog_is_metadata_only_and_failure_cannot_carry_content(self):
        data = payload(); data.update(status='catalog', slides=[]); data['scope']['slides'] = None
        self.assertEqual(self.m.ReadPresentationResponse.model_validate(data).slides, [])
        for mutate in (lambda d: d.update(slides=payload()['slides']), lambda d: d['scope'].update(slides=[2])):
            bad = copy.deepcopy(data); mutate(bad)
            with self.assertRaises(ValidationError): self.m.ReadPresentationResponse.model_validate(bad)
        for status in ('unsupported','invalid_selection','expired','limit_reached','capacity_exhausted','blocked','error'):
            failure = self.m.ReadPresentationResponse(status=status)
            self.assertIsNone(failure.scope); self.assertEqual(failure.slides, []); self.assertFalse(failure.selection_complete)
            bad = payload(); bad.update(status=status)
            with self.assertRaises(ValidationError): self.m.ReadPresentationResponse.model_validate(bad)

    def test_unsupported_objects_strict_unique_sorted_and_notes_opt_in(self):
        invalid = [[dict(source='slide', kind='picture', count=True)], [dict(source='slide',kind='picture',count=0)],
                   [dict(source='slide',kind='picture',count=200001)], [dict(source='slide',kind='unknown',count=1)],
                   [dict(source='slide',kind='picture',count=1)] * 2,
                   [dict(source='slide',kind='table',count=1),dict(source='slide',kind='chart',count=1)]]
        for objects in invalid:
            data=payload();data['slides'][0]['unsupported_objects']=objects
            with self.subTest(objects=objects), self.assertRaises(ValidationError): self.m.ReadPresentationResponse.model_validate(data)
        data=payload();data['scope']['include_notes']=False;data['slides'][0]['notes']=None
        with self.assertRaises(ValidationError): self.m.ReadPresentationResponse.model_validate(data)
        data=payload();data['slides'][1]['unsupported_objects']=[dict(source='notes',kind='picture',count=1)]
        with self.assertRaises(ValidationError): self.m.ReadPresentationResponse.model_validate(data)

    def test_total_string_budget_includes_notes_and_object_labels(self):
        data=payload();data['slides'][0].update(text='x'*9990,notes='y'*9994);data['slides'][1]['text']=''
        # notes+field adds 10, so 19984+10 is admitted; one more than 20000 is rejected.
        self.m.ReadPresentationResponse.model_validate(data)
        data['slides'][0]['notes']='y'*10001
        with self.assertRaises(ValidationError): self.m.ReadPresentationResponse.model_validate(data)


class Provider:
    def __init__(self): self.account=17; self.denied=False; self.deadlines=[]
    def ensure_ready(self):
        if self.denied: raise AuthorizationBlocked('blocked')
    def get_account_id(self): return self.account
    def request_budget(self, deadline): self.deadlines.append(deadline); return nullcontext()


class PresentationReaderTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.presentations'), 'presentation reader missing')
        self.m=importlib.import_module('telegram_search_mcp.presentation_models'); self.r=importlib.import_module('telegram_search_mcp.presentations')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.wall=datetime.now(timezone.utc);self.now=100.
        self.store=ArtifactStore(cache_dir=self.base/'cache',clock=lambda:self.wall.timestamp())
        self.path=self.base/'slides.pptx';self.path.write_bytes(b'parser-independent artifact')
        self.artifact=self.store.store(self.path)
        self.metadata={self.artifact.artifact_id:(None,'slides.pptx','application/vnd.openxmlformats-officedocument.presentationml.presentation','document')}
        self.provider=Provider();self.reader=self.make_reader();self.parse_calls=[]
        self.parser=patch.object(self.r,'read_pptx',side_effect=self.result);self.parser.start();self.addCleanup(self.parser.stop)

    def make_reader(self):
        return self.r.PresentationReader(client=self.provider,store=self.store,metadata_lookup=self.metadata.get,
                    client_id='client_'+'a'*24,broker_generation=GENERATION,clock=lambda:self.now,wall_clock=lambda:self.wall)
    def req(self, slides=[2,1], **kwargs):
        return self.m.ReadPresentationRequest(artifact_id=self.artifact.artifact_id,slides=slides,**kwargs)
    def result(self,*args,**kwargs):
        self.parse_calls.append(kwargs)
        if kwargs['slides'] is None: return SimpleNamespace(status='catalog',catalog=tuple(CATALOG),slides=(),detail='untrusted worker detail')
        returned=[dict(index=i,text='é🙂' if i==2 else 'public',notes='notes' if kwargs['include_notes'] and i==2 else None,
                       unsupported_objects=[dict(source='slide',kind='picture',count=1)] if i==2 else []) for i in kwargs['slides'] if i in (1,2)]
        return SimpleNamespace(status='complete',catalog=tuple(CATALOG),slides=tuple(returned),detail='untrusted worker detail')
    def assert_empty(self,result,status=None):
        if status: self.assertEqual(result.status,status)
        else:self.assertNotIn(result.status,('catalog','complete'))
        self.assertEqual(result.slides,[]);self.assertIsNone(result.scope);self.assertFalse(result.selection_complete)

    def test_selected_order_notes_and_safe_coverage_detail(self):
        result=self.reader.read(self.req(include_notes=True));self.assertEqual(result.status,'complete')
        self.assertEqual([s.index for s in result.slides],[2,1]);self.assertEqual(result.slides[0].text,'é🙂')
        self.assertEqual(result.slides[0].notes,'notes');self.assertEqual(result.scope.artifact_id,self.artifact.artifact_id)
        self.assertEqual(result.scope.broker_generation,GENERATION);self.assertEqual(result.scope.slides,[2,1])
        self.assertIn('omitted',result.detail);self.assertIn('untrusted',result.detail);self.assertNotIn('worker detail',result.detail)
        result=self.reader.read(self.req());self.assertIsNone(result.slides[0].notes)

    def test_catalog_notes_flag_has_no_content_or_extraction_effect(self):
        result=self.reader.read(self.req(None,include_notes=True));self.assertEqual(result.status,'catalog')
        self.assertEqual(result.slides,[]);self.assertIsNone(result.scope.slides);self.assertTrue(result.selection_complete)
        self.assertIsNone(self.parse_calls[-1]['slides']);self.assertFalse(self.parse_calls[-1]['include_notes'])

    def test_hostile_parser_scope_note_content_and_budget_fail_empty(self):
        for mode in ('order','extra','catalog_leak','notes','missing_notes','catalog','boolean','budget','object_budget','invalid'):
            def hostile(*a,**kw):
                result=self.result(*a,**kw);data=[dict(s) for s in result.slides]
                if mode=='order':data.reverse()
                elif mode=='extra':data[0]['extra']='private'
                elif mode=='catalog_leak':result.status='catalog'
                elif mode=='notes':data[0]['notes']='private'
                elif mode=='missing_notes':data[0]['notes']=None
                elif mode=='catalog':result.catalog=({'index':2,'hidden':True,'has_notes':True},)
                elif mode=='boolean':data[0]['index']=True
                elif mode=='budget':data[0]['text']='x'*20001
                elif mode=='object_budget':data[0]['text']='x'*19990;data[1]['text']='';data[0]['unsupported_objects']=[dict(source='slide',kind='picture',count=1)]
                else:result.status='invalid_selection'
                result.slides=tuple(data);return result
            with self.subTest(mode=mode),patch.object(self.r,'read_pptx',side_effect=hostile):
                self.assert_empty(self.reader.read(self.req(include_notes=mode=='missing_notes')), 'limit_reached' if mode in ('budget','object_budget') else None)

    def test_invalid_model_copy_does_not_dispatch_and_missing_metadata_artifact_expire(self):
        before=len(self.parse_calls)
        self.assert_empty(self.reader.read(self.req().model_copy(update={'slides':[True]})),'error')
        self.assertEqual(len(self.parse_calls),before)
        self.metadata.clear();self.assert_empty(self.reader.read(self.req()),'expired')
        self.metadata[self.artifact.artifact_id]=(None,'slides.pptx',None,'document');self.artifact.path.unlink()
        self.assert_empty(self.reader.read(self.req()),'expired')

    def test_unauthorized_and_expired_catalog_are_empty(self):
        self.provider.denied=True;self.assert_empty(self.reader.read(self.req(None)),'blocked');self.provider.denied=False
        self.wall+=timedelta(seconds=43201);self.assert_empty(self.reader.read(self.req(None)),'expired')

    def test_inflight_close_generation_account_metadata_retention_and_bytes_drop_result(self):
        for mode in ('close','generation','account','metadata','retention','bytes','wall_expiry','mono_expiry','denied'):
            reader=self.make_reader();self.provider.account=17;self.provider.denied=False
            self.wall=datetime.now(timezone.utc);self.now=100.;self.artifact=self.store.store(self.path)
            self.metadata[self.artifact.artifact_id]=(None,'slides.pptx',None,'document')
            def inflight(*a,**kw):
                result=self.result(*a,**kw)
                if mode=='close':reader.close()
                elif mode=='generation':reader._generation='broker_'+'f'*32
                elif mode=='account':self.provider.account=18
                elif mode=='metadata':self.metadata[self.artifact.artifact_id]=(None,'changed.pptx',None,'document')
                elif mode=='retention':os.utime(self.artifact.path,(self.wall.timestamp()-1,self.wall.timestamp()-1))
                elif mode=='bytes':self.artifact.path.write_bytes(b'changed')
                elif mode=='wall_expiry':self.wall+=timedelta(seconds=43201)
                elif mode=='mono_expiry':self.now+=43201
                else:self.provider.denied=True
                return result
            with self.subTest(mode=mode),patch.object(self.r,'read_pptx',side_effect=inflight):self.assert_empty(reader.read(self.req()))

    def test_lookup_cannot_substitute_other_valid_artifact(self):
        other_path=self.base/'other.pptx';other_path.write_bytes(b'other content')
        other=self.store.store(other_path)
        with patch.object(self.store,'lookup',return_value=other):
            self.assert_empty(self.reader.read(self.req()))

    def test_invalid_account_deadlines_and_parser_failure_never_disclose(self):
        for account in (True, 0, 2**53, '17'):
            self.provider.account=account
            self.assert_empty(self.reader.read(self.req()),'error')
        self.provider.account=17
        for deadline in (True,float('nan'),float('inf'),'future'):
            self.assert_empty(self.reader.read(self.req(),deadline=deadline),'error')
        for status in ('unsupported','invalid_selection','limit_reached','error','invented'):
            result=SimpleNamespace(status=status,catalog=tuple(CATALOG),slides=tuple(payload()['slides']),detail='secret')
            with patch.object(self.r,'read_pptx',return_value=result):
                self.assert_empty(self.reader.read(self.req()),status if status!='invented' else 'error')
        for error in (OSError('secret'),TimeoutError('secret'),ValueError('secret')):
            with patch.object(self.r,'read_pptx',side_effect=error):self.assert_empty(self.reader.read(self.req()),'error')

    def test_four_active_calls_reject_fifth_then_release_capacity(self):
        entered=threading.Condition();release=threading.Event();count=0;results=[]
        def paused(*a,**kw):
            nonlocal count
            with entered:count+=1;entered.notify_all()
            release.wait(3);return self.result(*a,**kw)
        with patch.object(self.r,'read_pptx',side_effect=paused):
            workers=[threading.Thread(target=lambda:results.append(self.reader.read(self.req()))) for _ in range(4)]
            try:
                for worker in workers:worker.start()
                with entered:self.assertTrue(entered.wait_for(lambda:count==4,2))
                self.assert_empty(self.reader.read(self.req()),'capacity_exhausted')
            finally:
                release.set()
                for worker in workers:worker.join(3)
        self.assertEqual(len(results),4);self.assertTrue(all(result.status=='complete' for result in results))
        self.assertEqual(self.reader.read(self.req()).status,'complete')

    def test_deadline_caps_request_and_parser_and_drops_late_content(self):
        before=time.monotonic();result=self.reader.read(self.req(),deadline=before+600)
        self.assertEqual(result.status,'complete');self.assertLessEqual(self.provider.deadlines[-1],before+30.01)
        self.assertGreater(self.parse_calls[-1]['timeout'],0);self.assertLessEqual(self.parse_calls[-1]['timeout'],15)
        before=len(self.parse_calls);self.assert_empty(self.reader.read(self.req(),deadline=time.monotonic()-1),'limit_reached')
        self.assertEqual(len(self.parse_calls),before)
        def late(*a,**kw):
            result=self.result(*a,**kw);time.sleep(.02);return result
        with patch.object(self.r,'read_pptx',side_effect=late):self.assert_empty(self.reader.read(self.req(),deadline=time.monotonic()+.01),'limit_reached')

if __name__=='__main__':unittest.main()
