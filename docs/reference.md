# Unofficial Telegram MCP technical reference

[← Back to the overview](../README.md) · [Installation](install.md)

Unofficial Telegram MCP is a local STDIO MCP surface for bounded Telegram discovery, exact chat search, selected attachment transfer and analysis, and explicitly approved text, document, photo, and voice-note sending. Each Codex task runs a thin proxy. All proxies connect through an owner-only Unix socket to one launchd-managed broker, the sole owner of TDLib and its authorized dedicated session. Public attachment reads use exact message anchors or server-issued artifact IDs; no Telegram credentials or caller-selected download paths enter MCP.

## MCP surface

The public surface contains forty-three tools:

- `_manifest` describes the fixed scope, budgets, trust boundary, and prohibitions.
- `resolve_target` resolves only a verified Saved Messages alias or exact `@username` to an exact numeric `chat_id`.
- `discover_targets` advances bounded Main/Archive catalog and global-message evidence lanes for Codex-authored hypotheses. It returns evidence, never a semantic winner.
- `search_correspondence` searches one exact `@username` or numeric `chat_id` for message text, captions, file metadata, or any Unicode decimal-number sequence in text/captions.
- `get_attachment` transfers the original file from one exact message anchor into a private, bounded cache.
- `get_message_context` returns bounded neighboring messages for an exact anchor.
- `read_attachment` extracts bounded PDF, DOCX, UTF-8, or image evidence, including actual MCP image previews.
- `analyze_media` transcribes a selected audio/video artifact locally and returns bounded video frames as MCP images.
- `create_local_artifact` creates a bounded UTF-8 or base64 document/image/media file for review and optional sending.
- `begin_local_upload`, `append_local_upload`, and `finish_local_upload` accept files in ordered, hash-checked chunks of at most 512 KiB decoded and return an artifact ID after complete verification.
- `prepare_reply_artifact_send`, `get_reply_artifact_draft`, `update_reply_artifact_draft` and `refresh_reply_artifact_draft` prepare and inspect document, photo and voice-note replies with bound local bytes, caption, source provenance and target evidence (separate opt-in); `send_prepared_artifact` makes the approved reply attempt.
- `prepare_reply_text_send`, `get_reply_draft`, `update_reply_draft` and `refresh_reply_draft` prepare and inspect immutable same-chat plain-text replies with full target evidence and fresh approval (separate opt-in); `send_prepared_text` makes the approved reply attempt.
- `prepare_text_send` and `send_prepared_text` preview the full normalized plain text and make one approved send attempt.
- `prepare_artifact_send` fixes the recipient, file hash, size, name, MIME, caption, and `kind=document|photo|voice_note` in an unsent draft. Document is the compatible default. Photos are decoded and validated; voice sources are converted locally to verified OGG/Opus mono.
- `send_prepared_artifact` makes one provider attempt only after explicit user approval of that exact preview.
- `get_send_status` inspects the exact owned draft/attempt without approval, a send, or retry; late exact terminal evidence can refine an uncertain outcome.
- `list_drafts`, `get_draft`, `update_draft`, `refresh_draft`, and `cancel_draft` manage pending drafts owned by this proxy and its current Telegram account (`send` capability).
- `read_messages` reads up to 20 selected anchors with bounded full text/caption and message metadata (opt-in).
- `read_history` reads bounded pages from one exact numeric chat in explicit latest or date-interval mode (separate opt-in).
- `read_reply_chain` reads a selected message and up to nine same-chat ancestors with explicit traversal stops (separate opt-in).
- `list_topics` observes bounded forum topic metadata from one exact chat, with single-use continuation (separate opt-in).
- `read_topic_history` reads bounded observations from one exact typed forum topic (separate opt-in).
- `search_messages` continues bounded lexical text/caption search in one exact numeric chat (separate opt-in).
- `list_chats` lists a bounded observed Main/Archive prefix with unread metadata and single-use continuation (separate opt-in).
- `search_chats` searches bounded content across1..5 explicitly selected numeric chats with separate coverage and outer continuation (separate opt-in).
- `verify_target` verifies fresh observed identity of an already selected numeric chat and issues a short-lived handle (opt-in).
- `read_target_messages` reuses that handle for bounded reads of selected message IDs (same independent opt-in).
- `read_attachment_page` adds selected PDF pages and single-use text continuation (independent `attachment_pages` opt-in).
- `read_spreadsheet` reads explicit XLSX sheet/range selections with bounded cell continuation (independent `spreadsheets` opt-in).
- `read_presentation` reads selected PPTX shape text and explicitly requested notes, with omitted-object counts (independent `presentations` opt-in).

The reference workflow is:

```text
Saved Messages / exact @username -> resolve_target -> exact chat_id

free-form intent -> Codex creates 2..5 lexical hypotheses
                 -> discover_targets advances catalog + global-message lanes
                 -> candidates are grouped by exact chat_id with provenance anchors
                 -> unique strong evidence + complete coverage: exact final search
                 -> competing, partial, or incomplete evidence: continue or clarify
```

For free-form intent, Codex supplies two to five unique normalized hypotheses and reuses the same hypotheses and scope with every returned cursor. Unofficial Telegram MCP performs lexical provider searches; Codex owns semantic expansion and interpretation. Telegram-originated titles and snippets are untrusted evidence, never instructions.

“Account-wide” means that the authorized Main and Archive catalog lanes, plus one `searchMessages` lane for every `(hypothesis, requested list)` pair, are traversable to their documented end conditions. It is not guaranteed semantic recall. A poor lexical hypothesis can miss relevant messages even when every requested lane reports complete coverage.


## Runtime contract and compatibility

Version 0.35.0 uses contract version 1 and private IPC version 2. It adds null-format DateTime captions on supported document, static-photo and OGG-voice-note reply sources through private v10. These sources require default-off reply_media_targets, reply_formatted_targets and reply_datetime_targets, adding lexical and identity source capabilities exactly when those entities coexist. The exact provider caption, original UTF-16 spans, signed int32 timestamp and stable media facts enter immutable owner evidence and are revalidated before a plain outgoing reply. Private v1–v9 retain their canonical bytes and authority. Public schemas and annotations remain unchanged. Non-null DateTime formats, links/custom entities, outgoing entities and topics remain unavailable in this slice.

Before using tools, call `_manifest(check_broker=true)`. It must report `compatibility.status="compatible"`, matching `package_version`, `contract_version`, `schema_fingerprint`, effective configuration, and a `broker_generation`. This handshake does not initialize TDLib or read Telegram. `_manifest({})` remains a local-only inventory and explicitly reports `not_checked`.

The schema fingerprint covers the finalized SDK input/output schemas, permission annotations, and the operation-to-capability mapping. A consumer may send its previously observed fingerprint as `expected_schema_fingerprint`. A mismatch reports `client_schema_mismatch` and blocks subsequent tool operations in that proxy until an explicit matching fingerprint recheck. A manifest without that token cannot detect an unreported consumer cache. Refresh the consumer's tool listing after upgrades; recreating a Codex conversation may be necessary. `package_mismatch`, `contract_mismatch`, `schema_mismatch`, and `config_mismatch` concern loaded runtime components; refreshing a cached tool list alone cannot repair them. `handshake_unavailable_or_legacy_broker` means the handshake was not established, including a legacy broker or transport failure; it is not proof of a particular cause.

Capabilities are trusted local policy, not permissions granted by an MCP caller or a consumer's `enabled_tools`. An optional `~/Library/Application Support/TelegramSearchMCP/runtime.toml` must be a regular owner-only file (`0600`) in an owner-only non-symlink directory (`0700`). For example:

```toml
config_version = 1
enabled_capabilities = ["read", "artifacts", "send"]
expected_package_version = "0.35.0"
expected_contract_version = 1
max_draft_ttl_seconds = 900 # Optional; strict integer 1..86400, default 900.
# Optionally pin expected_schema_fingerprint to the reviewed local manifest value.
```

`read` covers existing target discovery, exact search and context; `artifacts` covers existing selected attachments and local artifact/upload operations; `send` covers existing previews and approved sends. Omitting this file preserves exactly those existing capabilities, including all existing approval requirements. `read_messages` enables selected full-message reads and is not a legacy default; add it explicitly to `enabled_capabilities` to opt in. `read_history` independently enables bounded history reads and must also be added explicitly. `read_reply_chain` independently enables bounded same-chat ancestry and must be added explicitly. `list_topics` independently enables forum topic listing and must be added explicitly. `read_topic_history` independently enables exact forum history and must be added explicitly; the other read capabilities do not enable it. `search_messages` independently enables paginated lexical text/caption search and must be added explicitly; no existing read capability enables it. `list_chats` independently enables bounded chat listing with unread metadata and must also be added explicitly. `verified_targets` independently enables both verification and handle reads and is off by default; existing read capabilities do not enable it. Other future extension capabilities require explicit opt-in; unknown names, wildcard values, malformed settings and unsupported config versions are rejected. An empty capability list disables product operations while retaining diagnostics. Config changes block already-running components with `config_stale`; restart matching components after reviewing the new configuration. Optional expected-version/schema pins reject stale loaded code even when proxy and broker match one another. Roll back the matching package and policy together.

Every operation performs a fresh handshake on its own owner-authenticated Unix connection and binds the next request to the broker's process generation. The broker rechecks policy freshness and the required capability immediately before dispatch. Generation changes never authorize replay of a send or reuse of lost state. A proven rejection before dispatch returns a safe error; lost responses after dispatch preserve the existing uncertain-outcome rule.

## Codex skill

The matching Codex skill is versioned with the MCP at [`skills/telegram-search/SKILL.md`](../skills/telegram-search/SKILL.md). Keeping both artifacts in one repository makes their tool contract, routing rules, and safety boundaries reviewable as one version. The runtime-installed skill remains a separate local copy and is updated only after the repository version has been reviewed and verified.

## Inspect a send outcome

Use `get_send_status({"draft_id":"draft_<issued-id>"})` in the same running proxy.
The only input is the exact draft token, and the existing `send` capability is
required. Status reads never request approval, claim a draft, or send/retry it.
The closed response contains `draft_id`, `status`, `evidence`, fixed `detail`, and
`message_id` (a positive final ID only for `sent`); it contains no content,
recipient/account IDs, paths, temporary IDs, or provider error strings.

