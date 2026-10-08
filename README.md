![TelegramSearch](docs/assets/telegram-search-header.png)

# TelegramSearch

Connect Telegram to **Claude Code, Codex, or another local MCP client**.

**Before connecting:** complete the [one-time local setup](docs/install.md) on an Apple Silicon Mac, including Telegram authorization. Your assistant and TelegramSearch must run on the same Mac. Replace `/ABSOLUTE/PATH/TO/telegram-search` below with your installation folder.

## Claude Code

Run in your terminal:

```bash
claude mcp add --transport stdio --scope user telegram_search -- \
  "/ABSOLUTE/PATH/TO/telegram-search/.venv/bin/telegram-search-mcp"
```

Open Claude Code and run `/mcp` to check the connection. [Claude Code MCP guide](https://code.claude.com/docs/en/mcp).

## Codex

Add this block to `~/.codex/config.toml`, preserving your existing settings:

```toml
[mcp_servers.telegram_search]
command = "/ABSOLUTE/PATH/TO/telegram-search/.venv/bin/telegram-search-mcp"
startup_timeout_sec = 30
tool_timeout_sec = 600
required = true
```

Restart Codex and open a fresh chat. [Codex MCP guide](https://developers.openai.com/codex/mcp).

## Other MCP clients

Add a **local STDIO server** named `telegram_search`. Use these fields in your client's MCP configuration:

```json
{
  "command": "/ABSOLUTE/PATH/TO/telegram-search/.venv/bin/telegram-search-mcp",
  "args": []
}
```

Set the tool timeout to **600 seconds** if available, then reconnect the client.

## Check the connection

Ask your assistant:

> Use TelegramSearch’s `_manifest` tool with `check_broker=true` and report whether compatibility is `compatible`.

[Setup and troubleshooting](docs/install.md)
