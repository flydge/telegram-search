from __future__ import annotations
import copy
import importlib
import secrets
import socket
import threading
import time
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from telegram_search_mcp.broker_client import BrokerClient,BrokerCompatibilityError,BrokerUnavailable
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import contract_descriptor
from telegram_search_mcp.broker_protocol import receive_request,send_frame,PROTOCOL_VERSION,MAX_RESPONSE_BYTES
GEN='broker_'+'a'*32
ART='artifact_'+'0'*32+'_'+'b'*64+'_10'

def response(*,start=0,scope=None,more=True,total=4):
    scope=copy.deepcopy(scope) if scope else dict(artifact_id=ART,artifact_sha256='b'*64,artifact_bytes=10,extraction_fingerprint='c'*64,
        extractor_version=1,broker_generation=GEN,source_anchor=None,catalog=[dict(index=1,name='Public',state='visible'),dict(index=2,name='Hidden',state='hidden')],
        selections=[dict(sheet_index=2,range='A1:B2')],total_cells=total,max_cells=2,expires_at=(datetime.now(timezone.utc)+timedelta(seconds=299)).isoformat())
    positions=[('A1',1,1),('B1',1,2),('A2',2,1),('B2',2,2)]
    cells=[dict(selection_index=1,address=a,row=r,column=c,value_type='blank',value=None,formula=None,formula_kind=None,formula_ref=None,formula_shared_index=None) for a,r,c in positions[start:start+2]]
    return dict(contract_version=1,status='page' if more else 'complete',scope=scope,cells=cells,cell_start=start,cell_end=start+len(cells),
        scope_complete=not more,has_more=more,next_cursor='spreadsheet_'+secrets.token_hex(32) if more else None,detail='selected cells')