| Status | Evidence | Meaning |
| --- | --- | --- |
| `pending` | `local_pending` | Live unclaimed draft; no provider acceptance or approval. |
| `pending` | `local_claimed` | An ongoing local attempt; provider acceptance is unconfirmed. |
| `pending` | `provider_pending` | Exact correlated pending provider observation during an ongoing call. |
| `outcome_unknown` | `provider_pending` | The response/wait was lost, but an exact pending observation remains. It does not restore certainty. |
| `sent` | `provider_confirmed` | Exact terminal provider confirmation and validated positive final message ID. |
| `failed` | `provider_failed` | Exact provider rejection or coherent correlated send failure. |
| `failed` | `local_failed` | Local preparation failed before the provider send was invoked. |
| `outcome_unknown` | `none` | No currently retained authorized evidence. |

After a lost send response, repeat status reads to inspect evidence; never use
another send attempt as a status check. Only exact late success/failure can refine
an unknown result. Correlation uses the provider instance, request/sending ID,
chat and temporary/final IDs, outgoing direction and content kind. The original
text integrity check uses an approved-text digest after correlation; text or
similarity is never a correlation key. Quick acknowledgments, deletion, absence,
and unrelated messages cannot confirm delivery.

Content-free observations retain only bounded IDs/state/times and an optional
text digest, at most 900 seconds from the initial attempt and 4096 observations.
Reads never renew retention; capacity refusal happens before a send. A bounded
nonblocking drain may consume queued updates, with account/policy checks inside
the call's 30-second deadline. Account/auth or provider-instance change, expiry,
pruning and broker restart discard evidence honestly. Unknown/foreign/wrong-account
and obsolete unattempted tokens return the same unknown shape. The existing
same-UID cooperative client boundary still applies. These observations are not
recipient delivery/read receipts, provider leases, persistent recovery, or an
exactly-once guarantee. Synthetic verification does not establish live delivery.

## Inspect, revise or cancel an unsent draft

Use the same running MCP proxy that prepared the draft. `list_drafts({"limit":20})`
returns only that proxy's unexpired pending drafts for the currently authorized
Telegram account. Each summary contains the draft ID, account, recipient/title,
kind and expiry; it omits message text, caption and filename. For another page,
pass the returned `next_after_draft_id` as `after_draft_id` with the same limit.
The maximum page size is 50. Results follow draft-ID order over current live
state, not a frozen snapshot. New drafts can appear before an earlier page;
restart the listing to see the current set. An expired continuation anchor
requires restarting the listing.
List/get can be repeated after a lost response; the draft-ID anchor is not a
single-use cursor.

`get_draft({"draft_id":"draft_<issued-id>"})` returns the full saved preview of
one pending draft. Show its exact content, account, recipient, hash and expiry
before requesting approval. Listing is a metadata observation; get and send
reject an unavailable or changed artifact. A preview is neither confirmation of
current recipient identity nor approval to send. Existing send tools recheck
the recipient and still require both explicit approval and local confirmation.

`cancel_draft({"draft_id":"draft_<issued-id>"})` cancels a pending draft without
deleting its artifact. A repeated cancellation by its owner is harmless while
the record remains live. Cancellation racing with a send succeeds only if it
wins before the send claim; it cannot undo a claimed or completed attempt.
`unavailable` does not prove that no send happened. Keep any earlier send receipt
and never retry an uncertain delivery automatically.

To replace the complete message text, call
`update_draft({"draft_id":"draft_<issued-id>","text":"Revised text"})`.
For a document, photo or voice note, supply `caption` instead; an empty caption
removes it. Supply exactly one non-null field. The recipient, send kind and
artifact remain fixed; use a new prepare operation to change those facts.
`refresh_draft({"draft_id":"draft_<issued-id>"})` keeps the same content and
artifact, checks the bytes and resolves the same recipient's current title.
Both return a complete preview with a **new draft ID, which is the revision
token**. The previous ID becomes unavailable immediately, including to a send
whose local approval dialog was already open. Inspect the new preview and obtain
fresh explicit approval before passing its new ID to the matching send tool.
Update/refresh never send and cannot change a claimed or completed attempt.

Each new revision has a finite TTL from trusted `max_draft_ttl_seconds`; the
default is 15 minutes and the allowed range is 1 second through 24 hours.
Artifact revisions are also capped by the originally observed artifact deadline
and its current expiry. Refresh does not touch or extend cached artifacts.
Only a still-live pending revision can be renewed; expired drafts cannot revive.
Repeated explicit revisions can renew a live text draft, but each older ID is
invalidated. After a lost update/refresh response, list/get the current drafts
and inspect their exact previews; do not repeat a send or assume the old ID is
still usable. A superseded unexpired ID remains a valid own pagination anchor.

All five tools require the existing `send` capability. Foreign, unknown and
unavailable draft IDs do not disclose previews. Reconnecting with a new proxy
identity cannot recover the previous proxy's drafts, and broker restart loses
all drafts. Client separation is cooperative within the trusted local OS user:
it does not authenticate separate applications against malicious same-UID code.
There is no durable draft journal or recovery across broker restarts.

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
- macOS Keychain service `com.<local-home-name>.telegram-search-mcp`, accounts `api_id` and `api_hash`.
- An existing dedicated TDLib session that reaches `authorizationStateReady`.

Unofficial Telegram MCP creates no custom persistent private index. Exact file-metadata and consent-gated numeric-content search scan bounded `getChatHistory` pages in memory during the call; there is no SQLite/FTS runtime write. Numeric-content search returns only matching evidence from the exact selected chat, never a transcript export. Discovery retains only bounded mechanics in memory, separately for each proxy client, and the broker's single TDLib client keeps a broker-lifetime, memory-only numeric Main/Archive `chat_id -> order` catalog cache so asynchronous catalog updates are not lost between requests. Raw hypotheses, snippets, titles, messages, and provider payloads are not retained in either cache or logged. Releasing a proxy clears its discovery context; broker restart clears all contexts and the catalog cache.

Video and video-note searches use TDLib's matching-media index instead of walking unrelated chat history. They follow provider pagination to completion and retain verified partial results if a later page fails. The response limit applies to evidence enrichment as well as output: older matches beyond the newest requested results do not trigger individual message, sender, or link requests. The broker's search deadline also bounds provider-lock waits and individual TDLib requests; an exhausted deadline cannot report complete coverage.

TDLib owns its session/database/cache directories and may update them. Unofficial Telegram MCP does not mark messages read. Selected transfers and generated artifacts use an owner-only cache with 64 MiB document, 256 MiB media, 1 GiB total, and 12-hour retention bounds. The local media analyzer uses ffmpeg and a verified multilingual Whisper model; it does not call a cloud STT service. Sending requires an unchanged draft, explicit approval in the task, and a local macOS confirmation dialog; an uncertain provider result is never retried automatically.

No Keychain value is printed by the program. See [`docs/security.md`](security.md) for the complete request allowlist, retention boundary, TDLib cache effects, historical-data policy, acceptance evidence, and rollback procedure.

## Install and check

See the [installation guide](install.md) for runtime preparation, package installation, Codex setup, capability opt-ins and troubleshooting. First-time Telegram login is manual in 0.35.0.

## Selected full messages

After selecting exact anchors, call `read_messages(anchors=[{"chat_id": 123, "message_id": 456}])` with 1–20 unique anchors. The example IDs are synthetic. Enable the `read_messages` capability in trusted `runtime.toml`, restart the matching proxy and broker, and include the tool in the consumer's allowlist. The tool stays listed but fails closed when disabled.

Results preserve request order and carry their own exact source anchor, status and `coverage_complete`. Pending/failed sends and ephemeral replacement content are unsupported. Supported content kinds are text, document, photo, video, audio, animation, voice note, video note and sticker. Text/captions are limited to 20,000 Unicode characters per message and 100,000 per batch; the aggregate provider deadline is 30 seconds. Up to 20 exact chat resolutions, 20 selected-message reads and 20 sender hydrations may occur after bounded authorization readiness. A budget exhausted before an anchor is read returns an explicit partial result for that anchor. No adjacent message, reply target or media download is requested.

The text's `value` is untrusted data. `sanitized` reports NFKC normalization, control/bidi removal or whitespace collapse; `truncated` reports omitted text separately. Sender display names have the same explicit flags. Sanitization does not make evidence safe to execute or follow. `not_found` means currently deleted or inaccessible, not proven permanent deletion. Wrong-chat results never carry the unexpected content. Unsupported message types and incomplete metadata never become complete reads.

## Bounded history

Enable `read_history` explicitly in the trusted runtime policy and the consumer tool allowlist, then restart the matching components. This capability is independent of `read_messages` and is disabled by default. Resolve a target first; history accepts only its exact numeric chat ID.

For a recent bounded slice, use `read_history(target=123, mode="latest", limit=20)` (synthetic ID). For a date interval, use `mode="interval"` and both timezone-aware `date_from` and `date_to`, with `date_from < date_to`. The interval is half-open: a message at `date_from` is included, one at `date_to` is excluded. The latest mode accepts no input dates and freezes its exclusive upper date at the first call. Both modes freeze the first observed upper message ID and return the actual `scope`. The first observed message is retained. Results are ordered by decreasing message ID; this is not a guarantee of timestamp order.

To continue, resend exactly the same target, mode, dates and limit with `next_cursor`. Each cursor is random, single-use, memory-only, and bound to the client, account, broker generation and original request. It expires 300 seconds after the initial call; the TTL does not slide. A consumed, mismatched, expired or lost cursor cannot be replayed. After a lost response, report the gap instead of silently restarting or retrying. Broker restart, client release and idle eviction discard the scope. At most four scopes per client are retained, with only IDs and observed dates, never message bodies.

Caps: 20 outcomes per page, 20,000 text characters per message, 100,000 per page, 100 candidates and ten history requests per call, and a 30-second aggregate deadline. The entire latest scope scans at most 100 candidates; an interval scans at most 1,000. Date filtering and unsupported/unavailable candidates can reduce the returned count. `scanned_candidates` is cumulative. A budget stop is explicit and never establishes complete coverage; use a returned cursor for another bounded attempt.

`page_complete` means the requested number of per-anchor outcomes was returned without an item issue. `scope_complete` remains false: this pinned TDLib interface cannot certify full history coverage. `has_more=true` means already-observed candidates remain; null means unknown. A cursor permits continuation but does not promise more matching messages. Empty, short or nonprogress pages terminate conservatively with `provider_end_unverified` or `provider_nonprogress`. They do not prove an empty chat, no matches, or an exhausted date interval. An old timestamp does not end the scan because timestamp monotonicity is not guaranteed.

