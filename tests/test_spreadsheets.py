from __future__ import annotations
import importlib
import os
import tempfile
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from telegram_search_mcp.artifact_store import ArtifactStore

class Provider:
    account=17
    def __init__(self):self.calls=[]
    def ensure_ready(self):self.calls.append('ready')
    def get_account_id(self):self.calls.append('account');return self.account
    def request_budget(self,deadline):return nullcontext()

class SpreadsheetTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.spreadsheets'),'F9 spreadsheet reader missing')
        self.m=importlib.import_module('telegram_search_mcp.spreadsheet_models')
        self.r=importlib.import_module('telegram_search_mcp.spreadsheets')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.now=100.;self.wall=datetime.now(timezone.utc)
        self.store=ArtifactStore(cache_dir=self.base/'cache',clock=lambda:self.wall.timestamp())
        p=self.base/'book.xlsx';p.write_bytes(b'parser-independent artifact');self.artifact=self.store.store(p)
        self.metadata={self.artifact.artifact_id:(None,'book.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','document')}
        self.provider=Provider();self.reader=self.make_reader()
        self.parse_calls=[];self.parse=patch.object(self.r,'read_xlsx',side_effect=self.result);self.parse.start();self.addCleanup(self.parse.stop)
    def make_reader(self,client_id='client_'+'a'*24):
        return self.r.SpreadsheetReader(client=self.provider,store=self.store,metadata_lookup=self.metadata.get,client_id=client_id,
            broker_generation='broker_'+'b'*32,clock=lambda:self.now,wall_clock=lambda:self.wall)
    def req(self,**kw):
        return self.m.ReadSpreadsheetRequest(artifact_id=self.artifact.artifact_id,selections=[{'sheet_index':2,'range':'A1:B2'}],max_cells=2,**kw)
    def result(self,*args,**kw):
        self.parse_calls.append(kw)
        catalog=({'index':1,'name':'Public','state':'visible'},{'index':2,'name':'Secret','state':'veryHidden'})
        if kw['selections'] is None:return SimpleNamespace(status='catalog',catalog=catalog,cells=(),cell_start=0,cell_end=0,total_cells=0,has_more=False,detail='catalog')
        cells=[dict(selection_index=1,address=a,row=r,column=c,value_type=t,value=v,formula=f,formula_kind='normal' if f is not None else None,formula_ref=None,formula_shared_index=None)
            for a,r,c,t,v,f in [('A1',1,1,'s','first',None),('B1',1,2,'blank',None,None),('A2',2,1,'n',None,'A1+1'),('B2',2,2,'b','1',None)]]
        start=kw['offset'];end=min(start+kw['max_cells'],4)
        return SimpleNamespace(status='page' if end<4 else 'complete',catalog=catalog,cells=tuple(cells[start:end]),cell_start=start,cell_end=end,total_cells=4,has_more=end<4,detail='selected')
    def test_exact_selected_scope_blank_and_formula_reassembly_single_use(self):
        first=self.reader.read(self.req());self.assertEqual(first.status,'page');self.assertEqual([c.address for c in first.cells],['A1','B1'])
        self.assertEqual(first.cells[1].value_type,'blank');self.assertEqual(first.scope.total_cells,4)
        last=self.reader.read(self.req(cursor=first.next_cursor));self.assertEqual(last.status,'complete');self.assertEqual([c.address for c in last.cells],['A2','B2'])
        self.assertIsNone(last.cells[0].value);self.assertEqual(last.cells[0].formula,'A1+1');self.assertEqual(last.scope,first.scope)
        before=len(self.provider.calls);bad=self.reader.read(self.req(cursor=first.next_cursor));self.assertEqual(bad.status,'invalid_cursor');self.assertEqual(bad.cells,[]);self.assertEqual(len(self.provider.calls),before)
    def test_catalog_only_never_issues_token_or_returns_values(self):
        result=self.reader.read(self.req().model_copy(update={'selections':None}));self.assertEqual(result.status,'catalog');self.assertEqual(len(result.scope.catalog),2)
        self.assertEqual(result.cells,[]);self.assertIsNone(result.next_cursor);self.assertEqual(self.parse_calls[-1]['selections'],None)
    def test_changed_foreign_closed_expired_cursor_never_reads_provider(self):
        first=self.reader.read(self.req());before=len(self.provider.calls)
        changed=self.req(cursor=first.next_cursor).model_copy(update={'max_cells':1})
        self.assertEqual(self.reader.read(changed).status,'invalid_cursor')
        self.assertEqual(self.make_reader('client_'+'c'*24).read(self.req(cursor=first.next_cursor)).status,'invalid_cursor')
        self.now+=301;self.assertEqual(self.reader.read(self.req(cursor=first.next_cursor)).status,'invalid_cursor')
        self.reader.close();self.assertEqual(self.reader.read(self.req()).status,'invalid_cursor');self.assertEqual(len(self.provider.calls),before)
    def test_account_metadata_and_artifact_drift_return_empty(self):
        for mode in ('account','metadata','artifact'):
            reader=self.make_reader();first=reader.read(self.req())
            if mode=='account':self.provider.account+=1
            elif mode=='metadata':self.metadata[self.artifact.artifact_id]=(None,'other.xlsx','other','document')
            else:self.artifact.path.write_bytes(b'changed')
            bad=reader.read(self.req(cursor=first.next_cursor));self.assertNotIn(bad.status,('page','complete'));self.assertEqual(bad.cells,[]);self.assertIsNone(bad.scope)
    def test_inflight_close_expiry_account_metadata_artifact_drop_whole_result(self):
        initial_wall=self.wall
        for mode in ('close','expiry','generation','account','metadata','artifact'):
            self.now=100.;self.wall=initial_wall
            self.artifact.path.write_bytes(b'parser-independent artifact')
            os.utime(self.artifact.path,(initial_wall.timestamp(),initial_wall.timestamp()))
            self.metadata[self.artifact.artifact_id]=(None,'book.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','document')
            reader=self.make_reader()
            def parse(*args,**kwargs):
                result=self.result(*args,**kwargs)
                if mode=='close':reader.close()
                elif mode=='expiry':self.now+=301
                elif mode=='generation':reader._generation='broker_'+'c'*32
                elif mode=='account':self.provider.account+=1
                elif mode=='metadata':self.metadata[self.artifact.artifact_id]=(None,'changed.xlsx','changed','document')
                else:self.artifact.path.write_bytes(b'changed')
                return result
            with self.subTest(mode=mode),patch.object(self.r,'read_xlsx',side_effect=parse):
                bad=reader.read(self.req());self.assertNotIn(bad.status,('page','complete','catalog'));self.assertEqual(bad.cells,[]);self.assertIsNone(bad.scope)
    def test_fixed_ttl_does_not_renew(self):
        first=self.reader.read(self.req());self.now+=299;self.wall+=timedelta(seconds=299)
        last=self.reader.read(self.req(cursor=first.next_cursor));self.assertEqual(last.scope.expires_at,first.scope.expires_at)
    def test_sixteen_live_and_pending_capacity_never_dispatches_excess(self):
        for _ in range(15):self.assertEqual(self.reader.read(self.req()).status,'page')
        entered=threading.Event();release=threading.Event();results=[]
        def paused(*a,**k):entered.set();release.wait(3);return self.result(*a,**k)
        with patch.object(self.r,'read_xlsx',side_effect=paused):
            worker=threading.Thread(target=lambda:results.append(self.reader.read(self.req())));worker.start()
            try:
                self.assertTrue(entered.wait(2));before=len(self.provider.calls)
                self.assertEqual(self.reader.read(self.req()).status,'capacity_exhausted');self.assertEqual(len(self.provider.calls),before)
            finally:release.set();worker.join(3)
        self.assertEqual(results[0].status,'page')
    def test_four_active_calls_and_fixed_256_call_ceiling(self):
        entered=threading.Condition();release=threading.Event();count=[0];results=[]
        def paused(*a,**k):
            with entered:count[0]+=1;entered.notify_all()
            release.wait(3);return self.result(*a,**k)
        with patch.object(self.r,'read_xlsx',side_effect=paused):
            workers=[threading.Thread(target=lambda:results.append(self.reader.read(self.req()))) for _ in range(4)]
            try:
                for w in workers:w.start()
                with entered:self.assertTrue(entered.wait_for(lambda:count[0]==4,2))
                self.assertEqual(self.reader.read(self.req()).status,'capacity_exhausted')
            finally:
                release.set()
                for w in workers:w.join(3)
        self.assertTrue(all(r.status=='page' for r in results))
        def one(*a,**k):
            offset=k['offset'];cell=dict(selection_index=1,address='A'+str(offset+1),row=offset+1,column=1,value_type='blank',value=None,formula=None,formula_kind=None,formula_ref=None,formula_shared_index=None)
            return SimpleNamespace(status='page',catalog=({'index':1,'name':'Public','state':'visible'},),cells=(cell,),cell_start=offset,cell_end=offset+1,total_cells=1000,has_more=True,detail='bounded')
        request=self.req().model_copy(update={'selections':[self.m.SheetRange(sheet_index=1,range='A1:A1000')],'max_cells':1})
        reader=self.make_reader()
        with patch.object(self.r,'read_xlsx',side_effect=one):
            for _ in range(255):
                result=reader.read(request);self.assertEqual(result.status,'page');request=request.model_copy(update={'cursor':result.next_cursor})
            stopped=reader.read(request);self.assertEqual(stopped.status,'limit_reached');self.assertEqual(stopped.cells,[])
    def test_registry_retains_metadata_without_cell_bodies(self):
        first=self.reader.read(self.req());state=self.reader._states[first.next_cursor]
        self.assertNotIn('first',repr(state));self.assertNotIn('cells',vars(state))

    def test_artifact_retention_shortens_lifetime_and_never_extends(self):
        created=self.wall.timestamp()
        os.utime(self.artifact.path,(created-43198,created-43198))
        first=self.reader.read(self.req());self.assertEqual(first.status,'page')
        self.assertEqual(first.scope.expires_at,self.wall+timedelta(seconds=2))
        self.now+=3;self.wall+=timedelta(seconds=3)
        before=len(self.provider.calls);bad=self.reader.read(self.req(cursor=first.next_cursor))
        self.assertEqual(bad.status,'invalid_cursor');self.assertEqual(bad.cells,[]);self.assertEqual(len(self.provider.calls),before)
    def test_lost_parser_response_consumes_token_once(self):
        first=self.reader.read(self.req())
        with patch.object(self.r,'read_xlsx',side_effect=TimeoutError):
            bad=self.reader.read(self.req(cursor=first.next_cursor));self.assertEqual(bad.status,'error');self.assertEqual(bad.cells,[])
        before=len(self.provider.calls)
        self.assertEqual(self.reader.read(self.req(cursor=first.next_cursor)).status,'invalid_cursor');self.assertEqual(len(self.provider.calls),before)

