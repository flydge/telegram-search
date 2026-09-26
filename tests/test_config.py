from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from telegram_search_mcp import config
from telegram_search_mcp.config import ConfigurationError, ensure_private_directory


class PortableRuntimeTests(unittest.TestCase):
    def test_runtime_paths_follow_the_installing_users_home(self) -> None:
        root, keychain_service, agent_label, agent_plist = config._owner_paths(
            Path("/fixture/account")
        )

        self.assertEqual(root, Path("/fixture/account/Library/Application Support/TelegramSearchMCP"))
        self.assertEqual(keychain_service, "com.account.telegram-search-mcp")
        self.assertEqual(agent_label, "com.account.telegram-search-mcp.broker")
        self.assertEqual(
            agent_plist,
            Path("/fixture/account/Library/LaunchAgents/com.account.telegram-search-mcp.broker.plist"),
        )


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
