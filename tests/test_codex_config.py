from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


class CodexConfigTests(unittest.TestCase):
    def test_example_is_project_scoped_and_exactly_allowlists_current_tools(self) -> None:
        path = Path(__file__).parents[1] / ".codex" / "config.toml.example"
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        server = config["mcp_servers"]["telegram_search"]

        self.assertEqual(
            server["enabled_tools"],
            ["_manifest", "resolve_target", "discover_targets", "search_correspondence",
             "get_attachment", "get_message_context", "read_attachment", "analyze_media",
             "create_local_artifact", "begin_local_upload", "append_local_upload", "finish_local_upload",
             "prepare_reply_artifact_send", "get_reply_artifact_draft", "update_reply_artifact_draft", "refresh_reply_artifact_draft", "prepare_reply_text_send", "get_reply_draft", "update_reply_draft", "refresh_reply_draft", "prepare_text_send", "send_prepared_text", "prepare_artifact_send", "send_prepared_artifact", "read_messages", "read_history", "read_reply_chain", "list_topics", "read_topic_history", "search_messages", "list_chats", "search_chats", "verify_target", "read_target_messages", "read_attachment_page", "read_spreadsheet", "read_presentation", "list_drafts", "get_draft", "get_send_status", "cancel_draft", "update_draft", "refresh_draft"],
        )
        self.assertEqual(
            server["command"],
            "/ABSOLUTE/PATH/TO/telegram-search/.venv/bin/telegram-search-mcp",
        )
        self.assertEqual(server["cwd"], "/ABSOLUTE/PATH/TO/telegram-search")
        self.assertTrue(server["required"])
        self.assertEqual(server["startup_timeout_sec"], 30)
        self.assertEqual(server["tool_timeout_sec"], 600)
        self.assertNotIn("env", server)
        self.assertNotIn("api_id", str(server).casefold())
        self.assertNotIn("api_hash", str(server).casefold())


if __name__ == "__main__":
    unittest.main()
