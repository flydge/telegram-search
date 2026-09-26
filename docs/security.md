# TelegramSearch security model

## Authority boundary

The dedicated TDLib session holds the Telegram account's authority. Exactly one launchd-managed broker owns that session and the sole `TDLibClient`; per-task STDIO MCP proxies never load TDLib. TelegramSearch narrows authority at three layers: MCP exposes exactly `_manifest`, `resolve_target`, `discover_targets`, and `search_correspondence`; the private broker protocol accepts only `resolve`, `discover`, `search`, `check`, `health`, and `release_client`; and the TDLib client rejects every request type outside this fixed read-only allowlist before transport:

```text
getAuthorizationState
setTdlibParameters
checkDatabaseEncryptionKey
getMe
createPrivateChat
searchPublicChat
searchChats
searchChatsOnServer
getChats
loadChats
getChat
searchMessages
searchChatMessages
getMessage
getChatHistory
getMessageLink
getUser
```

`createPrivateChat` has one narrowly approved use: after `getMe` identifies the authorized account, Saved Messages resolution calls it with that same user ID and verifies the returned private chat. `loadChats` advances only a fixed Main or Archive catalog lane. `searchMessages` is reachable only through the typed discovery wrapper and the exact request shape below. None of these methods is a general bridge to caller-chosen TDLib operations.

The broker socket lives in an owner-only directory with mode `0700`; the socket and owner lock use mode `0600`. The broker also verifies the kernel-reported peer UID through Darwin `getpeereid`. A nonblocking owner lock prevents a second broker. Only the lock holder may replace a same-owner, non-symlink Unix socket after proving that it is stale. Same-UID processes are inside this local filesystem trust boundary. Requests use versioned length-prefixed strict JSON, a fixed operation allowlist, public-model validation on both sides, 1 MiB request and 8 MiB response limits, random process-lifetime client IDs, random request IDs, a 250-millisecond per-read bound, and monotonic deadlines capped at 570 seconds.

The broker fails closed unless the dedicated session reaches `authorizationStateReady`. It never accepts API credentials, login codes, passwords, session paths, index paths, account selectors, wildcards, caller-controlled provider limits/filters/dates/functions, or arbitrary TDLib requests through MCP or IPC. `api_id` and `api_hash` are read only from the fixed macOS Keychain service by the broker and are never logged or included in an error.

## Direct resolution, discovery, and exact search

`resolve_target` accepts one bounded string. Verified Saved Messages aliases use the self-chat path; exact `@username` values use `searchPublicChat`. Every other input returns `discovery_required` without catalog or message search. Secret chats are rejected.

For free-form intent, Codex—not TDLib—creates two to five unique normalized lexical hypotheses. `discover_targets` maintains independent catalog lanes for each requested Main/Archive list and one global-message lane for every `(hypothesis_index, chat_list)` pair. Account-wide coverage means those authorized lanes can reach their documented ends; it does not claim semantic search or guaranteed recall.

Every global-message request has exactly this shape, with `chatListArchive` substituted only for an Archive lane:

```json
{
  "@type": "searchMessages",
  "chat_list": {"@type": "chatListMain"},
  "query": "<current request hypothesis>",
  "offset": "<memory-only provider offset or empty string>",
  "limit": 10,
  "filter": null,
  "chat_type_filter": null,
  "min_date": 0,
  "max_date": 0
}
```

A null, omitted, arbitrary, or caller-selected chat list is forbidden. The caller also cannot change the offset, limit, filters, or dates. A lane's opaque offset advances independently; only an empty `next_offset` completes that lane. A short or empty page with a nonempty offset remains incomplete, and approximate `foundMessages.total_count` is never a completion signal. Repeated offsets and the bounded prior-offset digest budget fail the lane partial rather than loop or claim completeness.

Catalog traversal uses `getChats` only to seed the available ordered prefix and `loadChats(chat_list, limit=50)` to advance one requested list. A short load or fixed count does not complete the list; only TDLib's documented 404 end signal does. Main/Archive catalog completion is independent from all global-message lanes. Overall completion requires every requested catalog and hypothesis/list lane plus exact hydration to complete without integrity failure.

