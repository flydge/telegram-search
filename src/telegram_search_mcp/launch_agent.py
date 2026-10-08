"""Safe launchd lifecycle for the single Unofficial Telegram MCP broker."""

from __future__ import annotations

import argparse
import os
import plistlib
import secrets
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .broker import Broker, BrokerStartupError
from .config import (
    BROKER_LOCK_PATH,
    BROKER_SOCKET_PATH,
    LAUNCH_AGENT_LABEL,
    LAUNCH_AGENT_PLIST,
    validate_tdlib_runtime,
)
from .keychain import read_api_credentials

LAUNCHCTL = "/bin/launchctl"


class LaunchAgentError(RuntimeError):
    """Raised when launchd installation or lifecycle checks fail safely."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _default_preflight() -> None:
    validate_tdlib_runtime()
    read_api_credentials()


def launch_agent_payload(*, python_executable: Path) -> dict[str, Any]:
    executable = python_executable.absolute()
    resolved_executable = executable.resolve(strict=True)
    if not resolved_executable.is_file():
        raise LaunchAgentError("Python executable is invalid")
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(executable),
            "-m",
            "telegram_search_mcp.launch_agent",
            "run",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "LimitLoadToSessionType": "Aqua",
        "Umask": 0o077,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
    }


def _managed_payload(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    expected_keys = {
        "Label",
        "ProgramArguments",
        "RunAtLoad",
        "KeepAlive",
        "ProcessType",
        "LimitLoadToSessionType",
        "Umask",
        "StandardOutPath",
        "StandardErrorPath",
        "EnvironmentVariables",
    }
    arguments = value.get("ProgramArguments")
    return (
        set(value) == expected_keys
        and value.get("Label") == LAUNCH_AGENT_LABEL
        and isinstance(arguments, list)
        and len(arguments) == 4
        and isinstance(arguments[0], str)
        and Path(arguments[0]).is_absolute()
        and arguments[1:] == ["-m", "telegram_search_mcp.launch_agent", "run"]
        and value.get("RunAtLoad") is True
        and value.get("KeepAlive") is True
        and value.get("ProcessType") == "Background"
        and value.get("LimitLoadToSessionType") == "Aqua"
        and type(value.get("Umask")) is int
        and value.get("Umask") == 0o077
        and value.get("StandardOutPath") == "/dev/null"
        and value.get("StandardErrorPath") == "/dev/null"
        and value.get("EnvironmentVariables") == {"PYTHONDONTWRITEBYTECODE": "1"}
    )


def _open_launch_agent_directory(plist_path: Path) -> int:
    parent = plist_path.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as error:
        raise LaunchAgentError("LaunchAgent directory is unsafe") from error
    if parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
        raise LaunchAgentError("LaunchAgent directory is unsafe")
    if (
        parent_metadata.st_uid != os.getuid()
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        raise LaunchAgentError("LaunchAgent directory owner is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(parent, flags)
    except OSError as error:
        raise LaunchAgentError("LaunchAgent directory is unsafe") from error
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (
        parent_metadata.st_dev,
        parent_metadata.st_ino,
    ):
        os.close(descriptor)
        raise LaunchAgentError("LaunchAgent directory changed during validation")
    return descriptor


def _read_launch_agent(directory_descriptor: int, name: str) -> object | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LaunchAgentError("existing LaunchAgent plist is unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_size > 1024 * 1024
        ):
            raise LaunchAgentError("existing LaunchAgent plist is unsafe")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return plistlib.load(stream)
    except plistlib.InvalidFileException as error:
        raise LaunchAgentError("existing LaunchAgent plist is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_launch_agent(*, plist_path: Path, python_executable: Path) -> None:
    """Atomically write the fixed, credential-free LaunchAgent definition."""
    directory_descriptor = _open_launch_agent_directory(plist_path)
    temporary_name = f".{plist_path.name}.{secrets.token_hex(16)}.tmp"
    temporary_exists = False
    try:
        existing = _read_launch_agent(directory_descriptor, plist_path.name)
        if existing is not None and not _managed_payload(existing):
            raise LaunchAgentError("existing LaunchAgent plist is not owned by this service")
        payload = launch_agent_payload(python_executable=python_executable)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_exists = True
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            plistlib.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary_name,
            plist_path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporary_exists = False
        os.fsync(directory_descriptor)
        written = os.stat(
            plist_path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(written.st_mode)
            or written.st_uid != os.getuid()
            or stat.S_IMODE(written.st_mode) != 0o600
        ):
            raise LaunchAgentError("written LaunchAgent plist is unsafe")
    finally:
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        try:
            os.close(directory_descriptor)
        except OSError:
            pass


def _run_launchctl(
    arguments: list[str],
    *,
    runner: Runner,
    required: bool,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    completed = runner(
        arguments,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if required and completed.returncode != 0:
        raise LaunchAgentError("launchctl operation failed")
    return completed


def install_launch_agent(
    *,
    plist_path: Path = LAUNCH_AGENT_PLIST,
    python_executable: Path = Path(sys.executable),
    runner: Runner = subprocess.run,
    preflight: Callable[[], None] = _default_preflight,
    uid: int | None = None,
) -> None:
    preflight()
    write_launch_agent(
        plist_path=plist_path,
        python_executable=python_executable,
    )
    user_id = os.getuid() if uid is None else uid
    domain = f"gui/{user_id}"
    service = f"{domain}/{LAUNCH_AGENT_LABEL}"
    _run_launchctl(
        [LAUNCHCTL, "bootout", service],
        runner=runner,
        required=False,
    )
    # A successful bootout may return before launchd retires the registration.
    for _ in range(50):
        state = _run_launchctl(
            [LAUNCHCTL, "print", service], runner=runner, required=False, timeout=1,
        )
        if state.returncode == 113:  # launchd: service not found
            break
        if state.returncode != 0:
            raise LaunchAgentError("unable to verify previous service shutdown")
        time.sleep(0.1)
    else:
        raise LaunchAgentError("previous service did not shut down")
    _run_launchctl(
        [LAUNCHCTL, "bootstrap", domain, str(plist_path)],
        runner=runner,
        required=True,
    )
    # RunAtLoad starts the new broker; kickstart -k would terminate it again.


def launch_agent_status(
    *,
    runner: Runner = subprocess.run,
    uid: int | None = None,
) -> bool:
    user_id = os.getuid() if uid is None else uid
    completed = _run_launchctl(
        [LAUNCHCTL, "print", f"gui/{user_id}/{LAUNCH_AGENT_LABEL}"],
        runner=runner,
        required=False,
    )
    return completed.returncode == 0


def restart_launch_agent(
    *,
    runner: Runner = subprocess.run,
    uid: int | None = None,
) -> None:
    user_id = os.getuid() if uid is None else uid
    _run_launchctl(
        [
            LAUNCHCTL,
            "kickstart",
            "-k",
            f"gui/{user_id}/{LAUNCH_AGENT_LABEL}",
        ],
        runner=runner,
        required=False,
    )


def uninstall_launch_agent(
    *,
    plist_path: Path = LAUNCH_AGENT_PLIST,
    runner: Runner = subprocess.run,
    uid: int | None = None,
) -> None:
    directory_descriptor = _open_launch_agent_directory(plist_path)
    try:
        payload = _read_launch_agent(directory_descriptor, plist_path.name)
        if payload is not None and not _managed_payload(payload):
            raise LaunchAgentError("existing LaunchAgent plist is not owned by this service")
        user_id = os.getuid() if uid is None else uid
        _run_launchctl(
            [LAUNCHCTL, "bootout", f"gui/{user_id}/{LAUNCH_AGENT_LABEL}"],
            runner=runner,
            required=False,
        )
        if payload is not None:
            os.unlink(plist_path.name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def run_broker() -> int:
    validate_tdlib_runtime()
    broker = Broker(socket_path=BROKER_SOCKET_PATH, lock_path=BROKER_LOCK_PATH)

    def stop(_signum: int, _frame: object) -> None:
        broker.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        broker.serve_forever()
    except BrokerStartupError:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegram-search-broker")
    parser.add_argument("command", choices=("run", "install", "status", "uninstall"))
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "run":
            return run_broker()
        if arguments.command == "install":
            install_launch_agent()
            print("INSTALLED")
            return 0
        if arguments.command == "status":
            active = launch_agent_status()
            print("RUNNING" if active else "NOT_RUNNING")
            return 0 if active else 1
        uninstall_launch_agent()
        print("UNINSTALLED")
        return 0
    except Exception:
        print("BROKER_LIFECYCLE_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
