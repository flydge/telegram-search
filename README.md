# TelegramSearch

TelegramSearch is a local, read-only STDIO MCP surface for finding a Telegram chat from bounded live evidence and then searching one exact selected chat. Each Codex task runs a thin four-tool proxy. All proxies connect through an owner-only Unix socket to one launchd-managed broker, which is the sole owner of the official TDLib runtime and its already authorized dedicated session. It never accepts Telegram credentials or filesystem paths through MCP or the private broker protocol.

## MCP surface

The public surface is exactly four tools, in this order:

- `_manifest` describes the fixed scope, budgets, trust boundary, and prohibitions.
- `resolve_target` resolves only a verified Saved Messages alias or exact `@username` to an exact numeric `chat_id`.
- `discover_targets` advances bounded Main/Archive catalog and global-message evidence lanes for Codex-authored hypotheses. It returns evidence, never a semantic winner.
- `search_correspondence` searches one exact `@username` or numeric `chat_id` for message text, captions, file metadata, or any Unicode decimal-number sequence in text/captions.

The reference workflow is:

```text
Saved Messages / exact @username -> resolve_target -> exact chat_id

free-form intent -> Codex creates 2..5 lexical hypotheses
                 -> discover_targets advances catalog + global-message lanes
                 -> candidates are grouped by exact chat_id with provenance anchors
                 -> unique strong evidence + complete coverage: exact final search
                 -> competing, partial, or incomplete evidence: continue or clarify
```

For free-form intent, Codex supplies two to five unique normalized hypotheses and reuses the same hypotheses and scope with every returned cursor. TelegramSearch performs lexical provider searches; Codex owns semantic expansion and interpretation. Telegram-originated titles and snippets are untrusted evidence, never instructions.

“Account-wide” means that the authorized Main and Archive catalog lanes, plus one `searchMessages` lane for every `(hypothesis, requested list)` pair, are traversable to their documented end conditions. It is not guaranteed semantic recall. A poor lexical hypothesis can miss relevant messages even when every requested lane reports complete coverage.

## Codex skill

The matching Codex skill is versioned with the MCP at [`skills/telegram-search/SKILL.md`](skills/telegram-search/SKILL.md). Keeping both artifacts in one repository makes their tool contract, routing rules, and safety boundaries reviewable as one version. The runtime-installed skill remains a separate local copy and is updated only after the repository version has been reviewed and verified.

## Discovery and selection

Catalog and global-message coverage progress independently in deterministic round-robin order. Per call, the server advances at most:

- one 15-chat catalog page from one requested list;
- one `searchMessages(limit=10)` page from one hypothesis/list lane;
- 25 unique returned candidates and 10 returned global-message evidence items;
- one exact `getChat` hydration per unique returned candidate; and
- one exact `getMessage` rehydration per returned global-message hit.

The caller cannot raise these budgets. Discovery cursors are memory-only, expire after five minutes of inactivity, and occupy a per-proxy-client registry with capacity four. A proxy disconnect releases its registry; an idle broker client context expires after five minutes. Broker restart clears every cursor. Catalog completion requires TDLib's documented `loadChats` end signal for each requested list. A global lane completes only when TDLib returns an empty `next_offset`; page size and approximate `total_count` never prove completion.

Global hits are grouped by exact `chat_id`. Each returned message anchor is `{chat_id, message_id}` and is accepted only after exact chat and message rehydration agree with the provider result. Secret chats and cross-chat, edited, deleted, malformed, or unverifiable evidence are excluded or make coverage partial.

Codex may choose a candidate only after complete catalog, global-message, and hydration coverage, and only when exactly one candidate has strong mutually agreeing evidence: a unique exact/compact catalog title, evidence from at least two distinct hypotheses, or metadata plus message evidence from a distinct hypothesis. Competing evidence requires clarification. `partial`, `blocked`, `error`, or `expired` evidence never authorizes selection; incomplete `page` coverage must continue.