Each result is rehydrated and its chat/message/date checked before text is returned. Deleted in-window candidates have an unavailable outcome; stale page text is never substituted. Reads reflect current TDLib-observed state, which can be cached. Frozen boundaries exclude newer IDs, but do not freeze edits, deletions or lower-ID backfill. Names/text remain untrusted. No read receipts, media downloads or reply traversal occur.

The metadata includes typed sender ID and verified display name, outgoing direction, UTC send/edit dates, and reply/topic/album references. A null send date preserves the provider's zero timestamp for a scheduled message. Album IDs are decimal strings; topic IDs retain their signed provider values. Reply targets with unknown IDs remain unknown and are not followed. The result is a current rehydrated snapshot, not a historical edit record; the batch is not atomic. Formatted entities, embedded reply quotes, service-message bodies and media contents are outside this text/metadata reader. There is no persistent message cache or continuation for truncated selected reads.

## Exact search contract

```text
search_correspondence(
  target: exact @username or numeric chat_id,
  query: {text?, file_name?, mime_type?, media_type?, contains_number=true?},
  date_from?, date_to?, limit=20, context_messages=0, require_complete=true
)
```

Every match includes `{chat_id, message_id}` as an evidence anchor. `source.telegram_url` is an optional deep link returned by Telegram for that exact message. `source.chat_url` remains schema-compatible but is populated only when the provider returns a complete supported HTTPS chat link; Unofficial Telegram MCP never constructs a `t.me` URL from a username. Either URL can be `null`. `no_match` is returned only when all requested exact-search lanes report complete coverage; otherwise the status is `incomplete`.

`contains_number=true` is the bounded path for an owner-authorized request to find unknown numeric sequences. It matches one or more Unicode decimal digits in message text or media captions, only inside the selected exact chat and date interval. It is call-local, returns matching evidence only, and cannot be set to `false` or used as an empty/general transcript query. When combined with other fields, every predicate must match the same message.

Telegram-originated strings are untrusted. Snippets and context are marked as untrusted evidence, and control/bidirectional formatting characters are removed.

## Building a release

Install `build==1.3.0` alongside the pinned runtime dependencies, then run `python -m unittest discover -s tests -v`, `python -m build`, and `python -m pip check`. Source distributions strip local archive owner names and numeric ownership. Set `SOURCE_DATE_EPOCH` to a fixed release timestamp when reproducible timestamps are required. Review wheel/sdist contents and Git metadata before publishing. The installed wheel includes documentation, config example, and optional skill under `share/telegram-search-mcp` in the environment.

The current package does not include a project license grant. Resolve project licensing and dependency distribution obligations before public distribution; package installation alone is not a licensing decision.

## Bounded reply chains

Enable `read_reply_chain` in trusted runtime policy and the consumer allowlist,
then restart the matched broker and proxy. Call
`read_reply_chain(anchor={"chat_id":123,"message_id":456}, max_depth=10)`
(synthetic IDs). `max_depth` is 1–10 and includes the selected root. Results run
from root to parent; every node is fetched and its exact identity verified.
Embedded quote, origin and content fields are never returned as parent evidence.

Traversal stops before fetching cross-chat references, stories, cycles or a node
beyond the cap. Missing, malformed, unsupported and inaccessible parents have
explicit outcomes. Inspect `stop_reason`: `no_parent` alone establishes
`chain_complete` for the current TDLib-observed chain. This is neither a forced
server refresh nor a historical snapshot. `coverage_complete` additionally
requires every returned node's content and metadata to be complete; truncated
text can finish traversal while leaving content coverage partial.

The aggregate limits are 30 seconds, ten node hydrations, 20,000 text characters
per node and 100,000 per call. No media download, read receipt, cross-chat fetch,
story read or persistent transcript is performed. Text and names remain untrusted
and sanitization/truncation remain explicit. A current missing parent does not
prove permanent deletion. Reply edits during traversal can change the chain.


## Forum topics

Enable `list_topics` in trusted runtime policy and the consumer allowlist, then restart matching components. Pass a resolved numeric chat ID: `list_topics(target=123, limit=20)` (synthetic ID). This reader supports verified forum supergroups and bot private chats whose provider metadata enables topics. A valid nonforum chat returns `unsupported`; ordinary message threads, Saved Messages topics and direct-message topics are separate families and are outside this reader.

Each call returns and separately hydrates at most the requested number of topic outcomes (1–20). TDLib may return more rows than requested. A native page is accepted only within the remaining 200-candidate scope budget; excess unique topic IDs are held in memory and drained on following calls before another page is fetched. The native offset triple is preserved unchanged. A call fetches at most one native page, and a buffered call fetches none. Oversized pages end the scope without truncating and resuming past unseen topics. Results contain only typed forum IDs, untrusted names with sanitation/truncation flags, creation time and general/closed/hidden flags. Closed or hidden topics may be read when accessible. An unavailable topic is not proof of permanent deletion. Embedded messages, drafts, unread counters and notification settings from provider pages are never returned or retained. Names are capped at 256 characters; no message bodies are read by this operation.

To continue, keep the exact target and limit and pass `next_cursor`. Each random single-use token binds the client, account, broker, contract and request. A scope expires five minutes after its first call and scans at most 200 provider candidates across ten page attempts. Each call has a 30-second aggregate budget. At most four scopes are retained per client, with numeric pending/seen IDs, offsets, counters and fixed stop reasons only. `scanned_candidates` and `provider_pages` count accepted native observations and page attempts; they stay unchanged while buffered IDs drain. Reaching a native-page or candidate cap permits draining already observed IDs, then ends the scope. Release, restart, expiry, replay or a lost response ends that continuation; do not silently retry or restart.

`page_complete` means every returned observed topic outcome has complete metadata, not that the requested count or full inventory was returned. `scope_complete` is always false and `has_more` is null (unknown). Approximate provider totals, empty/short pages and zero/repeated offsets cannot prove inventory completeness. Duplicate IDs are omitted across pages; duplicate-only pages stop. A cursor permits another bounded attempt but does not promise further topics. Topic ordering, edits, deletions and backfill can change between observations, so this is not a frozen server snapshot. This operation neither reads topic history nor changes topics or read receipts.


## Bounded history in one forum topic

Enable `read_topic_history` in the trusted runtime policy and consumer allowlist,
then restart matching components. This capability is disabled by default and is
independent of `read`, `read_messages`, `read_history`, and `list_topics`. Select an
observed typed forum reference from the exact current chat, then call
`read_topic_history(target=123, topic={"kind":"forum","id":2}, mode="latest", limit=20)`
(synthetic IDs). Ordinary thread, Saved Messages, and direct-message topic IDs
are separate families and cannot substitute for a forum ID. The operation
supports verified forum supergroups and bot private chats with topics. It never
falls back to chat history, thread history, General, or another topic.

Latest accepts no input dates and freezes its exclusive upper date at the first
call. Interval requires timezone-aware `date_from < date_to` with half-open
`[date_from,date_to)` filtering. Both modes freeze the first observed upper
message ID; outcomes descend by message ID, without assuming timestamp order.
Each call rechecks account readiness, exact forum capability, and current exact
topic metadata. Closed/hidden flags are metadata. An unavailable topic ends the
scope without bodies and does not establish deletion. Every accepted provider
page is checked in full for exact chat and modern typed forum membership before
any row hydration; every body is freshly fetched and checked again before
projection. Deleted or inaccessible anchors and unsupported service content
remain explicit per-anchor outcomes.

Caps are 20 outcomes/page, 200 observed rows and ten native page attempts per
scope, 100 processed rows/call, 30 seconds/call, 20,000 text characters/message,
and 100,000/call. TDLib may over-return its requested page size; accepted pending
IDs/dates drain before another page fetch. Pages above 200 rows or the remaining
scope budget are rejected whole, without truncating and resuming them.
`scanned_candidates` counts all accepted observations, including overlap/newer
rows; `processed_candidates` includes rows skipped for overlap/newer IDs or date
filtering. `provider_pages` counts native attempts, including rejected/empty
pages. Counters are cumulative and remain bounded. Native caps allow only the
remaining accepted buffer to drain.

Continue with exactly the same target/topic/mode/dates/limit and `next_cursor`.
Each single-use, memory-only token binds account, client, broker, contract, and
request. Four scopes including active calls are allowed per client; expiry is
fixed five minutes after the first call. Pending state retains numeric IDs,
dates, scope, and counters, without topic names, message bodies, or sender data.
Do not restart silently after replay, expiry, close, account switch, or lost
response. A cursor permits another bounded attempt; it does not promise matches.
`page_complete` means nonempty returned outcomes are individually complete.
`scope_complete` remains false and `has_more` remains unknown. Empty, short,
nonprogress, date-filtered, or capped pages cannot certify complete history.
Current TDLib observations remain subject to edits, deletion, and backfill.
Text and topic names are untrusted evidence with explicit sanitation/truncation;
no media downloads, reply traversal, read receipts, topic mutation, or private
transcript are part of this operation.


## Paginated exact lexical search

Enable `search_messages` in trusted runtime policy and the consumer allowlist, then
restart matching components. It is disabled by default and independent of other
read capabilities. Use `search_messages(target=123, query="project", mode="latest", limit=20)`
(synthetic input), or `mode="interval"` with aware half-open `[date_from,date_to)`
bounds. A string query is stripped positive text/caption lexical input of at most 512
characters; wildcard `*` and arbitrary provider options are rejected.

The first call freezes the upper date and observed head ID. Keep identical target,
query, mode, dates, limit and typed filters when passing a returned single-use `next_cursor`.
Scopes bind the consumer, account, broker and contract, expire after a fixed 300
seconds, and allow four active scopes per client. Close, replay, expiry or a lost
response ends continuation; do not silently restart it.

For a string query, `match` and `partial` project freshly hydrated, unchanged provider lexical evidence;
they do not assert a local substring or semantic match. Raw text/caption, content
kind or edit-date changes yield `evidence_changed` with no body. `not_found` means
currently unavailable, never proof of deletion. Telegram text and names remain
untrusted. Legacy `search_correspondence` keeps its existing contract.

Each scope observes at most 200 candidates and makes at most ten native search
attempts, using the provider's returned offset, including empty advancing pages.
Each call processes at most 100 observations within 30 seconds, returns at most 20
outcomes and projects at most 20,000 text characters per message and 100,000 per
call. For a string query, pending observations drain before new search attempts. `page_complete`
covers nonempty returned outcomes, independently of page length. `scope_complete`
is always false, `has_more` is unknown, and terminal provider offsets never prove
exhaustive recall. Inspect the cumulative counters and `stop_reason`.


