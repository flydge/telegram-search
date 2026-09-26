"""Fixed runtime locations and owner-only storage helpers."""

from __future__ import annotations

import os
import platform
from pathlib import Path

def _owner_paths(home: Path) -> tuple[Path, str, str, Path]:
    """Derive per-user runtime locations without recording an account name."""
    root = home / "Library" / "Application Support" / "TelegramSearchMCP"
    keychain_service = f"com.{home.name}.telegram-search-mcp"
    agent_label = f"{keychain_service}.broker"
    agent_plist = home / "Library" / "LaunchAgents" / f"{agent_label}.plist"
    return root, keychain_service, agent_label, agent_plist


APP_SUPPORT_ROOT, KEYCHAIN_SERVICE, LAUNCH_AGENT_LABEL, LAUNCH_AGENT_PLIST = (
    _owner_paths(Path.home())
)
TDLIB_SESSION_DIRECTORY = APP_SUPPORT_ROOT / "tdlib"
TDLIB_LIBRARY = Path("/opt/homebrew/opt/tdlib/lib/libtdjson.dylib")
EXPECTED_TDLIB_KEG = "HEAD-d1085f9"
BROKER_RUNTIME_DIRECTORY = APP_SUPPORT_ROOT / "run"
BROKER_SOCKET_PATH = BROKER_RUNTIME_DIRECTORY / "broker.sock"
BROKER_LOCK_PATH = BROKER_RUNTIME_DIRECTORY / "broker.lock"


class ConfigurationError(RuntimeError):
    """Raised when a fixed local runtime boundary is unsafe or unavailable."""


def ensure_private_directory(path: Path) -> Path:
    """Create a non-symlink directory and enforce owner-only permissions."""
    if path.is_symlink():
        raise ConfigurationError("refusing a symlink storage directory")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigurationError("unable to prepare private storage") from error
    if not path.is_dir() or path.is_symlink():
        raise ConfigurationError("private storage path is not a directory")
    try:
        os.chmod(path, 0o700)
    except OSError as error:
        raise ConfigurationError("unable to secure private storage") from error
    return path


def validate_tdlib_runtime() -> Path:
    """Verify the approved Homebrew TDLib runtime without reading its contents."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ConfigurationError("the approved TDLib runtime requires Apple ARM64 macOS")
    if not TDLIB_LIBRARY.is_file():
        raise ConfigurationError("the pinned TDLib library is unavailable")
    try:
        resolved = TDLIB_LIBRARY.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError("the pinned TDLib library cannot be resolved") from error
    if EXPECTED_TDLIB_KEG not in resolved.parts:
        raise ConfigurationError("the installed TDLib keg does not match the approved pin")
    return resolved
