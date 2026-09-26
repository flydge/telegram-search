from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


class CodexConfigTests(unittest.TestCase):
    def test_example_is_project_scoped_and_exactly_allowlists_four_tools(self) -> None:
        path = Path(__file__).parents[1] / ".codex" / "config.toml.example"
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        server = config["mcp_servers"]["telegram_search"]

        self.assertEqual(
            server["enabled_tools"],
            ["_manifest", "resolve_target", "discover_targets", "search_correspondence"],
        )
        self.assertTrue(Path(server["cwd"]).is_absolute())
        self.assertEqual(
            Path(server["command"]),
            Path(server["cwd"]) / ".venv" / "bin" / "telegram-search-mcp",
        )
        self.assertTrue(server["required"])
        self.assertEqual(server["startup_timeout_sec"], 30)
        self.assertEqual(server["tool_timeout_sec"], 600)
        self.assertNotIn("env", server)
        self.assertNotIn("api_id", str(server).casefold())
        self.assertNotIn("api_hash", str(server).casefold())


if __name__ == "__main__":
    unittest.main()
