from __future__ import annotations

import os
import runpy
import stat
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from telegram_search_mcp.config import ConfigurationError, ensure_private_directory
from telegram_search_mcp import config


class OwnerPathsTests(unittest.TestCase):
    def test_runtime_paths_follow_the_current_owner_home(self) -> None:
        # An invented home verifies that another account cannot use author storage.
        with patch.object(Path, "home", return_value=Path("/fixture-home/sample-owner")):
            paths = runpy.run_path(config.__file__)
        self.assertEqual(paths["APP_SUPPORT_ROOT"], Path("/fixture-home/sample-owner/Library/Application Support/TelegramSearchMCP"))
        self.assertEqual(paths["TDLIB_SESSION_DIRECTORY"], Path("/fixture-home/sample-owner/Library/Application Support/TelegramSearchMCP/tdlib"))
        self.assertEqual(paths["BROKER_SOCKET_PATH"], Path("/fixture-home/sample-owner/Library/Application Support/TelegramSearchMCP/run/broker.sock"))
        self.assertEqual(paths["KEYCHAIN_SERVICE"], "com.sample-owner.telegram-search-mcp")
        self.assertEqual(paths["LAUNCH_AGENT_LABEL"], "com.sample-owner.telegram-search-mcp.broker")
        self.assertEqual(paths["LAUNCH_AGENT_PLIST"], Path("/fixture-home/sample-owner/Library/LaunchAgents/com.sample-owner.telegram-search-mcp.broker.plist"))



class PrivateDirectoryTests(unittest.TestCase):
    def test_creates_and_repairs_owner_only_directory_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent) / "private"
            path.mkdir(mode=0o755)

            ensure_private_directory(path)

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_rejects_symlink_storage_directory(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            os.symlink(real, link)

            with self.assertRaises(ConfigurationError):
                ensure_private_directory(link)


if __name__ == "__main__":
    unittest.main()
