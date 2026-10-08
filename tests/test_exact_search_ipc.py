"""Exercise F5 through authenticated framed IPC, not direct dispatch shortcuts."""
from pathlib import Path
import tempfile
import threading
import unittest

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.schemas import SearchMessagesRequest
from test_exact_search_reader import SearchProvider
from test_message_reader import message


class ExactSearchIPCTests(unittest.TestCase):
    def test_framed_search_reaches_provider_and_continues_past_twenty(self):
        class Provider(SearchProvider):
            def close(self):
                pass
        provider = Provider({i: message(i) for i in range(1, 26)})
        policy = RuntimePolicy(enabled_capabilities=('search_messages',))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = Broker(socket_path=root / 'broker.sock', lock_path=root / 'broker.lock',
                artifact_store=ArtifactStore(cache_dir=root / 'cache'),
                client_factory=lambda: provider, policy=policy)
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()
            proxy = BrokerClient(socket_path=root / 'broker.sock', policy=policy, restart_callback=lambda: None)
            try:
                self.assertTrue(broker.wait_until_ready(timeout=2))
                request = SearchMessagesRequest(target=7, query='selected', mode='latest')
                first = proxy.search_messages(request)
                self.assertEqual([r.anchor.message_id for r in first.results], list(range(25, 5, -1)))
                self.assertEqual(first.status, 'page')
                second = proxy.search_messages(request.model_copy(update={'cursor': first.next_cursor}))
                self.assertEqual([r.anchor.message_id for r in second.results], [5, 4, 3, 2, 1])
                self.assertEqual(second.stop_reason, 'provider_end_unverified')
                self.assertIsNone(second.next_cursor)
                self.assertFalse(second.scope_complete)
                self.assertTrue(all(r.status == 'match' for r in first.results + second.results))
                replay = proxy.search_messages(request.model_copy(update={'cursor': first.next_cursor}))
                self.assertEqual(replay.status, 'invalid_cursor')
            finally:
                proxy.close()
                broker.shutdown()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