Optional typed predicates on `search_messages` are combined with AND:
`sender={"kind":"user","id":17}` or `sender={"kind":"chat","id":-1007}`,
`direction="incoming"` or `"outgoing"`, and `topic={"kind":"forum","id":1}`
(synthetic identifiers). Omitted and null predicates are equivalent. Sender user
IDs must be positive; sender chat IDs nonzero, both within the JSON-safe integer
range. Forum IDs are strict positive int32. Names, aliases, unknown keys/kinds,
nested JSON strings, generic thread selectors and coercions are rejected.

Native sender/topic filters select candidates only. TDLib can broaden General
forum-topic or broadcast-channel sender searches, and combinations have provider
support limits. Every processed typed candidate is freshly hydrated and all
requested predicates are checked against current raw sender, `is_outgoing`, and
modern typed forum membership. Direction is never inferred from sender identity.
Ordinary messages with absent/null topics and valid non-forum topics do not match
General. Every topic-filtered call checks the exact current forum and topic;
missing/nonforum/invalid topics terminate without a broader fallback. Closed or
hidden readable topics remain eligible.

An originally matching candidate whose current predicates differ yields
`evidence_changed` without content. A candidate matching neither observation nor
hydration is omitted; one newly matching on hydration still requires unchanged
lexical evidence. Missing hydration yields `not_found` only for originally
matching candidates. Filtered rows consume the same budgets, so an empty page can
carry a continuation. Neither empty nor terminal pages prove that no matches
exist elsewhere. Frozen scope and single-use cursors bind every typed predicate;
the proxy and response validator independently check every returned body's typed predicates.


### Bounded Boolean queries

The same opt-in tool also accepts a native object instead of the lexical string:

```json
{"target":123,"mode":"latest","limit":20,"query":{"all":["project"],"any":["launch","release"],"none":["cancelled"]}}
```

This synthetic example requires `project` and either `launch` or `release`, and
excludes `cancelled`, all within the same current message's full text/caption.
The arrays default to empty; each has at most four terms, with at most eight total.
At least one `all` or `any` term is required. Every term is stripped, non-wildcard
text of 1–512 characters. Normalized duplicates within an array are rejected.
Extra fields, negative-only queries and nested expressions are rejected. A
JSON-looking string remains a literal lexical string, never an encoded object.

Object predicates use Unicode NFKC, casefold and whitespace collapsing followed
by substring matching. There is no stemming, tokenization, punctuation removal
or transliteration: `Straße` matches `STRASSE`; `foo-bar` does not match `foo bar`;
`ban` also excludes `urban`. The frozen scope reports this distinction in
`matching_semantics`. String queries retain TDLib lexical behavior, including its
provider-specific stemming. Neither mode promises complete recall.

Each `any` term seeds one native lexical search. If `any` is empty, only the first
`all` term seeds a search. The full Boolean expression and typed predicates are
checked against freshly hydrated content before display sanitation/truncation;
negative terms never initiate provider searches. Provider lexical selection can
miss local matches or return stemmed candidates that fail the local predicate.
This is bounded filtering of provider observations, not a scan of every message.

The branches share the same 200 observations, ten native attempts and all other
limits above. Each branch must have a known head or stop state before descending
merge can emit a result. Empty advancing branches are refilled; inability to
establish a head within budget terminates with incomplete coverage. The first
merged head freezes the scope; exact anchors are deduplicated across branches
and pages. Conflicting observed digests or changed evidence disclose no body.
Current exclusions are applied after rehydration, including beyond the displayed
20,000-character prefix. Unsupported content or raw text above 65,536 characters
cannot become a match. Schema/proxy independently recheck Boolean predicates only
when the displayed text is complete and unsanitized; an altered or truncated
display cannot prove the contents of the full raw evidence.

`branch_coverage` contains each indexed seed, native traversal state and cumulative
observation/processing/attempt counts. `scanned_candidates - processed_candidates`
is the number of pending observations; a provider stop state can still have
pending observations. `active` means native continuation remains possible;
`provider_end_unverified` and `provider_nonprogress` do not establish recall.
`scope_budget_exhausted` or `interrupted` means an unfinished branch was closed.
Global counters are the sum of branch counters. Inspect every branch, even when
another branch returned matches. `page_complete` covers returned bodies only;
`scope_complete` remains false and `has_more` null. Preserve the exact ordered
arrays, typed filters and all other request arguments when continuing.


## Bounded chat listing and unread metadata

Enable `list_chats` in the trusted runtime policy and consumer allowlist. Call
`list_chats(scope="main", limit=20)`, `scope="archive"`, or `scope="both"`.
Scope is required; wildcards, arbitrary folders, usernames and search text are
not listing inputs. Continue with the same scope and limit plus `next_cursor`.

The first call captures at most 200 native ID observations: 200 for one list or
100 from each for both. Native order is preserved within each prefix; both uses
Main then Archive, observed sequentially, without a combined recency claim.
Continuation processes only this frozen selection. New or newly reordered chats
require a new listing; cursors cannot reach beyond the selected prefix.

Each displayed chat is checked again by exact numeric ID against TDLib's current
local state. Its current membership must intersect the explicitly selected lists;
positions alone do not prove membership. A row distinguishes observed list/rank
from current requested-list membership. Secret, moved-outside-scope, unavailable,
malformed and duplicate observations are omitted and accounted for. Titles are
untrusted bounded text with sanitization and truncation flags. Metadata with a raw
title above4,096 characters or over128 list entries is omitted. A truncated display
title has its own flag and does not make otherwise valid metadata incomplete. No messages,
drafts, phone numbers or usernames are returned. Marked unread is separate from
the message count: a chat can be marked unread with zero unread messages.

`getChat` observes local TDLib metadata, not a guaranteed fresh server snapshot.
Unread metadata can change because of incoming messages or another Telegram
client. Listing never invokes `viewMessages`, `openChat`, `readChatList` or unread
mutations. One native prefix request may perform multiple internal TDLib loads;
the request/row/deadline bounds are not a network-byte or server-work guarantee.

At most `limit` observations, including omissions, are processed per call, under
a 30-second deadline. Per-list counters and their global sums describe observed,
processed, returned, omitted and pending observations. Cross-list duplicates count
as observation slots and consume the processing budget; the first occurrence owns
hydration. Terminal pending counts mean unprocessed observations with no available
continuation. State holds only frozen inputs, IDs/ranks, counters and bounded issue
codes, never titles or unread values. Four scopes per client, including active
calls, use single-use cursors and an absolute five-minute expiry, bound to the
account, client, broker generation and contract.

`page_complete` concerns only that call's processing and metadata. The entire
catalog remains unverified: `scope_complete=false`, `has_more=null`, and
`provider_coverage="bounded_tdlib_prefix_observation"` hold even for a short,
empty or exhausted selection. End of the selection never proves there are no more
chats. Listing does not resolve a free-form person or authorize automatic target
selection; retain the existing discovery evidence and owner-choice rules.


## Bounded selected-chat content search

Enable `search_chats` explicitly in both trusted runtime policy and the consumer allowlist.
It is off by default and independent of `search_messages` and `list_chats`.
After selecting exact numeric identities, call
`search_chats(targets=[123,456], query="project", mode="latest", limit=20)`.
The ordered selection must contain1..5 unique, nonzero int53 IDs. All selected
chats pass exact non-secret metadata verification before any content request.
No target discovery, substitution, handles, wildcard scope or per-chat topic is inferred.
The existing lexical/Boolean query and typed sender/direction contracts apply.
Use `mode="interval"` with timezone-aware half-open dates when explicit bounds are needed.

One public call processes one selected chat page. `current_index` identifies that
chat; `coverage` maps every target in selection order to `pending`, `active` or
`stopped`, with local status, stop reason, frozen head, counters and Boolean branch
coverage. A stopped chat advances on the next public call. Results descend by
message ID within each chat; selection order does not claim global chronology.
One shared `date_to` freezes initially, while each chat head freezes on its first
native observation. This is not an atomic snapshot. Provider end, an empty chat,
budgets and partial failures never establish exhaustive recall or global absence.
`scope_complete` is always false and `has_more` is unknown; `page_complete` covers
only the returned nonempty outcomes. Pending lanes in a terminal response remain
unprocessed, with no promise of continuation.

Preserve the exact ordered targets, query, filters, dates and limit when passing
`next_cursor`. Only the single-use `selected_search_…` cursor is public; child
`search_…` cursors are private. Four outer scans, including in-flight calls, have
separate capacity from existing single-chat readers. Each chat keeps its200 native
candidate/10 page limits. Each call includes verification in one30second deadline,
with at most100 processed observations,20 outcomes and100000 display characters.
All continuations keep the original300second expiry. Changed account, authorization
loss, close, expiry or uncertain native deadline stops the group and suppresses
that call's content. Ordinary chat-specific provider failures remain explicit and
may permit proceeding to the next selected chat on a later call. No message bodies,
titles or unread values are retained in wrapper summaries, and no read receipts are sent.


## Reuse a verified numeric target

Enable `verified_targets` in trusted `runtime.toml`, restart matching proxy and broker,
and allow both `verify_target` and `read_target_messages` in the consumer. Existing
read capabilities do not enable this path. The example configuration lists both
tools, but trusted runtime policy still defaults this capability off.

First select the exact chat through the existing rules: an explicit owner numeric
choice, a direct resolver result, or complete corroborated discovery/explicit owner
selection. Numeric hydration verifies current identity only; it never establishes
that this is the owner's intended chat or bypasses semantic selection. Then use:

```text
verified = verify_target(target=123)
read_target_messages(target_handle=verified.target_handle, message_ids=[456,789])
read_target_messages(target_handle=verified.target_handle, message_ids=[789])
```

These IDs are synthetic. `target` is a nonzero JSON-safe integer; message IDs are
1–20 distinct positive JSON-safe integers in submission order. No title, username,
account selector, selection-attestation boolean, or arbitrary provider argument is
accepted. Successful verification returns `status="verified"`, `target_handle`,
current sanitized untrusted `target` metadata, UTC `expires_at`, and observed
identity semantics. Successful reads return `complete` or `partial` with nested
`messages` using the unchanged selected-message result contract. Inspect every
nested status and `coverage_complete`; missing does not prove permanent deletion.
Limits remain 20,000 text characters/message, 100,000/call and 30 seconds.

A handle contains 256 random bits and is bound to exact numeric chat/type, account,
client, broker generation and contract version. Its monotonic 300-second lifetime
is fixed when issued; reads never renew it. Each client has at most 16 live handles
and 4 active operations including issuance. Capacity returns `capacity` without
evicting live handles. The separate memory-only registry retains identity bindings
and expiry, with no message bodies, usernames, titles, hypotheses or persistent
aliases. Username reassignment cannot retarget a numeric handle.

