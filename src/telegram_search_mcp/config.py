"""Fixed runtime locations and owner-only storage helpers."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
import stat
import tomllib
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


# Trusted local policy is independent of the MCP consumer's tool allowlist.
RUNTIME_POLICY_PATH = APP_SUPPORT_ROOT / "runtime.toml"
LEGACY_CAPABILITIES = ("artifacts", "read", "send")
KNOWN_CAPABILITIES = frozenset((*LEGACY_CAPABILITIES, "read_messages", "read_history", "read_reply_chain", "list_topics", "read_topic_history", "search_messages", "list_chats", "search_chats", "verified_targets", "attachment_pages", "spreadsheets", "presentations", "reply_text_send", "reply_artifact_send", "reply_media_targets", "reply_formatted_targets", "reply_lexical_targets", "reply_identity_targets", "reply_datetime_targets"))


@dataclass(frozen=True)
class RuntimePolicy:
    config_version: int = 1
    enabled_capabilities: tuple[str, ...] = LEGACY_CAPABILITIES
    expected_package_version: str | None = None
    expected_contract_version: int | None = None
    expected_schema_fingerprint: str | None = None
    source_path: Path | None = None
    max_draft_ttl_seconds: int = 900

    def __post_init__(self) -> None:
        if type(self.max_draft_ttl_seconds) is not int or not 1 <= self.max_draft_ttl_seconds <= 86400:
            raise ValueError("draft TTL must be an integer from 1 to 86400 seconds")

    def public_settings(self) -> dict[str, object]:
        return {"config_version": self.config_version,
                "enabled_capabilities": list(self.enabled_capabilities),
                "expected_package_version": self.expected_package_version,
                "expected_contract_version": self.expected_contract_version,
                "expected_schema_fingerprint": self.expected_schema_fingerprint,
                "max_draft_ttl_seconds": self.max_draft_ttl_seconds}

    def require_current(self) -> None:
        if self.source_path is not None and load_runtime_policy(self.source_path) != self:
            raise ConfigurationError("runtime configuration changed; restart matching components")


def load_runtime_policy(path: Path = RUNTIME_POLICY_PATH) -> RuntimePolicy:
    """Read at most 16 KiB from one owner-only regular file; never accept MCP paths."""
    directory_fd = None
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent_metadata = os.fstat(directory_fd)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        if parent_metadata.st_uid != os.getuid() or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
            os.close(fd)
            raise ConfigurationError("runtime configuration directory must be owner-only")
    except FileNotFoundError:
        return RuntimePolicy(source_path=path)
    except OSError:
        raise ConfigurationError("runtime configuration is unsafe or unavailable") from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > 16384):
            raise ConfigurationError("runtime configuration must be a bounded owner-only file")
        data = os.read(fd, 16385)
        if len(data) != metadata.st_size:
            raise ConfigurationError("runtime configuration changed during reading")
    finally:
        os.close(fd)
    try:
        value = tomllib.loads(data.decode("utf-8"))
        allowed = {"config_version", "enabled_capabilities", "expected_package_version",
                   "expected_contract_version", "expected_schema_fingerprint", "max_draft_ttl_seconds"}
        if set(value) - allowed or type(value.get("config_version")) is not int or value["config_version"] != 1:
            raise ValueError
        capabilities = value.get("enabled_capabilities")
        if (not isinstance(capabilities, list) or any(type(v) is not str or v not in KNOWN_CAPABILITIES for v in capabilities)
                or len(set(capabilities)) != len(capabilities)):
            raise ValueError
        package = value.get("expected_package_version")
        contract = value.get("expected_contract_version")
        fingerprint = value.get("expected_schema_fingerprint")
        if package is not None and (type(package) is not str or not 1 <= len(package) <= 64):
            raise ValueError
        if contract is not None and (type(contract) is not int or contract < 1):
            raise ValueError
        if fingerprint is not None and (type(fingerprint) is not str or len(fingerprint) != 64
                                       or any(c not in "0123456789abcdef" for c in fingerprint)):
            raise ValueError
        return RuntimePolicy(enabled_capabilities=tuple(sorted(capabilities)),
                             expected_package_version=package, expected_contract_version=contract,
                             expected_schema_fingerprint=fingerprint, source_path=path,
                             max_draft_ttl_seconds=value.get("max_draft_ttl_seconds", 900))
    except (ValueError, UnicodeError, TypeError):
        raise ConfigurationError("runtime configuration version or settings are invalid") from None
