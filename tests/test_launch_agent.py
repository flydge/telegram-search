from __future__ import annotations

import importlib
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from telegram_search_mcp.config import LAUNCH_AGENT_LABEL


class LaunchAgentTests(unittest.TestCase):
    def test_pyproject_exposes_the_broker_lifecycle_command(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with (repository / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)

        self.assertEqual(
            project["project"]["scripts"]["telegram-search-broker"],
            "telegram_search_mcp.launch_agent:main",
        )

    def test_generated_plist_is_valid_private_and_contains_no_sensitive_inputs(self) -> None:
        try:
            launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        except ModuleNotFoundError:
            self.fail("launch agent support is not implemented")

        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            plist_path = root / f"{LAUNCH_AGENT_LABEL}.plist"
            venv_python = root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(Path(sys.executable))
            (root / ".venv" / "pyvenv.cfg").write_text(
                "\n".join(
                    (
                        f"home = {Path(sys.base_prefix) / 'bin'}",
                        "include-system-site-packages = false",
                        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    )
                ),
                encoding="utf-8",
            )
            package = (
                root
                / ".venv"
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
                / "telegram_search_mcp"
            )
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            launch_agent.write_launch_agent(
                plist_path=plist_path,
                python_executable=venv_python,
            )
            payload = plistlib.loads(plist_path.read_bytes())
            lint = subprocess.run(
                ["plutil", "-lint", str(plist_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            mode = stat.S_IMODE(plist_path.stat().st_mode)
            import_check = subprocess.run(
                [
                    payload["ProgramArguments"][0],
                    "-c",
                    "import telegram_search_mcp",
                ],
                cwd=root,
                env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(lint.returncode, 0, lint.stderr)
        self.assertEqual(import_check.returncode, 0, import_check.stderr)
        self.assertEqual(payload["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(
            payload["ProgramArguments"],
            [str(venv_python.absolute()), "-m", "telegram_search_mcp.launch_agent", "run"],
        )
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(payload["ProcessType"], "Background")
        self.assertEqual(payload["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(payload["Umask"], 0o077)
        self.assertEqual(mode, 0o600)
        serialized = plistlib.dumps(payload).decode("utf-8")
        for forbidden in (
            "api_id",
            "api_hash",
            "target",
            "query",
            "message",
            "snippet",
            "cursor",
        ):
            self.assertNotIn(forbidden, serialized.casefold())

    def test_install_preflights_then_uses_argument_list_launchctl_calls(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        calls: list[list[str]] = []
        preflights: list[str] = []
        print_results = iter((0, 113))

        def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            return subprocess.CompletedProcess(
                arguments,
                next(print_results, 113) if arguments[1] == "print" else 0,
                "",
                "",
            )

        with tempfile.TemporaryDirectory() as parent:
            plist_path = Path(parent) / "broker.plist"
            launch_agent.install_launch_agent(
                plist_path=plist_path,
                python_executable=Path(sys.executable),
                runner=runner,
                preflight=lambda: preflights.append("available"),
                uid=501,
            )

        self.assertEqual(preflights, ["available"])
        self.assertEqual(
            calls,
            [
                ["/bin/launchctl", "bootout", f"gui/501/{LAUNCH_AGENT_LABEL}"],
                ["/bin/launchctl", "print", f"gui/501/{LAUNCH_AGENT_LABEL}"],
                ["/bin/launchctl", "print", f"gui/501/{LAUNCH_AGENT_LABEL}"],
                ["/bin/launchctl", "bootstrap", "gui/501", str(plist_path)],
                [
                    "/bin/launchctl",
                    "kickstart",
                    "-k",
                    f"gui/501/{LAUNCH_AGENT_LABEL}",
                ],
            ],
        )

    def test_install_stops_before_bootstrap_when_old_service_never_exits(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        operations: list[str] = []

        def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            operations.append(arguments[1])
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with tempfile.TemporaryDirectory() as parent, patch("time.sleep", return_value=None):
            with self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.install_launch_agent(
                    plist_path=Path(parent) / "broker.plist",
                    python_executable=Path(sys.executable),
                    runner=runner,
                    preflight=lambda: None,
                    uid=501,
                )

        self.assertEqual(operations[0], "bootout")
        self.assertIn("print", operations)
        self.assertNotIn("bootstrap", operations)
        self.assertNotIn("kickstart", operations)

    def test_existing_symlink_plist_is_rejected_without_touching_target(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            target = root / "sentinel"
            target.write_text("keep", encoding="utf-8")
            plist_path = root / "broker.plist"
            plist_path.symlink_to(target)

            with self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.write_launch_agent(
                    plist_path=plist_path,
                    python_executable=Path(sys.executable),
                )

            self.assertTrue(plist_path.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_group_or_world_writable_launch_agent_directory_is_rejected(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        with tempfile.TemporaryDirectory() as parent:
            unsafe = Path(parent) / "unsafe"
            unsafe.mkdir(mode=0o700)
            unsafe.chmod(0o777)
            plist_path = unsafe / "broker.plist"

            with self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.write_launch_agent(
                    plist_path=plist_path,
                    python_executable=Path(sys.executable),
                )

            self.assertFalse(plist_path.exists())

    def test_existing_foreign_regular_plist_is_not_overwritten(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        with tempfile.TemporaryDirectory() as parent:
            plist_path = Path(parent) / "broker.plist"
            original = plistlib.dumps(
                {"Label": "com.example.unrelated", "ProgramArguments": ["/bin/true"]}
            )
            plist_path.write_bytes(original)

            with self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.write_launch_agent(
                    plist_path=plist_path,
                    python_executable=Path(sys.executable),
                )

            self.assertEqual(plist_path.read_bytes(), original)

    def test_uninstall_removes_only_a_plist_with_the_complete_managed_shape(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        calls: list[list[str]] = []

        def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            managed = root / "managed.plist"
            launch_agent.write_launch_agent(
                plist_path=managed,
                python_executable=Path(sys.executable),
            )
            launch_agent.uninstall_launch_agent(
                plist_path=managed,
                runner=runner,
                uid=501,
            )
            self.assertFalse(managed.exists())

            foreign = root / "foreign.plist"
            foreign.write_bytes(
                plistlib.dumps(
                    {
                        "Label": LAUNCH_AGENT_LABEL,
                        "ProgramArguments": ["/bin/echo", "not-owned"],
                    }
                )
            )
            with self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.uninstall_launch_agent(
                    plist_path=foreign,
                    runner=runner,
                    uid=501,
                )

            self.assertTrue(foreign.exists())
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