Per call, `discover_targets` advances at most one 15-chat catalog page and one 10-result global-message page, returns at most 25 unique candidates and 10 message evidence items, and performs at most one exact `getChat` per returned candidate and one exact `getMessage` per returned global hit. The caller cannot increase these caps or cause parallel fan-out.

Each provider hit is validated, grouped by exact `chat_id`, hydrated with exact `getChat`, and rehydrated with `getMessage(chat_id, message_id)`. The rehydrated IDs must equal the provider IDs. Secret chats, unsupported chat types, malformed content, deleted/edited evidence, and cross-chat or cross-message mismatches are discarded and make the affected coverage partial when integrity cannot be established. Returned evidence anchors contain the same exact `{chat_id, message_id}` provenance.

Only complete coverage and one uniquely strong, mutually corroborated candidate may authorize Codex selection. Competing evidence requires owner clarification. `partial`, `blocked`, `error`, `expired`, or still-incomplete `page` coverage cannot authorize automatic selection. Final `search_correspondence` remains separate and exact-chat only; evidence from global discovery never becomes a global final search.

Text and captions in final search use `searchChatMessages`. File metadata uses bounded `getChatHistory` scans and call-local filtering. With the owner's explicit request or consent, `contains_number=true` uses the same bounded exact-chat history path to find one or more Unicode decimal digits in text or media captions. The scan is transient, returns only matching evidence, never exposes a general transcript, and cannot cross the selected chat or date interval. Combined predicates must match the same message. Every returned match is rehydrated with exact `getMessage` before return; numeric evidence is rechecked after rehydration so edited or deleted content fails closed. Context is bounded to at most four neighboring messages in each direction. Evidence includes the exact resolved `chat_id` and `message_id`; supported HTTPS Telegram links are optional and must be returned complete by Telegram. TelegramSearch never synthesizes a `t.me` URL from a username.

## Memory, persistence, and logging

Each proxy client receives an independent `DiscoveryRegistry` with a five-minute inactivity TTL and capacity four. The broker holds at most 64 client contexts and expires an entire idle context after five minutes. It retains only bounded scan mechanics:

- a random opaque scan cursor;
- a SHA-256 digest and count of the normalized hypotheses, plus scope;
- per-list numeric catalog positions, emitted and hydration-retry IDs, completion/error flags, and round-robin position;
- per-hypothesis/list current opaque provider offset, started/completion/error flags, page/hit counts, and at most 64 SHA-256 digests of prior nonempty offsets;
- numeric returned-candidate IDs, the global round-robin position, an opaque work lease, and last-touch time.

The registry does not retain raw hypotheses, titles, usernames, messages, snippets, Telegram payloads, file metadata, provider errors, credentials, or paths. Candidate evidence exists only while the current MCP call is assembled and returned. Raw hypothesis strings exist only in that current request/provider call and are not logged.

Separately, the broker's sole `TDLibClient` keeps a broker-lifetime, memory-only numeric catalog cache. It contains only Main/Archive mappings of integer `chat_id -> order`, reduced from validated asynchronous catalog updates regardless of which request received them. It does not retain raw update dictionaries, chat objects, titles, usernames, messages, hypotheses, snippets, paths, credentials, provider errors, or historical raw offsets. Broker restart clears this cache; there is no cleanup file.

The custom `MetadataIndex`, SQLite/FTS database, `INDEX_ROOT`, and all custom index writes have been removed from runtime code. Exact file-metadata and numeric-content searches are call-local. TelegramSearch creates no persistent alias, catalog, hypothesis, query, offset, snippet, evidence, or private-index store.

Application logging is restricted to constant readiness/error markers. Broker health exposes only uptime, queue depth, active-client count, completed/error counts, peak handler overlap, and aggregate TDLib serialization-wait count; it never includes targets, hypotheses, queries, IDs, titles, messages, snippets, offsets, paths, credentials, or exception text. Private request bodies and provider errors are not logged. The legacy `td_json_client_execute` symbol is used only once before client creation for `setLogVerbosityLevel(0)`, preventing TDLib diagnostics from contaminating the STDIO protocol. It is not reachable from an MCP tool.