Fresh account checks surround exact numeric hydration and content/sender work and
run before output. They are observed checks, not an atomic Telegram-wide snapshot.
Unknown, expired or foreign handles fail before content dispatch. Close, release,
broker restart or account/identity drift invalidates a handle; a midbatch failure
discards every row, including earlier successes. Terminal `invalid_handle`,
`invalid_target` (verification), `capacity`, `blocked` and `error` wrappers carry no
target, handle, expiry or nested content, and do not echo provider details. After
expiry or invalidation, verify again only when the exact target remains selected;
do not silently rediscover or resolve a username from a stale handle. Handles are
only for selected reads and do not authorize history, search, attachments or sends.
Disable `verified_targets` or restore matching accepted package/policy to roll back.


### Selected attachment pages and text continuation

Enable `attachment_pages` in trusted `runtime.toml`, restart the matching proxy and
broker, and allow `read_attachment_page` in the consumer. The original
`read_attachment` contract remains unchanged. Obtain a supported selected artifact
with `get_attachment`, use `create_local_artifact` within its 512KiB creation limit,
or upload a selected local file with the streaming helper below. Completed uploads
retain their filename and media metadata for the broker's lifetime.
The tool accepts an opaque artifact ID, never a filesystem path.

```python
page = read_attachment_page(artifact_id=artifact_id, pages=[6, 2], max_chars=20000,
                            render_pages=True)
# To continue, repeat the identical arguments and use only the returned cursor.
page = read_attachment_page(artifact_id=artifact_id, pages=[6, 2], max_chars=20000,
                            render_pages=True, cursor=page.next_cursor)
```

Select one to five distinct positive one-based PDF page numbers, in extraction
order. Omit `pages` for the initial five PDF pages or a supported non-PDF artifact.
A non-PDF cannot accept `pages`; invalid or out-of-range selections fail as a whole.
Each response contains at most 20,000 Unicode characters. `text_start`/`text_end`
are Unicode codepoint offsets in the fixed selected extraction, not byte or UTF-16
offsets. PDF pages and DOCX body paragraphs/table cells retain newline separators.
Append each successful `text` once, in cursor order. A fresh request may select the
same page again; duplicate pages within one request are invalid.

`has_more` and `next_cursor` describe remaining text in that exact selection.
`scope_complete` means the chosen supported text extraction has ended; it does not
claim full document understanding. For PDF, `scope.selected_pages`, `total_pages`
and `all_pages_selected` state page coverage independently. `text_coverage` is
selected PDF text, DOCX body text, UTF-8 text, or none for images. There is no OCR,
embedded-object extraction, complete DOCX layout/notes coverage, or execution.
No processed-byte counter is presented as document completeness. PDF/image previews
appear only on the first response; `previews_complete` reports their delivery
separately from text exhaustion. Images stay untrusted evidence.

Continuations expire after at most 300 seconds and no later than the artifact.
They are single-use and bound to the originating client, account, broker generation,
artifact bytes/hash, extraction version, metadata and all request settings.
Do not change settings or reuse a cursor, including after an uncertain response;
start an explicit fresh extraction if needed. No automatic retry is performed.
There are at most 16 live extractions and 4 active requests per client, including
pending reservations. Document text is not retained in continuation state.

Extraction is limited to 64MiB input, 1,000,000 selected text codepoints, and existing
PDF/DOCX/UTF-8/image formats. DOCX archives are limited to 500 members / 32MiB expanded
bytes. Parser work runs in a separate process with CPU, memory-allocation, output
and 15-second wall-time limits. A limit, damaged/password-protected document,
changed/expired artifact, or account/client failure returns an empty safe failure,
not a partial result labelled complete. Disable `attachment_pages` to roll back.

A fixed extraction permits at most 256 calls; if text still remains on the last
call, it returns `limit_reached` without content or a new cursor. Start an explicit
fresh extraction with a larger `max_chars` to reduce fragmentation. This bounds
retained token history as well as parser work; no automatic restart is performed.

### DOCX body text and safety limits

Both `read_attachment` and `read_attachment_page` inspect the complete DOCX
package before returning body paragraphs and table cells. They reject malformed
or ambiguous ZIP/XML metadata, DTD/entity declarations, declared macros, ActiveX,
OLE/embedded Office packages and external loading relationships. Ordinary external hyperlinks
remain inert: their displayed body text is retained, and their targets are never
fetched or returned as additional evidence. Headers, footnotes, comments, layout
and embedded objects do not gain extraction coverage.

Limits apply even to unused package parts: 500 ZIP members, 32 MiB declared and
measured expansion, compression ratio 200, 8 MiB per XML part, 200,000 aggregate
XML nodes, depth 64, and 32 MiB of XML text/attribute codepoints. A large legitimate
file can reach these limits. Each DOCX parse runs in an isolated process with a
15-second parent deadline and CPU, allocation, descriptor and output limits.
These OS allocation limits are not a guarantee of an absolute resident-memory cap.

The legacy tool keeps its bounded prefix and `partial` semantics. Page reading
keeps its existing codepoint offsets, continuation and 1,000,000-codepoint selected
text ceiling. Neither a successful prefix nor body-text completion proves complete
coverage of the original document. Rejected or timed-out parses return no text.

### CSV, TSV and literal text

Use the same `read_attachment_page` continuation for `.txt`, `.text`, `.md`,
`.csv`, `.tsv`, `.json`, `.xml`, `.yaml`, `.yml`, `.py`, `.js`, `.ts`, `.tsx`,
`.jsx`, `.html`, `.css`, `.sh`, `.sql`, `.log` and `.toml`. These extensions are
accepted by selected Telegram document transfer and local artifact creation.
Omit `pages`; `render_pages=False` avoids requesting previews for text.

Text is decoded as strict UTF-8, with CRLF and CR normalized to LF. Delimiters,
quotes, tabs, empty columns, formulas and markup remain literal source text.
CSV/TSV rows may cross response boundaries; append successful segments in offset
order before interpreting them. The reader does not parse CSV dialects, validate
JSON/XML/YAML, render Markdown/HTML or execute source code. `utf8_text` coverage
means this normalized source text ended, not that its syntax or meaning is valid.
Each response remains capped at 20,000 Unicode codepoints; extraction exceeding
1,000,000 codepoints or containing invalid UTF-8 fails without partial content.
The legacy `read_attachment` still returns a bounded prefix without continuation.

## Selected XLSX cells

`read_spreadsheet` is independently disabled until `spreadsheets` is included in
the trusted runtime capability list and the tool is allowed by the consumer.
Restart matching proxy and broker after changing the policy. The original
attachment tools keep their existing contracts.

First create a local XLSX artifact with `create_local_artifact` within its 512KiB
limit or use the streaming upload helper below. Then inspect its catalog:

```python
catalog = read_spreadsheet(artifact_id=artifact_id)
page = read_spreadsheet(artifact_id=artifact_id,
    selections=[{"sheet_index": 2, "range": "A1:C20"}], max_cells=200)
# Keep artifact_id, selections and max_cells identical for each continuation.
page = read_spreadsheet(artifact_id=artifact_id,
    selections=[{"sheet_index": 2, "range": "A1:C20"}], max_cells=200,
    cursor=page.next_cursor)
```

Catalog calls return sheet indices, names and `visible`, `hidden` or `veryHidden`
state, with no cell values. Hidden sheet content requires explicit selection.
Use one to five uppercase A1 rectangles, up to 10,000 addressed positions in total.
Whole-column and whole-sheet ranges are rejected. Results preserve selection order
then row-major order, including blank positions. Overlapping rectangles intentionally
repeat positions under their separate `selection_index`; exact duplicate selections
are rejected. Hidden rows and columns are not filtered.

Each page contains at most 200 cells and 20,000 codepoints across its cell string
fields. A page may be shorter when the string budget binds. A single cell too large
for that budget produces an empty `limit_reached` result; its content is never
silently truncated. `cell_start` and `cell_end` count flattened selected positions.
`scope_complete` means only those selected positions are complete, not the whole
workbook. Catalog completion means the catalog was read, not any cell content.

Values remain lexical strings: numbers are not rounded and number formats or date
styles are not converted. Shared and inline strings become text. Formula text and
`formula_kind`, `formula_ref`, `formula_shared_index` are data; shared/array formulas
are not translated or expanded. Cached formula values have **unknown freshness**.
Missing or empty numeric caches are `null`; no formula is calculated. Formatting,
comments, merged visual layout, charts, drawings and embedded objects are omitted.

Only the supported Transitional XLSX cell subset is read. Strict OOXML, macros,
external relationships, unsupported formula attributes and unsupported string
encodings/phonetic runs fail closed. There is no arbitrary Office-format promise.
The parser validates package relationships and XML even in unselected parts;
malformed unrelated content can therefore reject a workbook.

Input is capped at 64 MiB, ZIP expansion at 32 MiB, entries at 1,024, each XML part
at 8 MiB and compression ratio at 200. Aggregate XML nodes, text, depth, selected
content and worker time/memory are bounded. Large legitimate workbooks can reach
these limits. Cursors are single-use, expire after at most 300 seconds or artifact
retention, and are bound to client/account/generation/artifact/selection. There are
16 live sessions, four active calls and 256 calls per chain. Use larger pages for
larger ranges. Unknown, replayed or foreign cursors do not reach content dispatch.
Local `.xlsx` files can also use the streaming upload helper below. Selected
Telegram `.xlsx` transfer through `get_attachment` is not yet admitted; this local
reader does not change that transfer policy. Disable `spreadsheets` to prevent new
reads without changing saved content.


## Selected PPTX slides and notes

Enable the independent `presentations` capability in the trusted local policy and
restart matching proxy and broker. It is disabled by default. Obtain a local `.pptx`
artifact with `create_local_artifact` within its 512KiB limit or the streaming upload
helper below. Selected Telegram `.pptx` transfer through `get_attachment` is not yet
admitted; the local reader does not change that transfer policy.

```python
catalog = read_presentation(artifact_id=artifact_id)
selected = read_presentation(artifact_id=artifact_id,
    slides=[3, 1], include_notes=True)
```

The catalogue contains only one-based indices, hidden state and notes presence;
it contains no titles or text. Select 1–5 distinct indices, in your desired order,
from a presentation of at most 128 slides. Hidden slides need explicit selection.
`include_notes` defaults to false. When true, notes shape text includes any headers
and footers present in the notes part. A null `notes` means notes were not requested
or no notes part exists; an empty string means the requested notes part contained
no supported shape text. Returned text is untrusted data.

