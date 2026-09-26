from __future__ import annotations

import subprocess
import unittest

from telegram_search_mcp.keychain import KeychainError, read_api_credentials
from telegram_search_mcp.config import KEYCHAIN_SERVICE


class KeychainTests(unittest.TestCase):
    def test_reads_only_the_two_fixed_keychain_accounts(self) -> None:
        calls: list[list[str]] = []
        values = {"api_id": "123456", "api_hash": "hash-value"}

        def runner(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, values[args[-1]] + "\n", "")

        credentials = read_api_credentials(runner=runner)

        self.assertEqual(credentials.api_id, 123456)
        self.assertEqual(credentials.api_hash, "hash-value")
        self.assertEqual(
            calls,
            [
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-w",
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-a",
                    "api_id",
                ],
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-w",
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-a",
                    "api_hash",
                ],
            ],
        )

    def test_failure_never_includes_keychain_output(self) -> None:
        leaked = "super-secret-value"

        def runner(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 44, leaked, leaked)

        with self.assertRaises(KeychainError) as caught:
            read_api_credentials(runner=runner)

        self.assertNotIn(leaked, str(caught.exception))

    def test_invalid_api_id_does_not_preserve_the_sensitive_conversion_cause(self) -> None:
        sentinel = "INVALID_API_ID_SENTINEL_784bd1"

        def runner(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, sentinel + "\n", "")

        with self.assertRaises(KeychainError) as caught:
            read_api_credentials(runner=runner)

        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(sentinel, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
