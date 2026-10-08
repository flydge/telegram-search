from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from telegram_search_mcp.approval_prompt import confirm_approved_send


class ApprovalPromptTests(unittest.TestCase):
    def test_only_explicit_local_dialog_approval_returns_true(self) -> None:
        arguments = dict(recipient_title="Synthetic Recipient", recipient=123,
                         display_name="report.txt", size_bytes=14, sha256="a" * 64,
                         caption="Reviewed")
        with patch("telegram_search_mcp.approval_prompt.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, b"APPROVED\n")) as run:
            self.assertTrue(confirm_approved_send(**arguments))
            self.assertIn("report.txt", run.call_args.args[0][-1])
            self.assertIn("a" * 64, run.call_args.args[0][-1])
        with patch("telegram_search_mcp.approval_prompt.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, b"DENIED\n")):
            self.assertFalse(confirm_approved_send(**arguments))


if __name__ == "__main__":
    unittest.main()
