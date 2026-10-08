# TelegramSearch security model

## Authority boundary

The dedicated TDLib session holds the Telegram account's authority. Exactly one launchd-managed broker owns that session and the sole `TDLibClient`; per-task STDIO MCP proxies never load TDLib. TelegramSearch narrows authority at three layers: MCP exposes the tools listed in the technical reference; the private broker protocol accepts only the matching fixed operations plus `check`, `health`, and `release_client`; and the TDLib client rejects request types outside its fixed allowlist before transport:

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
getSupergroup
getForumTopics
getForumTopic
getForumTopicHistory
searchMessages
searchChatMessages
getMessage
getMessageProperties (closed exact-chat reply eligibility read)
getChatHistory
getMessageLink
getUser
downloadFile
getFile
cancelDownloadFile
sendMessage (fixed text, document, photo, or voice-note shape, only after explicit draft approval)
```

`createPrivateChat` has one narrowly approved use: after `getMe` identifies the authorized account, Saved Messages resolution calls it with that same user ID and verifies the returned private chat. `loadChats` advances only a fixed Main or Archive catalog lane. `searchMessages` is reachable only through the typed discovery wrapper and the exact request shape below. None of these methods is a general bridge to caller-chosen TDLib operations.

The broker socket lives in an owner-only directory with mode `0700`; the socket and owner lock use mode `0600`. The broker also verifies the kernel-reported peer UID through Darwin `getpeereid`. A nonblocking owner lock prevents a second broker. Only the lock holder may replace a same-owner, non-symlink Unix socket after proving that it is stale. Same-UID processes are inside this local filesystem trust boundary. IPC version 2 requires a same-connection contract handshake before every operation; IPC version 1 has no execution compatibility. The handshake compares loaded package/contract/finalized schema identity and effective trusted config. The next request binds the process generation; config freshness and capability checks occur before dispatch. Handshake-only diagnostics do not initialize TDLib. Requests use versioned length-prefixed strict JSON, a fixed operation allowlist, public-model validation on both sides, 1 MiB request and 8 MiB response limits, random process-lifetime client IDs, random request IDs, a 250-millisecond per-read bound, and monotonic deadlines capped at 570 seconds.

The broker fails closed unless the dedicated session reaches `authorizationStateReady`. It never accepts API credentials, login codes, passwords, session paths, index paths, account selectors, wildcards, caller-controlled provider limits/filters/dates/functions, or arbitrary TDLib requests through MCP or IPC. `api_id` and `api_hash` are read only from the fixed macOS Keychain service by the broker and are never logged or included in an error.

## Pending draft ownership, revisions and cancellation

The broker captures a validated native account ID and the handshake-bound proxy
client ID when preparing a draft. The same immutable owner pair is checked for
list/get/update/refresh/cancel and existing send access, including terminal receipt replay.
Public tool arguments cannot select either owner field. Account identity comes
from the authorized TDLib session; client identity is an opaque process-lifetime
proxy ID inside the same-UID socket boundary. This isolates ordinary consumers,
not hostile programs running as the same OS user that can spoof a known client
ID. It does not establish independent multi-account readiness.

List exposes at most 50 pending summaries without content, captions or filenames.
Continuation anchors must be unexpired records belonging to the same owner;
foreign and unknown anchors fail identically. Pagination is a live observation.
Get exposes the full saved preview only for an unexpired pending own draft, with
no cache/provider filesystem path or proxy ID. User-supplied content and display
metadata remain literal preview data. Listing does not revalidate artifact bytes;
get/send reject selected artifacts that no longer match. These reads do not
grant approval or prove that the recipient still has the same identity.

Cancellation and claiming share the registry lock. A cancellation that wins
changes pending to cancelled; a claim that wins makes cancellation unavailable.
Cancellation checks account authorization, but does not delete artifacts, send or
delete Telegram messages, alter terminal receipts,
or roll back provider effects. Own repeated cancellation is idempotent until
expiry. Unknown, foreign, expired and non-cancellable drafts return no preview
or receipt. A generic unavailable response cannot establish whether a prior
attempt succeeded.

After local owner confirmation, the broker freshly checks account and effective
policy before claiming. These observations are not an atomic account/provider
lease. Each draft ID identifies one immutable revision. Update changes only text
or caption; refresh preserves content. Both re-resolve the same recipient and
check the account/policy again before committing a replacement under the same
registry lock used by claim/cancel. The old pending ID becomes superseded; an
already-open approval for that ID cannot claim the new content. A claim that wins
first makes revision unavailable. Receipts never transfer between revisions.
Wrong-kind edits, capacity exhaustion and ID collisions preserve the old draft.
Missing/replaced artifact bytes invalidate it. New revisions retain the original
artifact deadline privately, so changing cache mtime cannot extend their lifetime.

Trusted `max_draft_ttl_seconds` is a strict integer in 1..86400, default900, included
in the configuration fingerprint and drift check. Initial drafts and each explicit
live renewal expire within this maximum and the original/current artifact expiry.
Expired drafts cannot revive; no operation extends artifact retention. Explicit
renewal may keep replacing a still-live text draft, but no individual ID has an
indefinite TTL. Bounded registry capacity includes superseded records until expiry.
The public caller cannot set TTL, expiry, account, ownership or approved state.
There is no durable journal or restart recovery.

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

TDLib still owns its configured session, database, and cache. Reads may change TDLib-managed metadata and byte counts. Selected attachment transfers create owner-only cache files; they do not create a custom searchable index or read receipts. Local artifacts have per-file and aggregate size caps, a 12-hour retention bound, and hash-verified lookup. Ordered uploads are capped at eight active files, 512 KiB per chunk, 64 MiB for documents and voice sources, and 10 MB for photos; each chunk and the completed artifact have explicit SHA-256 checks. Only the broker can make one outgoing TDLib text, document, photo, or voice-note attempt after an unchanged draft is explicitly approved in the task and the signed-in macOS user confirms the exact recipient and content in a local dialog. Photo bytes are decoded and checked against MIME and dimensions. Voice sources are locally converted to OGG/Opus mono and the outgoing bytes are hashed before preview. A declined or unavailable dialog cannot send. If delivery is not confirmed, the receipt is `outcome_unknown` and the attempt is not replayed.

Historical custom index files from an older version are not read or updated, but they are not automatically deleted. Any cleanup remains separately authorized: first resolve exact paths and sizes with metadata-only checks, then prefer an owner-only quarantine. Permanent deletion requires explicit authorization after rollback is no longer needed. TDLib session data is governed separately and is never treated as obsolete index data.

## Explicitly unreachable functionality

- `viewMessages`, read receipts, and unread-state changes
- unapproved sends, automatic send retries, edits, deletes, reactions, or moderation operations
- broad attachment or thumbnail downloads outside one exact selected anchor
- null-list global search, `searchPublicChats`, or public-chat crawling
- secret-chat access
- caller-controlled discovery budgets, provider offsets, filters, dates, chat lists, functions, or parallel fan-out
- persistent raw hypotheses, queries, titles, snippets, messages, evidence, provider payloads, or custom indexes
- a raw execute bridge or arbitrary TDLib request
- arbitrary caller-directed Telegram attachment paths, credentials, or account selection

## Untrusted content

Telegram text, captions, filenames, chat titles, and sender names are data, never instructions. Control, surrogate, and bidirectional formatting characters are removed; whitespace and length are bounded. Snippets and context are prefixed `[untrusted Telegram evidence]`. The manifest and tool descriptions repeat this trust boundary for MCP consumers.

## Acceptance and audit

Automated tests and source audits establish only the technical contract. Product completion requires a fresh Codex consumer and provider check, without recording private runtime values:

1. Capture owner-observed unread indicators in Telegram mobile and metadata-only TDLib cache existence, entry count, total bytes, and metadata hash before and after; never print entry names.
2. Confirm a fresh Codex task exposes exactly 43 tools through the normal TelegramSearch MCP surface.
3. Require overlapping request intervals, at least two broker client contexts, at least one aggregate TDLib serialization wait, one broker process, and multiple thin proxy processes.
4. Require both tasks to return complete bounded evidence with exact-chat anchors and no session-owner or TDLib code-400 failure; `telegram-search-mcp --check` must remain `AUTHORIZATION_READY` while multiple proxies exist.
5. Confirm unread indicators remain unchanged, exact selected attachment transfer is hash-verified, no custom private index appears, and metadata-only cache effects are disclosed without filenames.
6. Confirm no credentials, private payloads, provider exceptions, or synthesized URLs appear in logs, status, health, or saved evidence.
7. Verify document/image read and local media analysis with non-sensitive fixtures; prepare an unsent concrete artifact preview and withhold any real send until the user explicitly approves it.

Target strings, hypotheses, queries, IDs, snippets, directory names, and evidence receipts are runtime-private and must not be copied into repository files or logs. If a side-effect or coverage check cannot be confirmed, mark it unverified and do not claim product approval.

The `--check` path uses the broker to verify TDLib authorization without reading messages or creating another session owner. Automated and subprocess tests use synthetic evidence and do not substitute for the live owner-observed workflow.

## Rollback

Before an upgrade, save the previous installed runtime, owner-only plist, consumer config, and installed skill in a private rollback directory outside the source tree. Boot out only the validated `com.<local-home-name>.telegram-search-mcp.broker` LaunchAgent. Restore those saved files, then bootstrap the restored plist and check the prior proxy. This leaves the original checkout, its `.venv`, TDLib session, credentials, historical data, and user files intact. Restart clears in-memory drafts, incomplete uploads, discovery cursors, and the broker-lifetime numeric catalog cache; uncertain sends are never replayed.

Do not delete historical custom-index or TDLib session data as part of rollback. Historical index cleanup remains separately authorized and recoverable; session revocation is an explicit owner action if authority must be withdrawn.

## Optional local media prerequisites

Media analysis uses `/opt/homebrew/bin/ffmpeg`, `/opt/homebrew/bin/ffprobe`, and `/opt/homebrew/bin/whisper-cli`. Its default multilingual model is `~/Library/Application Support/TelegramSearchMCP/models/ggml-small.bin`. Verify the model against its distributor’s checksum during trusted provisioning. The runtime checks that it is a nonempty, owner-owned regular file; it does not recheck a model digest on each call. Provision binaries and model only through a trusted local setup; do not place private media or model downloads in the public repository. A missing dependency does not enable a cloud fallback.

## Selected message reads

`read_messages` requires its own trusted opt-in capability at both the proxy and broker; missing policy preserves the three legacy capabilities only. The strict request accepts 1–20 unique committed message anchors and no paths/provider arguments. Each chat and message is rehydrated and exact IDs checked before returning content. Sender hydration separately verifies identity. A wrong-chat response is discarded. The reader performs only bounded authorization, getChat, getMessage and getUser requests, with a 30-second aggregate deadline; it never calls viewMessages, follows reply references or writes a private index. Text and names remain untrusted and include explicit sanitation/truncation flags. Per-anchor unavailable, unsupported, malformed and budget outcomes prevent complete coverage. Provider error strings are not returned.

## History cursor isolation

`read_history` is a separate trusted opt-in at both boundaries. Numeric exact targets, explicit latest/date-interval modes and strict half-open date bounds prevent accidental wildcard/export scopes. The first observed upper ID and date limit are frozen; hydration rechecks chat, message and dates. Provider pages must have valid exact-chat identities and decreasing committed IDs. Continuation reuses the last observed ID with equality discarded, without arithmetic predecessor assumptions. Short, empty, old-dated and repeated pages never certify full history coverage.

Each broker-leased client owns at most four history states with a fixed five-minute TTL. States retain only bounded IDs/dates, counters and scope bindings, not text. Random single-use tokens bind account identity, client, broker process, contract and original request. An atomic claim consumes the token before provider access; concurrent replay is rejected. Active requests reserve capacity. Closing a service discards its states, including services that share the broker's TDLib owner. Account identity is reverified per page and rejects booleans or out-of-range IDs. There is no durable cursor journal or automatic retry after an uncertain response.

The aggregate deadline and scan/text/request caps apply even when a date range has few matches. A response's page completeness is separate from provider coverage, which stays unverified. Memory limits apply per client and the existing broker client capacity bounds their aggregate. No history path calls viewMessages, downloads media or persists a custom transcript/index.

## Reply-chain isolation

`read_reply_chain` is an independent trusted opt-in. Its exact committed root and
1–10 node bound are validated before provider creation. The reader verifies chat
and message identities at every hop, follows only hydrated same-chat message
references, detects cycles and never uses embedded reply content. Story/foreign/
unknown references stop. The proxy validates the bound, root, unique exact-chat
nodes, adjacency, aggregate text size and completion consistency before exposing
results. No new provider method is allowed. The shared deadline covers readiness,
chat resolution, nodes and sender identity; no custom transcript is retained.


## Forum topic boundary

`list_topics` is independently disabled by default. Strict numeric chat IDs and
limits are checked at the MCP and broker boundaries before provider creation.
Only `getSupergroup`, `getForumTopics` and `getForumTopic` extend the provider
allowlist. The capability check rehydrates exact chat/supergroup or bot identity;
malformed capability metadata never becomes verified support. Listing always uses
an empty query and the exact provider offset triple. Every new topic is separately
hydrated and its exact chat and positive int32 forum ID checked before projection.
No embedded message, draft, notification or unread metadata crosses this boundary.

Each client has at most four active/reserved scopes, a fixed five-minute TTL,
ten page attempts and 200 observed candidates. A native page may over-return its
requested limit; the local 200-row guard is not a provider guarantee. A page over
the remaining candidate budget ends the scope before identity hydration, with
no truncated-page resume. Accepted extra IDs are drained before another native
page, with at most the requested number of hydrated outcomes per call. State
retains numeric pending/seen identities, offsets, counts, fixed stop reasons and
bindings only. No topic object or content is retained. Counters stay unchanged
during buffer drains; provider caps forbid new pages but permit draining IDs
already observed within budget. Atomic consumption rejects concurrent replay;
release and shutdown discard state, including replies arriving after closure.
The proxy separately validates scope binding, disjoint IDs, counters and token
progress, including cumulative emitted IDs bounded by observed candidates and
no candidate increment without a native-page increment. Readiness, account and
forum capability are rechecked on buffered calls. The 30-second provider budget covers readiness, capability resolution,
page reads and hydration. No retry or partial-page restart occurs after failure.
Provider totals and empty/short pages are not completeness proofs. Metadata
sanitation/truncation and every unavailable topic are explicit; names remain
untrusted. No new read-receipt, media-download or mutation method is permitted.


## Exact forum history boundary

`read_topic_history` has an independent trusted opt-in checked at server and
broker boundaries. Strict numeric target plus `ForumTopicReference` accepts only
int32 forum IDs; caller paths, raw provider options, thread IDs, and other topic
families are excluded. Latest/interval dates retain the strict half-open UTC
contract. Current forum capability and exact topic metadata are rechecked on
every call. Missing topics stop without bodies or deletion claims; closed or
hidden topic flags do not prohibit an otherwise authorized read.

Native history uses only `getForumTopicHistory` with exact chat/forum IDs, offset
zero, and at most 20 requested rows. There is no `only_local`, `viewMessages`, or
fallback history request. The native envelope is locally capped at 200 rows and
validated whole before body hydration. Each observed and freshly hydrated row
must have strict committed message ID, exact chat, valid date, and modern
`messageTopicForum` with the exact forum ID. Mismatched or malformed content is
never projected. Dates are rechecked after hydration without assuming date
monotonicity. Unsupported/unavailable anchors remain explicit safe outcomes.
Provider `total_count` does not establish completeness; provider error strings
are not returned.

Atomic scopes are single-use and bind client, account, broker generation,
contract, and request; four including active calls, fixed TTL 300 seconds.
State stores numeric pending IDs/dates and counters only. Up to 200 observations,
ten native attempts/scope, 100 processed rows, 30 seconds, and 100,000 projected
text characters/call are permitted. Overlap/newer observations count as scanned
and later processed even when skipped. Buffers drain before fresh native fetch;
provider/candidate caps permit buffered progress only. Expiry, replay, close,
account switch, time/auth/provider/integrity failure discard continuation.
Proxy validation independently checks frozen topic/date/head/expiry scope,
exact metadata/body membership, descending disjoint IDs across the cursor chain,
monotone counters, at most 100 processed delta, emitted outcomes no greater than
processed delta, no scans without native attempts, no new attempts after prior
caps, and no repeated cursor. Text/names remain untrusted. `scope_complete` is
strictly false and `has_more` unknown; the operation cannot certify topic history
or infer deletion, and stores no private transcript.


## F5/F6 exact lexical search boundary

`search_messages` has independent trusted opt-in checks at both MCP and broker
boundaries. It accepts exact numeric targets, bounded strict positive text/caption
query input, explicit latest/interval mode, and aware half-open dates. Optional
strict typed sender, direction and forum predicates are combined with AND; their
absence preserves F5 search behavior. It adds no global search, raw request input,
read receipt or runtime install.
The existing native `searchChatMessages` method and request allowlist stay pinned.

State retains the frozen caller scope, numeric observations, counters, provider
cursor and SHA256 of raw pre-truncation text/caption, content kind and edit date;
it retains no Telegram bodies or sender names. Raw evidence hashing is capped at
65,536 characters. Each current body is hydrated and compared before the standard
selected-message projection; changes disclose no body, unavailable evidence never
proves deletion, and string-query provider lexical membership is not tested as a local substring.

Cursors are opaque, single-use, bound to client/account/broker/contract and exact
arguments, with four active scopes and fixed nonrefreshing 300-second expiry.
Proxy checks frozen scope, current request, descending anchors, counter deltas,
output bounds and both wall/monotonic expiry on continuing and terminal scoped
responses. Forged broker evidence becomes safe `broker_unavailable` without content.
Caps are 200 observed native rows and ten search attempts per scope; 100 processed
observations, 30 seconds and 100,000 projected text characters per call. Native
returned offsets are authoritative, including empty advancing pages; repeated or
nondecreasing offsets terminate without refetch loops. No terminal offset, short
page or count establishes exhaustive recall: `scope_complete=false` and unknown
`has_more` preserve that uncertainty.


F6 scope additionally binds nullable typed sender/direction/topic. Observation
buffers retain only the existing numeric ID/date, bounded lexical digest and one
predicate-match bit, never sender/topic names or bodies. The entire native page's
identity, order, dates and requested filter metadata validate before any candidate
hydration/projection. Required malformed predicate metadata fails closed even
when another predicate does not match. Native filtering is only an optimization;
pinned TDLib clears broadcast self-sender restriction and General native thread
restriction, and filter combinations have undocumented support limits. Current
raw typed checks enforce exact membership locally with no sender-based direction
inference or lexical substring substitute. Valid ordinary/null and other typed
topics are nonmatching, never General.

Every topic call resolves the exact forum and gets the current exact topic with
F4 metadata validation. Unavailable, nonforum or invalid evidence terminates,
without whole-chat or alternative-topic fallback. A matching-to-nonmatching race
returns only `evidence_changed`; a neither-matching row is omitted. Current
matching evidence always requires the unchanged lexical digest. Hydration failure
returns `not_found` only if the observed predicates matched. Every processed typed
candidate is hydrated, including duplicate observations; duplicates cannot be
projected twice. Response validation and proxy scope/body checks separately
reject forged predicate evidence. All F5 limits, TTL, capability and uncertainty
claims remain, including empty partial pages that consume candidate budgets.


Boolean object queries are closed flat positive `all`/`any` plus `none` arrays,
not a native query language. Their local NFKC/casefold/whitespace substring rules
are separate from the unchanged string-query lexical rules. A maximum of four
native seed branches share existing scope/call/TTL limits; negatives never seed
requests. Branches retain only seed scope, offsets, counters and bounded numeric
observations/digests/membership bits. Raw bodies remain call-local. Every branch
is primed/refilled before descending merge and duplicate anchors are rehydrated
without duplicate output. Exclusions use the full bounded current raw text,
including content after the displayed prefix. Incompatible observed/current
hashes suppress bodies. Unsupported raw evidence cannot establish a local match.

Response schema and proxy independently validate exact branch mapping, sum and
monotonicity of branch counters, frozen Boolean scope and semantics, and visible
full Boolean predicates when display text is untruncated and unsanitized. A
sanitized/truncated display cannot establish the truth of omitted raw text; its
Boolean predicate evidence is the broker's pre-projection check. Native stop
states can retain pending observations but cannot resume native requests.
Unknown branch heads on budget/deadline failure close the scope honestly.
No Boolean result certifies provider recall, a complete history or absence.


## Bounded chat listing

`list_chats` is an independent opt-in capability checked at both proxy and broker.
Its closed list selector accepts Main, Archive or both; no wildcard, arbitrary
folder or raw provider arguments are exposed. Its native path uses only readiness,
account identity, `getChats` prefix observations and exact `getChat` metadata.
It invokes no content retrieval, read receipts, open-chat operation or unread
mutation. Existing content tools retain their separate approved native paths.

Initial native prefixes contain at most 200 observations total, with Main100 then
Archive100 for both. Prefix arrays validate before hydration; invalid identities,
coercions, oversized pages and in-array duplicates fail closed. Current membership
comes from `chat_lists`, never historical positions. Recognized folder entries do
not expand scope. Exact nonsecret type, identity and unread metadata are checked
before projection; invalid or excluded candidates cannot disclose their metadata.
Native title input is capped at4,096 characters and membership arrays at128 entries
before normalization or iteration. Only bounded untrusted titles, selected-list
identity/rank, kind and unread counts are returned. Last messages, drafts, native payloads and exception text are absent.

Cursors freeze scope and limit and retain bounded numeric observations and
counters only. Account/client/broker/contract isolation, four scopes including
in-flight calls, absolute 300-second expiry and single-use consumption apply.
The proxy independently checks requested/frozen scope, order, duplicate IDs,
metadata bounds, per-list counter progression and cursor expiry. Continuing a
selection cannot fetch a new prefix or silently reset evidence. Per-call work is
limited by observed candidates, including exclusions, rather than emitted rows.
Terminal pending counts disclose unprocessed selected observations without a
promise of continuation. Prefix exhaustion and local metadata are explicitly
unverified for whole-account completeness and server freshness.


## Selected-chat search boundary

`search_chats` is an independent off-by-default capability checked by the STDIO
proxy and authenticated broker. Its strict native argument boundary accepts an
ordered unique list of1..5 nonzero int53 chat IDs, the existing bounded lexical or
Boolean query, date mode/bounds, typed sender/direction, limit and opaque cursor.
No arbitrary provider arguments, paths, accounts, handles or per-chat topics enter
this operation. Every selected numeric identity is rehydrated and validated against
allowlisted non-secret native chat types before the first content request. The
current chat and account are checked again around child content work.

Each outer slot owns one private `ExactSearchReader`, without consuming existing
single-chat capacity. At most four active/idle outer slots exist per client. A call
works on one selected lane and uses one aggregate30second request budget. Each lane
retains the accepted200-candidate/10-page bounds; each call retains100 processed
observations,20 outcomes and100000 display characters. Stored outer summaries
contain requested scope, coverage/head/counters and private child continuation;
they do not retain bodies, titles or unread metadata. Stops close owned child state;
close/expiry in flight also suppresses content and continuation. Unexpected claimed
failures discard the child. Shape/binding mismatch before claim preserves the valid
cursor. Cursor binding includes ordered selection/query/filters/dates/limit,
client/account/broker/contract and the original absolute300second expiry.

The independent proxy verifies immutable scope, exact selected order, one-lane
progress, unchanged other lane summaries, heads, per-call counter deltas, Boolean
branches, descending result IDs, exact anchors, predicates and original expiry.
A stopped lane advances exactly one on the next call, resetting the per-chat
result-ID boundary; zero native progress can advance only through that explicit
lane stop. Replay, skipped/reopened chats, forged counters, foreign bodies, expiry
and close fail without returning content. Account/auth/session loss and uncertain
deadlines terminate the group; pending lanes remain honestly unprocessed. Ordinary
per-chat provider failures may advance on a later call, and never imply absence.
The shared date bound and separately observed chat heads provide no atomic snapshot,
global chronological ordering or provider recall guarantee. `scope_complete=false`
and `has_more=null` apply to every response. Existing provider allowlists and unread
behavior are unchanged; no send, secret-chat access or read-receipt operation is added.


## Verified target handle boundary

Package 0.14.0 appends `verify_target` and `read_target_messages` with independent
`verified_targets` capability checks at STDIO, proxy and broker. Default policy
omits this capability. Prior 24 schemas/annotations, contract 1, private IPC 2 and
pinned SDK 2.2.0 remain unchanged. Caller selection must already satisfy the owner,
direct resolver or complete corroborated discovery rules. `verify_target` performs
numeric identity hydration only and makes no semantic-selection claim.

The per-client registry is separate from discovery scans and stores only immutable
account/client/broker-generation/contract/numeric-chat/type identity and expiry.
No bodies, titles, usernames or hypotheses enter it. Tokens are `target_` plus
64 lowercase hex characters from 256 random bits. TTL is non-sliding 300 seconds
on a monotonic clock. Capacity is 16 live handles with pending issuance reserved;
there are at most 4 active operations including issuance and no live eviction.
Close/release/restart invalidate the store; pending completion cannot resurrect it.
Locks cover state transitions/publication only, never provider I/O.

The reader uses the existing selected-message parser through a guarded facade.
Fresh account observations run before/after exact numeric `getChat` hydration,
before/after selected `getMessage` and sender hydration, and before output. Native
chat envelopes, numeric identity and type discriminators must agree. No
`searchPublicChat`, content discovery, history traversal, media download, read
receipt or mutation is introduced. These checks cannot promise an atomic remote
snapshot between observations. A dedicated fatal RuntimeError guard escapes all
legacy reader/sender TDLib catches; account/identity/lifecycle drift discards the
entire batch and prevents later content calls. Transport failures fail closed.
Selected missing messages retain per-message incomplete outcomes when identity
remains valid. Final response construction precedes locked publication checks.

The proxy independently retains bounded issuance bindings and fixed expiry. It
rejects unknown/foreign/expired handles before dispatch, invalidates on handshake
generation changes, and verifies token, target/type, original expiry, exact ordered
anchors, source anchors and aggregate text limits. The broker rechecks raw IPC
models, capability and its own client-scoped registry independently. SDK raw
arguments are validated before its JSON-string parsing so quoted message-ID arrays
cannot become a permitted selection. Closed wrapper schemas require terminal
states to have no identity metadata or nested content. Provider errors/tokens are
not echoed in errors. Disable `verified_targets` or restore the accepted matching
package/policy as rollback; no Telegram-side mutation needs undoing.


## Selected attachment extraction

Package 0.15.0 appends `read_attachment_page` behind independent default-off
`attachment_pages` checks in STDIO, proxy and broker. All 26 prior schemas and
annotations remain unchanged. A request has only an issued artifact ID, ordered
PDF page selection (at most5 distinct one-based pages), max_chars<=20000,
render_pages and an opaque continuation. No arbitrary path or parser option crosses
MCP. Broker-owned metadata and ArtifactStore integrity/retention checks remain the
materialization boundary; the parser uses immutable hash-pinned bytes and the
broker rechecks the original artifact after parsing and preview storage.

Memory-only single-use continuations bind client/account/generation/contract,
artifact ID/hash/size and extraction metadata/settings/version. The original
300-second monotonic expiry is never renewed and is limited by artifact retention.
There are 16 live reservations and 4 active operations per client. Account readiness
and exact getMe identity are checked before and after extraction. Close, expiry,
metadata drift and account drift discard the entire result. The independent proxy
checks issued cursors, handshake generation, fixed scope, offsets, request limits,
completion facts and hostile wire types before accepting content.

The parser runs separately with bounded input, expanded archive data, extracted
text, rendered pixels, encoded preview bytes, CPU, allocation and wall time. It
loads at most 64MiB, extracts at most 1,000,000 codepoints in its selected scope, and
returns at most 20000 characters / 5 previews. Rendering is at most 768 pixels per edge;
previews are first-response-only. No OCR or embedded-object coverage is claimed.
DOCX body text retains the 500-entry/32MiB expansion ceiling and the package-wide
safeguards described below. Changed/corrupt/password/limit
outcomes contain no document data or actionable continuation. Complete means the
supported selected text ended, not full coverage of the original document.

Package 0.18.0 admits TSV and the existing literal-text suffix set consistently
through selected document transfer and local artifact creation. It preserves all
29 public schemas/annotations and the existing extraction/cursor contract. CSV,
TSV, JSON, XML, YAML, markup and source files are strict UTF-8 text with CRLF/CR
normalized to LF; no structural parsing, formula evaluation, link fetching or
execution is performed. Unicode codepoint windows may split a logical row or
token. Text beyond the 1,000,000-codepoint extraction ceiling or invalid UTF-8
returns an empty failure. This does not broaden the separate DOCX parser policy.

Package 0.19.0 strengthens DOCX parsing without changing the 29 public schemas,
annotations, extractor version or supported body-text ordering. Both legacy and
page readers audit the same immutable bytes they give to python-docx. Every ZIP
member, including unused parts, is checked for canonical names, duplicate/case
aliases, special file types, encryption, allowed stored/deflate compression,
CRC/length consistency and measured expansion. Limits are 500 members, 32 MiB
aggregate expansion, ratio 200 and 8 MiB per XML part. Aggregate XML budgets are
200,000 nodes, depth 64 and 32 MiB text/attribute codepoints. Encoding-aware parsing
rejects DTD/entity declarations; XML is recognized by suffix and declared MIME.
Content-type and relationship metadata must occupy their canonical paths and
namespaces, including nested or wrong-extension metadata. Relationships must have
unambiguous IDs, modes and internal targets, with one correct main document.
Active/loading families (VBA, ActiveX, OLE/embedded packages, templates and controls)
are rejected using known content-type and relationship identifiers, including
known full Office-package MIME declarations on unused inner parts. These checks
do not classify arbitrary binary payloads. External hyperlinks remain inert
metadata; other external loading relationships are rejected. No URL is fetched, and no new body/layout/notes
coverage is claimed. Unused malformed parts can therefore reject an otherwise
readable short body.

Legacy DOCX processing now runs in a short-lived fixed-DOCX worker, retaining its
prefix truncation and byte-accounting contract without imposing the page reader's
1,000,000-codepoint selected-text ceiling. The parent admits only bounded, closed private requests
and results, checks source identity, caps wall time at 15 seconds, and kills/reaps the child
process group on timeout or interruption. The child reads at most 64 MiB, validates
and parses one immutable buffer, and uses CPU, descriptor/core and allocation
limits plus a 1 MiB output-file ceiling. On Darwin, the allocation limit permits the
initial virtual mapping plus 768 MiB; it is not an absolute RSS guarantee. The page
worker keeps its existing independent limits and protocol. No provider/native
client runs in either parser worker. Failures return empty safe details, without
member names, relationship targets, local paths or document excerpts.

Package 0.20.0 adds a trusted-local streaming upload CLI without changing MCP
schemas or private IPC version 2. Only this local command accepts a selected source
path; MCP continues to accept upload/artifact handles. The helper opens absolute
path components through directory descriptors without following symlinks, rejects
traversal and non-owner/nonregular/multilink sources, hashes the pinned source and
streams fixed-size chunks. File/path identity, size, timestamps and stream hash
are checked before completion. This detects ordinary races; same-UID processes
remain inside the existing local trust boundary.

The helper uses the existing policy/contract/capability-checked broker protocol,
disables service restart and does not replay a dispatched operation. Lost replies
or interruption stop the transfer; restart means a new upload from byte zero.
An uncertain finish may have completed a local artifact. It is never a send and
never authorizes one. Incomplete bytes cannot produce a successful finish. Upload
handles/state are broker-wide and memory-only, with eight active uploads and a
15-minute lifetime. They are not per-client confidentiality grants. Existing
large-orphan quarantine limits and cache retention still apply.

Completion registers coherent filename/MIME/media metadata before returning the
existing artifact response; it creates no Telegram source anchor. Unknown document
suffixes can be stored but gain no parser support. Metadata remains broker-lifetime,
while cache bytes have independent bounded retention. Local XLSX/PPTX upload does
not change selected-provider transfer admission. Upload adds no account registry,
persistent catalog or generic filesystem access through MCP.

Package 0.21.0 extends that local helper with selected-artifact download to an
explicit absolute destination. Only a canonical generated artifact name is read
under the fixed cache root; no returned artifact path is followed. The helper
requires owner-only cache/file modes (0700/0600), a regular single-link file, a
matching declared size/hash within the 256 MiB cache ceiling and a current 12-hour
TTL. It holds a no-follow source descriptor and rechecks full file facts and the
named directory chain. This read does not call the pruning cache lookup or change
source bytes, permissions or expiry. Retained valid bytes may outlive broker reader
metadata. Local handles still provide no separation between hostile same-UID
clients.

Before reading/staging and immediately before publication, download explicitly
requires the captured trusted policy to remain current and enable `artifacts`.
Two read-only handshakes must return the same compatible descriptor and broker
generation. Observed policy changes, restart, mismatch or unavailable handshake
stop publication. There is no provider call, service restart or new IPC operation.
These checks are observations, not a transaction locking policy or generation
across the filesystem publication boundary.

The existing destination parent is opened through a no-follow directory chain,
must belong to the owner, and cannot be writable by group or others. Cache and
quarantine destinations, including descendant or identity aliases, are rejected.
All existing destination entries are rejected, with exclusive hard-link
publication enforcing no overwrite even if another entry appears after preflight.
The staged copy is a new private inode; the cache inode is never linked into the
destination. Bounded copying, independent staged hash/size/fact checks, file fsync,
source/TTL/path rechecks and final directory fsync precede a success receipt.

Failures before a link attempt retain the private generated partial. A failure or
signal after a link attempt can leave a complete final file; the helper preserves
it and reports that the owner must inspect the destination and partials. It does
not replay publication, delete a possible final result, promise crash rollback or
offer resume. Staging cleanup only removes the known verified staging name after
publication; final success requires one link and matching identity. Unsupported
filesystem durability operations fail instead of weakening these checks. Recovery
files remain outside the cache and can occupy disk until deliberate cleanup.

Inline previews are reopened through the fixed owner-only cache directory and
checked for exact issued artifact hash, byte size, mode and regular-file identity;
no symlink or arbitrary broker-supplied path is followed. Image-delivery failure
changes only previews_complete; it cannot manufacture a text continuation.

A fixed extraction permits at most 256 calls; if text still remains on the last
call, it returns `limit_reached` without content or a new cursor. Start an explicit
fresh extraction with a larger `max_chars` to reduce fragmentation. This bounds
retained token history as well as parser work; no automatic restart is performed.

On macOS, the hard AS/DATA worker limit is its initial Mach virtual map plus
768 MiB of additional allocation, set before parser imports and source reads.
It is not a 768 MiB absolute resident-memory guarantee. Linux uses an absolute
768 MiB limit, but that branch has not been accepted on the supported macOS host.

### XLSX cell extraction

Package 0.16.0 appends `read_spreadsheet`; all 27 preceding tool schemas and
annotations are pinned unchanged. Independent `spreadsheets` policy checks apply
at the STDIO, proxy and broker boundaries. Catalog-only calls expose sheet metadata;
selected hidden sheets are never treated as access controls or silently excluded.
Returned cell/formula text remains untrusted evidence, including prompt-like text.

Each authorized artifact is read into immutable hash-pinned bytes in a short-lived
isolated parser process. ZIP member/type/path/expansion checks precede extraction;
all package relationships and content-type declarations are checked, including
unused parts. External targets, active/macro parts, duplicate/ambiguous references,
DTD and entities are rejected. XML depth/node/text budgets apply to the entire
validation. The worker never extracts ZIP members to their named paths, resolves
external links, executes macros or evaluates formula content. Cached results have
unknown freshness. Format/style/rendering and whole-workbook coverage are not claimed.

Worker limits include 15-second wall time, CPU, file output, descriptors, core dumps
and memory. On macOS the hard address/data limit is inherited virtual mappings plus
768 MiB; it is not an absolute RSS limit. Native support remains macOS ARM64; other
platform branches require their own acceptance evidence. The independent broker
and proxy maps retain only bounded scope metadata/offsets/tokens, with pre/post
account, artifact and lifetime checks. No workbook cells are retained in cursors.
Fixed 300-second expiry, 16 live including pending, four active and 256-call ceilings
bound session state. Closing/releasing a client invalidates in-flight publication.


### PPTX selected-text boundary

Package 0.17.0 appends `read_presentation` behind default-off `presentations`.
All 28 prior tool schemas and annotations are pinned unchanged. Only issued
artifact IDs cross MCP; no filesystem path or provider argument is accepted.
Catalogue returns indices/hidden/notes flags only. Selected text and explicitly
requested notes are data, never instructions. Detected omitted objects are labelled,
`full_content_complete` stays false, and a 20,000-codepoint aggregate response cap
returns an empty limit result rather than truncating. No cursor state exists.

The standalone isolated worker reuses pinned-file, bounded XML and OS-limit
primitives from the installed XLSX module, without changing that reader. Its own
closed package-type and relationship policy rejects unknown/active/external
families, ambiguous targets, malformed or duplicate package entries, DTD/entities,
and unsupported string encodings. It reads immutable bytes with hash/metadata
checks before and after extraction. All package XML is bounded, including unused
parts; this is not full OOXML schema validation. It does not render, decode media,
follow links, or execute fields/macros. Timeouts kill and reap the worker. The
parent strictly validates worker output before public models validate selection,
notes mode, ordering and aggregate budgets.

Broker and proxy each enforce the trusted capability, four-active-read cap,
generation and close state, and 30-second request deadline. The broker revalidates
account, source metadata, artifact retention and immutable bytes. Both sides
recheck trusted policy at completion so an in-flight revocation withholds content.
Proxy diagnostics use fixed local text, never free-form broker failure detail.
Actual runtime acceptance uses a small owner-created local synthetic PPTX and
account guards only; it does not establish real Telegram attachment, mobile UI,
whole-account state or user-value acceptance.

## Read-only send status evidence

`get_send_status` requires the existing `send` capability and accepts only a strict
issued draft token. It is read-only and idempotent: it never prompts approval,
claims, retries or invokes a provider send. Ownership binds proxy client and
current account; attempted facts also bind the same provider instance. The broker
rechecks account, policy, local attempt retention and provider observations before
returning facts. Authorization/identity loss invalidates correlation; restart and
expiry cannot reconstruct it. Unknown, foreign, wrong-account and obsolete tokens
expose no recipient/account/message identifiers or content.

The closed response separates pending/sent/failed/outcome_unknown from exact
evidence. Local pending/claimed is not provider acceptance. A retained pending
provider observation may accompany outcome_unknown after response loss; only
exact terminal evidence refines it. Confirmed sent requires a strict positive
final message ID; all other responses omit message evidence. Provider failure
requires coherent outer and failed-message error codes, matching chat/outgoing
kind and strict correlated IDs. Provider text/errors, captions, files and paths
never enter status responses or observation metadata. An optional SHA256 of the
approved text preserves existing integrity checks after exact correlation. Text
is never a correlation key; similarity, quick acknowledgments, deletion/404 or
absence do not establish success/failure. Terminal facts never switch outcomes.

Observations are volatile, content-free and bounded to 4096 entries for 900 elapsed
seconds from initial registration, with no read renewal. Registry attempt facts
use a separate elapsed-time clock, preserving that bound across wall-clock drift.
Capacity/correlation refusal occurs before raw send; a transport exception remains
unknown because acceptance may already have occurred. Status can drain at most 64
immediately available events for at most 50ms under a nonblocking existing provider
lock; auth/account reads remain inside the outer 30-second deadline. Catalog/auth
updates and unrelated send observations are preserved. No new background worker,
durable journal, recipient read receipt, exactly-once promise or live-send authority
is introduced. Synthetic checks are not evidence of live Telegram delivery.

## Plain-text reply evidence and revalidation

`reply_text_send` is a separate trusted opt-in and requires existing `send`
permission. Four additive closed tools accept only recipient/text/selected exact
anchor for preparation, draft ID for inspection/refresh, and draft ID/text for
update. No consumer field enables capability or selects account/client ownership.
Legacy public draft and send schemas stay unchanged. Legacy preview/revision
methods refuse reply-bearing drafts so target evidence cannot disappear from a
review. New reply methods refuse ordinary drafts; metadata-only list/cancel/status
continue to use the existing lifecycle.

A source snapshot is an immutable canonical serialized projection of validated
anchor, typed sender, direction, dates, raw bounded formatted text with no entities,
and relevant null/disabled topic/link-preview/scheduling/ephemeral state. Raw
Unicode and safety-critical scalars are validated before storing. Nullable TDLib
objects may be omitted in native JSON; emitted safety scalars cannot default from
missing to zero. Source text has no controls or format characters other than
newline/tab. Full marked display is explicitly sanitized and never truncated.
Raw text hashes preserve even whitespace/NFKC-equivalent changes. Display evidence
is untrusted data. Hashing ignores volatile provider progress and unrelated fields.

The review digest covers version, complete nested text draft, target anchor/raw
source digest/display evidence, null topic, empty outgoing entities, reply
constructor, disabled link-preview options and every fixed user-significant send
option. Random transport sending ID is excluded. Closed output validation
recomputes the digest; proxy request/response binding rejects unrelated IDs, anchors
or outgoing text. Update preserves the target snapshot, refresh rehydrates the
same anchor, and both atomically replace the revision without approval/receipt
transfer. Registry locks are never held during provider I/O.

The owner dialog shows the complete outgoing text, account, exact reply anchor,
full target evidence, display flags and source/review hashes. After confirmation
and claim, a distinct provider method holds the reentrant local provider lock for
account/recipient/source/hash and strict reply-eligibility revalidation. A final
broker guard checks current policy and captured provider/epoch before registering
any observation or raw send. Pre-send refusal is `MessageSendNotAttempted` and
records `local_failed`. Missing reply-aware provider support never falls back to
ordinary sending. Bounded request budgets include provider lock waits.

Only the closed same-chat `inputMessageReplyToMessage` constructor is allowed,
with exact positive message ID, null quote, checklist task zero and empty poll
option. The surrounding recipient is exact and topic is null. Final and
preliminary observations require exact `messageReplyToMessage` metadata: chat/ID,
strict checklist task zero, empty poll option, origin-send-date zero, and nullable
quote/origin/content. A missing/wrong/malformed anchor never confirms a reply or
exposes a final positive ID. Late reconciliation applies identical checks and
never resends; terminal failure evidence remains final. Normal sends preserve
their existing observation behavior.

`getMessage` and `getMessageProperties` are cached/offline observations. The local
lock prevents local provider-call interleaving, not a remote target-edit race. The
pinned provider may silently discard an invalid reply anchor and send an ordinary
message; the output guard reports uncertainty after that side effect. This slice
does not establish an atomic Telegram reply guarantee or actual delivery/read
status. Topics, outgoing formatting, embedded quotes, external-chat and scheduled replies
remain unsupported; bounded media sources require the additional capability below. Synthetic technical evidence does not verify live Telegram
delivery or the macOS owner dialog.


### Bounded artifact-reply extension

The four artifact-reply lifecycle tools require independent opt-in
`reply_artifact_send` plus `send`; existing 39 schemas/descriptions/annotations and
legacy capability defaults stay unchanged. Variant inspection/revision refuses
ordinary and text-reply drafts. Send-only metadata listing, cancellation and
status retain their original scope. Preparation performs no send. A distinct
artifact-reply digest binds the complete immutable outgoing preview and target,
caption entities=[], null topic/quote/markup and fixed per-kind wire options.
The owner dialog derives all facts from that validated preview. Revisions use
new IDs and retain outgoing artifact lifetime caps without transferring approval.

New voice preparation copies the server-issued source through a private bounded
non-symlink regular-file snapshot with exact size/hash verification before the
existing converter consumes it. Snapshot cleanup occurs on every local exit;
source admission checks expiry and request budget. A live derivative is independent
of later input expiry. Staging verifies final approved bytes. Provider lock,
account/title/source/properties/policy/epoch/deadline and correlation setup errors
before raw transport are definite `local_failed`; exceptions at or after the raw
transport boundary remain uncertain, with retained staging and no retry.

Artifact-reply observations retain caption and waveform hashes plus bounded
expected scalars, never captions, waveforms, paths or arbitrary provider objects.
Exact outer messageDocument/messagePhoto/messageVoiceNote shapes are required,
with typed nested file/localFile/remoteFile/document/photoSize/thumbnail objects;
nullable pinned objects may be absent/null. Unknown keys, missing safety scalars,
nonempty caption entities, unsafe photo flags or photo.has_stickers, live photo
video, and nonnull voice speech_recognition_result are rejected. Voice duration
and decoded waveform must match and MIME must be audio/ogg; is_listened is only
a strict boolean, not immutable expectation. File IDs/progress are not compared
between evidence events. Limits include 20 photo sizes, 100 progressive-size
entries, dimensions 1..16384, blob/string fields at most 64KiB and smaller path/name/
MIME/ID bounds. These product limits are deliberately narrower than full TDLib.
Correlated provider IDs and verified local input do not establish remote byte
hashes, photo transformation equality or arbitrary requested document MIME equality.
Live delivery and broader target/consumer validation remain outstanding.


### Bounded media-target identity and authorization

`reply_media_targets` independently gates supported source media for both outgoing
reply workflows, in addition to `send` and the matching reply capability. It is
absent from legacy defaults. All 43 public argument/result schemas, annotations
and operation mappings stay unchanged; only eight reply-tool descriptions change.
Broker policy derives media classification from validated private projections v2/v4/v6,
never display strings or proxy inference. Owner/TTL/lock-bound classification
includes terminal receipts. Pending inspection/revision, preparation, both sides
of refresh, pre/post owner decision and final rawsend guards enforce opt-in.
Metadata-only status/list/cancel remain send-only.

The canonical closed v2 projection binds the unchanged ordinary null-topic shell,
raw caption entities=[], explicit kind and closed per-kind stable facts. Documents
bind filename/MIME/positive known size/unique-ID SHA; static photos bind each
variant's type/dimensions/positive size/unique-ID SHA and sort by
(type,width,height,identity SHA), rejecting duplicate keys even with unequal size;
voice notes bind duration/audio/ogg/positive size/unique-ID SHA. SHA256 uses exact
`remote.unique_id` UTF8 bytes; this is provider media identity, not a remote byte
hash. Provider file IDs, remote.id, paths, download/upload counters, expected_size,
thumbnail/progressive detail, waveform and listened state are strictly typed and
bounded but excluded from stable facts. No mutable provider objects escape.

Main identities/sizes must be known and nonempty/positive. Caption bounds are
1024 Unicode characters and valid UTF8 without controls except newline/tab;
filename/MIME bounds are 255/127 UTF8 bytes. Photo vectors cap at 20 sizes and
100 progressive entries (0..2**31-1), dimensions 1..16384; unsafe flags, live video
and stickers refuse. Voice duration is 1..600 seconds, MIME exactly audio/ogg,
strict base64 waveform 0..100 decoded bytes and null/absent speech-recognition
result. MP3/M4A and longer voice targets remain unsupported. Thumbnail files may
have zero size; minithumbnails decode to at most 65536 bytes. Stored v2 JSON caps
at 65536 UTF8 bytes before parsing, validates exact canonical keys/types/variants
and checks display feasibility. Constructed projections are not authenticated;
fresh exact provider revalidation remains authoritative.

Render original strings through sanitation, then JSON escaping, with full facts
and explicit empty caption. The final marker-inclusive text is sanitizer-idempotent
and at most 4096 characters; overflow refuses instead of truncating. Raw facts
remain in the source SHA so sanitation cannot hide edits. The media text path
passes its existing guard to the shared pretransport boundary. A false result or
exception after observation registration discards only the unattempted observation
and raises MessageSendNotAttempted. Nothing at/after raw transport is classified
as local_failed. Existing claim, ownership, deadline, staging, correlation and
unknown/no-retry semantics remain authoritative. No target download/path opening
occurs. Synthetic evidence does not establish atomic remote edits, live delivery,
actual owner dialog or readiness of other consumers.


### Closed style-only text source projection

`reply_formatted_targets` is an independent default-off trusted capability for
both reply workflows. Private projection v3 retains the raw messageText shell
and canonical nonempty entities; v1 plain-text and v2 media canonical bytes stay
unchanged, and `is_media` includes v2, styled-caption v4 and lexical-caption v6. No public schema, tool annotation,
operation mapping, contract version or IPC version changes. Preparation,
inspection/update, both sides of refresh, approval, send/replay and pretransport
guards enforce every source-required capability. Gated v2/v3/v4/v5/v6 terminal replay checks
original provider instance/epoch as well as owner/account and monotonic retention
under the registry lock. Pending lifetime uses wall expiry; terminal retention
refuses at attempted_at+900 inclusive. Plain v1 replay behavior and read-only
send-status rules remain unchanged.

The exact allowed types are Bold, Italic, Underline, Strikethrough, Spoiler, Code,
Pre, PreCode, BlockQuote and ExpandableBlockQuote. Exact textEntity fields are
@type, offset, length and type; only PreCode's exact type has a language field.
Offsets/lengths are strict int32 values, never booleans, with nonnegative offset,
positive length and both original UTF-16 boundaries aligned to whole Unicode
scalars. Raw source is nonblank, at most 4096 Python characters, valid UTF8 and
has no category-C controls except newline/tab. Language has at most 64 characters
and 128 UTF8 bytes, with no controls. There are 1–32 entities; canonical order is
(offset, descending length, type, language). Duplicate identical spans/types,
crossings and unknown fields/types refuse. Code/Pre/PreCode are disjoint from all
other entities; block quote pairs are mutually disjoint. Adjacent spans and
ordinary style nesting/coextensiveness are allowed. These conservative exclusions
are a product bound. Semantics are pinned to td_api.tl SHA256
`326b65b41442901ad6bf0ca2f7c356ae54365d6c343956a62e06a8b3cb305e87`,
lines 109–117 and 5716–5785; native execution/upgrade is unnecessary.

The immutable canonical projection caps at 65536 UTF8 bytes before parsing.
Constructor and target rendering independently validate the complete projection,
including shell, version, anchor and canonical entity order. The full marked
`Formatted target:` JSON record includes sanitized source, all styles, raw
UTF-16 offsets/lengths, sanitized exact covered substrings and optional sanitized
language. Its literal offset_basis is `original source UTF-16 code units`.
Sanitation never reassigns offsets; any source/language/span display change sets
sanitized. Every string is JSON-escaped and no HTML/Markdown style or hidden target
is interpreted. The entire marker-inclusive display is at most 4096 characters;
overflow refuses instead of truncating. V3 continues to refuse lexical types;
v5 below admits a separate bounded subset; v7 admits identity text and v9 admits
null-format DateTime text under their separate authority. URL, custom emoji,
media timestamps, non-null DateTime formats and embedded
reply/forward/import/markup remain unavailable.

Every revalidation compares the raw source digest, so range/language/quote-kind
changes fail closed. V3 text dispatch now passes the existing guard through the
shared pretransport boundary; staged artifact replies retain their immutable byte
checks and guards. A false/raising guard after registration discards only the
unattempted observation and records local_failed. Raw transport attempts remain
uncertain when confirmation is lost; late exact reducers may resolve them without
resend. Outgoing text/caption entities remain empty with unchanged topic, options,
markup and same-chat reply shape. Disable the capability or revert the candidate
change to roll back. Synthetic source/installed-process checks do not prove actual
Telegram delivery or broad consumer readiness.


### Styled media captions require conjunctive source authority

Private v4 combines the unchanged media projection with a nonempty styled caption.
V2 must have empty caption entities; v4 must have nonempty canonical entities.
Constructor and target independently check that boundary and canonical order.
The same closed ten style types, 32-span, original scalar-aligned UTF-16 and
overlap policies apply, with a nonblank caption bound of 1024 Unicode characters.
The complete marked JSON caption includes text, offset basis, style, raw
offset/length, covered text and optional language alongside all stable media facts.
Every displayed string contributes to the sanitation flag. Raw facts bind the
source hash before sanitation; full display caps at 4096 characters including
the marker and canonical source caps at 65536 UTF8 bytes. Overflow never truncates.

Source authority is an explicit tuple: plain (), media (reply_media_targets),
styled text (reply_formatted_targets), styled media (media then formatting), or
lexical text (reply_formatted_targets, reply_lexical_targets), or lexical media
(reply_media_targets, reply_formatted_targets, reply_lexical_targets).
Every production policy check requires every member. Legacy singular classifiers
refuse v4/v5/v6 rather than returning incomplete authority. The registry classifier
retains owner/account, pending wall expiry, attempted monotonic retention and
original provider/epoch checks. Both stored and freshly read sources require
their complete tuples during refresh. Revoking either source capability blocks
preparation, inspection/revision, owner confirmation, final transport guards and
terminal replay. Send/status metadata rules and all public schemas remain
unchanged. No source attachment is downloaded; outgoing entities remain empty.


### Lexical text source evidence requires both source opt-ins

Private v5 admits text only, with at least one exact fieldless Hashtag, Cashtag or
BotCommand provider entity and optionally the ten supported styles. Explicit
lexical parser mode is selected for these text sources and the separate v6 media
sources below; default style and media-caption parsing reject lexical types.
V1–v4 canonical/display bytes are
preserved. Constructor, target rendering and capability classification independently
validate the complete stored projection, version discriminator and canonical order.
The source requires (`reply_formatted_targets`, `reply_lexical_targets`), both
default-off; singular classification fails closed. V5 is not media; lexical
captions require the separate triple-authority v6 projection.

Exact closed shapes, nonboolean int32 coordinates, original scalar-aligned UTF-16
boundaries, canonical ordering, 1–32 entities and existing duplicate/crossing/code
exclusions apply. Lexical pairs and lexical/quote overlaps are additionally refused;
simple styles may nest or coincide with lexical spans in either direction. Raw
nonblank text caps at 4096 characters, canonical UTF8 caps at 65536 bytes before
JSON parsing, and complete marker-inclusive evidence caps at 4096 characters with
no truncation. The JSON labels `hashtag`, `cashtag` and `bot_command` accompany
covered text derived before sanitation. All displayed strings affect `sanitized`;
sanitation never changes source coordinates. These fieldless types follow pinned
td_api.tl lines 5722–5728; no independent lexical recognition grammar is invented.

Provider labels and source syntax remain untrusted evidence. They authorize no
execution, resolution, navigation or account selection. Raw shell/text/entities
bind the source digest; text, range, kind or metadata edits invalidate approval,
while entity permutation and irrelevant volatile fields do not. Every existing
plural lifecycle guard checks both capabilities, including old/new refresh and
post-owner/register-before-wire boundaries. Owner/account, finite TTL and original
provider/epoch checks remain in force. False or raising pretransport guards discard
unattempted observations; unknown attempts retain uncertainty and exact late
reconciliation never resends. Outgoing entities stay empty. Topics, links and custom
emoji remain unsupported; identity text uses v7 and null-format DateTime text uses
the separate v9 boundary below. Public schemas,
annotations, operation mappings, contract 1 and IPC 2 are unchanged; only the eight
reply-tool descriptions are intentionally corrected. Remove the lexical capability
or revert this local batch to roll back. Synthetic technical checks do not prove
live Telegram delivery or actual owner confirmation. Shipped skill source/caption
guidance is explicitly deferred to F18; complete consumer guidance is not claimed.


### Lexical media captions require all three source opt-ins

Private v6 extends only the existing closed document, static-photo and OGG
voice-note source projections with at least one exact fieldless Hashtag, Cashtag
or BotCommand entity, optionally mixed with the ten existing styles. V6 must have
a nonblank lexical caption; empty/styles-only entity lists fail the v6 boundary.
V2/v4 reject lexical entities. Text remains v5 and cannot be forged as v6. The
explicit lexical mode reuses the existing formatted validator; parser, validator
and renderer defaults remain style-only. V1–v5 canonical bytes, display bytes,
source authority, versions and bounds are preserved.

V6 requires (`reply_media_targets`, `reply_formatted_targets`,
`reply_lexical_targets`) conjunctively, beside send and the selected reply path.
All three source capabilities remain default-off. The source is media, and
singular source/registry accessors refuse to underreport its authority. Existing
plural guards enforce the exact tuple for preparation, pending get/update,
old/new refresh, terminal replay, before/after owner decision, after fresh provider
properties and after observation registration before wire. Owner/account, wall
expiry, terminal monotonic retention and original provider/epoch checks are
unchanged. No guard or public operation/schema/IPC contract is broadened.

The raw caption has at most 1024 original Unicode scalars and 1–32 entities.
Exact fieldless type shapes, nonboolean int32 coordinates, scalar-aligned original
UTF-16 boundaries, canonical ordering and existing duplicate/crossing/overlap
exclusions remain in force. The complete marked Media target record contains all
sanitized caption text, every covered span with provider label and raw offset/
length, optional style language, and all stable media facts. Sanitation changes
are explicitly flagged; display coordinates never replace source coordinates.
Caption/span/media evidence is digest-bound before sanitation, never truncated.
Canonical projection caps at 65536 characters and UTF8 bytes before JSON parse;
complete marker-inclusive display caps at 4096 characters. Source file identity,
strict provider shape validation, volatile-field exclusions and media refusal
rules remain unchanged. Projection/render fetch no provider data, read no paths,
download no media, resolve no identities and execute no commands or links.

False/raising final guards discard only unattempted observations. Once raw
transport is attempted, uncertainty remains until exact reconciliation and
never authorizes resend. Outgoing text/caption entities remain empty. Disable
reply_lexical_targets or revert the local batch to roll back. Technical synthetic
checks do not establish live delivery, actual owner clicks or consumer readiness;
the shipped consumer skill remains unchanged conservative F18 debt.


### Identity text source authority is explicit and conjunctive

Private v7 admits `messageText` with at least one exact provider identity entity:
fieldless `textEntityTypeMention`, or `textEntityTypeMentionName` with only `@type`
and `user_id`. The latter ID is a strict nonboolean positive int53 below 2**53;
strings, floats, missing/extra fields, zero and negative values refuse. V7 cannot
be forged from media or sources without identity entities. V1–v6 retain their
canonical bytes, rendering and authority; old version tags cannot authorize
identity entities. Identity media captions use the separate v8 boundary below.

Every v7 source requires (`reply_formatted_targets`, `reply_identity_targets`);
any lexical entity appends `reply_lexical_targets`. These capabilities remain
default-off and augment `send` plus the selected reply path. Existing plural
registry/Broker/provider guards enforce the exact conjunction on preparation,
get/update, old/new refresh, before/after owner decision, after fresh source and
properties checks, after observation registration before wire, and terminal
replay. Singular authority accessors refuse conjunctive sources. Owner/account,
finite expiry, terminal retention and original provider/epoch checks are retained.

The closed parser preserves original UTF-16 offsets and lengths, exact type
metadata and canonical entity order. Nonboolean int32 coordinates must align
with original Unicode scalar boundaries. Identity spans exclude overlap with
other identity spans, lexical spans, code/pre and quotes; style nesting follows
existing exclusions. Duplicate/crossing spans refuse. The source has 1–32
entities and at most 4096 original Unicode scalars. Canonical projections cap at
65536 characters and UTF8 bytes before JSON parse; complete marker-inclusive
formatted evidence caps at 4096 characters. Overflow refuses before registry
mutation and never supplies a partial preview.

The complete escaped preview contains every identity span's label, original
coordinates and covered text; MentionName includes the declared `user_id`.
Sanitation changes are flagged but never alter digest-bound raw source facts.
Changing only `user_id` changes the source digest and refuses a stale send when
observed during final provider revalidation, including an edit during the local
owner decision. Provider evidence is cached/offline and is not an atomic remote
Telegram guarantee. Identity labels and IDs are untrusted assertions, never
authentication or recipient authority. No user resolution, navigation, media
download or source-directed send occurs. Outgoing text/caption entities stay
empty. Denial and terminal/unknown replay never cause an extra send. Disable
reply_identity_targets or revert this local batch to roll back. Public schemas,
annotations, mappings, contract 1 and IPC 2 remain unchanged; only the eight reply
descriptions change. Synthetic checks do not prove live delivery, actual owner
clicks or consumer readiness; the installed/shipped consumer skill remains F18 debt.


### Identity media captions require the exact v8 source authority

Private v8 extends only the existing closed document, static-photo and OGG
voice-note source shapes. Its caption must contain at least one exact provider
Mention or MentionName entity. Parsing, stored validation and rendering require
explicit identity mode; old media v2/v4/v6 reject identity entities, and v8 rejects
empty, styles-only and lexical-only captions. V7 remains text-only. The same
closed formatted parser enforces positive nonboolean MentionName IDs below 2**53,
original UTF-16 scalar boundaries, canonical ordering, duplicate/crossing refusal
and conservative identity/lexical/code/quote overlap exclusions.

Every v8 source requires (`reply_media_targets`, `reply_formatted_targets`,
`reply_identity_targets`); exactly when any lexical entity is present,
`reply_lexical_targets` is appended. The default-off source capabilities augment
send and the chosen reply path. Existing plural server/registry/Broker/provider
guards retain their complete conjunction at preparation, get/update, old/new
refresh, before/after owner decision, final provider checks, observation
registration before raw transport and terminal replay. No new capability or
operation is introduced. Singular authority accessors refuse conjunctive sources.

The complete marked Media target record shows caption text, every entity label,
original offset and length, covered text, declared user ID and stable media
metadata. Caption text remains bounded at 1024 original Unicode scalars, entities
at 32, canonical source at 65536 characters and UTF8 bytes, and the complete
marker-inclusive escaped display at 4096 characters. Sanitation is flagged and
never replaces raw digest-bound facts; overflow refuses before registry mutation.
No source retrieval performs identity resolution, navigation, media download,
path reads or read receipts. Outgoing text/caption entities remain empty and
recipient/anchor selection remains unchanged.

Changing only user_id or stable media identity changes the source digest and
invalidates approval when observed at final source validation. A stale source
may still render the immutable stored owner preview before that final refusal.
The final source read and raw send are not atomic; cached/offline evidence is not
a remote Telegram guarantee. Owner denial or a policy refusal may leave a pending
draft; terminal and unknown replay never perform an extra send. Disable
reply_identity_targets or revert this local batch to roll back. V1–v7 canonical
bytes, displays and authority, all 43 schemas/annotations, operation mappings,
contract 1 and IPC 2 are preserved. Only the eight reply descriptions change.
Synthetic technical checks do not prove live delivery, actual owner clicks or
consumer readiness; the installed/shipped consumer skill remains F18 debt.


### DateTime text sources require the exact v9 source authority

Private v9 is text-only and requires at least one pinned
`textEntityTypeDateTime unix_time:int32 formatting_type:DateTimeFormattingType`.
The pinned td_api.tl line 5784 specifies that a null format leaves original text
unchanged; td_api.h declares signed `int32 unix_time_` and nullable
`object_ptr<DateTimeFormattingType> formatting_type_`. Admission requires exact
nonboolean signed int32 `unix_time` and absent/null `formatting_type`; omission
normalizes to explicit null. Missing timestamps, coercible values, unknown fields
and all non-null formats refuse. Stored v9 must retain explicit null and canonical
order. Old v1–v8 tags cannot authorize DateTime, and v9 cannot authorize a source
without it. DateTime captions use the separately validated private v10 boundary below.

Every v9 source requires `reply_formatted_targets` and default-off
`reply_datetime_targets`, adding `reply_lexical_targets` exactly when lexical
entities coexist and `reply_identity_targets` exactly when identity entities
coexist. The existing plural source-policy guards enforce that conjunction during
preparation, inspection/update, both old/new refresh, owner decision, final source
and properties checks, observation registration before raw transport and terminal
replay. The existing send/reply-path, owner/account, finite retention and original
provider/epoch constraints remain in force. Status/list/cancel retain their
existing send-only authority. No public operation, schema or annotation is added.

DateTime is exclusive of every overlapping entity, including ordinary styles;
adjacent spans remain eligible. This is a conservative supported subset rather
than a claim about all possible provider entity combinations. The current closed
shapes, 1–32 entities, 4096 original scalars, whole-scalar UTF-16 coordinates,
65536-character/UTF8-byte canonical source and complete marker-inclusive
4096-character preview bounds still apply. Overflow refuses before registry
mutation without truncation. The escaped preview shows every DateTime span's
`type="date_time"`, `provider_type="textEntityTypeDateTime"`, signed `unix_time`,
`formatting_type=null`, original offset/length and covered text. Sanitation is
flagged without changing raw source identity or coordinates.

No timestamp parsing, localization, timezone inference, source-text agreement
assertion, conversion or time-triggered action occurs. DateTime fields and text
are untrusted provider assertions. Raw timestamp changes alter the source and
preview digests even when covered text is unchanged. An edited source may still
reach the immutable stored owner preview; final source validation after owner
rendering and before raw send refuses timestamp/non-null-format drift. The final
read/send interval remains non-atomic, and cached/offline provider evidence is
not an atomic remote Telegram guarantee. Outgoing text/caption entities stay
empty. Denial, policy refusal, receipts and unknown outcomes never permit an
automatic resend; existing immutable revision and status semantics are retained.

Disable `reply_datetime_targets` or revert this local batch to roll back. V1–v8
canonical bytes, displays and authority, all 43 public schemas/annotations,
contract 1, IPC 2 and schema fingerprint are preserved. Only the eight relevant
tool descriptions change. Synthetic technical checks do not prove live delivery,
actual owner clicks or consumer readiness; the installed/shipped consumer skill
remains separate F18 debt and is unchanged.

### DateTime captions require exact private v10 authority

The existing bounded document/static-photo/OGG-voice-note projections admit
DateTime captions only through v10, with at least one DateTime entity. The media
parser enables DateTime explicitly; stored v2/v4/v6/v8 projections keep it disabled.
The signed-int32 and absent/null-format rules, strict UTF-16 boundaries and all
DateTime overlap exclusions are identical to v9. A v10 projection without DateTime,
with noncanonical entities, unknown fields, or unsafe media facts is rejected.
V1–v9 canonical bytes, display and required capability sets remain unchanged.

V10 requires `reply_media_targets`, `reply_formatted_targets` and
`reply_datetime_targets`, with lexical/identity authority exactly when present.
No capability is enabled by this update. The full escaped caption span record,
provider timestamp/type and stable media facts enter the immutable preview digest.
Checks at preparation, revision, refresh, owner rendering and final provider read
retain the same refusal semantics. Neither source captions nor timestamp fields
become outgoing entities, identity resolution, date conversion or scheduled work.
The final source revalidation/send interval is still non-atomic, and an uncertain
transport outcome is never retried. Actual owner/provider delivery requires its
own concrete approval; synthetic installed-consumer evidence proves only the
reported technical scenario.
