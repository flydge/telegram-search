# Installation and configuration

[← Overview](../README.md) · [Tool reference](reference.md) · [Security](security.md)

## 1. Prepare the Telegram runtime

The current package runs on **Apple Silicon macOS** with **Python 3.11–3.14**. It uses an existing, dedicated Telegram session; it does not include a first-time login wizard or a reproducible TDLib provisioning script.

Prepare these locally before installing the broker:

| Requirement | Exact expectation |
| --- | --- |
| TDLib | Homebrew keg `HEAD-d1085f9`, library `1.8.67`, legacy `td_json_client_*` ABI. `/opt/homebrew/opt/tdlib/lib/libtdjson.dylib` must resolve into that keg. A current unpinned `brew install tdlib` is not equivalent. |
| Telegram API credentials | Your own `api_id` and `api_hash`, stored as generic-password items in macOS Keychain. Use service `com.<home-directory-name>.telegram-search-mcp`, with separate accounts `api_id` and `api_hash`. |
| Authorized session | A dedicated TDLib session under `~/Library/Application Support/TelegramSearchMCP/tdlib`, using its `db` and `files` subdirectories, compatible with this runtime and ready for `authorizationStateReady`. |

The service name uses the last component of your home directory, not your display name. Enter credentials through trusted local provisioning; never place them in project files, prompts, MCP arguments or shell history. The broker only reads Keychain values. It cannot ask for your phone number, login code or two-step password.

A logged-in Telegram Desktop app does **not** supply this TDLib session. If you do not already have compatible provisioning, stop here: first-time setup is a current product gap, not something the package-install commands below solve. Do not copy an unrelated application's live session files.

**Optional media dependencies:** local transcription and frame extraction use `/opt/homebrew/bin/ffmpeg`, `/opt/homebrew/bin/ffprobe` and `/opt/homebrew/bin/whisper-cli`. Provision the multilingual model at `~/Library/Application Support/TelegramSearchMCP/models/ggml-small.bin` and verify its distributor's checksum. Basic text search and document reads do not need Whisper. See the [security model](security.md) for details.

## 2. Install the package and broker

Use a stable, owner-only checkout. Select a supported Python interpreter if your `python3` is outside the supported range.

```bash
git clone https://github.com/flydge/telegram-search.git
cd telegram-search
chmod 700 .
python3 -m venv .venv
.venv/bin/python -m pip install .
mkdir -p "$HOME/Library/LaunchAgents"
.venv/bin/telegram-search-broker install
.venv/bin/telegram-search-mcp --check
```

`install` registers the background broker in your logged-in macOS session. The broker alone owns TDLib; MCP clients connect to it. Keep the checkout and virtual environment at the same path.

The check prints `INITIALIZING_TDLIB` followed by `AUTHORIZATION_READY` to stderr and exits `0` on success. It initializes the session when needed but does not search or read messages. Exit `2` means authorization is blocked; exit `1` indicates another setup, transport or compatibility failure.

## 3. Connect an MCP client

### Codex

Merge the server block from [`.codex/config.toml.example`](../.codex/config.toml.example) into `.codex/config.toml` in the project where you want to use TelegramSearch. Preserve any existing settings. Replace **both** absolute-path placeholders. The example does not expand `~` or shell variables.

The example lists all 43 tools and sets the required timeouts. Start a fresh chat in that project after configuration or schema changes.

Install the optional skill by copying [the skill file](../skills/telegram-search/SKILL.md) to `~/.agents/skills/telegram-search/SKILL.md`. Back up an existing copy first. The skill supplies search and approval guidance; the broker enforces its own permissions.

### Other local MCP clients

Choose a **STDIO** server with:

```json
{
  "command": "/ABSOLUTE/PATH/TO/telegram-search/.venv/bin/telegram-search-mcp",
  "args": []
}
```

Use your client's configuration format and allow up to 600 seconds for bounded media operations. This release does not expose an HTTP endpoint. Start with `_manifest({"check_broker": true})` and verify compatibility before using the other tools.

## Choose capabilities

The client tool list controls what is visible. The broker's local policy controls what is allowed. Enabling a tool in a client does not enable its capability.

Without a policy file, the legacy defaults are `read`, `artifacts` and `send`; sending still requires the exact preview and local approval. Extended readers and reply features are opt-in.

Before changing policy, check any pending send outcomes. A policy edit makes running components stale, and a broker restart loses in-memory send status. Never repeat an uncertain send automatically.

For a read-only starting configuration, create or carefully edit:

```text
~/Library/Application Support/TelegramSearchMCP/runtime.toml
```

```toml
config_version = 1
expected_package_version = "0.35.0"
expected_contract_version = 1
enabled_capabilities = ["read", "artifacts"]
```

The containing directory must be owned by you with mode `0700`; the regular policy file must have mode `0600`. Symlinks are rejected. Preserve existing policy when upgrading rather than overwriting it blindly.

| To enable | Add these capabilities |
| --- | --- |
| Selected messages and history | `read_messages`, `read_history`, `read_reply_chain` |
| Topics and richer search | `list_topics`, `read_topic_history`, `search_messages`, `list_chats`, `search_chats` |
| Reuse a verified chat | `verified_targets` |
| Document pages, spreadsheets, presentations | `attachment_pages`, `spreadsheets`, `presentations` |
| Approved sending | `send` |
| Text or attachment replies | `send` plus `reply_text_send` or `reply_artifact_send` respectively |

Replying to media or formatted sources can require additional source capabilities. Use the [reply reference](reference.md#prepared-same-chat-text-replies) for the exact combinations; do not enable every capability as a shortcut.

After a policy change, re-run `.venv/bin/telegram-search-broker install` using the same environment and reconnect your MCP client. This restarts the managed broker and clears in-memory drafts, uploads and cursors.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Pinned TDLib unavailable or wrong keg | Confirm the exact library path and pin above. |
| Missing credentials or blocked authorization | Check the local Keychain records and dedicated session; the package has no login flow. |
| `package_mismatch`, `schema_mismatch` or `config_mismatch` | Use matching package and policy in proxy and broker, restart the broker, then open a fresh client session. |
| A listed tool is disabled | Add its capability to trusted local policy and restart matching components. |
| Media analysis is unavailable | Check the fixed binary paths and locally provisioned Whisper model. |
| `status` succeeds but tools fail | Broker registration and Telegram authorization are separate; run `--check` and the manifest handshake. |

```bash
.venv/bin/telegram-search-broker status
.venv/bin/telegram-search-mcp --check
```

To stop and remove the managed service:

```bash
.venv/bin/telegram-search-broker uninstall
```

This removes the validated LaunchAgent, not your TDLib session or credentials. For upgrades, preserve the previous environment, configuration and installed skill so they can be restored together. See [rollback](security.md#rollback).