class ActualParserReaderTests(unittest.TestCase):
    def test_hidden_selection_actual_worker_reassembles_blanks_formula_and_overlap(self):
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.xlsx_reader'),'F9 XLSX worker missing')
        from telegram_search_mcp.spreadsheet_models import ReadSpreadsheetRequest
        from telegram_search_mcp.spreadsheets import SpreadsheetReader
        from xlsx_fixtures import worksheet,write_xlsx,xlsx_parts
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            path=write_xlsx(base/'book.xlsx',xlsx_parts([
                ('Visible','visible',worksheet('<row r="1"><c r="A1"><v>42</v></c></row>')),
                ('Hidden','veryHidden',worksheet('<row r="2" hidden="1"><c r="B2" t="inlineStr"><is><t>é🙂</t></is></c><c r="C2"><f>B2+1</f></c></row>'))]))
            store=ArtifactStore(cache_dir=base/'cache');artifact=store.store(path)
            metadata={artifact.artifact_id:(None,'book.xlsx',None,'document')}
            reader=SpreadsheetReader(client=Provider(),store=store,metadata_lookup=metadata.get,client_id='client_'+'a'*24,broker_generation='broker_'+'b'*32)
            catalog=reader.read(ReadSpreadsheetRequest(artifact_id=artifact.artifact_id))
            self.assertEqual(catalog.status,'catalog');self.assertEqual(catalog.cells,[]);self.assertIsNone(catalog.next_cursor)
            self.assertEqual([(s.index,s.name,s.state) for s in catalog.scope.catalog],[(1,'Visible','visible'),(2,'Hidden','veryHidden')])
            request=ReadSpreadsheetRequest(artifact_id=artifact.artifact_id,selections=[dict(sheet_index=2,range='B2:C3'),dict(sheet_index=2,range='C2')],max_cells=2)
            observed=[];scope=None
            for expected_status in ('page','page','complete'):
                result=reader.read(request);self.assertEqual(result.status,expected_status)
                if scope is None:scope=result.scope
                self.assertEqual(result.scope,scope);self.assertEqual(result.scope.total_cells,5)
                observed.extend((c.selection_index,c.address,c.row,c.column,c.value_type,c.value,c.formula) for c in result.cells)
                request=request.model_copy(update={'cursor':result.next_cursor})
            self.assertEqual(observed,[(1,'B2',2,2,'inlineStr','é🙂',None),(1,'C2',2,3,'n',None,'B2+1'),(1,'B3',3,2,'blank',None,None),(1,'C3',3,3,'blank',None,None),(2,'C2',2,3,'n',None,'B2+1')])
            self.assertTrue(result.scope_complete);self.assertIsNone(result.next_cursor)

if __name__=='__main__':unittest.main()