class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(BrokerClient,'read_spreadsheet'),'F9 proxy missing')
        self.m=importlib.import_module('telegram_search_mcp.spreadsheet_models');self.policy=RuntimePolicy(enabled_capabilities=('spreadsheets',))
        self.operations=[];self.responses=[];owner=self
        class Proxy(BrokerClient):
            def _request(self,op,payload):
                owner.operations.append((op,payload));self._spreadsheet_operation_state.generation=GEN;self._spreadsheet_generation=GEN
                return copy.deepcopy(owner.responses.pop(0)) if owner.responses else {'released':True}
        self.proxy=Proxy(socket_path=Path('/unused'),policy=self.policy)
    def request(self,**kw):return self.m.ReadSpreadsheetRequest(artifact_id=ART,selections=[dict(sheet_index=2,range='A1:B2')],max_cells=2,**kw)
    def read(self,**kw):return self.proxy.read_spreadsheet(self.request(**kw))
    def issue(self):self.responses=[response()];return self.read()
    def test_exact_positions_continuation_replay_and_body_free_registry(self):
        first=self.issue();self.assertEqual(first.status,'page')
        state=self.proxy._spreadsheet_states[first.next_cursor];self.assertNotIn('cells',vars(state))
        self.responses=[response(start=2,scope=first.scope.model_dump(mode='json'),more=False)]
        last=self.read(cursor=first.next_cursor);self.assertEqual(last.status,'complete');self.assertEqual([c.address for c in last.cells],['A2','B2'])
        before=len(self.operations);bad=self.read(cursor=first.next_cursor);self.assertEqual(bad.status,'invalid_cursor');self.assertEqual(bad.cells,[]);self.assertEqual(len(self.operations),before)
    def test_foreign_parameter_change_expiry_closed_and_capability_precede_dispatch(self):
        first=self.issue();before=len(self.operations)
        changed=self.request(cursor=first.next_cursor).model_copy(update={'selections':[self.m.SheetRange(sheet_index=1,range='A1:B2')]})
        self.assertEqual(self.proxy.read_spreadsheet(changed).status,'invalid_cursor');self.assertEqual(self.read(cursor='spreadsheet_'+'f'*64).status,'invalid_cursor')
        self.assertEqual(len(self.operations),before)
        first=self.issue()
        with patch('telegram_search_mcp.broker_client.time.monotonic',return_value=time.monotonic()+301):self.assertEqual(self.read(cursor=first.next_cursor).status,'expired')
        self.proxy._policy=RuntimePolicy()
        with self.assertRaises(BrokerCompatibilityError):self.read(cursor='spreadsheet_'+'f'*64)
        self.proxy._policy=self.policy;self.proxy.close();before=len(self.operations);self.assertEqual(self.read().status,'invalid_cursor');self.assertEqual(len(self.operations),before)
    def test_hostile_wire_substitutions_return_empty_and_issue_no_authority(self):
        for change in ('artifact','selection','maxcells','generation','start','expiry','contract_bool','extractor_bool','failure_content','total','coordinate','address','selection_index','bytes_string','hasmore_int','offset_float','budget','catalog','extra'):
            raw=response()
            if change=='artifact':raw['scope'].update(artifact_id='artifact_'+'0'*32+'_'+'d'*64+'_10',artifact_sha256='d'*64)
            if change=='selection':raw['scope']['selections'][0]['sheet_index']=1
            if change=='maxcells':raw['scope']['max_cells']=3
            if change=='generation':raw['scope']['broker_generation']='broker_'+'d'*32
            if change=='start':raw.update(cell_start=2,cell_end=4)
            if change=='expiry':raw['scope']['expires_at']=(datetime.now(timezone.utc)+timedelta(seconds=401)).isoformat()
            if change=='contract_bool':raw['contract_version']=True
            if change=='extractor_bool':raw['scope']['extractor_version']=True
            if change=='failure_content':raw['status']='error'
            if change=='total':raw['scope']['total_cells']=5
            if change=='coordinate':raw['cells'][0].update(address='A2',row=2)
            if change=='address':raw['cells'][0]['address']='B1'
            if change=='selection_index':raw['cells'][0]['selection_index']=2
            if change=='bytes_string':raw['scope']['artifact_bytes']='10'
            if change=='hasmore_int':raw['has_more']=1
            if change=='offset_float':raw['cell_start']=0.0
            if change=='budget':raw['cells'][0].update(value_type='s',value='x'*20001)
            if change=='catalog':raw['scope']['catalog'][1]['index']=1
            if change=='extra':raw['cells'][0]['secret']='body'
            self.responses=[raw]
            with self.subTest(change=change):
                bad=self.read();self.assertEqual(bad.status,'error');self.assertEqual(bad.cells,[]);self.assertIsNone(bad.scope)
                before=len(self.operations);self.assertEqual(self.read(cursor=raw['next_cursor']).status,'invalid_cursor');self.assertEqual(len(self.operations),before)
    def test_cross_column_and_grid_edge_positions_are_derived_independently(self):
        selections=[dict(sheet_index=2,range='Z1048575:AA1048576'),dict(sheet_index=1,range='XFD1048576')]
        request=self.request().model_copy(update={'selections':[self.m.SheetRange(**x) for x in selections]})
        positions=[(1,'Z1048575',1048575,26),(1,'AA1048575',1048575,27),(1,'Z1048576',1048576,26),(1,'AA1048576',1048576,27),(2,'XFD1048576',1048576,16384)]
        scope=None
        for offset in (0,2,4):
            raw=response();raw['scope'].update(selections=selections,total_cells=5)
            if scope is not None:raw['scope']=scope
            raw['cells']=[dict(raw['cells'][0],selection_index=i,address=a,row=r,column=c) for i,a,r,c in positions[offset:offset+2]]
            raw.update(cell_start=offset,cell_end=min(offset+2,5),status='complete' if offset==4 else 'page',has_more=offset!=4,scope_complete=offset==4,next_cursor=None if offset==4 else raw['next_cursor'])
            self.responses=[raw];result=self.proxy.read_spreadsheet(request);self.assertEqual(result.status,'complete' if offset==4 else 'page')
            self.assertEqual([(c.selection_index,c.address,c.row,c.column) for c in result.cells],positions[offset:offset+2])
            scope=result.scope.model_dump(mode='json');request=request.model_copy(update={'cursor':result.next_cursor})

    def test_catalog_status_is_valid_only_for_unselected_request(self):
        raw=response();raw.update(status='catalog',cells=[],cell_start=0,cell_end=0,scope_complete=True,has_more=False,next_cursor=None);raw['scope'].update(selections=None,total_cells=0)
        self.responses=[raw];self.assertEqual(self.read().status,'error')
        self.responses=[raw];result=self.proxy.read_spreadsheet(self.request().model_copy(update={'selections':None}));self.assertEqual(result.status,'catalog');self.assertIsNone(result.next_cursor)
    def test_frozen_scope_offset_and_cursor_history(self):
        for change in ('fingerprint','anchor','expiry','catalog','offset','reuse'):
            first=self.issue();raw=response(start=2,scope=first.scope.model_dump(mode='json'),more=False)
            if change=='fingerprint':raw['scope']['extraction_fingerprint']='d'*64
            if change=='anchor':raw['scope']['source_anchor']={'chat_id':7,'message_id':11}
            if change=='expiry':raw['scope']['expires_at']=(datetime.now(timezone.utc)+timedelta(seconds=250)).isoformat()
            if change=='catalog':raw['scope']['catalog'][1]['name']='different'
            if change=='offset':raw['cell_start']=1;raw['cell_end']=3
            if change=='reuse':raw.update(status='page',has_more=True,scope_complete=False,next_cursor=first.next_cursor);raw['cell_end']=3;raw['cells']=raw['cells'][:1]
            self.responses=[raw]
            with self.subTest(change=change):self.assertEqual(self.read(cursor=first.next_cursor).status,'error')
    def test_short_page_preserves_selection_order_with_overlap(self):
        raw=response();raw['scope'].update(selections=[dict(sheet_index=2,range='B2'),dict(sheet_index=1,range='A1:B1'),dict(sheet_index=2,range='A2:B2')],total_cells=5)
        raw['cells']=raw['cells'][:1];raw['cells'][0].update(address='B2',row=2,column=2);raw['cell_end']=1
        self.responses=[raw];request=self.request().model_copy(update={'selections':[self.m.SheetRange(**s) for s in raw['scope']['selections']]})
        first=self.proxy.read_spreadsheet(request);self.assertEqual(first.status,'page');self.assertEqual(first.cell_end,1)
        raw2=copy.deepcopy(raw);raw2.update(cell_start=1,cell_end=3,next_cursor='spreadsheet_'+'e'*64)
        raw2['cells']=[dict(raw['cells'][0],selection_index=2,address='A1',row=1,column=1),dict(raw['cells'][0],selection_index=2,address='B1',row=1,column=2)]
        self.responses=[raw2];second=self.proxy.read_spreadsheet(request.model_copy(update={'cursor':first.next_cursor}));self.assertEqual(second.status,'page')
    def test_capacity_counts_pending_and_rejects_fifth_active(self):
        for _ in range(15):self.assertEqual(self.issue().status,'page')
        entered=threading.Event();release=threading.Event();results=[];calls=[]
        def paused(op,payload):
            calls.append(op);entered.set();release.wait(3);self.proxy._spreadsheet_operation_state.generation=GEN;self.proxy._spreadsheet_generation=GEN;return response()
        self.proxy._request=paused;worker=threading.Thread(target=lambda:results.append(self.read()));worker.start()
        try:self.assertTrue(entered.wait(2));self.assertEqual(self.read().status,'capacity_exhausted');self.assertEqual(len(calls),1)
        finally:release.set();worker.join(3)
        self.assertEqual(results[0].status,'page')
    def test_close_inflight_never_publishes(self):
        entered=threading.Event();release=threading.Event();results=[]
        def paused(op,payload):
            entered.set();release.wait(3);self.proxy._spreadsheet_operation_state.generation=GEN;self.proxy._spreadsheet_generation=GEN;return response()
        self.proxy._request=paused;worker=threading.Thread(target=lambda:results.append(self.read()));worker.start();self.assertTrue(entered.wait(2));self.proxy.close();release.set();worker.join(3)
        self.assertEqual(results[0].status,'invalid_cursor');self.assertIsNone(results[0].scope)
    def test_255_remembered_tokens_bound_256th_call(self):
        request=self.request().model_copy(update={'selections':[self.m.SheetRange(sheet_index=1,range='A1:A1000')],'max_cells':1})
        initial=None
        for i in range(256):
            raw=response();raw['scope'].update(selections=[dict(sheet_index=1,range='A1:A1000')],total_cells=1000,max_cells=1)
            if initial:raw['scope']=initial
            raw.update(cell_start=i,cell_end=i+1);raw['cells']=[dict(raw['cells'][0],address='A'+str(i+1),row=i+1)]
            self.responses=[raw];result=self.proxy.read_spreadsheet(request)
            if i==255:self.assertEqual(result.status,'limit_reached');self.assertEqual(result.cells,[])
            else:
                self.assertEqual(result.status,'page');initial=result.scope.model_dump(mode='json');request=request.model_copy(update={'cursor':result.next_cursor})

    def test_lost_response_consumes_token_without_restoration(self):
        first=self.issue()
        with patch.object(self.proxy,'_request',side_effect=BrokerUnavailable('lost response')):
            bad=self.read(cursor=first.next_cursor);self.assertEqual(bad.status,'error');self.assertEqual(bad.cells,[])
        before=len(self.operations);self.assertEqual(self.read(cursor=first.next_cursor).status,'invalid_cursor');self.assertEqual(len(self.operations),before)
    def test_four_active_calls_reject_fifth_before_dispatch(self):
        entered=threading.Condition();release=threading.Event();count=[0];results=[]
        def paused(op,payload):
            with entered:count[0]+=1;entered.notify_all()
            release.wait(3);self.proxy._spreadsheet_operation_state.generation=GEN
            with self.proxy._spreadsheet_lock:self.proxy._spreadsheet_generation=GEN
            return response()
        self.proxy._request=paused
        workers=[threading.Thread(target=lambda:results.append(self.read())) for _ in range(4)]
        try:
            for worker in workers:worker.start()
            with entered:self.assertTrue(entered.wait_for(lambda:count[0]==4,2))
            self.assertEqual(self.read().status,'capacity_exhausted');self.assertEqual(count[0],4)
        finally:
            release.set()
            for worker in workers:worker.join(3)
        self.assertTrue(all(r.status=='page' for r in results))

