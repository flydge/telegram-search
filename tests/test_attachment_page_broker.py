from __future__ import annotations
import tempfile
import time
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError

class Provider:
    def ensure_ready(self):pass
    def get_account_id(self):return 17
    def request_budget(self,deadline):return nullcontext()
    def close(self):pass

class AttachmentPageBrokerTests(unittest.TestCase):
    def test_dispatch_binds_client_and_release_invalidates_continuation(self):
        self.assertIn('read_attachment_page',OPERATIONS,'F8 IPC operation missing')
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);store=ArtifactStore(cache_dir=base/'cache')
            broker=Broker(socket_path=base/'broker.sock',artifact_store=store,client_factory=Provider,
                policy=RuntimePolicy(enabled_capabilities=('artifacts','attachment_pages')))
            self.addCleanup(broker._executor.shutdown)
            def dispatch(op,payload,client='client_'+'a'*24):
                return broker._dispatch({'operation':op,'payload':payload,'client_id':client,
                    'deadline':time.monotonic()+30,'broker_generation':broker._generation})
            made=dispatch('create_local_artifact',{'file_name':'sample.txt','content':'abcdef'})
            self.assertEqual(made['status'],'complete')
            req={'artifact_id':made['artifact_id'],'max_chars':2,'render_pages':False}
            first=dispatch('read_attachment_page',req);self.assertEqual(first['text'],'ab')
            resume={**req,'cursor':first['next_cursor']}
            foreign=dispatch('read_attachment_page',resume,'client_'+'b'*24)
            self.assertEqual(foreign['status'],'invalid_cursor');self.assertEqual(foreign['text'],'')
            dispatch('release_client',{})
            self.assertEqual(dispatch('read_attachment_page',resume)['status'],'invalid_cursor')
            broker._policy=RuntimePolicy()
            with self.assertRaises(CompatibilityError):dispatch('read_attachment_page',req)

    def test_real_proxy_accepts_lifetime_started_after_request_transit(self):
        from telegram_search_mcp.broker_client import BrokerClient
        from telegram_search_mcp.attachment_page_models import ReadAttachmentPageRequest
        from telegram_search_mcp.schemas import CreateLocalArtifactRequest
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);policy=RuntimePolicy(enabled_capabilities=('artifacts','attachment_pages'))
            broker=Broker(socket_path=base/'broker.sock',artifact_store=ArtifactStore(cache_dir=base/'cache'),client_factory=Provider,policy=policy)
            thread=threading.Thread(target=broker.serve_forever);thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=3))
            proxy=BrokerClient(socket_path=base/'broker.sock',policy=policy,restart_callback=lambda:None)
            try:
                artifact=proxy.create_local_artifact(CreateLocalArtifactRequest(file_name='x.txt',content='abcdef'))
                request=ReadAttachmentPageRequest(artifact_id=artifact.artifact_id,max_chars=2,render_pages=False)
                first=proxy.read_attachment_page(request)
                self.assertEqual((first.status,first.text),('page','ab'))
                second=proxy.read_attachment_page(request.model_copy(update={'cursor':first.next_cursor}))
                self.assertEqual(second.text,'cd');self.assertEqual(second.scope,first.scope)
            finally:
                proxy.close();broker.shutdown();thread.join(3)

    def test_transferred_attachment_anchor_survives_new_strict_scope(self):
        from telegram_search_mcp.broker_client import BrokerClient
        from telegram_search_mcp.attachment_page_models import ReadAttachmentPageRequest
        from telegram_search_mcp.schemas import AttachmentRequest
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);download=base/'downloads';download.mkdir();source=download/'text.txt';source.write_text('Aé🙂B')
            class FileProvider(Provider):
                def resolve_target(self,target):return {'id':target,'type':{'@type':'chatTypePrivate','user_id':17}}
                def get_message(self,chat_id,message_id):return {'chat_id':chat_id,'id':message_id,'content':{'@type':'messageDocument','document':{'file_name':'synthetic.txt','mime_type':'text/plain','document':{'@type':'file','id':1,'size':source.stat().st_size}}}}
                def download_file(self,file_id,**kwargs):return source
            policy=RuntimePolicy(enabled_capabilities=('artifacts','attachment_pages'))
            broker=Broker(socket_path=base/'broker.sock',artifact_store=ArtifactStore(cache_dir=base/'cache'),client_factory=FileProvider,policy=policy,download_source_root=download)
            thread=threading.Thread(target=broker.serve_forever);thread.start()
            self.assertTrue(broker.wait_until_ready(timeout=3))
            proxy=BrokerClient(socket_path=base/'broker.sock',policy=policy,restart_callback=lambda:None)
            try:
                artifact=proxy.get_attachment(AttachmentRequest(anchor={'chat_id':17,'message_id':1024}))
                self.assertEqual(artifact.status,'complete')
                page=proxy.read_attachment_page(ReadAttachmentPageRequest(artifact_id=artifact.artifact_id,max_chars=2,render_pages=False))
                self.assertEqual((page.status,page.text),('page','Aé'))
                self.assertEqual(page.scope.source_anchor.model_dump(),{'chat_id':17,'message_id':1024})
            finally:
                proxy.close();broker.shutdown();thread.join(3)

if __name__=='__main__':unittest.main()
