# Phase 26A — real-time Context Gateway

Implemented from `main` **4de48bd** (Paper v2 cutover serialization fix merged).
Gateway activation defaults **off**. The implementation initially changed no public
domain, credentials, production event, paper hypothesis, admission rule, or deployment.
Production activation on 2026-10-09 is recorded separately in
[`REALTIME_CONTEXT_GATEWAY_ACTIVATION.md`](REALTIME_CONTEXT_GATEWAY_ACTIVATION.md),
including the completed OAuth/Work connection and measured genuine Work submissions.

External AI discovery is treated as an information source, never an execution authority.

Prism's authoritative knowledge time begins when Prism receives the event, not when an external model claims to have discovered it.

## Problem and scope

Phase 23 already accepts `context_import_v1` files, ingests official feeds, retains
immutable events and append-only updates, and constructs point-in-time context snapshots.
Phase 26A removes human file copying from external discovery. It adds transport,
receipts, durability, rapid authoritative ingestion and descriptive latency/quality metrics.
It does not implement Phase 26's source network or change `paper_exploratory_v2_2`.

## Work feasibility spike (verified 2026-10-09)

| Question | Established capability and practical limit |
|---|---|
| Arbitrary authenticated HTTPS POST? | Conditional through Work code/shell when its public-network policy permits the destination. This is an inference from the documented network capability, not a tested arbitrary-HTTP primitive or assurance about this account. No arbitrary POST tool is installed in this session. Do not put gateway credentials in agent prompts, files or its general execution environment. |
| Custom plugin/connector/action? | **Yes:** official instructions explicitly connect a custom MCP server as a personal plugin and invoke it in **Work**. Read and write tools are supported. Workspace policy/Lockdown can block installation. |
| One narrow submission action? | **Yes:** a streamable HTTP MCP server can advertise just `submit_market_event`. This implementation has exactly that one tool; no resources or prompts. |
| Authentication/secrets? | ChatGPT MCP supports OAuth authorization-code + PKCE, with predefined clients, CIMD or DCR. The ChatGPT-hosted MCP client cannot supply a custom API key, machine-to-machine grant or customer mTLS certificate. OAuth access/refresh credentials belong to the host connection; gateway service credentials live in Railway variables. |
| Latency/retries? | No fixed Work discovery cadence, delivery SLA, MCP timeout or automatic retry count was established. Model/task/tool scheduling adds variable latency. This server is stateless and acknowledgements are synchronous. After ambiguous results, retry the **identical** payload with the same submission ID, then stop after bounded attempts. |
| Synchronous acknowledgement? | **Yes:** normal MCP tool results return to the calling workflow. The response confirms durable receipt, not completed DB ingestion. This was exercised with a signed JWT and real MCP initialize/list/call exchanges in tests. |
| Outbound constraints? | Direct code/shell networking has separate workspace and user destination controls. MCP uses a connected HTTPS endpoint (or Secure MCP Tunnel), OAuth and workspace permissions. No claim is made about unrestricted outbound methods/domains or this account's actual approval settings. This gateway additionally limits JSON bodies to 32 KiB and advertised payload fields. |

