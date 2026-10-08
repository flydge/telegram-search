from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.outgoing_drafts import DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.schemas import PrepareTextSendRequest, SendPreparedTextRequest
from telegram_search_mcp.server import build_server
from telegram_search_mcp.tdjson import TDLibClient, MessageSendOutcomeUnknown

CLIENT = "client_" + "a" * 24
FOREIGN = "client_" + "b" * 24
TOKEN = "draft_" + "e" * 32


def message(identifier=-10, chat=123, kind="messageText", **changes):
    return {"@type": "message", "id": identifier, "chat_id": chat,
            "is_outgoing": True, "content": {"@type": kind, "text": {"@type":"formattedText", "text":"PRIVATE BODY", "entities":[]}}, **changes}


class Raw:
    """Only synthetic raw transport; production reducer and broker stay real."""
    def __init__(self):
        self.events = deque()
        self.sent = []
        self.account = 7
        self.mode = "lost"
        self.fail_receive = False

    def send(self, request):
        self.sent.append(request)
        kind = request["@type"]
        if kind == "getAuthorizationState":
            reply = {"@type": "authorizationStateReady"}
        elif kind == "getMe":
            reply = {"@type": "user", "id": self.account}
        elif kind == "getChat":
            reply = {"@type": "chat", "id": request["chat_id"], "title": "Recipient",
                     "type": {"@type": "chatTypePrivate"}}
        elif kind == "sendMessage":
            reply = message(sending_state={"@type": "messageSendingStatePending",
                            "sending_id": request["options"]["sending_id"]})
            if self.mode == "lost_before_reply":
                self.fail_receive = True
                return
            self.events.append({**reply, "@extra": request["@extra"]})
            if self.mode == "sent":
                self.events.append({"@type": "updateMessageSendSucceeded", "old_message_id": -10,
                                    "message": message(701, sending_state=None)})
            else:
                self.fail_receive = True
            return
        else:
            raise AssertionError(kind)
        self.events.append({**reply, "@extra": request["@extra"]})

    def receive(self, timeout):
        if self.events:
            return self.events.popleft()
        if self.fail_receive:
            self.fail_receive = False
            raise OSError("synthetic response loss")
        return None

    def close(self):
        pass


class SendStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.raw = Raw()
        self.provider = TDLibClient(raw=self.raw)
        self.approvals = []
        self.broker = Broker(socket_path=self.root / "broker.sock",
            artifact_store=ArtifactStore(cache_dir=self.root / "cache"),
            client_factory=lambda: self.provider, verify_peer_uid=False,
            approval_prompt=lambda **kw: self.approvals.append(kw) or True)
        self.preview = self.broker._prepare_text_send(
            PrepareTextSendRequest(recipient=123, text="PRIVATE BODY"), client_id=CLIENT)

    def tearDown(self):
        self.broker.shutdown()
        self.temporary.cleanup()

    def status(self, token=None, client=CLIENT):
        from telegram_search_mcp.send_status_models import GetSendStatusRequest
        return self.broker._get_send_status(GetSendStatusRequest(draft_id=token or self.preview.draft_id),
                                            client_id=client)

    def send(self):
        return self.broker._send_prepared_text(SendPreparedTextRequest(
            draft_id=self.preview.draft_id, approved=True), client_id=CLIENT)

    def test_public_tool_is_closed_read_only_send_capability(self):
        server = build_server(policy=RuntimePolicy())
        names = {tool.name for tool in server._tool_manager.list_tools()}
        self.assertIn("get_send_status", names)
        tool = server._tool_manager.get_tool("get_send_status")
        self.assertEqual(set(tool.parameters["properties"]), {"draft_id"})
        self.assertIs(tool.parameters["additionalProperties"], False)
        self.assertEqual(tool.annotations.read_only_hint, True)
        self.assertEqual(tool.annotations.destructive_hint, False)
        self.assertEqual(tool.annotations.idempotent_hint, True)

    def test_pending_foreign_cancelled_unknown_and_no_approval_or_send(self):
        result = self.status()
        self.assertEqual((result.status, result.evidence), ("pending", "local_pending"))
        self.assertEqual(self.status(client=FOREIGN).model_dump(exclude={"draft_id"}),
                         self.status(TOKEN).model_dump(exclude={"draft_id"}))
        owner = DraftOwner(CLIENT, 7)
        self.broker._drafts.cancel(self.preview.draft_id, owner=owner)
        self.assertEqual((self.status().status, self.status().evidence), ("outcome_unknown", "none"))
        self.assertEqual(self.approvals, [])
        self.assertFalse(any(r["@type"] == "sendMessage" for r in self.raw.sent))

    def test_lost_response_late_exact_success_never_retries(self):
        self.assertEqual(self.send().status, "outcome_unknown")
        self.assertEqual((self.status().status, self.status().evidence), ("outcome_unknown", "provider_pending"))
        self.raw.events.extend([
            {"@type": "updateMessageSendSucceeded", "old_message_id": -10, "message": message(901, chat=999)},
            {"@type": "updateMessageSendSucceeded", "old_message_id": True, "message": message(901)},
            {"@type": "updateMessageSendSucceeded", "old_message_id": -10, "message": message(True)},
            {"@type": "updateMessageSendSucceeded", "old_message_id": -10, "message": message(-1)},
            {"@type": "updateMessageSendSucceeded", "old_message_id": -10, "message": message(901,kind="messagePhoto")},
            {"@type": "updateMessageSendSucceeded", "old_message_id": -10, "message": message(901,is_outgoing=False)},
            {"@type": "updateMessageSendSucceeded", "old_message_id": -10, "message": message(901, sending_state=None)},
        ])
        result = self.status()
        self.assertEqual((result.status, result.evidence, result.message_id),
                         ("sent", "provider_confirmed", 901))
        self.raw.events.append({"@type": "updateMessageSendFailed", "old_message_id": -10,
                               "message": message(-10, sending_state={"@type":"messageSendingStateFailed","error":{"@type":"error","code":400}}), "error": {"@type":"error", "code":400, "message":"PRIVATE"}})
        self.assertEqual(self.status().status, "sent")
        self.assertEqual(self.send().status, "sent")
        self.assertEqual(sum(r["@type"] == "sendMessage" for r in self.raw.sent), 1)
        self.assertNotIn("PRIVATE", result.model_dump_json())

    def test_late_exact_failure_and_wrong_failed_chat_ignored(self):
        self.send()
        self.raw.events.append({"@type": "updateMessageSendFailed", "old_message_id": -10,
            "message": message(-10, chat=999, sending_state={"@type":"messageSendingStateFailed","error":{"@type":"error","code":400}}), "error": {"@type":"error", "code":400, "message":"PRIVATE"}})
        self.assertEqual((self.status().status,self.status().evidence), ("outcome_unknown","provider_pending"))
        self.raw.events.append({"@type": "updateMessageSendFailed", "old_message_id": -10,
            "message": message(-10, sending_state={"@type":"messageSendingStateFailed","error":{"@type":"error","code":400}}), "error": {"@type":"error", "code":400, "message":"PRIVATE"}})
        result = self.status()
        self.assertEqual((result.status, result.evidence), ("failed", "provider_failed"))
        self.assertIsNone(result.message_id)
        self.assertNotIn("PRIVATE", result.model_dump_json())

    def test_sending_id_recovers_lost_preliminary_reply(self):
        self.raw.mode = "lost_before_reply"
        self.send()
        request = next(r for r in self.raw.sent if r["@type"] == "sendMessage")
        self.raw.events.extend([
            {"@type":"updateNewMessage", "message": message(sending_state={
                "@type":"messageSendingStatePending", "sending_id":request["options"]["sending_id"]})},
            {"@type":"updateMessageSendSucceeded", "old_message_id":-10, "message":message(801, sending_state=None)},
        ])
        self.assertEqual(self.status().message_id, 801)

    def test_account_change_provider_replacement_and_restart_hide_facts(self):
        self.raw.mode = "sent"
        self.assertEqual(self.send().status, "sent")
        self.raw.account = 8
        self.assertEqual(self.status().evidence, "none")
        self.raw.account = 7
        self.assertEqual(self.status().evidence, "none")
        self.broker._client = TDLibClient(raw=Raw())
        self.assertEqual(self.status().evidence, "none")
        self.broker._drafts = OutgoingDraftRegistry(self.broker._artifact_store)
        self.assertEqual(self.status().evidence, "none")

    def test_invalid_requests_and_disabled_capability_have_no_send_or_approval(self):
        from pydantic import ValidationError
        from telegram_search_mcp.send_status_models import GetSendStatusRequest
        for bad in [True, 3, "draft_bad", TOKEN + "\n"]:
            with self.assertRaises(ValidationError):
                GetSendStatusRequest(draft_id=bad)
        with self.assertRaises(ValidationError):
            GetSendStatusRequest(draft_id=TOKEN, approved=True)
        self.broker._policy = RuntimePolicy(enabled_capabilities=("read",))
        self.assertEqual(self.status().evidence, "none")
        self.assertEqual(self.approvals, [])
        self.assertFalse(any(r["@type"] == "sendMessage" for r in self.raw.sent))

    def test_retention_is_initial_and_status_reads_never_renew_it(self):
        now = [1000.0]
        self.provider._send_observations._clock = lambda: now[0]
        self.raw.mode = "sent"
        self.send()
        now[0] += 899
        self.assertEqual(self.status().status, "sent")
        now[0] += 1
        self.assertEqual(self.status().evidence, "none")
        self.assertEqual(self.send().status, "sent")
        self.assertEqual(sum(r["@type"] == "sendMessage" for r in self.raw.sent), 1)

    def test_capacity_and_random_collision_refuse_before_raw_send(self):
        from telegram_search_mcp.send_observations import SendObservations
        self.provider._send_observations = SendObservations(capacity=1)
        self.provider._send_observations.register("occupied","old-extra",1,999,"messageText")
        with patch("telegram_search_mcp.tdjson.secrets.randbelow", return_value=0):
            result = self.send()
        self.assertEqual(result.status,"failed")
        self.assertEqual(self.status().evidence,"local_failed")
        self.assertFalse(any(r["@type"] == "sendMessage" for r in self.raw.sent))

    def test_auth_loss_restore_and_no_correlated_preliminary_remain_unknown(self):
        self.send()
        self.raw.events.extend([
            {"@type":"updateAuthorizationState","authorization_state":{"@type":"authorizationStateLoggingOut"}},
            {"@type":"updateAuthorizationState","authorization_state":{"@type":"authorizationStateReady"}},
            {"@type":"updateMessageSendSucceeded","old_message_id":-10,"message":message(901)},
        ])
        self.assertEqual(self.status().evidence,"none")
        self.assertEqual(self.status().evidence,"none")

    def test_drain_is_bounded_and_nonblocking_when_provider_lock_is_busy(self):
        import threading
        self.send()
        acquired = threading.Event(); release = threading.Event()
        def hold():
            with self.provider._lock:
                acquired.set(); release.wait(2)
        thread = threading.Thread(target=hold); thread.start()
        self.assertTrue(acquired.wait(1))
        try:
            observation = self.provider.get_send_observation(self.preview.draft_id)
            self.assertEqual(observation.status,"pending")
            from telegram_search_mcp.send_status_models import GetSendStatusRequest
            import time
            before = time.monotonic()
            response = self.broker._get_send_status(GetSendStatusRequest(draft_id=self.preview.draft_id),
                client_id=CLIENT, deadline=before+0.03)
            self.assertEqual(response.evidence,"none")
            self.assertLess(time.monotonic()-before,0.3)
        finally:
            release.set();thread.join(2)
        self.raw.events.extend({"@type":"updateMessageSendAcknowledged","chat_id":123,"message_id":-10}
                               for _ in range(70))
        self.provider.get_send_observation(self.preview.draft_id)
        self.assertEqual(len(self.raw.events),6)

    def test_unrelated_read_reduces_late_confirmation_without_status_drain(self):
        self.send()
        self.raw.events.append({"@type":"updateMessageSendSucceeded","old_message_id":-10,
                                "message":message(902)})
        self.provider.resolve_target(123)
        self.assertEqual(self.status().message_id,902)

    def test_pending_observation_never_overrides_concurrent_unknown_receipt(self):
        owner = DraftOwner(CLIENT, 7)
        self.broker._drafts.claim(self.preview.draft_id, owner=owner, approved=True, provider=self.provider, provider_epoch=self.provider.send_observation_epoch)
        self.provider._send_observations.register(self.preview.draft_id, "extra",1,123,"messageText")
        self.provider._send_observations.reduce({**message(sending_state={
            "@type":"messageSendingStatePending","sending_id":1}), "@extra":"extra"})
        original = self.broker._draft_owner
        calls = []
        def check(client):
            result = original(client)
            calls.append(client)
            if len(calls) == 2:
                self.broker._drafts.finish(self.preview.draft_id, owner=owner, status="outcome_unknown")
            return result
        with patch.object(self.broker,"_draft_owner",side_effect=check):
            result = self.status()
        self.assertEqual((result.status,result.evidence),("outcome_unknown","provider_pending"))

    def test_disabled_status_does_not_instantiate_provider(self):
        from telegram_search_mcp.send_status_models import GetSendStatusRequest
        def forbidden_factory():
            raise AssertionError("disabled operation initialized provider")
        broker = Broker(socket_path=self.root/"disabled.sock", artifact_store=self.broker._artifact_store,
                        policy=RuntimePolicy(enabled_capabilities=("read",)), client_factory=forbidden_factory,
                        verify_peer_uid=False)
        try:
            self.assertEqual(broker._get_send_status(GetSendStatusRequest(draft_id=TOKEN),client_id=CLIENT).evidence,"none")
        finally:
            broker.shutdown()

    def test_exact_extra_rejection_retains_no_error_text(self):
        original = self.raw.send
        def reject(request):
            if request["@type"] == "sendMessage":
                self.raw.sent.append(request)
                self.raw.events.append({"@type":"error","code":400,"message":"PRIVATE", "@extra":request["@extra"]})
            else:
                original(request)
        with patch.object(self.raw,"send",side_effect=reject):
            self.assertEqual(self.send().status,"failed")
        result = self.status()
        self.assertEqual((result.status,result.evidence),("failed","provider_failed"))
        self.assertNotIn("PRIVATE", result.model_dump_json())

    def test_malformed_unrelated_and_similarity_events_never_confirm(self):
        self.raw.mode = "lost_before_reply"
        self.send()
        for event in [
            {"@type":"updateNewMessage","message":message(sending_state={"@type":"messageSendingStatePending","sending_id":True})},
            {"@type":"updateMessageSendSucceeded","old_message_id":-10,"message":message(999)},
            {"@type":"updateDeleteMessages","chat_id":123,"message_ids":[-10],"is_permanent":True},
            {"@type":"updateMessageSendAcknowledged","chat_id":123,"message_id":-10},
        ]:
            self.raw.events.append(event)
        self.assertEqual(self.status().evidence,"none")

    def test_local_failure_retention_uses_elapsed_time_despite_wall_clock_rollback(self):
        now = [1000.0]
        self.broker._drafts._attempt_clock = lambda: now[0]
        self.broker._drafts._clock = lambda: 1000.0
        self.provider._send_observations._capacity = 1
        self.provider._send_observations.register("occupied","extra",1,999,"messageText")
        self.send()
        self.assertEqual(self.status().evidence,"local_failed")
        now[0] += 901
        self.broker._drafts._clock = lambda: 1.0
        self.assertEqual(self.status().evidence,"none")

    def test_pending_or_failed_final_and_incoherent_failure_never_confirm(self):
        self.send()
        for state in [{"@type":"messageSendingStatePending","sending_id":1},
                      {"@type":"messageSendingStateFailed","error":{"@type":"error","code":400}}]:
            self.raw.events.append({"@type":"updateMessageSendSucceeded","old_message_id":-10,
                                    "message":message(901,sending_state=state)})
        for state in [{"@type":"messageSendingStateFailed"},
                      {"@type":"messageSendingStateFailed","error":{"@type":"error","code":401}},
                      {"@type":"messageSendingStateFailed","error":{"@type":"error","code":True}}]:
            self.raw.events.append({"@type":"updateMessageSendFailed","old_message_id":-10,
                "message":message(901,sending_state=state),"error":{"@type":"error","code":400,"message":"PRIVATE"}})
        self.assertEqual((self.status().status,self.status().evidence),("outcome_unknown","provider_pending"))

    def test_auth_epoch_change_invalidates_even_preprovider_local_failure(self):
        self.provider._send_observations._capacity = 1
        self.provider._send_observations.register("occupied","extra",1,999,"messageText")
        self.send()
        self.assertEqual(self.status().evidence,"local_failed")
        self.raw.events.extend([
            {"@type":"updateAuthorizationState","authorization_state":{"@type":"authorizationStateLoggingOut"}},
            {"@type":"updateAuthorizationState","authorization_state":{"@type":"authorizationStateReady"}},
        ])
        self.assertEqual(self.status().evidence,"none")
        self.assertEqual(self.status().evidence,"none")

    def test_late_text_integrity_mismatch_does_not_bypass_approved_preview(self):
        self.send()
        self.raw.events.append({"@type":"updateMessageSendSucceeded","old_message_id":-10,
            "message":message(903,content={"@type":"messageText","text":{
                "@type":"formattedText","text":"DIFFERENT PRIVATE BODY","entities":[]}})})
        result = self.status()
        self.assertEqual((result.status,result.evidence,result.message_id),
                         ("outcome_unknown","provider_pending",None))
        self.raw.events.append({"@type":"updateMessageSendSucceeded","old_message_id":-10,"message":message(903)})
        self.assertEqual(self.status().message_id,903)
        self.assertEqual(sum(r["@type"] == "sendMessage" for r in self.raw.sent),1)

    def test_oversized_provider_text_is_rejected_before_encoding_or_hashing(self):
        self.send()
        self.raw.events.append({"@type":"updateMessageSendSucceeded","old_message_id":-10,
            "message":message(904,content={"@type":"messageText","text":{
                "@type":"formattedText","text":"x"*4097,"entities":[]}})})
        # Instrument the expensive primitive only after the approved draft was
        # hashed. A bounded reducer must reject this candidate before hashing.
        with patch("telegram_search_mcp.send_observations.hashlib.sha256",
                   side_effect=AssertionError("oversized provider text was hashed")):
            self.assertEqual(self.provider.resolve_target(123)["id"],123)
        self.assertEqual((self.status().status,self.status().evidence),
                         ("outcome_unknown","provider_pending"))

    def test_invalid_utf8_provider_text_does_not_abort_unrelated_read(self):
        self.send()
        self.raw.events.append({"@type":"updateMessageSendSucceeded","old_message_id":-10,
            "message":message(905,content={"@type":"messageText","text":{
                "@type":"formattedText","text":"invalid \ud800","entities":[]}})})
        self.assertEqual(self.provider.resolve_target(123)["id"],123)
        self.assertEqual((self.status().status,self.status().evidence),
                         ("outcome_unknown","provider_pending"))

    def test_real_unix_ipc_status_is_owned_closed_and_bounded_without_send(self):
        import threading
        import time
        from telegram_search_mcp.broker_client import BrokerUnavailable
        from telegram_search_mcp.send_status_models import GetSendStatusRequest
        errors = []
        deadlines = []
        original = self.broker._dispatch
        def dispatch(request):
            if request["operation"] == "get_send_status":
                deadlines.append(request["deadline"]-time.monotonic())
            return original(request)
        def serve():
            try:
                self.broker.serve_forever()
            except Exception as error:
                errors.append(error)
        proxies = []
        with patch.object(self.broker,"_dispatch",side_effect=dispatch):
            thread = threading.Thread(target=serve,daemon=True)
            thread.start()
            try:
                self.assertTrue(self.broker._ready.wait(2))
                self.assertEqual(errors,[])
                owner = BrokerClient(socket_path=self.root/"broker.sock",client_id=CLIENT,
                    policy=RuntimePolicy(),restart_callback=lambda:None,retry_backoff_seconds=0)
                foreign = BrokerClient(socket_path=self.root/"broker.sock",client_id=FOREIGN,
                    policy=RuntimePolicy(),restart_callback=lambda:None,retry_backoff_seconds=0)
                denied = BrokerClient(socket_path=self.root/"broker.sock",policy=RuntimePolicy(enabled_capabilities=("read",)),
                    restart_callback=lambda:None,retry_backoff_seconds=0)
                proxies.extend([owner,foreign,denied])
                prepared = owner.prepare_text_send(PrepareTextSendRequest(recipient=123,text="PRIVATE BODY"))
                self.assertEqual(prepared.status,"prepared")
                request = GetSendStatusRequest(draft_id=prepared.draft_id)
                for _ in range(2):
                    result = owner.get_send_status(request)
                    self.assertEqual((result.draft_id,result.status,result.evidence),
                                     (prepared.draft_id,"pending","local_pending"))
                self.assertTrue(deadlines)
                self.assertTrue(all(0 < value <= 30 for value in deadlines))
                self.assertEqual(foreign.get_send_status(request).evidence,"none")
                self.assertEqual(denied.get_send_status(request).evidence,"none")
                with self.assertRaises(BrokerUnavailable):
                    owner._request("get_send_status",{"draft_id":prepared.draft_id,"approved":True})
                with self.assertRaises(BrokerUnavailable):
                    owner._request("get_send_status_unknown",{"draft_id":prepared.draft_id})
                self.assertEqual(self.approvals,[])
                self.assertFalse(any(r["@type"] == "sendMessage" for r in self.raw.sent))
            finally:
                for proxy in proxies:
                    proxy.close()
                self.broker.shutdown()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])

    def test_status_ipc_envelope_is_strict_and_allows_only_named_operation(self):
        import time
        from telegram_search_mcp.broker_protocol import validate_request, BrokerProtocolError
        valid = {"version":2,"client_id":CLIENT,"request_id":"request_"+"r"*24,
                 "operation":"get_send_status","payload":{"draft_id":TOKEN},
                 "deadline":time.monotonic()+30,"broker_generation":"broker_"+"c"*32}
        self.assertEqual(validate_request(valid)["operation"],"get_send_status")
        for changes in [{"version":True},{"operation":"get_send_status_unknown"},
                        {"deadline":True},{"payload":[]},{"account_id":7}]:
            with self.subTest(changes=changes), self.assertRaises(BrokerProtocolError):
                validate_request({**valid,**changes})

    def test_proxy_rejects_mismatch_extra_payload_and_false_evidence(self):
        from telegram_search_mcp.send_status_models import GetSendStatusRequest
        service = BrokerClient(socket_path=self.root / "unused.sock")
        request = GetSendStatusRequest(draft_id=TOKEN)
        malformed = [
            {"draft_id": self.preview.draft_id, "status":"sent", "evidence":"provider_confirmed", "message_id":901,
             "detail":"exact provider confirmation; recipient delivery and read status are not asserted"},
            {"draft_id":TOKEN,"status":"outcome_unknown","evidence":"none","message_id":901,
             "detail":"outcome is unknown; no resend is authorized"},
            {"draft_id":TOKEN,"status":"outcome_unknown","evidence":"none","message_id":None,
             "detail":"PRIVATE"},
            {"draft_id":TOKEN,"status":"outcome_unknown","evidence":"none","message_id":None,
             "detail":"outcome is unknown; no resend is authorized","text":"PRIVATE"},
        ]
        for response in malformed:
            with self.subTest(response=response), patch.object(service,"_request",return_value=response):
                result = service.get_send_status(request)
                self.assertEqual((result.draft_id,result.status,result.evidence,result.message_id),
                                 (TOKEN,"outcome_unknown","none",None))
        service.close()
