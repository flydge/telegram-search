from __future__ import annotations

import asyncio
import contextlib
import io
import os
import tempfile
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from typing import Any
from pathlib import Path
from unittest.mock import patch

from mcp import Client
from PIL import Image

from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.schemas import (
    AttachmentRequest,
    AttachmentResponse,
    MessageContextRequest,
    MessageContextResponse,
    ReadAttachmentRequest,
    ReadAttachmentResponse,
    AttachmentImage,
    Coverage,
    DiscoverTargetsRequest,
    ResolveTargetRequest,
    SearchRequest,
    SearchResponse,
    TargetDiscoveryResponse,
    TargetResolutionResponse,
)
from telegram_search_mcp.server import build_server
from telegram_search_mcp import server as server_module


class StubService:
    def __init__(self) -> None:
        self.last_request: SearchRequest | None = None
        self.last_resolve_request: ResolveTargetRequest | None = None
        self.last_discovery_request: DiscoverTargetsRequest | None = None

    def resolve(self, request: ResolveTargetRequest) -> TargetResolutionResponse:
        self.last_resolve_request = request
        return TargetResolutionResponse.model_validate(
            {
                "status": "not_found",
                "coverage": {
                    "complete": True,
                    "saved_messages": {"status": "complete", "detail": "complete test fixture"},
                    "exact_username": {"status": "complete", "detail": "complete test fixture"},
                    "search_chats": {"status": "complete", "detail": "complete test fixture"},
                    "search_chats_on_server": {"status": "complete", "detail": "complete test fixture"},
                    "recent_main": {"status": "complete", "detail": "complete test fixture"},
                    "hydration": {"status": "complete", "detail": "complete test fixture"},
                    "detail": "complete test fixture",
                },
            }
        )

    def search(self, request: SearchRequest) -> SearchResponse:
        self.last_request = request
        return SearchResponse(
            status="no_match",
            coverage=Coverage(
                complete=True,
                text_status="complete",
                metadata_status="not_requested",
                date_from=request.date_from,
                date_to=request.date_to,
                detail="complete test fixture",
            ),
            matches=[],
        )

    def discover(self, request: DiscoverTargetsRequest) -> TargetDiscoveryResponse:
        self.last_discovery_request = request
        return TargetDiscoveryResponse.model_validate(
            {
                "status": "page",
                "candidates": [],
                "coverage": {
                    "complete": False,
                    "catalog": {
                        "main": {
                            "status": "scanning",
                            "scanned_count": 0,
                            "emitted_count": 0,
                            "end_reached": False,
                        },
                        "archive": {
                            "status": "scanning",
                            "scanned_count": 0,
                            "emitted_count": 0,
                            "end_reached": False,
                        },
                    },
                    "global_messages": {
                        "lanes": [
                            {
                                "hypothesis_index": index,
                                "chat_list": chat_list,
                                "status": "not_started",
                                "pages_scanned": 0,
                                "hits_seen": 0,
                            }
                            for index in range(2)
                            for chat_list in ("main", "archive")
                        ]
                    },
                    "hydration": "complete",
                    "detail": "discovery coverage details redacted",
                },
                "next_cursor": "scan_abcdefghijklmnopqrstuvwx",
            }
        )

    def close(self) -> None:
        pass

    def get_attachment(self, request: AttachmentRequest) -> AttachmentResponse:
        return AttachmentResponse(
            status="too_large", anchor=request.anchor, coverage="none",
            detail="attachment exceeds the transfer limit",
        )

    def get_message_context(self, request: MessageContextRequest) -> MessageContextResponse:
        return MessageContextResponse(
            status="complete", anchor=request.anchor, messages=[],
            coverage_complete=True, detail="bounded context around exact anchor",
        )

    def read_attachment(self, request: ReadAttachmentRequest) -> ReadAttachmentResponse:
        return ReadAttachmentResponse(
            status="complete", text="synthetic text", processed_bytes=14, total_bytes=14,
            coverage_complete=True, detail="bounded local document read",
        )


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_attachment_accepts_only_exact_anchor(self) -> None:
        async with Client(self.server) as client:
            result = await client.call_tool("get_attachment", {"anchor": {"chat_id": 7, "message_id": 8}})

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["status"], "too_large")
        self.assertEqual(result.structured_content["anchor"], {"chat_id": 7, "message_id": 8})

    async def test_get_message_context_accepts_bounded_radius(self) -> None:
        async with Client(self.server) as client:
            result = await client.call_tool("get_message_context", {
                "anchor": {"chat_id": 7, "message_id": 8}, "radius": 2,
            })

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["status"], "complete")

    async def test_read_attachment_returns_bounded_text_by_artifact_id(self) -> None:
        artifact_id = "artifact_" + "a" * 32 + "_" + "b" * 64 + "_14"
        async with Client(self.server) as client:
            result = await client.call_tool("read_attachment", {"artifact_id": artifact_id})

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["text"], "synthetic text")

    async def test_read_attachment_exposes_actual_image_content_to_mcp_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_id = "artifact_" + "a" * 32 + "_" + "b" * 64 + "_80"
            path = root / artifact_id
            Image.new("RGB", (2, 2), "red").save(path, format="PNG")
            os.chmod(path, 0o600)

            class ImageService(StubService):
                def read_attachment(self, request: ReadAttachmentRequest) -> ReadAttachmentResponse:
                    return ReadAttachmentResponse(
                        status="complete", coverage_complete=True, detail="synthetic image",
                        images=[AttachmentImage(
                            artifact_id=request.artifact_id, artifact_path=str(path),
                            mime_type="image/png",
                        )],
                    )

            server = build_server(service_factory=ImageService, artifact_root=root)
            async with Client(server) as client:
                result = await client.call_tool("read_attachment", {"artifact_id": artifact_id})

            self.assertFalse(result.is_error)
            self.assertTrue(any(block.type == "image" for block in result.content))
    async def asyncSetUp(self) -> None:
        self.service = StubService()
        self.server = build_server(service_factory=lambda: self.service)

    async def test_tools_list_exposes_strict_search_and_attachment_tools(self) -> None:
        async with Client(self.server) as client:
            listing = await client.list_tools()

        self.assertEqual(
            [tool.name for tool in listing.tools],
            ["_manifest", "resolve_target", "discover_targets", "search_correspondence", "get_attachment", "get_message_context", "read_attachment", "analyze_media", "create_local_artifact", "begin_local_upload", "append_local_upload", "finish_local_upload", "prepare_text_send", "send_prepared_text", "prepare_artifact_send", "send_prepared_artifact", "read_messages", "read_history", "read_reply_chain", "list_topics", "read_topic_history", "search_messages", "list_chats", "search_chats", "verify_target", "read_target_messages", "read_attachment_page", "read_spreadsheet", "read_presentation", "prepare_reply_artifact_send", "get_reply_artifact_draft", "update_reply_artifact_draft", "refresh_reply_artifact_draft", "prepare_reply_text_send", "get_reply_draft", "update_reply_draft", "refresh_reply_draft", "list_drafts", "get_send_status", "get_draft", "cancel_draft", "update_draft", "refresh_draft"],
        )
        for tool in listing.tools[:4]:
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertTrue(tool.annotations.idempotent_hint)
            self.assertTrue(tool.annotations.open_world_hint)
            self.assertFalse(tool.input_schema["additionalProperties"])
        resolve_tool = listing.tools[1]
        self.assertEqual(set(resolve_tool.input_schema["properties"]), {"target"})
        self.assertEqual(resolve_tool.input_schema["properties"]["target"]["type"], "string")
        self.assertFalse(resolve_tool.input_schema["additionalProperties"])
        self.assertIn("bounded", resolve_tool.description.casefold())
        self.assertIn("untrusted", resolve_tool.description.casefold())
        discovery_tool = listing.tools[2]
        self.assertEqual(
            set(discovery_tool.input_schema["properties"]),
            {"hypotheses", "scope", "cursor"},
        )
        self.assertFalse(discovery_tool.input_schema["additionalProperties"])
        description = discovery_tool.description.casefold()
        for phrase in (
            "codex",
            "lexical",
            "untrusted",
            "complete main/archive coverage",
            "never chooses or searches",
        ):
            self.assertIn(phrase, description)
        search_tool = listing.tools[3]
        self.assertFalse(search_tool.input_schema["additionalProperties"])
        self.assertIn("numeric", search_tool.description.casefold())
        self.assertIn("exact", search_tool.description.casefold())
        self.assertEqual(
            set(search_tool.input_schema["properties"]),
            {
                "target",
                "query",
                "date_from",
                "date_to",
                "limit",
                "context_messages",
                "require_complete",
            },
        )
        properties = search_tool.input_schema["properties"]
        self.assertEqual(properties["limit"]["minimum"], 1)
        self.assertEqual(properties["limit"]["maximum"], 20)
        self.assertEqual(properties["context_messages"]["minimum"], 0)
        self.assertEqual(properties["context_messages"]["maximum"], 4)
        target_types = properties["target"]["anyOf"]
        self.assertEqual(target_types[0]["pattern"], r"^@[A-Za-z0-9_]+$")
        self.assertEqual(target_types[0]["minLength"], 6)
        self.assertEqual(target_types[0]["maxLength"], 33)
        self.assertEqual(target_types[1]["minimum"], -(2**63))
        self.assertEqual(target_types[1]["maximum"], 2**63 - 1)
        self.assertEqual(properties["date_from"]["anyOf"][0]["format"], "date-time")
        self.assertEqual(properties["require_complete"]["type"], "boolean")
        query_schema = search_tool.input_schema["$defs"]["SearchQuery"]
        self.assertFalse(query_schema["additionalProperties"])
        self.assertEqual(
            query_schema["properties"]["contains_number"]["anyOf"][0]["type"],
            "boolean",
        )
        self.assertTrue(
            query_schema["properties"]["contains_number"]["anyOf"][0]["const"]
        )
        media_types = query_schema["properties"]["media_type"]["anyOf"][0]["enum"]
        self.assertNotIn("other", media_types)
        attachment_tool = listing.tools[4]
        self.assertFalse(attachment_tool.annotations.read_only_hint)
        self.assertFalse(attachment_tool.input_schema["additionalProperties"])
        self.assertEqual(set(attachment_tool.input_schema["properties"]), {"anchor"})

    def test_default_service_is_a_lazy_broker_proxy(self) -> None:
        service = server_module._default_service()

        self.assertIsInstance(service, BrokerClient)

    async def test_server_description_routes_discovery_before_exact_chat_search(self) -> None:
        async with Client(self.server) as client:
            description = client.server_info.description

        self.assertIsNotNone(description)
        self.assertIn("account-wide", description.casefold())
        self.assertIn("exact-chat", description.casefold())

    async def test_resolve_description_routes_only_direct_targets(self) -> None:
        async with Client(self.server) as client:
            listing = await client.list_tools()

        description = listing.tools[1].description
        self.assertIn("saved messages", description.casefold())
        self.assertIn("exact @username", description.casefold())
        self.assertIn("discover_targets", description)
        self.assertNotIn("natural-language", description.casefold())
        self.assertNotIn("fuzzy", description.casefold())

    async def test_manifest_declares_account_wide_lexical_evidence_contract(self) -> None:
        async with Client(self.server) as client:
            result = await client.call_tool("_manifest", {})

        self.assertFalse(result.is_error)
        manifest = result.structured_content
        self.assertEqual(
            manifest["tools"],
            ["_manifest", "resolve_target", "discover_targets", "search_correspondence", "get_attachment", "get_message_context", "read_attachment", "analyze_media", "create_local_artifact", "begin_local_upload", "append_local_upload", "finish_local_upload", "prepare_reply_artifact_send", "get_reply_artifact_draft", "update_reply_artifact_draft", "refresh_reply_artifact_draft", "prepare_reply_text_send", "get_reply_draft", "update_reply_draft", "refresh_reply_draft", "prepare_text_send", "send_prepared_text", "prepare_artifact_send", "send_prepared_artifact", "read_messages", "read_history", "read_reply_chain", "list_topics", "read_topic_history", "search_messages", "list_chats", "search_chats", "verify_target", "read_target_messages", "read_attachment_page", "read_spreadsheet", "read_presentation", "list_drafts", "get_draft", "get_send_status", "cancel_draft", "update_draft", "refresh_draft"],
        )
        self.assertFalse(manifest["read_only"])
        self.assertEqual(manifest["authorization_required"], "authorizationStateReady")
        self.assertIn("untrusted", manifest["trust_boundary"].casefold())
        self.assertEqual(manifest["semantic_layer"], "Codex agent")
        provider_capabilities = manifest["provider_capabilities"]
        self.assertIn("searchMessages", provider_capabilities)
        self.assertIn("Main", provider_capabilities)
        self.assertIn("Archive", provider_capabilities)
        self.assertIn("lexical", provider_capabilities.casefold())
        self.assertEqual(
            manifest["automatic_selection_policy"],
            "complete coverage and one strongly corroborated candidate; otherwise clarification",
        )
        self.assertEqual(manifest["catalog_page_size"], 15)
        self.assertEqual(manifest["global_message_page_size"], 10)
        self.assertEqual(manifest["scan_ttl_seconds"], 300)
        self.assertEqual(manifest["scan_capacity"], 4)
        self.assertFalse(manifest["persistent_private_index"])
        exact_chat_analysis = manifest["exact_chat_analysis"]
        self.assertIn("numeric", exact_chat_analysis.casefold())
        self.assertIn("transient", exact_chat_analysis.casefold())
        self.assertIn("exact chat", exact_chat_analysis.casefold())
        forbidden = " ".join(manifest["forbidden"])
        self.assertIn("caller-controlled attachment paths", forbidden.casefold())
        self.assertIn("null-list global search", forbidden.casefold())
        self.assertIn("caller-controlled provider controls", forbidden.casefold())
        self.assertIn("semantic scoring", forbidden.casefold())
        self.assertIn("persistent private evidence", forbidden.casefold())
        self.assertIn("secret chats", forbidden.casefold())
        self.assertIn("global public search", forbidden.casefold())
        self.assertIn("raw execute", forbidden.casefold())
        self.assertIn("mutations", forbidden.casefold())
        self.assertIn("read receipts", forbidden.casefold())

    async def test_manifest_is_local_and_does_not_connect_to_the_broker(self) -> None:
        with patch.object(
            server_module,
            "BrokerClient",
            side_effect=AssertionError("manifest must stay local"),
        ):
            server = build_server()
            async with Client(server) as client:
                result = await client.call_tool("_manifest", {})

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["tools"], [
            "_manifest",
            "resolve_target",
            "discover_targets",
            "search_correspondence",
            "get_attachment",
            "get_message_context",
            "read_attachment",
            "analyze_media",
            "create_local_artifact",
            "begin_local_upload",
            "append_local_upload",
            "finish_local_upload",
            "prepare_reply_artifact_send",
            "get_reply_artifact_draft",
            "update_reply_artifact_draft",
            "refresh_reply_artifact_draft",
            "prepare_reply_text_send",
            "get_reply_draft",
            "update_reply_draft",
            "refresh_reply_draft",
            "prepare_text_send",
            "send_prepared_text",
            "prepare_artifact_send",
            "send_prepared_artifact",
            "read_messages",
            "read_history",
            "read_reply_chain",
            "list_topics",
            "read_topic_history",
            "search_messages",
            "list_chats",
            "search_chats",
            "verify_target",
            "read_target_messages",
            "read_attachment_page",
            "read_spreadsheet",
            "read_presentation",
            "list_drafts",
            "get_draft",
            "get_send_status",
            "cancel_draft",
            "update_draft",
            "refresh_draft",
        ])

    async def test_resolve_target_validates_and_forwards_only_a_request(self) -> None:
        async with Client(self.server) as client:
            result = await client.call_tool("resolve_target", {"target": " Saved Messages "})

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["status"], "not_found")
        self.assertIsInstance(self.service.last_resolve_request, ResolveTargetRequest)
        self.assertEqual(self.service.last_resolve_request.target, "Saved Messages")

    async def test_resolve_target_rejects_unscoped_and_extra_inputs_before_service(self) -> None:
        invalid_arguments = (
            {"target": 123},
            {"target": True},
            {"target": "chat*name"},
            {"target": "api_hash=RESOLVE_SECRET_SENTINEL"},
            {"target": "/tmp/telegram"},
            {"target": "account:personal"},
            {"target": "known chat", "api_hash": "RESOLVE_SECRET_SENTINEL"},
            {"target": "known chat", "filesystem_path": "/tmp/telegram"},
            {"target": "known chat", "account": "personal"},
            {"target": "known chat", "limit": 1},
            {"target": "\u0000\u0001"},
            {"target": "\u202e\u2066"},
            {"target": " \t-—…! \n"},
        )
        async with Client(self.server) as client:
            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments):
                    result = await client.call_tool("resolve_target", arguments)
                    self.assertTrue(result.is_error)
                    self.assertNotIn("RESOLVE_SECRET_SENTINEL", result.model_dump_json())

        self.assertIsNone(self.service.last_resolve_request)

    async def test_discovery_tool_accepts_only_hypotheses_scope_and_cursor(self) -> None:
        async with Client(self.server) as client:
            result = await client.call_tool(
                "discover_targets",
                {"hypotheses": ["garden group", "seed exchange"], "scope": "both"},
            )

        self.assertFalse(result.is_error)
        self.assertIsInstance(self.service.last_discovery_request, DiscoverTargetsRequest)
        self.assertEqual(
            self.service.last_discovery_request.hypotheses,
            ["garden group", "seed exchange"],
        )
        self.assertEqual(self.service.last_discovery_request.scope, "both")
        self.assertIsNone(self.service.last_discovery_request.cursor)

    async def test_discovery_rejects_raw_provider_controls_without_echo(self) -> None:
        sentinel = "SYNTHETIC_SECRET_SENTINEL"
        async with Client(self.server) as client:
            result = await client.call_tool(
                "discover_targets",
                {
                    "hypotheses": ["garden", "seeds"],
                    "offset": sentinel,
                    "limit": 100,
                    "filter": "photo",
                    "tdlib_function": "searchMessages",
                },
            )

        self.assertTrue(result.is_error)
        self.assertIsNone(self.service.last_discovery_request)
        self.assertNotIn(sentinel, result.model_dump_json())

    async def test_search_tool_validates_and_forwards_the_approved_contract(self) -> None:
        date_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
        async with Client(self.server) as client:
            result = await client.call_tool(
                "search_correspondence",
                {
                    "target": -1001,
                    "query": {"text": "needle"},
                    "date_from": date_from.isoformat(),
                    "limit": 3,
                    "context_messages": 1,
                    "require_complete": True,
                },
            )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["status"], "no_match")
        self.assertEqual(self.service.last_request.target, -1001)
        self.assertEqual(self.service.last_request.limit, 3)
        self.assertEqual(self.service.last_request.context_messages, 1)

    async def test_search_tool_rejects_credentials_and_arbitrary_fields(self) -> None:
        sentinel = "MCP_API_HASH_SENTINEL_9f5717"
        async with Client(self.server) as client:
            result = await client.call_tool(
                "search_correspondence",
                {
                    "target": -1001,
                    "query": {"text": "needle"},
                    "api_hash": sentinel,
                    "tdlib_function": "sendMessage",
                },
            )

        self.assertTrue(result.is_error)
        self.assertIsNone(self.service.last_request)
        self.assertNotIn(sentinel, result.model_dump_json())

    async def test_search_tool_rejects_coercible_scalar_and_wrong_shape_inputs(self) -> None:
        invalid_values = (
            ("target", True),
            ("target", 1.5),
            ("limit", True),
            ("limit", "3"),
            ("context_messages", False),
            ("context_messages", "1"),
            ("require_complete", "false"),
            ("require_complete", 1),
            ("date_from", 1_750_000_000),
            ("date_to", {"year": 2026}),
            ("query", ["needle"]),
        )
        async with Client(self.server) as client:
            for field, value in invalid_values:
                arguments = {"target": -1001, "query": {"text": "needle"}}
                arguments[field] = value
                with self.subTest(field=field, value=value):
                    result = await client.call_tool("search_correspondence", arguments)
                    self.assertTrue(result.is_error)

        self.assertIsNone(self.service.last_request)

    async def test_search_tool_rejects_sdk_preparsed_query_and_timestamp_string(self) -> None:
        invalid_arguments = (
            {
                "target": -1001,
                "query": '{"text":"needle"}',
            },
            {
                "target": -1001,
                "query": {"text": "needle"},
                "date_from": "1750000000",
            },
        )
        async with Client(self.server) as client:
            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments):
                    result = await client.call_tool("search_correspondence", arguments)
                    self.assertTrue(result.is_error)
                    self.assertEqual(
                        result.content[0].text,
                        "Error executing tool search_correspondence",
                    )

        self.assertIsNone(self.service.last_request)

    def test_check_redacts_broker_failures_in_subprocess(self) -> None:
        sentinel = "CLI_SECRET_SENTINEL_d3a0b7"
        scripts = (
            f"""
from telegram_search_mcp import server
from telegram_search_mcp.broker_client import BrokerUnavailable
class FakeClient:
    def check(self):
        raise BrokerUnavailable({sentinel!r})
    def close(self):
        pass
server.BrokerClient = lambda **kwargs: FakeClient()
raise SystemExit(server._run_check())
""",
            f"""
from telegram_search_mcp import server
from telegram_search_mcp.broker_client import BrokerUnavailable
server.BrokerClient = lambda **kwargs: (_ for _ in ()).throw(BrokerUnavailable({sentinel!r}))
raise SystemExit(server._run_check())
""",
        )
        for script in scripts:
            with self.subTest(script=script.splitlines()[1]):
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(
                    result.stderr,
                    "INITIALIZING_TDLIB\nTDLIB_CHECK_FAILED\n",
                )
                self.assertNotIn(sentinel, result.stdout + result.stderr)

    def test_check_uses_broker_without_constructing_a_tdlib_client(self) -> None:
        class ReadyBroker:
            def check(self) -> str:
                return "ready"

            def close(self) -> None:
                pass

        stderr = io.StringIO()
        with patch.object(
            server_module, "BrokerClient", return_value=ReadyBroker()
        ), patch(
            "telegram_search_mcp.tdjson.TDLibClient.from_defaults",
            side_effect=AssertionError("must not construct TDLib in proxy"),
        ), contextlib.redirect_stderr(stderr):
            result = server_module._run_check()

        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "INITIALIZING_TDLIB\nAUTHORIZATION_READY\n")


if __name__ == "__main__":
    unittest.main()