Evidence: [Work cloud security](https://learn.chatgpt.com/docs/enterprise/chatgpt-work-cloud-security),
[Work overview](https://learn.chatgpt.com/docs/enterprise/chatgpt-work-overview),
[Work plugin quickstart](https://developers.openai.com/plugins/quickstart),
[custom MCP connection](https://developers.openai.com/api/docs/guides/custom-mcp-server),
[plugin authentication](https://developers.openai.com/plugins/build/auth),
[MCP tool results](https://developers.openai.com/plugins/concepts/mcp-server).

**Account-side evidence boundary:** this session has no callable capability to register a
custom Work connection or run a new Work task. The account's installation, OAuth linking,
write-confirmation behavior and a Work-origin production submission remain activation
checks, not completed tests. Do not describe the local harness as Work discovering news.

## Chosen delivery architecture

**Option B**, with the MCP action hosted inside the gateway, no relay:

```text
Work → OAuth-protected /mcp → submit_market_event(payload)
                              ↓
                        receipt clock + validation
                              ↓
                      immutable fsync'd receipt spool
                              ↓
                   five-second authoritative worker
                              ↓
                     existing Phase 23 ledger
```

A trusted non-Work service can use `POST /context/v1/events` with a scoped service bearer
credential or the same allowlisted OAuth identity. Both paths invoke the same validator
and spool. Service bearer credentials are deliberately rejected on `/mcp`.

Option B wins even where arbitrary HTTP is possible: credentials stay with a connected
OAuth identity and the model gets one structured capability. It does not get Railway,
remote shell, the normal Prism CLI, DB queries or a general API. No additional Railway
service, message broker or external relay is needed.

## Exact API/action contract

The machine-readable action contract is
[`config/context/gateway/submit_market_event.schema.json`](../config/context/gateway/submit_market_event.schema.json).
Generate/inspect it with `market context gateway contract --json`.
The schema's `$defs` live at the tool root so JSON Schema references resolve correctly.

This is a **single-item transport envelope extending `context_import_v1`**. Its `item`
inherits Phase 23 `ImportItem`; existing file imports and batch envelopes are unchanged.
The authenticated connection supplies the producer identity; callers cannot select a
trusted official source tier.

```json
{
  "schema": "context_import_v1",
  "submission_id": "one-stable-random-id",
  "external_event_id": "publisher-event-id",
  "sent_at": "2026-10-09T12:00:05Z",
  "sender_version": "work_monitor_v1",
  "test": false,
  "item": {
    "first_seen_at": "2026-10-09T12:00:04Z",
    "published_at": "2026-10-09T12:00:00Z",
    "event_time": "2026-10-09T11:59:00Z",
    "source": "Publisher name",
    "source_type": "ai_monitor",
    "url": "https://publisher.example/event",
    "title": "Short factual headline",
    "summary": "Source-attributed facts only.",
    "category": "protocol",
    "subcategory": "protocol_upgrade",
    "assets": ["ETH"],
    "entities": ["ethereum_foundation"],
    "market_wide": false,
    "country": "US",
    "region": "North America",
    "confidence": "REPORTED",
    "factual_claims": ["The source published an upgrade announcement."],
    "provenance": ["Original announcement, retrieved by Work"],
    "corroborating_urls": [],
    "attributes": {"kind": "generic", "facts": {}}
  }
}
```

The example is illustrative and timestamp-bound: **never send it as live news**.
`item.first_seen_at` retains the legacy import name and means **sender discovery time**.
It becomes `reported_first_seen_at`, never authoritative context `first_seen_at`.
Sender identity is `auth_identity`, derived from the verified connection; version is
`sender_version`. Publisher identity and provider identity remain separate.
`submission_id`, `external_event_id` and `sender_version` are 1–128 characters matching
`^[A-Za-z0-9_.:-]{1,128}$`. A GitHub path containing `/` is not a valid identifier; use
e.g. `geth-release-407043450` and place the complete source path in `item.url`.
`external_event_id`, factual claims, provenance and corroborating URLs are retained in
the immutable receipt. Main source URL, source-reported summary, generic scalar facts,
country and region enter the context observation. Extra claims/URLs do not automatically
become independent trusted provider observations.

Initial confidence accepts **UNCONFIRMED or REPORTED only**. OFFICIAL, CONFIRMED and DENIED
are rejected; existing independent official providers can subsequently raise/deny
confidence using Phase 23's append-only corroboration rules.

Bounds: 32 KiB total HTTP body (including MCP envelope); one item; 16 assets, 16 entities,
16 short claims, eight provenance entries and eight corroborating URLs. Source URLs must
be public HTTPS syntax, without credentials, private IPs, nonstandard ports or fragments.
Generic facts are flat, <=32 keys and <=8,000 canonical characters. Unknown fields,
unknown taxonomy, unknown/ambiguous entities/assets, duplicate JSON keys, nonfinite numbers,
naive/invalid/future observation/publication/discovery timestamps and non-JSON flags reject.
Scheduled events can have future event times; unscheduled events cannot.

The gateway rejects direction, trade/order/position size, stop, target, sentiment,
recommendations, materiality overrides, code/shell/script fields, external update kinds,
forced dedup keys and relevance overrides. Interpretation is omitted in v1. Facts remain
**reported facts**, not independently verified truth.

HTTP result after durable acceptance: `202`; identical retry: `200`:

```json
{
  "status": "ACCEPTED",
  "receipt_id": "ctxgw_<64-hex-digest>",
  "gateway_received_at": "<Prism UTC receipt>",
  "spool_fsynced_at": "<Prism UTC durable receipt completion>",
  "logical_event_id": null
}
```

MCP returns the same object as structured content and readable text. `DUPLICATE` returns
the original receipt/times and, when already ingested, its logical event ID. Rejections
return `status=REJECTED` and a predefined error code. HTTP uses 400/401/413/422/429/409/503
for malformed input/auth/size/schema/rate/conflict-or-stale/storage failures. There is no
public receipt-read or DB endpoint.

## Authentication, rotation and replay

Work primary route: existing OAuth 2.1 authorization server, `context:submit` scope,
RS256 signed JWTs, exact gateway `/mcp` audience, issuer/JWKS pins, required exp/iat/sub,
and an explicit **subject + OAuth client ID** allowlist. JWT algorithms are not chosen
from attacker input. JWKS refresh supports issuer key rotation; use short token lifetimes
(e.g. 15 minutes). Removing a principal and restarting revokes its gateway access even
if its token has not expired. IdP refresh/revocation policy remains external.

Optional service route: >=32-character cryptographically random bearer tokens from
`PRISM_CONTEXT_GATEWAY_TOKENS`, mapping token → stable integration ID. Verification
compares SHA-256 digests with `hmac.compare_digest`. Overlap old/new keys for rotation,
map both to the same identity, restart, update the trusted client, then remove the old key.
Never place secrets in source, action arguments, shell command arguments, logs or docs.
No credentials appear in receipts or returned acknowledgements.

Authentication precedes JSON processing; HTTP requires TLS except explicitly loopback-only
`--dev`, which is forbidden on Railway. Authenticated new requests must have `sent_at`
within five minutes of receipt. Persisted `(integration ID, submission ID)` gives a stable
receipt ID. Identical accepted retries remain valid outside the window; changed payload
with the same key returns conflict. No HMAC nonce exchange is required for this bearer/TLS
transport. HTTPS protects the authenticated request; persisted receipt identity provides
idempotent replay suppression.

Default limit: ten accepted/retry submissions per identity per minute, persisted in the
spool audit across restart. Global pre-auth cap: 120 HTTP requests per minute;
body upload deadline five seconds, HTTP concurrency 32. Global abuse state is in memory
and resets on restart; authenticated submission state persists. Low-volume legitimate
retries count toward rate limits. All responses contain sanitized codes, never raw
validation echoes or tokens. Access logging is disabled for the public server.

## Authoritative timestamps and latency

| Timestamp | Authority/use |
|---|---|
| `event_time` | Sender/source-described occurrence or schedule; never availability. |
| `published_at` | Sender-claimed source publication time; not independently verified here. |
| `item.first_seen_at` / `reported_first_seen_at` | Sender discovery time; provenance only. |
| `sent_at` | Sender dispatch time; freshness window only. |
| `gateway_received_at` | Prism clock at HTTP request arrival, before auth/body processing. |
| `spool_fsynced_at` | Prism clock after receipt file and directory fsync; timing seal is itself fsync'd before acknowledgement. |
| `ingest_started_at` | Prism worker starts processing receipt. |
| `ingest_recorded_at` | Prism stamps the transactional DB receipt before commit. |
| `context_available_at` | Prism clock immediately after commit; durable completion marker records it. |
| context `first_seen_at` | Original new event receives gateway receipt time. A previously known event keeps its immutable original first_seen/source. |

`gateway inspect` derives source→discovery, discovery→gateway, gateway→durable,
durable→available, gateway→available and source→available seconds. Source latency anchors
on publication where present, otherwise unscheduled occurrence. Scheduled future event
times are not information-latency anchors. Missing anchors remain null. Sender/source
clock values can be inaccurate; the receiver/ingester clocks are server controlled.
Each server stage floors its clock at the previous stage so a host clock back-step cannot
backdate durability/availability or produce negative internal latency. Explicit after-ingest
snapshot checks use the recorded availability time if the host wall clock steps backwards.

After a crash between DB commit and completion-file publication, recovery records a
**conservative availability upper bound**, marked `recovered_after_commit=true`, rather
than pretending to know the exact lost post-commit timestamp. Preserve that distinction
in analyses. A snapshot taken during a backlog cannot yet consume the pending spool;
a later reconstruction uses the ledger's truthful receipt time. Existing immutable
first-source/first-seen semantics are not retroactively rewritten by a delayed report.

Targets: durable acknowledgement <1 s typical; gateway→context <30 s p95 when the runtime
lock is available. Worker attempts every five seconds; lock waits are measured, not
hidden. Shared research/backups can hold the lock longer than 30 s. This is an initial
engineering target, not an unconditional production SLA.

## Durable spool and authoritative ingest

Default persistent root `/data/context_gateway`:

- `receipts/ctxgw_*.json`: immutable original payload, canonical SHA-256, normalized
  observation, receipt time, provider, authenticated integration and entity-map version.
- `seals/`: immutable timing seals after receipt fsync.
- `audit.jsonl`: append-only accept/duplicate/reject reason codes, no payloads or credentials.
- `attempts/`: immutable retry/error records with exception **type**, not sensitive text.
- `done/`: immutable ingest result, event reference, timestamps or `TEST_EXCLUDED`.
- `server.json`, `worker.json`, `watchdog.json`: replaceable liveness/operational state.

Creation writes a private temporary file, flushes/fsyncs it, atomically links an immutable
final path and fsyncs its directory. Receipt and seal durability precede success. A
process death before response may leave a valid receipt: identical retry recovers it.
A retry re-fsyncs existing receipt, timing seal and their directories before acknowledging.
Unterminated audit tails
are repaired; incomplete private temporary files never count as receipts.

The HTTP process never imports `Store`, the CLI, paper, strategy, execution or risk code.
Dedicated entrypoint: `python -m market_signal.context.gateway.launch`.
Worker entrypoint: `python -m market_signal.context.gateway.worker`.
It acquires the same runtime flock with **no waiting**, verifies authoritative role and
existing DB, opens DuckDB with zero lock timeout, and drains up to 50 oldest pending receipts.
Migration 26 adds `context_gateway_ingests` only. Ledger writes, provider run and DB receipt
commit together. Ledger's per-observation transaction participates in that outer transaction;
there is no second independent commit. A completion file is written after DB commit.

DB failure rolls back the entire receipt and leaves it pending. Crash after commit reads
the existing DB receipt and publishes completion without repeating an observation.
Restart/deploy reuses the `/data` files and catches up automatically. No acknowledged
receipt is deleted or pruned. Default total spool cap 128 MiB / minimum free disk 16 MiB;
new requests cleanly fail when full. Set `PRISM_CONTEXT_SPOOL_MAX_BYTES` for retention needs.
Keep this root on the persistent volume. DB-only backups do not cover receipt files;
include the volume/spool in disaster-recovery backups. Volume destruction or physical disk
loss is outside the restart durability guarantee.

## Deduplication, corroboration and AI trust boundary

Use Phase 23's existing source-URL, scheduled and entity/window matching, materiality
version, confidence caps, relevance windows and explicit entity links. No new semantic
LLM dedupe or materiality scoring. A source URL matching an existing SEC/Fed/exchange
observation appends provenance/corroboration to that logical event; the first source and
time remain unchanged. A different Work submission describing an unchanged observation
can become a ledger duplicate, while its separate transport receipt remains auditable.
Gateway ingestion opts into **corroboration-only updates for existing events**: the
new Work report is retained in the update's raw observation and receipt, but cannot
replace the existing headline, summary, attributes, event time or materiality category.
This prevents an unverified Work rewrite inheriting an existing OFFICIAL confidence.
Source/entity provenance can be appended. A gateway report cannot reactivate a DENIED event. Existing Phase 23 callers retain their default
update behavior; independent deterministic/official providers still evolve factual content.
Independent later corroboration/denial appends updates; historical states before the
update are preserved. Source URL/claims supplied by Work are not promoted into a trusted
source merely because the publisher's name sounds official.

Alias resolution is exact and explicit, never fuzzy: canonical BTC/ETH/HYPE, known
Bitcoin/XBT/Hyperliquid aliases, Coinbase, Ethereum Foundation, US Treasury and Federal
Reserve resolve only through `config/context/entities.yaml`. Unknowns reject; v1 has no
quarantine moderation system. Market-wide submissions may omit individual assets/entities.
Configured market-wide asset links are deterministic.

URL validation contains malformed/private/credentialed inputs but cannot prove that a
URL exists or that a reported claim is true. The gateway fetches no sender URL and cannot
perform SSRF or verify headlines. Unsupported claims remain unverified REPORTED information.
Interpretation is excluded. Existing text-based link resolution is retained. Materiality
comes solely from Phase 23's versioned deterministic rules.

Provider `chatgpt_work_v1` keeps sender version, integration ID, receipt counts, provider
runs and health. Descriptive quality reports count later independent corroboration/denial,
median time to corroboration, transport acceptance/duplicate rates and median latency.
These are receipt-level descriptive measurements, refreshed at most once per minute under
the runtime lock; duplicates can share a logical event. **None change trading confidence,
hypotheses, admission, sizing or paper policy.**

## Health and operations

```bash
market context gateway status --json
market context gateway receipts --json
market context gateway backlog --json
market context gateway inspect RECEIPT_ID --json
market context gateway health --check --json
market context gateway contract --json
market context gateway test --json
market context status --json
```

All gateway inspection commands use spool files, not DuckDB. Status includes running,
auth state, last accept/reject, accepted/duplicates/rejected 24h, backlog/oldest age, worker
liveness, last ingest, retained non-test p50/p95 gateway-to-context latency, attempts and
provider quality. `context status` adds only concise gateway liveness/auth/backlog fields.
Public `/healthz` returns only `{ "running": true }`; full status is local operator-only.

Opt-in minute watchdog is independent of the worker. It alerts through the existing
infrastructure alert mechanism only after an operational condition persists >=60 s,
once per episode: listener unavailable, ten auth/schema failures per minute, spool write
failure, backlog older than 60 s or stalled worker/ingest. Duplicates never alert.
If the whole service is down, use the existing external runtime heartbeat/monitoring;
an internal watchdog cannot alert from a stopped container.

## Railway topology and production activation

Read-only Railway audit: **one** `prism-runtime` service, running deployed revision
`dfa2ddef44fe`; no public/custom domain; `/data` persistent volume **5,000 MB**, approximately
**2,114 MB** used. These observations are deployment state at the spike, not guarantees
about later changes. Existing scheduler/collector occupy no HTTP port. Single replica
remains mandatory; there is no shared-volume cross-service assumption.

Railway supports HTTPS domains with an explicit target port and supplies `X-Forwarded-Proto=https`:
[domain routing](https://docs.railway.com/networking/domains/working-with-domains),
[edge constraints](https://docs.railway.com/networking/public-networking/specs-and-limits).
The public listener can therefore coexist in this service. There is no need for another
service/relay. Do not expose a TCP proxy, debug listener, directory listing or normal CLI.

Activation is deliberately a separate operator step:

1. **Merge/deploy reviewed code with the gateway off.** Run `deploy/railway/deploy.sh main`
   using the existing exact-commit deployment procedure. Confirm `market ops preflight`,
   deployed revision and existing Paper v2 run identity. Migration 26 is additive.
2. **Configure authentication before exposure.** Create/reuse an OAuth server (e.g. Auth0
   Free) with API identifier `https://GATEWAY_HOST/mcp`, RS256, scope `context:submit`, short
   access tokens, authorization code + S256 PKCE, and refresh policy. Use a predefined
   OAuth client to avoid dynamic-registration requirements. Configure the exact callback
   URI shown by ChatGPT's custom MCP connection. For Auth0, enable the **Resource Parameter
   Compatibility Profile**, so MCP's `resource` binds to the API audience
   ([Auth0 guidance](https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0)).
   Grant only the trusted user's submission scope. In Railway's variable/secret UI set
   `PRISM_CONTEXT_GATEWAY_URL`, `PRISM_CONTEXT_OAUTH_ISSUER`, `PRISM_CONTEXT_OAUTH_JWKS` and
   `PRISM_CONTEXT_OAUTH_PRINCIPALS={"SUBJECT|CLIENT_ID":"chatgpt_work_personal_v1"}`.
   If a service client is needed, generate a random token and put its mapping in
   `PRISM_CONTEXT_GATEWAY_TOKENS`; never give Work Railway or service-token credentials.
3. **Start the gateway and worker.** Set `PORT=8787`, `PRISM_CONTEXT_INGEST_SECONDS=5`,
   `PRISM_CONTEXT_GATEWAY=on`. Set `PRISM_CONTEXT_TRUSTED_PROXIES` to the verified immediate
   Railway ingress proxy addresses/CIDRs; default trusts loopback only and otherwise
   **fails closed** on HTTP. Do not blindly set `*` or trust client-supplied forwarding
   headers. Railway edge terminates TLS; verify the container-side peer trust before
   exposing traffic. Generate the Railway HTTPS domain targeting port 8787 only after
   auth is configured. Keep one replica and `/data/context_gateway` on the volume.
4. **Verify health/security.** `railway ssh --service prism-runtime -- market context gateway status --json`.
   Check public HTTPS `/healthz`, `401` for unauthenticated writes and MCP's OAuth discovery
   challenge, and `404` for `/`, `/docs`, `/db`, `/orders`, `/paper`, `/risk`. Verify OAuth
   audience, scope, principal allowlist and token rotation with MCP Inspector. The minimal
   public health route reports listener state; local status checks the worker/spool too.
5. **Configure Work.** ChatGPT web → Plugins → `+` → Add custom MCP server. Set
   `https://GATEWAY_HOST/mcp`, choose OAuth, enter predefined client credentials if used,
   complete linking and install the personal plugin. Inspect/rescan the tool inventory:
   exactly `submit_market_event`. Begin a Work chat with `@Prism Context Gateway`. Account
   permission/confirmation requirements must be checked here, not guessed from API behavior.
6. **Send one safe end-to-end event.** First call `submit_market_event` with `test=true`
   and a fresh payload generated by the development fixture. This exercises receipt,
   spool and authoritative transaction but **never creates active context**. Confirm
   `TEST_EXCLUDED` with `gateway inspect`. To verify production context corroboration,
   select a real already-known low-risk event with `market context events --asset BTC --json`
   / `market context event ID --json`, copy its actual source URL/facts/taxonomy, use a fresh
   submission/discovery time and `test=false`. Never invent an exploit, sale or market
   event. The original logical event/time should survive, with new append-only provenance.
   Optional trusted-service smoke test: set `PRISM_CONTEXT_TEST_TOKEN` securely, then
   `market context gateway test --url https://GATEWAY_HOST --json` (always sends `test=true`).
7. **Verify availability and measure latency.** `gateway inspect RECEIPT_ID --json` must
   show durable time, completion/event ID and latency. `market context snapshot BTC --json`
   must include the real event. For already-known corroboration its original first_seen
   remains earlier than the gateway receipt. The isolated new-event tests establish
   before/after receipt causality. Record Work task discovery time, gateway receipt,
   completion and source time; compute production p95 from real receipts before claiming
   the <30-second target is met in Work/Railway.

Rollback: remove HTTPS routing and set `PRISM_CONTEXT_GATEWAY=off`, redeploy. Retain the
spool and DB receipts; do not delete acknowledged submissions or rewrite context history.
No strategy/paper restart or experiment registration is required.

## Work task prompts

After installing the personal plugin, use this monitoring task (its scheduling and
permission behavior still require an account-side test):

> Monitor high-materiality crypto and macro developments using reliable primary or
> reputable secondary sources. When you identify a materially relevant factual event,
> call `submit_market_event` once with one `context_import_v1` payload. Use the action's
> schema, a stable random submission ID and publisher event/URL identity. Obtain actual
> current UTC for `sent_at`; record when you first found the report in `item.first_seen_at`.
> Include the source publication time where established, source URL, short factual
> summary, canonical affected assets/entities, provenance and confidence UNCONFIRMED
> or REPORTED. Use only Prism's advertised categories/subcategories and known aliases.
> Omit interpretation, sentiment and trading recommendations. Never submit an order,
> position size, direction, stop, target, risk change or strategy. Do not claim OFFICIAL.
> If submission times out or returns a transient error, retry the exact same payload
> and ID after 5, 15 and 30 seconds, then stop and report the unresolved delivery.
> Return the receipt ID and gateway receipt time. An acknowledgement means durable
> information receipt; Prism determines its relevance and eventual use.

First safe production smoke test in Work:

> Use @Prism Context Gateway to run one transport test. Call `submit_market_event` with
> `test=true`, a fresh stable submission ID, `external_event_id=transport_test`,
> `sender_version=work_demo_v1`, and actual current UTC for `sent_at` and discovery
> `item.first_seen_at`. Use source "Prism transport test", URL `https://example.com/`,
> asset BTC, entity bitcoin, category protocol, subcategory protocol_upgrade,
> confidence REPORTED, title "SYNTHETIC transport test, excluded from active context",
> and summary "This is a transport fixture, not a market event." Omit publication and
> event times. Report the returned receipt and times. Do not send any live-news fixture.

The operator then runs `gateway inspect RECEIPT_ID --json` until `TEST_EXCLUDED` appears.
For the context-snapshot portion, submit the real already-known event described in
activation step 6; it should append corroboration and retain its original first-seen time.
The isolated new-event demo proves the new-event before/after snapshot semantics.

## Measured demo and incremental cost

Reproduce the safe development demos:

```bash
market context gateway test --json
python scripts/context_gateway_benchmark.py
```

Both create only temporary databases/spools. The 2026-10-09 **real loopback HTTP** benchmark
ran 20 synthetic submissions through a separate HTTP server and the separate authoritative
five-second worker. It used the real runtime lock, DB authority guard and fsync path:

| Measurement | Result |
|---|---:|
| Durable acknowledgement p50 / p95 | 0.0600 / 0.1685 s |
| Gateway → context available p50 / p95 | 4.908 / 5.047 s |
| Gateway → context available max | 5.091 s |
| First receipt → fsync | 0.0826 s |
| First receipt → context available | 5.091 s |
| Synthetic source → context available | 7.091 s (includes synthetic two-second source/discovery gap) |
| Before-receipt snapshot excludes / after-ingest includes | true / true |
| New context first_seen equals gateway receipt | true |
| HTTP server / worker resident RAM | 152 / 199 MiB |
| Typical retained spool bytes/receipt | 3,082 bytes |

OAuth MCP was separately exercised end to end with a signed local issuer test, synchronous
structured receipt, identical retry, ingest and snapshot. **These are implementation-time
local engineering measurements.** Subsequent production Railway and genuine Work-origin
measurements are in [the production activation record](REALTIME_CONTEXT_GATEWAY_ACTIVATION.md).
An attempted benchmark during the full concurrent regression run hit its five-second
HTTP client timeout. The table reports the successful final rerun after that full suite
finished. Host I/O contention can exceed the target; production lock/I/O load and bounded
identical retries must be measured before claiming a deployment SLA.

At implementation completion, no new infrastructure had been activated and incremental
billed cost was **$0**. See the activation record for subsequent production measurements.
Implementation-time cost projection, using [Railway resource rates](https://docs.railway.com/pricing/plans):
351 MiB combined measured resident memory ≈ **$3.68/month** at $10/GB monthly RAM (decimal GB);
allow 0.01–0.03 average vCPU ≈ **$0.20–$0.60/month** (CPU usage is a planning assumption,
not a measured production average). Budget **$4–$5/month**, allowing watcher/SDK/DB overhead;
check the actual service metrics, page cache and existing plan credit before attributing
an invoice increase. No new base-plan fee or separate service is required. Public egress
at $0.05/GB is pennies at these receipt volumes; inbound event payloads are not egress.

MCP implementation has no OpenAI API calls or relay fee. OAuth can use an existing issuer
or [Auth0 Free](https://auth0.com/pricing), currently $0 at the personal-user scale, subject
to its feature/plan conditions. The personal plugin has no separate server/API invocation
charge in this code; account-side plugin/Work entitlement must be verified. Work discovery
usage remains plan/credit/task dependent, **not a fixed price per event**:
[Work usage and cost](https://learn.chatgpt.com/docs/enterprise/chatgpt-work-usage-and-cost).
A cheaper trusted-service-only fallback uses the bearer route without OAuth/Work, but it
is not a substitute for validating the chosen Work route.

Retain original receipts and append-only outcomes for at least one year where capacity
permits. At a conservative typical **4 KiB/receipt**, excluding DB/page overhead and abuse:

| Events/day | 30 days | 365 days |
|---:|---:|---:|
| 10 | 1.17 MiB | 14.3 MiB |
| 100 | 11.7 MiB | 143 MiB |
| 1,000 | 117 MiB | 1.39 GiB |

At the maximum 32 KiB payload, receipt+normalized-observation duplication can approach
~64 KiB/receipt: allow up to ~1.83 GiB/month at 1,000/day. This is not intended as a news
firehose. Adjust the explicit spool cap/volume for retention; nothing is silently pruned.
Typical 10–100/day storage is small compared with microstructure. The default cap will
reject new writes before unlimited retention fills the shared runtime volume.
Reject diagnostics and retry-attempt writes obey the same capacity/free-space bounds;
unauthenticated traffic cannot bypass them to fill the runtime volume. Completion markers
remain writable for already-durable receipts. When diagnostics cannot be persisted, reject
counters are incomplete and the listener heartbeat records a spool failure instead.
Durable receipt/seal acknowledgement and identical retries remain valid even if their
diagnostic audit cannot be appended; diagnostic failure never cancels a durable receipt.

## Verification and limitations

Phase 26A tests cover valid/invalid/rotated bearer auth; signed OAuth subject/client/scope/
audience/expiry/algorithm rejection; real MCP protocol/action/schema/result; server time;
retries/conflicts/stale replay; malformed/nonfinite/duplicate-key JSON; forbidden execution
fields; taxonomy/entity/asset/URL/payload/rate constraints; fsync-before-ack; subprocess death
after fsync before response; restart and partial diagnostic repair; TEST exclusion; atomic
rollback; death after DB commit; a real independent DuckDB lock and catch-up; runtime lock
safety; existing-official dedupe; later corroboration/denial; snapshot causality; and both
static and dynamic imports proving the public entrypoint cannot reach execution/paper/CLI/DB.

Implementation-branch verification completed on 2026-10-09 (before production hardening):

- Full pytest: **1,151 passed**, four warnings, **1,502.28 s**. Executed with four local
  pytest-xdist workers (`-o addopts='' -q -n 4 --dist loadfile`), including Phase 25A,
  Phase 24B/scheduling, Phase 24A, Phase 23 and runtime suites. pytest-xdist was installed
  only in the local test environment, not added as a production dependency.
- Final Phase 26A suite after transport hardening and advertised-contract tightening:
  **69 passed**, one warning, **26.47 s**.
- Ruff, formatting, shell syntax and `git diff --check`: passed. JSON action schema
  references and CLI JSON status/receipts/backlog/contract were checked successfully.

Starlette currently warns that its
httpx-based TestClient adapter is deprecated; this affects the test harness, not uvicorn or
production transport. Subsequent production hardening passed 70 Phase 26A tests and
1,141 full deployed-source regression tests. Production exposure, Railway restart and
genuine Work delivery passed the activation checks recorded in
[the production activation report](REALTIME_CONTEXT_GATEWAY_ACTIVATION.md). The earlier
local crash/restart tests remain separate from the measured Railway verification.

Limitations deliberately retained: no source URL fetching/verification, interpretation,
moderation UI, dynamic taxonomy, guaranteed Work retry/schedule, guaranteed lock-free p95,
public DB/status/receipt access, direct execution, or independent replicated spool storage.
The same-process filesystem scan is acceptable at this personal-discovery rate; larger
retention/source volumes should first be measured, not optimized speculatively.

## Exactly one next source-expansion phase

After a genuine Work/Railway submission and production latency measurements prove the
transport, **Phase 26B: a deterministic, read-only government-labelled BTC transfer watcher**
using explicit versioned address labels and on-chain transaction IDs. It would provide
independent factual movement evidence that Work coverage can corroborate. It must reuse
this transport/ledger and preserve the execution boundary. It is **not implemented here**.