class WireTests(unittest.TestCase):
    def test_changed_handshake_generation_rejects_before_content_dispatch(self):
        self.assertTrue(hasattr(BrokerClient,'read_spreadsheet'),'F9 proxy missing')
        m=importlib.import_module('telegram_search_mcp.spreadsheet_models');policy=RuntimePolicy(enabled_capabilities=('spreadsheets',));observed=[];threads=[];generation=[GEN];raw=response()
        def connector(path):
            client_socket,server_socket=socket.socketpair()
            def serve():
                with server_socket:
                    hello=receive_request(server_socket)
                    send_frame(server_socket,{'version':PROTOCOL_VERSION,'request_id':hello['request_id'],'ok':True,'result':{**contract_descriptor(policy),'broker_generation':generation[0]}},max_bytes=MAX_RESPONSE_BYTES)
                    try:req=receive_request(server_socket)
                    except Exception:return
                    observed.append(req['operation']);send_frame(server_socket,{'version':PROTOCOL_VERSION,'request_id':req['request_id'],'ok':True,'result':raw},max_bytes=MAX_RESPONSE_BYTES)
            thread=threading.Thread(target=serve);thread.start();threads.append(thread);return client_socket
        proxy=BrokerClient(socket_path=Path('/unused'),policy=policy,connector=connector)
        req=m.ReadSpreadsheetRequest(artifact_id=ART,selections=[dict(sheet_index=2,range='A1:B2')],max_cells=2)
        first=proxy.read_spreadsheet(req);self.assertEqual(first.status,'page');generation[0]='broker_'+'d'*32
        bad=proxy.read_spreadsheet(req.model_copy(update={'cursor':first.next_cursor}));self.assertEqual(bad.status,'invalid_cursor');self.assertEqual(observed,['read_spreadsheet'])
        for thread in threads:thread.join(2);self.assertFalse(thread.is_alive())

if __name__=='__main__':unittest.main()