The broker admits at most 64 pending requests and 64 client contexts. Concurrent handlers may interleave service work, while the TDLib boundary serializes each raw send/receive transaction. Work whose deadline expires before dispatch is never started; an in-flight TDLib call is not killed unsafely. A proxy requests one launchd kickstart, waits one bounded backoff, and retries once only when the initial socket connection itself fails. Once any request may have been sent, transport loss or timeout fails closed without restart or replay, so discovery state cannot advance twice and unrelated in-flight work is not interrupted. Overload is returned as a correlated framed error. Broker restart deliberately expires cursors rather than reconstructing private state.

TDLib still owns its configured session, database, and cache. Read-only `getChats`, `loadChats`, `searchMessages`, hydration, or exact-search calls can cause TDLib-managed cache/database metadata and byte counts to change. The product therefore promises no custom persistence, no read receipts, and no attachment downloads—not byte-for-byte immutability of TDLib's own files.

Historical custom index files from an older version are not read or updated, but they are not automatically deleted. Any cleanup remains separately authorized: first resolve exact paths and sizes with metadata-only checks, then prefer an owner-only quarantine. Permanent deletion requires explicit authorization after rollback is no longer needed. TDLib session data is governed separately and is never treated as obsolete index data.

## Explicitly unreachable functionality

- `viewMessages`, read receipts, and unread-state changes
- send, edit, delete, reaction, or moderation operations
- attachment or thumbnail downloads
- null-list global search, `searchPublicChats`, or public-chat crawling
- secret-chat access
- caller-controlled discovery budgets, provider offsets, filters, dates, chat lists, functions, or parallel fan-out
- persistent raw hypotheses, queries, titles, snippets, messages, evidence, provider payloads, or custom indexes
- a raw execute bridge or arbitrary TDLib request
- caller-directed filesystem reads or writes, paths, credentials, or account selection

## Untrusted content

Telegram text, captions, filenames, chat titles, and sender names are data, never instructions. Control, surrogate, and bidirectional formatting characters are removed; whitespace and length are bounded. Snippets and context are prefixed `[untrusted Telegram evidence]`. The manifest and tool descriptions repeat this trust boundary for MCP consumers.

## Acceptance and audit

Automated tests and source audits establish only the technical contract. Product completion still requires authorized live acceptance on the built four-tool surface, without recording private runtime values:

1. Capture owner-observed unread indicators in Telegram mobile and metadata-only TDLib cache existence, entry count, total bytes, and metadata hash before and after; never print entry names.
2. Start two fresh Codex tasks back-to-back with different owner-approved exact targets and queries, using only the normal TelegramSearch MCP surface, and verify each exposes exactly four tools.
3. Require overlapping request intervals, at least two broker client contexts, at least one aggregate TDLib serialization wait, one broker process, and multiple thin proxy processes.
4. Require both tasks to return complete bounded evidence with exact-chat anchors and no session-owner or TDLib code-400 failure; `telegram-search-mcp --check` must remain `AUTHORIZATION_READY` while multiple proxies exist.
5. Confirm unread indicators remain unchanged, no attachment is downloaded, no custom private store appears, and metadata-only cache effects are disclosed without filenames.
6. Confirm no credentials, private payloads, provider exceptions, or synthesized URLs appear in logs, status, health, or saved evidence.
7. Record exactly `Approved`, `Changes Required`, `Blocked`, or `Concept Rejected` according to the approved decision rule.

Target strings, hypotheses, queries, IDs, snippets, directory names, and evidence receipts are runtime-private and must not be copied into repository files or logs. If a side-effect or coverage check cannot be confirmed, mark it unverified and do not claim product approval.

The `--check` path uses the broker to verify TDLib authorization without reading messages or creating another session owner. Automated and subprocess tests use synthetic evidence and do not substitute for the live owner-observed workflow.

## Rollback

Boot out the validated per-user LaunchAgent, remove only its validated generated plist/socket/lock, revert the implementation, rerun the prior editable install, and terminate only the current proxy PID snapshot so Codex respawns the preceding direct server. Restart clears all discovery cursors and the broker-lifetime numeric catalog cache. Rollback restores the known single-owner limitation.

Do not delete historical custom-index or TDLib session data as part of rollback. Historical index cleanup remains separately authorized and recoverable; session revocation is an explicit owner action if authority must be withdrawn.
