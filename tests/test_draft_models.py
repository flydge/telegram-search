"""Output validation rejects injected preview evidence and malformed continuations."""
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch
from pydantic import ValidationError
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.draft_models import (ListDraftsRequest,GetDraftRequest,CancelDraftRequest,
    ListDraftsResponse,GetDraftResponse,CancelDraftResponse,DraftPreview)

DID='draft_'+'a'*32
EXPIRY='2026-10-08T10:00:00Z'
SUMMARY={'draft_id':DID,'account_id':7,'recipient':123,'recipient_title':'Recipient',
         'kind':'text','expires_at':EXPIRY,'approval_required':True}
PREVIEW={**SUMMARY,'sha256':hashlib.sha256(b'exact').hexdigest(),'size_bytes':5,'text':'exact'}
GET={'status':'pending','draft':PREVIEW,'detail':'exact pending draft; no recipient verification or approval'}
LIST={'status':'listed','drafts':[SUMMARY],'has_more':False,'next_after_draft_id':None,
      'detail':'owned pending drafts; live view, no recipient verification or approval'}

class DraftModelTests(unittest.TestCase):
    def test_rejects_coercion_unbounded_pages_and_public_owner_injection(self):
        for value in [True,'20',0,51,1.0]:
            with self.subTest(value=value),self.assertRaises(ValidationError):ListDraftsRequest(limit=value)
        for model,payload in [(GetDraftRequest,{'draft_id':DID,'account_id':7}),
                              (CancelDraftRequest,{'draft_id':DID,'client_id':'client_secret'}),
                              (ListDraftsRequest,{'after_draft_id':True})]:
            with self.assertRaises(ValidationError):model.model_validate(payload)
        for changed in [{'account_id':True},{'account_id':2**53},{'size_bytes':'5'},
                        {'client_id':'client_secret'},{'artifact_path':'/private/secret'},{'sha256':'a'*64},
                        {'expires_at':123},{'expires_at':'0'},{'approval_required':False}]:
            with self.subTest(changed=changed),self.assertRaises(ValidationError):
                DraftPreview.model_validate({**PREVIEW,**changed})
    def test_terminal_shapes_and_metadata_summary_cannot_leak_evidence(self):
        for model,payload in [(GetDraftResponse,{**GET,'status':'unavailable'}),
                              (GetDraftResponse,{'status':'pending','detail':GET['detail']}),
                              (CancelDraftResponse,{'status':'unavailable','draft_id':DID,'detail':'draft is unavailable'}),
                              (ListDraftsResponse,{**LIST,'status':'unavailable'}),
                              (ListDraftsResponse,{**LIST,'has_more':True}),
                              (ListDraftsResponse,{**LIST,'next_after_draft_id':DID}),
                              (ListDraftsResponse,{**LIST,'drafts':[SUMMARY,SUMMARY]}),
                              (ListDraftsResponse,{**LIST,'drafts':[{**SUMMARY,'text':'secret'}]}),
                              (ListDraftsResponse,{**LIST,'drafts':tuple([SUMMARY])})]:
            with self.subTest(model=model.__name__,payload=payload),self.assertRaises(ValidationError):model.model_validate(payload)
        for field in ['text','caption','display_name','artifact_path','client_id']:
            with self.assertRaises(ValidationError):ListDraftsResponse.model_validate({**LIST,'drafts':[{**SUMMARY,field:'secret'}]})
    def test_proxy_rejects_malformed_outputs_to_generic_empty_results(self):
        proxy=BrokerClient(socket_path=Path('/unused-synthetic.sock'),policy=RuntimePolicy(),restart_callback=lambda:None)
        self.addCleanup(proxy.close)
        for method,request,bad in [
            (proxy.get_draft,GetDraftRequest(draft_id=DID),{**GET,'status':'unavailable'}),
            (proxy.list_drafts,ListDraftsRequest(),{**LIST,'drafts':[{**SUMMARY,'caption':'secret'}]}),
            (proxy.cancel_draft,CancelDraftRequest(draft_id=DID),{'status':'unavailable','draft_id':DID,'detail':'draft is unavailable'})]:
            with patch.object(proxy,'_request',return_value=bad):result=method(request).model_dump(mode='json')
            self.assertEqual(result['status'],'unavailable')
            self.assertNotIn('secret',str(result));self.assertNotIn(DID,str(result))
    def test_proxy_response_must_match_selected_draft_and_list_request(self):
        proxy = BrokerClient(socket_path=Path('/unused-synthetic.sock'), policy=RuntimePolicy(), restart_callback=lambda: None)
        self.addCleanup(proxy.close)
        other = 'draft_' + 'b' * 32
        cases = [
            (proxy.get_draft, GetDraftRequest(draft_id=DID), {**GET, 'draft': {**PREVIEW, 'draft_id': other}}),
            (proxy.cancel_draft, CancelDraftRequest(draft_id=DID), {
                'status': 'cancelled', 'draft_id': other, 'detail': 'pending draft cancelled; cached artifacts retained'}),
            (proxy.list_drafts, ListDraftsRequest(limit=1), {
                **LIST, 'drafts': [SUMMARY, {**SUMMARY, 'draft_id': other}]}),
            (proxy.list_drafts, ListDraftsRequest(after_draft_id=DID), LIST),
            (proxy.list_drafts, ListDraftsRequest(), {
                **LIST, 'drafts': [SUMMARY, {**SUMMARY, 'draft_id': other, 'account_id': 8}]}),
        ]
        for method, request, bad in cases:
            with self.subTest(method=method.__name__, request=request), patch.object(proxy, '_request', return_value=bad):
                result = method(request).model_dump(mode='json')
                self.assertEqual(result['status'], 'unavailable')
                self.assertNotIn(DID, str(result)); self.assertNotIn(other, str(result))

    def test_preview_rejects_artifact_limit_and_unrelated_kind_metadata(self):
        size=64*1024*1024+1
        data={**SUMMARY,'kind':'document','sha256':'a'*64,'size_bytes':size,
              'artifact_id':'artifact_'+'b'*32+'_'+'a'*64+'_'+str(size),
              'display_name':'file.bin','mime_type':'application/octet-stream','caption':''}
        with self.assertRaises(ValidationError):DraftPreview.model_validate(data)
        with self.assertRaises(ValidationError):DraftPreview.model_validate({**PREVIEW,'caption':''})