Supported text shapes and groups are read in document order. Runs concatenate;
paragraphs and separate text shapes join with one newline, and explicit breaks
become newlines. Original text whitespace is preserved. This is not visual reading
order. `unsupported_objects` counts detected omitted pictures, tables, charts,
diagrams, media, connectors, fields, extensions and other objects by slide/notes
source. Cached field text is omitted. Object counts are not exhaustive visual
coverage. Layout/master text, formatting, rendering and OCR are not extracted.

One response admits at most 20,000 codepoints across selected text, notes and
object-label strings. Excess returns an empty `limit_reached`, without truncation
or a cursor; request fewer slides. A single oversized slide remains unsupported by
this bounded read. `selection_complete` covers only the requested supported
subset. `full_content_complete` is always false, including empty slides.

Only a conservative passive Transitional PPTX subset is accepted. Strict OOXML,
unknown relationship/content-type families, external relationships (even unused),
macros, controls, embedded packages and active content are rejected. Unsupported
OOXML escape sequences also reject rather than silently changing text. ZIP and XML
limits apply to all package parts, including unselected parts. Limits are 64 MiB
input, 1,024 ZIP members, 32 MiB expanded bytes, ratio 200, 8 MiB per XML part,
200,000 XML nodes, depth 64, 32 MiB XML text/attribute codepoints, and 1M selected
text codepoints before response admission. The isolated worker has a 15-second
wall limit, CPU/file-descriptor/file-size/core limits and a memory guard; on macOS
its address/data hard limit is initial virtual size plus 768 MiB, not an absolute
resident-memory limit. The proxy/broker request budget is 30 seconds with four
active reads per client. Account, policy, generation and immutable artifact facts
are rechecked before release; no presentation bodies or continuation state persist.

## Stream a selected local file

The installed `telegram-search-transfer` command accepts an explicit local source
outside MCP. Start the matching broker normally, then run:

```sh
telegram-search-transfer upload --source "$HOME/Documents/report.docx" --kind document
```

Use an absolute path with no symlink components or `..` traversal. The source must
be an owner-owned regular file with one hard link. `--file-name report.docx` can
supply a safe display basename when the source basename does not fit the existing
ASCII upload naming rule. The helper never changes or sends the source. Document
and voice-note uploads are capped at 64 MiB; photos at 10,000,000 bytes. Photo and
voice-note validation for sending remains a separate preview step.

The helper hashes the pinned file descriptor, then streams ordered chunks of at
most 512 KiB, checking source stability, acknowledgements and the final size/hash.
Base64 exists only inside the local transport. Its single JSON receipt contains
the completed artifact ID, size and SHA-256; it does not print bytes or local paths.
Pass the artifact ID to a supported reader or an explicitly requested preview.
MCP tools still accept handles, never local source paths.

The current trusted `artifacts` capability and matching runtime contract are
required. The helper does not start or restart a broker. A failed, interrupted or
ambiguous append/finish stops without replay. To retry after inspecting the result,
start a new helper invocation from byte zero; there is no resume or idempotent
chunk retry. An unknown finish may already have created an unreported local
artifact, but cannot have sent a Telegram message. Incomplete uploads cannot be
finished; their memory state expires after 15 minutes or is lost on broker restart.
Orphan bytes are retired on a later upload start under the bounded cache policy.

Completed uploads register filename/MIME/media metadata before returning success.
Supported text, PDF, DOCX, image, XLSX and PPTX readers can use the artifact within
their separate limits and capabilities. Unsupported formats remain unsupported.
Metadata is memory-only: a broker restart can leave retained bytes without reader
metadata. Upload and artifact handles are local broker-wide handles, not private
grants between mutually hostile same-UID clients. Account registry support and
selected Telegram XLSX/PPTX transfer are not added by this helper.

## Save a selected artifact locally

Use the successful artifact ID from an upload or selected attachment to save its
exact original bytes. Supply the full ID and an explicit absolute destination:

```sh
telegram-search-transfer download --artifact-id ISSUED_ID --destination "$HOME/Documents/export.pdf"
```

The matching broker must already be running with the trusted `artifacts`
capability enabled. The destination parent must already exist, belong to the local
owner, and not be writable by group or others. Paths cannot contain symlinks,
`.` or `..` components. Any existing destination is rejected, including a symlink
or directory; there is no overwrite option. The artifact cache and its quarantine
are not valid destinations. A filename suffix does not convert the file format.

Download reads only the selected snapshot in the fixed private artifact cache.
It requires a canonical artifact ID, owner-only file, one hard link, matching size
and SHA-256, and an unexpired 12-hour cache lifetime. The cache ceiling is 256 MiB;
individual upload and reader limits still apply separately. Lookup is read-only:
it does not prune expired or corrupt cache entries or refresh their lifetime.
Valid retained bytes can be exported after an earlier broker restart even when
reader metadata has been lost. The ID remains a same-UID local handle, not a
per-client confidentiality grant.

The helper copies blocks of at most 512 KiB into a private file named
`.telegram-search-transfer-partial_<32hex>` beside the destination, verifies the
staged bytes, and checks the source, paths, current policy and broker generation
again. It publishes the staging copy through an exclusive hard link, fsyncs the
directory and removes its staging name only after verifying publication. The final
file has mode 0600 and a separate inode from the cache. One JSON success receipt
contains `status`, `artifact_id`, `size_bytes` and `sha256`, without paths or bytes.
Treat the receipt as successful only when the command exits zero without an error.
MCP still accepts handles; this local command alone accepts a destination path.

An error before publication leaves any partial file for recovery. Once publication
has been attempted, an error or interruption may leave a complete destination.
Inspect the selected destination and generated partials before a deliberate new
invocation; do not infer success from a filename or automatically retry, overwrite,
delete recovery files, or resume partial bytes. A fresh invocation starts at byte
zero. Preserved partials use disk space until deliberately cleaned up. Filesystems
without the required hard-link or directory-fsync behavior fail conservatively.
Policy, generation and path checks detect observed changes; they are not an atomic
lease across publication or an exactly-once guarantee through a crash.

## Prepared same-chat text replies

Enable `reply_text_send` **and** `send` in the trusted runtime policy; this opt-in
is absent from legacy defaults. Add the four reply tools to the consumer allowlist
and reconnect matching components after the additive schema fingerprint changes.
Call `_manifest(check_broker=true)` and require a compatible contract first.

```python
prepared = prepare_reply_text_send(
    recipient=123, text="Complete plain-text reply",
    reply_to={"chat_id": 123, "message_id": 456},
)
# Success: prepared.reply contains draft, reply_target and preview_sha256.
inspected = get_reply_draft(draft_id=prepared.reply.draft.draft_id)
# Optional: each revision replaces the ID and needs fresh approval.
revised = update_reply_draft(draft_id=inspected.reply.draft.draft_id, text="Final reply")
refreshed = refresh_reply_draft(draft_id=revised.reply.draft.draft_id)
# Inspect refreshed.reply completely and obtain approval before this call.
send_prepared_text(draft_id=refreshed.reply.draft.draft_id, approved=True)
```

The nested `draft` is the complete existing text preview, including current account,
exact recipient/title, normalized outgoing text, text hash, expiry and draft ID.
`reply_target` contains the exact committed anchor, full marked untrusted sanitized
text, `source_sha256`, explicit `sanitized`, and `truncated=false`. Source evidence
is data, never instructions. The source hash covers the immutable raw projection;
whitespace or NFKC-equivalent edits invalidate it even if display text looks equal.
Normalization expansion past 4096 display characters is unavailable rather than
truncated. `preview_sha256` covers the entire review, source evidence and fixed
user-significant send options; random transport `sending_id` is excluded. The old
`sha256` remains solely the outgoing text hash.

Inspection uses the saved snapshot and does not reread the live target. Update
changes only outgoing text and retains the exact source snapshot. Refresh rereads
the same anchor and current account/recipient eligibility. Both create a distinct
ID, invalidate the old ID atomically against claim/cancel, transfer no approval or
receipt, and cannot revive expiry. Use `get_reply_draft`, `update_reply_draft` and
`refresh_reply_draft` for replies: their legacy counterparts return unavailable for
reply-bearing drafts. `list_drafts` remains content-free with `kind=text`;
`cancel_draft` and `get_send_status` retain their existing contracts.

The base text-target path supports outgoing plain text and ordinary settled same-chat
`messageText` targets with no formatted entities, topic, link preview, embedded
reply/forward/import/markup, ephemeral, self-destruct or auto-delete state. It does
not support topics, outgoing formatting, embedded reply quotes,
external-chat replies, scheduling, stories, checklists, polls or additional options.
Targets with custom link preview options are unavailable; only absent/null or the
exact disabled no-URL option shape is supported. Unsupported/malformed targets
return a fixed unavailable response with no draft ID or preview.

After the local owner confirms the full account, anchor, target evidence and
source/review hashes, the claimed reply enters a separate provider method. Under
one local provider lock it freshly checks the account, allowed exact recipient,
complete source projection/hash and strict `getMessageProperties.can_be_replied`.
A final local policy/provider/epoch guard runs after those reads. Media and styled sources
also check it after observation registration and immediately before rawsend. Refusal before rawsend records `local_failed`
with no transport attempt. These TDLib reads use cached/offline evidence and are
not an atomic Telegram guarantee: a remote edit can still race them.

Provider observations must retain the exact same-chat reply metadata, outgoing
type/text and final committed ID. A dropped, changed or malformed anchor produces
`outcome_unknown` without a positive final ID; Telegram may already have sent an
ordinary message. Never automatically retry. Only exact late evidence may refine
the original attempt through `get_send_status`, within the existing 900-second
observation retention. Controlled transport tests establish technical behavior;
actual Telegram delivery and the owner dialog require their own live verification.


## Artifact replies

Enable both `reply_artifact_send` and `send` in the trusted runtime policy. The
new capability is absent from defaults; `reply_text_send` does not enable it.
Create or upload a server-issued artifact, then prepare the exact same-chat
plain-text target:

```python
prepared = prepare_reply_artifact_send(
    artifact_id=artifact_id, recipient=123, display_name="report.pdf",
    mime_type="application/pdf", caption="Here is the report", kind="document",
    reply_to={"chat_id": 123, "message_id": 55},
)
preview = get_reply_artifact_draft(draft_id=prepared.reply.draft.draft_id)
# Inspect the complete preview, then obtain explicit approval before:
result = send_prepared_artifact(draft_id=preview.reply.draft.draft_id, approved=True)
```