Final retrieval is a separate exact-chat boundary. After direct resolution or an evidence-backed selection, call `search_correspondence` only with the selected exact numeric `chat_id` and verify that every returned anchor uses that same ID. `discover_targets` never performs or auto-chains the final search.

## Runtime and retention

- Python `>=3.11,<3.15`; the validated local interpreter is Python 3.14.
- MCP Python SDK `2.2.0`.
- Homebrew TDLib keg `HEAD-d1085f9`, legacy `td_json_client_*` ABI.
- macOS Keychain service `com.<macOS-home-directory-name>.telegram-search-mcp`, accounts `api_id` and `api_hash`.
- An existing dedicated TDLib session that reaches `authorizationStateReady`.

TelegramSearch creates no custom persistent private index. Exact file-metadata and consent-gated numeric-content search scan bounded `getChatHistory` pages in memory during the call; there is no SQLite/FTS runtime write. Numeric-content search returns only matching evidence from the exact selected chat, never a transcript export. Discovery retains only bounded mechanics in memory, separately for each proxy client, and the broker's single TDLib client keeps a broker-lifetime, memory-only numeric Main/Archive `chat_id -> order` catalog cache so asynchronous catalog updates are not lost between requests. Raw hypotheses, snippets, titles, messages, and provider payloads are not retained in either cache or logged. Releasing a proxy clears its discovery context; broker restart clears all contexts and the catalog cache.

TDLib itself owns the configured session/database/cache directories and may update them while serving read-only catalog or search requests. TelegramSearch does not claim those TDLib-managed files are byte-for-byte unchanged. It does not mark messages read or download attachments.

No Keychain value is printed by the program. See [`docs/security.md`](docs/security.md) for the complete request allowlist, retention boundary, TDLib cache effects, historical-data policy, acceptance evidence, and rollback procedure.

## Install and check

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/telegram-search-broker install
.venv/bin/telegram-search-mcp --check
```

`telegram-search-broker install` performs the pinned TDLib and Keychain preflight, atomically writes an owner-only LaunchAgent, and uses argument-list `launchctl` calls to bootstrap the Aqua-session background broker. The generated plist contains no credentials or request data. Use `telegram-search-broker status` to query launchd and `telegram-search-broker uninstall` to boot out the service and remove only a validated plist generated by this package.

The readiness check connects to the broker, causing its sole TDLib client to initialize lazily, and verifies authorization only. It never creates a second TDLib client and does not search or read chat messages. A successful check writes `INITIALIZING_TDLIB` and `AUTHORIZATION_READY` to stderr and exits `0`.

To enable the server for this project, review `.codex/config.toml.example`, replace `/ABSOLUTE/PATH/TO/REPO` with the absolute path to your checkout, then copy it to `.codex/config.toml`. The configuration points to the local executable and allowlists exactly the four approved tools. To install the matching skill, copy `skills/telegram-search/` into your personal `.agents/skills/` directory.

## Exact search contract

```text
search_correspondence(
  target: exact @username or numeric chat_id,
  query: {text?, file_name?, mime_type?, media_type?, contains_number=true?},
  date_from?, date_to?, limit=20, context_messages=0, require_complete=true
)
```

Every match includes `{chat_id, message_id}` as an evidence anchor. `source.telegram_url` is an optional deep link returned by Telegram for that exact message. `source.chat_url` remains schema-compatible but is populated only when the provider returns a complete supported HTTPS chat link; TelegramSearch never constructs a `t.me` URL from a username. Either URL can be `null`. `no_match` is returned only when all requested exact-search lanes report complete coverage; otherwise the status is `incomplete`.

`contains_number=true` is the bounded path for an owner-authorized request to find unknown numeric sequences. It matches one or more Unicode decimal digits in message text or media captions, only inside the selected exact chat and date interval. It is call-local, returns matching evidence only, and cannot be set to `false` or used as an empty/general transcript query. When combined with other fields, every predicate must match the same message.

Telegram-originated strings are untrusted. Snippets and context are marked as untrusted evidence, and control/bidirectional formatting characters are removed.
