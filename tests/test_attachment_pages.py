from __future__ import annotations
import importlib
import tempfile
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from telegram_search_mcp.artifact_store import ArtifactStore

class Provider:
    account = 17
    def __init__(self): self.calls = []
    def ensure_ready(self): self.calls.append('ready')
    def get_account_id(self): self.calls.append('account'); return self.account
    def request_budget(self, deadline): return nullcontext()

class AttachmentPageTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.attachment_pages'), 'F8 reader missing')
        self.m = importlib.import_module('telegram_search_mcp.attachment_page_models')
        self.r = importlib.import_module('telegram_search_mcp.attachment_pages')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.now=100.;self.wall=datetime.now(timezone.utc)
        self.store=ArtifactStore(cache_dir=self.base/'cache',clock=lambda:self.wall.timestamp())
        p=self.base/'text.txt';p.write_text('Aé🙂BC',encoding='utf-8');self.artifact=self.store.store(p)
        self.metadata={self.artifact.artifact_id:(None,'text.txt','text/plain','document')}
        self.provider=Provider()
        self.reader=self.make_reader()
    def make_reader(self, client_id='client_'+'a'*24):
        return self.r.AttachmentPageReader(client=self.provider,store=self.store,metadata_lookup=self.metadata.get,
            client_id=client_id,broker_generation='broker_'+'b'*32,clock=lambda:self.now,wall_clock=lambda:self.wall)
    def req(self,**kw):
        return self.m.ReadAttachmentPageRequest(artifact_id=self.artifact.artifact_id,max_chars=2,render_pages=False,**kw)
    def test_unicode_continuation_exactly_reassembles_and_replay_is_empty(self):
        first=self.reader.read(self.req());self.assertEqual((first.status,first.text,first.text_start,first.text_end),('page','Aé',0,2))
        second=self.reader.read(self.req(cursor=first.next_cursor));self.assertEqual((second.text,second.text_start,second.text_end),('🙂B',2,4))
        last=self.reader.read(self.req(cursor=second.next_cursor));self.assertEqual((last.status,last.text,last.text_start,last.text_end),('complete','C',4,5))
        self.assertTrue(last.scope_complete);self.assertFalse(last.has_more);self.assertIsNone(last.next_cursor)
        before=len(self.provider.calls);replay=self.reader.read(self.req(cursor=first.next_cursor))
        self.assertEqual((replay.status,replay.text),('invalid_cursor',''));self.assertEqual(len(self.provider.calls),before)
    def test_parameter_change_is_rejected_before_account_or_parser(self):
        first=self.reader.read(self.req());before=len(self.provider.calls)
        bad=self.req(cursor=first.next_cursor).model_copy(update={'max_chars':3})
        self.assertEqual(self.reader.read(bad).status,'invalid_cursor');self.assertEqual(len(self.provider.calls),before)
    def test_expired_foreign_and_closed_cursor_are_empty_without_provider_reads(self):
        first=self.reader.read(self.req());other=self.make_reader('client_'+'c'*24);before=len(self.provider.calls)
        self.assertEqual(other.read(self.req(cursor=first.next_cursor)).status,'invalid_cursor')
        self.now+=301
        self.assertEqual(self.reader.read(self.req(cursor=first.next_cursor)).status,'invalid_cursor')
        self.reader.close();self.assertEqual(self.reader.read(self.req()).status,'invalid_cursor')
        self.assertEqual(len(self.provider.calls),before)
    def test_account_drift_discards_cursor_and_all_content(self):
        first=self.reader.read(self.req());self.provider.account=18
        bad=self.reader.read(self.req(cursor=first.next_cursor));self.assertEqual(bad.status,'invalid_cursor');self.assertEqual(bad.text,'');self.assertIsNone(bad.scope)
    def test_artifact_hash_change_returns_no_content(self):
        first=self.reader.read(self.req());self.artifact.path.write_bytes(b'changed')
        bad=self.reader.read(self.req(cursor=first.next_cursor));self.assertEqual(bad.status,'expired');self.assertEqual(bad.text,'')
    def test_extraction_metadata_change_cannot_resume(self):
        first=self.reader.read(self.req());self.metadata[self.artifact.artifact_id]=(None,'changed.csv','text/csv','document')
        bad=self.reader.read(self.req(cursor=first.next_cursor));self.assertEqual(bad.status,'invalid_cursor');self.assertEqual(bad.text,'')
    def test_cursor_lifetime_never_renews(self):
        first=self.reader.read(self.req());self.now+=299;self.wall+=timedelta(seconds=299)
        second=self.reader.read(self.req(cursor=first.next_cursor));self.assertEqual(second.scope.expires_at,first.scope.expires_at)
        self.now+=2;self.wall+=timedelta(seconds=2)
        self.assertEqual(self.reader.read(self.req(cursor=second.next_cursor)).status,'invalid_cursor')
    def test_bounded_capacity_reserves_outstanding_sessions(self):
        for _ in range(16):self.assertEqual(self.reader.read(self.req()).status,'page')
        before=len(self.provider.calls);self.assertEqual(self.reader.read(self.req()).status,'capacity_exhausted');self.assertEqual(len(self.provider.calls),before)
    def test_close_or_expiry_during_parser_drops_entire_batch(self):
        actual=self.r.read_document_page
        for mode in ('close','expire','account','artifact'):
            reader=self.make_reader()
            def run(*a,**kw):
                result=actual(*a,**kw)
                if mode=='close':reader.close()
                elif mode=='expire':self.now+=301
                elif mode=='account':self.provider.account+=1
                else:self.artifact.path.write_bytes(b'changed')
                return result
            with self.subTest(mode=mode),patch.object(self.r,'read_document_page',side_effect=run):
                bad=reader.read(self.req());self.assertNotIn(bad.status,('page','complete'));self.assertEqual(bad.text,'');self.assertIsNone(bad.scope)
    def test_continuation_work_has_fixed_call_ceiling(self):
        from telegram_search_mcp.document_page_reader import PageReadResult
        def parse(*args,**kwargs):
            offset=kwargs['offset']
            return PageReadResult(status='page',kind='text',text='x',text_start=offset,text_end=offset+1,has_more=True)
        request=self.req().model_copy(update={'max_chars':1})
        with patch.object(self.r,'read_document_page',side_effect=parse):
            for _ in range(255):
                result=self.reader.read(request);self.assertEqual(result.status,'page')
                request=request.model_copy(update={'cursor':result.next_cursor})
            stopped=self.reader.read(request)
            self.assertEqual(stopped.status,'limit_reached');self.assertEqual(stopped.text,'');self.assertIsNone(stopped.next_cursor)

    def test_numeric_contract_versions_reject_boolean_and_float(self):
        from pydantic import ValidationError
        first=self.reader.read(self.req())
        for key in ('contract_version','extractor_version'):
            for value in (True,1.0,'1'):
                raw=first.model_dump(mode='json')
                if key=='contract_version':raw[key]=value
                else:raw['scope'][key]=value
                with self.subTest(key=key,value=value),self.assertRaises(ValidationError):
                    self.m.ReadAttachmentPageResponse.model_validate(raw)
    def test_invalid_selection_and_strict_input_fail_before_work(self):
        from pydantic import ValidationError
        for pages in ([],[1,1],[0],[True],[1.0],['1'],list(range(1,7)),'[1]'):
            with self.subTest(pages=pages),self.assertRaises(ValidationError):self.req(pages=pages)
        for cursor in ('','attachment_'+'a'*63,'attachment_'+'A'*64,'../x'):
            with self.assertRaises(ValidationError):self.req(cursor=cursor)
        result=self.reader.read(self.req(pages=[1]));self.assertEqual(result.status,'invalid_selection');self.assertEqual(result.text,'')

if __name__=='__main__':unittest.main()