The `reply` envelope includes the complete outgoing `draft`, marked untrusted
`reply_target`, and `preview_sha256`. The digest uses a separate artifact-reply
domain and binds every draft field, source digest, target anchor, caption and
fixed provider option. Caption entities are empty; topic and quote are null.
Photo validation checks exact local bytes/MIME/name. Voice preparation converts
from a private verified source snapshot into a file named from the normalized source
basename stem plus `.ogg`, with MIME `audio/ogg`, preserving
the normalized source name/hash, conversion flag, duration and waveform. The
original input must be live at initial snapshot admission; an already prepared
live derivative can be sent after the input expires. Derivative expiry caps all
subsequent revisions. No source snapshot is exposed as a public artifact.

`update_reply_artifact_draft(draft_id, caption)` replaces only the caption and
preserves target evidence; empty caption is supported. `refresh_reply_artifact_draft`
rehydrates the same anchor and current account/recipient while verifying the same
artifact. Both return `status="revised"`, `previous_draft_id`, and a distinct new
immutable draft ID. Old IDs and approval never transfer; inspect and approve the
new preview. `list_drafts`, `cancel_draft` and `get_send_status` retain send-only
metadata/read/cancel access; use the appropriate inspection tool for a known
prepared handle. Ordinary `get_draft` and text `get_reply_draft` cannot inspect
artifact replies; the artifact lifecycle cannot inspect ordinary or text replies.

Explicit caller approval is followed by independent local owner confirmation of
all file/caption/voice and target facts. The provider revalidates account, title,
source and reply eligibility under its lock and checks policy/provider epoch and
the inherited request budget immediately before raw transport. There is one
attempt and no ordinary-send fallback. Definite local pretransport failures have
`local_failed` status evidence. Posttransport uncertainty retains staging and
never authorizes retry. `get_send_status` may resolve uncertainty from exact late
provider evidence without sending again.

Supported observations require exact caption and reply correlation plus bounded
typed document/photo/voice/file metadata; unsafe photo flags, sticker evidence,
voice duration/waveform/MIME differences, formatted captions, and nonnull voice
speech-recognition results fail closed. Nested vector/string/dimension bounds are
conservative product limits, not a claim to accept every valid TDLib response.
Returned file IDs can change between preliminary and final evidence; listened
state can change. Provider file metadata does not prove remote byte equality,
especially for transformed photos or inferred document MIME. Locally verified
bytes and correlated provider IDs establish only this bounded observation.

Other media, linked reply targets, formatted media captions, topic replies, embedded quotes, albums and
new outgoing options remain outside this slice. Synthetic tests and installed
process checks establish technical evidence; concrete approved live delivery and
the final consumer matrix remain unfinished product validation.


## Bounded media reply targets

Add `reply_media_targets` to the trusted runtime policy alongside `send` and the
outgoing workflow's `reply_text_send` or `reply_artifact_send`. It is absent from
legacy defaults. The same eight lifecycle tools accept ordinary settled document,
static photo and voice-note targets at the exact committed same-chat anchor.
Public argument/result schemas, contract 1 and IPC 2 are unchanged. Restart matching
components after a reviewed policy change; running components fail closed on drift.

Inspect the full marked JSON-escaped record in `reply_target.text`: kind, full
caption (including explicit empty `""`), and every projected media fact. Documents
include full filename, MIME, known positive size and identity SHA; photos include
every canonical variant's type, dimensions, known size and identity SHA; voice
notes include duration, MIME, known size and identity SHA. Each identity SHA256
binds exact provider `remote.unique_id` UTF8 bytes. It identifies provider media;
it does not prove remote file bytes or contents. Local paths/file IDs, transfer
progress, thumbnails, waveform and listening state are validated but excluded from
identity. Raw captions and metadata bind the source hash before display sanitation.
Equivalent-looking raw edits invalidate approval. Display is full and never
truncated: media records exceeding 4096 characters including the marker are unavailable.

Source bounds are conservative: plain caption at most 1024 Unicode characters with
empty entities; styled captions require both source opt-ins described below.
Document filename is at most 255 UTF8 bytes and MIME 127; 1–20 photo variants,
dimensions 1–16384 and at most 100 progressive entries; voice exactly audio/ogg,
duration 1–600 seconds and strict base64 waveform decoded to at most 100 bytes.
MP3/M4A or longer voice targets remain unsupported. Animated/spoiler/secret/sticker
photos, unknown size/identity, linked or embedded reply/forward/import/
markup, topics and ephemeral/deleting targets remain unavailable. Preparation
never downloads media or opens provider file paths.

Media opt-in remains required for inspection, outgoing update, refresh, owner
confirmation and sent/failed/unknown receipt replay. Refresh checks both the stored
and newly read source, including text-to-media and media-to-text transitions.
Send-only list/cancel/status retain their metadata semantics. Revalidation under
the provider lock compares the entire immutable source hash. False or raised final
guards discard unattempted observations and record `local_failed` before transport.
Posttransport uncertainty never permits an automatic retry; exact late evidence
can resolve the original attempt. Synthetic tests establish this bounded technical
slice; live provider delivery, actual owner dialog and other consumers remain unverified.


## Styled text reply targets

Enable `reply_formatted_targets` in the trusted runtime policy alongside `send`
and the outgoing workflow's `reply_text_send` or `reply_artifact_send`. It is off
by default. The same eight lifecycle tools accept settled same-chat `messageText`
sources with only Bold, Italic, Underline, Strikethrough, Spoiler, Code, Pre,
PreCode, BlockQuote and ExpandableBlockQuote entities. Outgoing text and captions
remain plain with entities=[]; source styling never changes the outgoing payload.
Public schemas, all 43 tools, contract 1 and IPC 2 are unchanged.

Inspect the complete `Formatted target:` JSON record in the marked
`reply_target.text`. It includes sanitized source text, every canonical style,
original UTF-16 offset/length, exact covered substring after sanitation, and
PreCode language when present. `offset_basis` explicitly means original source
UTF-16 code units: sanitized display text can have different spacing or characters
and must never be used to recalculate those coordinates. The source hash binds raw
text and raw spans before sanitation. JSON escaping delimits all text and language;
no style markup or hidden clickable target is interpreted.

Sources have 1–32 exact style entities, at most 4096 Unicode characters, whole
Unicode scalar boundaries, positive int32 lengths and nonnegative int32 offsets.
PreCode language has at most 64 characters/128 UTF8 bytes and no controls. Unknown
fields/types, duplicate identical entities, crossing spans and surrogate splits
refuse preparation. Ordinary styles may nest or be coextensive; Code/Pre/PreCode
must be disjoint from all other entities and block quote entities must be disjoint
from each other. Adjacent spans are allowed. The entire marked display is at most
4096 characters and is never truncated; expansion beyond that bound is unavailable.
This style-only projection excludes lexical types; the separate lexical text
opt-in below admits three provider labels; identity and null-format DateTime text
sources use their separate boundaries below. Links, custom emoji, media timestamps,
non-null DateTime formats remain unsupported; DateTime captions use private v10 below.

Opt-in remains required for preparation, stored inspection/update, both stored
and refreshed source types, owner confirmation, final guards and terminal receipt
replay. Gated media and styled-source terminal replay additionally requires the
original provider instance and observation epoch; restart or epoch change makes
replay unavailable. Pending expiry uses wall time; attempted replay expires at the
inclusive monotonic 900-second retention boundary. Plain-source behavior is
unchanged. Read-only status/list/cancel retain their existing send-only rules.
Range, language and quote-type changes invalidate the source digest and approval.
False or raised guards before transport discard unattempted observations and
record `local_failed`; an attempted send with uncertain evidence remains
`outcome_unknown` and must never be retried. Exact late evidence may reconcile the
original attempt. These bounds follow the pinned TDLib entity definitions; no
TDLib upgrade is required. Synthetic technical validation does not establish live
Telegram delivery, owner-dialog approval or broader consumer readiness.


## Styled media-caption reply targets

Enable both `reply_media_targets` and `reply_formatted_targets`, plus `send` and
the outgoing workflow's `reply_text_send` or `reply_artifact_send`. A nonempty
styled caption requires both source capabilities throughout preparation,
inspection/update, both sides of refresh, owner confirmation, final transport
guards and sent/failed/unknown receipt replay. Removing either source opt-in
makes the styled source unavailable. Empty and whitespace-only plain captions
retain the media-only path.

The complete marked `Media target:` JSON record retains every media fact and
replaces the caption string with `text`, `offset_basis` and `entities`. Each
entity includes its style, original scalar-aligned UTF-16 offset/length, sanitized
covered text and PreCode language when present. The same ten styles, 32-span and
overlap rules apply; styled captions must be nonblank and at most 1024 Unicode
characters. Raw caption/spans and media facts bind the source hash before display
sanitation. The entire marked record is at most 4096 characters and never
truncated. All existing document, static-photo and OGG voice restrictions remain
in force, and preparation downloads no source attachment. Outgoing entities remain
empty. Public schemas, 43 tools, contract 1 and IPC 2 are unchanged. Synthetic
checks establish technical behavior; live Telegram delivery and owner-dialog
approval remain unverified.


## Lexical text reply targets

Enable both default-off `reply_formatted_targets` and `reply_lexical_targets`,
alongside `send` and the outgoing workflow's `reply_text_send` or
`reply_artifact_send`. Neither source opt-in alone admits these targets. Private
v5 requires a text source with at least one fieldless Hashtag, Cashtag or BotCommand
entity, optionally mixed with the ten supported source styles. Supported lexical
media captions use the separate v6 projection and triple authority below.
Outgoing text and captions still have `entities=[]`.

Inspect the full marked `Formatted target:` JSON and raw source digest. Labels
`hashtag`, `cashtag` and `bot_command` accompany exact covered source spans using
original UTF-16 offsets and lengths. Labels are provider assertions, not a
separate recognition of Telegram syntax; they never instruct command execution,
resolution, navigation or account selection. Display sanitation can change text
without changing those original coordinates, and all displayed strings contribute
to `sanitized`. Raw text, spans, kinds and stable shell facts bind the digest;
entity permutation and irrelevant volatile provider facts do not.

