"""Minimal macOS Keychain adapter with redacted failures."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import KEYCHAIN_SERVICE

_SECURITY = "/usr/bin/security"


class KeychainError(RuntimeError):
    """Raised without preserving command output or credential material."""


@dataclass(frozen=True, repr=False)
class ApiCredentials:
    api_id: int
    api_hash: str = field(repr=False)


def _read_account(
    account: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    args = [
        _SECURITY,
        "find-generic-password",
        "-w",
        "-s",
        KEYCHAIN_SERVICE,
        "-a",
        account,
    ]
    try:
        result = runner(args, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise KeychainError("unable to access the required Keychain record") from None
    if result.returncode != 0:
        raise KeychainError("required Keychain record is unavailable")
    value = result.stdout.strip()
    if not value:
        raise KeychainError("required Keychain record is empty")
    return value


def read_api_credentials(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ApiCredentials:
    api_id_text = _read_account("api_id", runner=runner)
    api_hash = _read_account("api_hash", runner=runner)
    try:
        api_id = int(api_id_text)
    except ValueError:
        raise KeychainError("api_id Keychain record is invalid") from None
    if api_id <= 0:
        raise KeychainError("api_id Keychain record is invalid")
    return ApiCredentials(api_id=api_id, api_hash=api_hash)