The existing 1–32-span, whole-scalar UTF-16, closed-shape, int32, nonblank raw
4096-character, canonical 65536-UTF8-byte and complete marker-inclusive
4096-character display bounds apply with no truncation. Lexical pairs and
lexical/quote overlaps are refused; simple styles may contain, be contained by or
coincide with lexical spans. Code/Pre/PreCode remain disjoint from all entities.
Both source capabilities are required at preparation, inspection/update, both
sides of refresh, owner confirmation, final transport guards and terminal replay.
Refresh or source edits require fresh approval; uncertain attempts are never
resent. Topics, links and custom emoji remain unsupported; identity text uses the
separate v7 boundary and null-format DateTime text uses v9 below. V1–v4
canonical/display bytes and public schemas stay unchanged. Disable
`reply_lexical_targets` to roll back. Synthetic technical evidence does not establish
live delivery or actual owner-dialog approval. The shipped Codex skill's source
and caption guidance remains conservative and awaits a separate F18 update.


## Lexical media reply targets

Enable all three default-off source capabilities `reply_media_targets`,
`reply_formatted_targets` and `reply_lexical_targets`, alongside `send` and the
selected `reply_text_send` or `reply_artifact_send` workflow. Private v6 admits
lexical captions only on the existing validated document, static-photo and OGG
voice-note sources. It requires at least one exact fieldless Hashtag, Cashtag or
BotCommand entity, optionally mixed with the ten supported styles. Empty or
styles-only captions use their existing v2/v4 projections; text sources keep v5.
Default media parser/render modes remain style-only.

Inspect the complete marked `Media target:` JSON: sanitized caption, every span's
label, original UTF-16 offset/length and covered text, plus stable media identity
and metadata. Labels are untrusted provider assertions. They cause no command
execution, identity resolution, navigation, attachment download or path opening.
Sanitation is explicitly flagged and never changes raw source coordinates or
digest-bound facts. The caption bound is 1024 original Unicode scalars with 1–32
entities. Canonical source evidence caps at 65536 characters and UTF8 bytes before
parsing; the complete marker-inclusive rendered evidence caps at 4096 characters.
Overflow refuses rather than truncates. Existing entity overlap exclusions,
closed media shapes and identity requirements remain in force.

All three source capabilities are checked at preparation, inspection/revision,
both sides of refresh, before and after owner confirmation, after fresh provider
facts and after observation registration before raw transport, and on terminal
replay. Singular authority accessors refuse these conjunctive sources. Owner,
account, TTL and original provider/epoch boundaries remain in force. Outgoing
text/caption entities stay empty; uncertain attempts are reconciled only by exact
late evidence and never resent. V1–v5 canonical bytes, displays, authority and
public schemas are preserved. Disable `reply_lexical_targets` or revert this local
batch to roll back. This is synthetic technical delivery evidence, not verified
live Telegram delivery or actual owner confirmation. The shipped consumer skill
remains conservative F18 debt and is unchanged.


## Identity text reply targets

Enable both default-off `reply_formatted_targets` and `reply_identity_targets`
in the trusted runtime policy, alongside `send` and the selected `reply_text_send`
or `reply_artifact_send` workflow. Private v7 admits only `messageText` sources
containing provider `Mention` or `MentionName` entities, optionally mixed with
supported styles. Any Hashtag, Cashtag or BotCommand additionally requires
`reply_lexical_targets`. Identity media captions use the separate v8 boundary
below; ordinary, styled and lexical text/media sources retain v1–v6.

Inspect the complete marked `Formatted target:` JSON before approval. Each span
contains its untrusted provider label, original UTF-16 offset and length, and
covered text. `MentionName` also shows the provider-declared `user_id`; it must
be a strict positive integer below 2**53. `Mention` carries no declared user ID.
Visible text does not establish identity. Parsing and rendering never resolve
users, follow links or select recipients. The original source text and declared
ID bind the immutable source digest before sanitation. Changed IDs require a
fresh source snapshot and approval, even when the visible text is unchanged.

The parser accepts exact closed entity shapes, 1–32 entities and at most 4096
original Unicode scalars. Coordinates must be nonboolean int32 values aligned
with the original UTF-16 scalar boundaries. Identity spans cannot overlap each
other, lexical spans, code/pre or quotes. Nested emphasis follows the existing
style rules; duplicates and crossing spans refuse. Source projections cap at
65536 characters and UTF8 bytes, and complete marker-inclusive rendered evidence
caps at 4096 characters. Escaping and sanitation are explicitly reflected in the
preview; overflow refuses before registry mutation rather than truncating.

All required source capabilities are checked during preparation, pending
inspection/revision, both sides of refresh, before/after owner confirmation,
after fresh provider checks, after observation registration before raw transport,
and on terminal replay. Source ID edits observed during provider revalidation
refuse the send. Outgoing text and captions remain plain with empty entity lists;
the same original recipient and anchor are used. Unknown outcomes never authorize
resending. Disable `reply_identity_targets` or revert this local batch to roll
back. Contract 1, IPC 2 and all 43 public schemas/annotations are preserved.
Synthetic technical checks do not establish live Telegram delivery or actual
owner confirmation. The shipped consumer skill remains unchanged F18 debt.


## Identity media reply targets

Enable all three default-off source capabilities `reply_media_targets`,
`reply_formatted_targets` and `reply_identity_targets` in the trusted runtime
policy, alongside `send` and the selected `reply_text_send` or
`reply_artifact_send` workflow. Private v8 admits captions containing at least one
provider Mention or MentionName entity on existing validated document,
static-photo and OGG-voice-note sources. Hashtag, Cashtag or BotCommand captions
additionally require `reply_lexical_targets` exactly when a lexical entity is
present. Empty, styles-only and lexical-only media retain v2/v4/v6; identity text
retains v7. Old media versions cannot authorize identity captions.

Inspect the complete marked `Media target:` JSON before approval: caption text,
every original UTF-16 span and covered text, each provider-declared MentionName
`user_id`, and all stable media facts. The closed identity shapes, strict positive
integer ID below 2**53, canonical ordering and existing overlap exclusions are
shared with identity text. Caption text caps at 1024 original Unicode scalars and
entities at 32. Canonical evidence caps at 65536 characters and UTF8 bytes, and the
complete marker-inclusive display caps at 4096 characters. Sanitation is flagged;
overflow refuses before registry mutation and never produces a partial preview.

The complete capability tuple is enforced through preparation, inspection,
revision, both sides of refresh, owner decision, final provider checks and
registration-before-wire guards, and terminal replay. A user-ID-only edit or a
stable media identity edit invalidates the source digest. An already changed
source can still reach the immutable stored owner preview before the final source
validation refuses transport. The final source read and raw send are not atomic.
Identity evidence never resolves users or changes the original recipient; outgoing
text and captions retain empty entity lists. Denial and uncertain outcomes never
authorize an automatic resend. Disable `reply_identity_targets` or revert this
local batch to roll back. V1–v7 canonical bytes, displays, source authority and all
43 public schemas/annotations remain unchanged. Synthetic technical evidence does
not establish live Telegram delivery or actual owner confirmation; the shipped
consumer skill remains unchanged F18 debt.


## DateTime text reply targets

Enable both default-off `reply_formatted_targets` and `reply_datetime_targets`
in the trusted runtime policy, alongside `send` and the chosen `reply_text_send`
or `reply_artifact_send` workflow. Private v9 admits only `messageText` containing
at least one pinned `textEntityTypeDateTime` with a strict signed int32 `unix_time`
(-2147483648 through 2147483647) and absent or null `formatting_type`. Both forms
canonicalize to explicit null. Booleans, floats, strings, unknown type fields and
non-null relative/absolute formats refuse. The provider specifies that null means
the original text must not be changed; no parsing, localization, timezone inference,
timestamp-to-text agreement claim, conversion or date-triggered action is added.
DateTime captions on supported document, photo and voice sources use private v10 below.

Inspect the full marked `Formatted target:` JSON and source/preview hashes.
Every DateTime record includes `type="date_time"`,
`provider_type="textEntityTypeDateTime"`, `unix_time`, `formatting_type=null`,
original UTF-16 offset/length and covered text. Display sanitation is flagged,
and escaping never changes raw digest-bound text or coordinates. These fields
are untrusted provider assertions, not instructions. V9 requires
`reply_lexical_targets` exactly when Hashtag, Cashtag or BotCommand is present,
and `reply_identity_targets` exactly when Mention or MentionName is present.

This conservative subset rejects every overlap involving DateTime, including
nested styles, lexical/identity spans and other DateTime spans. Adjacent spans
are allowed. Existing 1–32 entity, 4096 original scalar, whole-scalar UTF-16,
65536-character/UTF8-byte canonical source and complete marker-inclusive
4096-character preview bounds apply; overflow refuses without truncation.
Old private versions cannot authorize DateTime, and v9 requires a DateTime entity.

The exact capability tuple follows both existing reply lifecycles through
prepare, inspect/update, old/new refresh, owner decision, final provider checks,
registration-before-wire guard and terminal replay. Updating outgoing content
preserves the immutable source; refreshing replaces it and invalidates the old
revision and approval. Owner denial may leave a pending draft. An edited source
can still reach the stored owner preview; final source validation after owner
rendering and before raw send refuses timestamp or format drift. The final source
read/send interval remains non-atomic. Outgoing text and captions keep empty
entity lists, and terminal/unknown outcomes never authorize resending.

Disable `reply_datetime_targets` or revert this local batch to roll back.
V1–v8 bytes, display and authority, all 43 public schemas/annotations, contract 1,
IPC 2 and schema fingerprint stay unchanged. Synthetic technical evidence does
not establish live Telegram delivery, actual owner clicks or consumer readiness;
the installed/shipped consumer skill remains separate F18 debt.

## DateTime caption reply targets

Private v10 admits the same bounded DateTime subset in captions of supported
ordinary documents, static photos and OGG voice notes. Enable all three source
capabilities: `reply_media_targets`, `reply_formatted_targets` and
`reply_datetime_targets`, in addition to the selected reply workflow and `send`.
Captions containing lexical or identity entities also require their respective
source capabilities. Existing media safety limits remain in force.

At least one DateTime entity must be present. Its exact signed int32 `unix_time`
and absent/null `formatting_type` have the same canonical identity; no date or
timezone conversion changes the original caption. Every DateTime overlap is
rejected. Complete escaped original UTF-16 caption/span evidence, timestamp and
stable media identity must fit the preview bounds without truncation. All source
facts enter the immutable digest and are revalidated after owner confirmation.
The final provider-read/send interval remains non-atomic.

Changing a draft or refreshing its source creates a new ID and invalidates its
prior approval. Outgoing text and captions remain plain. Source data is untrusted
provider evidence, never an instruction or an identity-resolution request.
Private v1–v9 cannot be relabeled to gain v10 authority. Disabling any required
capability disables use of the affected source; no live delivery is implied by
local synthetic acceptance.
