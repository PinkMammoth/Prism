# Phase 26A production activation — 2026-10-09

## Completed production activation

The HTTPS gateway and five-second authoritative worker are active in the existing
`prism-runtime` Railway production service. Durable transport, context ingestion,
snapshot knowledge time, corroboration, targeted crash recovery, full Railway restart,
public OAuth MCP discovery and **genuine personal Work-origin submissions are verified**.
No manual activation action remains. Phase 26B was not started.

Auth0 OAuth is restricted to the verified Google account and the predefined Prism
client. Authenticated MCP `initialize` and `tools/list` returned 200 and exposed exactly
one information write tool, `submit_market_event` (idempotent, non-destructive).
Missing/invalid OAuth and the valid REST service credential returned 401 on `/mcp`.

The account owner created the Auth0 tenant and approved CLI access. Sol created the
RS256 API with only `context:submit`, a public PKCE client, and a strict subject-plus-client
allowlist after verifying the owner's signed Google identity. The temporary device
grant was removed; only authorization-code and refresh-token grants remain. No client
secret is needed by Work. Management credentials were never given to the gateway or Work.

Personal Work connection configuration, completed by the account owner:

- Name: **Prism Context Gateway**.
- MCP URL: `https://prism-runtime-production.up.railway.app/mcp`.
- Authentication: OAuth, predefined client **`8ldi21f0ajUqhQUqLvy7g5tbS7a0GfI6`**,
  blank client secret, same verified Google account, `context:submit` permission.
- Registered callback: `https://chatgpt.com/connector_platform_oauth_redirect`.
- Issuer metadata advertises S256 and RFC 9207; resource-parameter profile is
  `compatibility`. The successful Work authorization-code exchange at
  **2026-10-09T18:03:01.699Z** matched the exact allowlisted subject/client and MCP audience.

The CLI session did not expose the installed personal plugin or a Work-task creation
capability. The user initiated the two tool calls in a Work chat. Sol verified the
resulting production receipts and snapshots directly; no files were copied/imported.
The receipts use `chatgpt_work_personal_v1`, distinct from `prism_activation_cli_v1`.
Railway HTTP logs corroborate OpenAI MCP requests (`openai-mcp/1.0.0 (Codex)`); that
user-agent is descriptive evidence, while authorization comes from the verified JWT.

Operator checks:

```sh
railway ssh --service prism-runtime --environment production -- 'market context gateway receipts --json'
railway ssh --service prism-runtime --environment production -- 'market context gateway inspect RECEIPT_ID --json'
railway ssh --service prism-runtime --environment production -- 'market context snapshot ETH --json'
```

## Revision, deployment and activation time

The changes from `72ca05a` were cherry-picked onto current `main` as `9821e07`, without
its unrelated dashboard parent. Deployment safeguards followed in `9c1f721`; gateway
process start timestamps in `57adf87`; the optional `test` ingest default fix in `4294c0e`.
Deployed source revision: `4294c0ee9b8f985e00d9d98a6d1f2ee89a61dc82`.
Original deployment ID: `1458a0d8-29a1-409a-b6ac-38bdd9be1f34`.
OAuth configuration redeployment: `daea9ea9-1873-43aa-9e8e-424d8f4aa63d` (SUCCESS).
Image: `6a8b43e0c8bfa6987359e3982d9266720bdd624b72cca1e407d1b17b20f807ee`.

The first gateway deployment was off. The initial enabled listener started at
**2026-10-09T16:53:02.036729Z**; rotating Railway ingress peers then required correction
of the initial single-peer trust configuration. The fully configured deployment's
server-authoritative start was **2026-10-09T17:01:35.947394Z**. Its configuration was
verified and durably recorded at **2026-10-09T17:12:32.444813Z** in
`/data/context_gateway/activation_20261009T170135.json`. After the targeted recovery
test, the listener restarted at **2026-10-09T17:13:01.915550Z**.
OAuth-enabled production activation: **2026-10-09T17:57:41.238775Z**; verified and
durably recorded at **2026-10-09T17:58:52.260342Z** in
`/data/context_gateway/activation_oauth_20261009T175852.json`. The code revision
remained unchanged during this Railway redeployment.
Runtime deployment audits: original `rtev_723c5b38d687421398372d1dc109ef59`;
OAuth activation `rtev_9e8587f233b04ccfabe42245ac5058f5`.
Work end-to-end verification completed at **2026-10-09T18:14:07.837434Z**; audit
verified at **2026-10-09T18:16:18.587822Z**, durably stored in
`/data/context_gateway/activation_work_20261009T181618.json`.

Railway project `3c479387-0ddf-40a5-b065-52e49cf3e2cd`, environment `production`, service
`f7f02159-bcaa-423b-8db1-ee60342724e8`, one authoritative replica, existing `/data` volume.
Pre-deploy backup:
`/data/backups/manual/prism-20261009T170032Z-pre-deploy-4294c0ee9b8f.duckdb`,
267923456 bytes, SHA-256
`2d8c7acb85f47aa94688a5bb03ad7dfff417405343ab141406da99a2433dee26`.

An initial unlinked archived checkout caused Railway CLI to create an unintended
temporary project. Its deletion was requested and its deployment removed; no active
deployment remained. The deploy helper now pins the project and environment on every
SSH/upload operation. Subsequent deployments targeted the existing Prism service.

## Exact non-secret configuration

| Setting | Production value |
|---|---|
| `PRISM_CONTEXT_GATEWAY` | `on` |
| `PORT` / Railway public target port | `8787` |
| `PRISM_CONTEXT_GATEWAY_URL` | `https://prism-runtime-production.up.railway.app` |
| `PRISM_CONTEXT_GATEWAY_DIR` | `/data/context_gateway` |
| `PRISM_CONTEXT_INGEST_SECONDS` | `5` |
| `PRISM_CONTEXT_TRUSTED_PROXIES` | `127.0.0.1,100.64.0.0/24` |
| `PRISM_CONTEXT_GATEWAY_TOKENS` | Configured, random service credential; value omitted |
| Service integration identity | `prism_activation_cli_v1` |
| `PRISM_CONTEXT_OAUTH_ISSUER` | `https://dev-spoapkyacre0gg6s.us.auth0.com/` |
| `PRISM_CONTEXT_OAUTH_JWKS` | `https://dev-spoapkyacre0gg6s.us.auth0.com/.well-known/jwks.json` |
| `PRISM_CONTEXT_OAUTH_PRINCIPALS` | One exact verified Google subject + predefined client; subject omitted |
| OAuth integration identity | `chatgpt_work_personal_v1` |
| OAuth audience | `https://prism-runtime-production.up.railway.app/mcp` |
| OAuth signature / scope / token lifetime | RS256 / `context:submit` / 3600 seconds |
| OAuth client ID / type | `8ldi21f0ajUqhQUqLvy7g5tbS7a0GfI6` / public native PKCE, no secret |
| Rate / payload / spool limits | Defaults: 10 accepted/retry submissions/minute/identity; 32 KiB; 128 MiB |
| Runtime role / ID | `authoritative` / `railway-prism-runtime` |

HTTPS samples showed rotating Railway ingress peers in `100.64.0.0/24`; forwarded
protocol headers were overwritten by the edge during a spoofing probe. Private service
network peers were observed in `10.128.0.0/9`, outside the trusted range. The trust setting
is confined to the observed ingress subnet rather than all private/CGNAT ranges or `*`.
A future ingress network change fails closed and requires operational review. These
are production observations, not an undocumented guarantee of permanently fixed peers.

## Measured CLI production transport and knowledge time

The following initial submissions originated from the CLI with its dedicated service
identity. The later genuine Work measurements are recorded separately below. No
synthetic event entered the Phase 23 event ledger.

### Synthetic smoke receipt

Receipt: `ctxgw_63afad85ab409332de781093090bdb5a4804a6bb66b2a95dfa65349c7b9fdf4b`.

| Timestamp / duration | Measured value |
|---|---|
| Gateway receipt | `2026-10-09T17:02:36.688764Z` |
| Durable spool | `2026-10-09T17:02:36.709997Z` |
| HTTP durable acknowledgement round trip | 0.062522 seconds |
| Worker completion | `2026-10-09T17:03:06.724073Z` |
| Receipt → completion | 30.035309 seconds |

Result `TEST_EXCLUDED`, no logical event. An identical retry returned the same receipt
and timestamps. The worker's configured interval was confirmed as five seconds. The
initial 30-second completion is retained as measured; the worker never bypasses runtime
locks. The later real-event measurements below establish normal observed latency, not
a production SLA or a guarantee that contention can never exceed 30 seconds.

### Real-event context/snapshot verification

Primary source: [official Geth v1.17.8 release](https://github.com/ethereum/go-ethereum/releases/tag/v1.17.8),
published **2026-10-08T16:35:36Z**. The factual candidate was explicitly labelled
transport verification, `REPORTED`, `protocol/protocol_upgrade`, canonical ETH/ethereum,
with no trading recommendation or materiality override. Deterministic materiality was
31 (low severity).

Receipt: `ctxgw_bdd7dab05ce7a483d79641480dec24c6b00e7d428729eb3cd4f28bc8c53e7872`.
Logical event: `ctxev_052577876d84c9053c5acfc2c8c6de093dc6980710ff8c37fc940a123992688a`.

| Timestamp / duration | Measured value |
|---|---|
| CLI-reported send/discovery | `2026-10-09T17:11:06.138552Z` |
| Gateway receipt / authoritative `first_seen_at` | `2026-10-09T17:11:07.001186Z` |
| Durable spool | `2026-10-09T17:11:07.014460Z` |
| Ingest started | `2026-10-09T17:11:09.686853Z` |
| Ingest recorded | `2026-10-09T17:11:09.730806Z` |
| Context available, post-commit | `2026-10-09T17:11:09.741917Z` |
| HTTP durable acknowledgement round trip | 0.123732 seconds |
| Gateway → durable | 0.013274 seconds |
| Gateway → context | **2.740731 seconds** |
| Source publication → context | 88533.741917 seconds |

The sender timestamp is a payload claim, not authoritative timing. The safe verification
used an already published release; source-to-context latency therefore includes its
age and must not be described as fresh Work discovery latency.

`market context snapshot ETH` queried at `2026-10-09T17:11:07.001185Z` excluded the
event. Queried at `2026-10-09T17:11:09.741917Z`, it included the event, with exact
server-stamped `first_seen_at`. A snapshot captured before submission also excluded it.

A separate historical [Bitcoin Core 31.1 release](https://github.com/bitcoin/bitcoin/releases/tag/v31.1)
verification entered the ledger in 1.312185 seconds. Receipt
`ctxgw_bf5f13a1f48ec55421678686b7ee170a629b8e2ab74ace5c0969c7216f8ed11f`, logical event
`ctxev_6d5bb3902a0423159cef39177cbf179073914d0bde5ea7349c44db053c367924`.
Its July event date is correctly outside the existing snapshot's 30-day event-time
filter, so it is retained for audit but excluded from normal snapshots. That rule was
preserved; the recent Geth release supplied the positive snapshot check.

### Corroboration and original first source

A resolved, already-known Coinbase incident was resubmitted using its original URL:
`https://status.coinbase.com/incidents/wrw9dc78yfcn`. Receipt
`ctxgw_0dab18a8f44f36d6cf4a6617d848ee94b9b4375e7306e9f25578ed6b3204dfc0`.
The existing logical event
`ctxev_32c244611e9aace3b8bfe589ebdcee1981724bd6e636b0545bc2149116661a8a`
received one append-only update and **zero new logical events**, in 1.728604 seconds.
Original `first_seen_at` remained `2026-10-08T03:38:02.929325Z`, first source remained
`rss_coinbase_status`, `OFFICIAL` confidence remained intact, and `chatgpt_work_v1`
was added to its source history. This receipt's auth identity still records CLI origin.

## Genuine Work end-to-end verification

The synthetic `test=true` smoke call was accepted as receipt
`ctxgw_5c4fdb2ea6bfd311677ce368ee46394c07e14706e1a818f80f27527755edbd87`. Work reported sending at
`2026-10-09T18:08:12.900Z`; gateway receipt was
`2026-10-09T18:08:14.436357+00:00`; durable time was `2026-10-09T18:08:14.457575+00:00`;
worker completion was `2026-10-09T18:08:17.886182+00:00`.
It completed **TEST_EXCLUDED**, with no logical event or active context entry, in
**3.449825 seconds** from gateway receipt.
The completion field is called `context_available_at` in the receipt format; for a
TEST receipt it indicates completion only, never availability of a synthetic event.

The initial real candidate was rejected with `schema_rejected`; it created no durable
receipt or context event. The user supplied the rejected arguments from Work. Its
`external_event_id` was `github:ethereum/go-ethereum:release:v1.17.8`, containing `/`,
which fails the advertised `^[A-Za-z0-9_.:-]{1,128}$` identifier pattern. The failure was
reproduced locally as `external_event_id: string_pattern_mismatch`. Work did not retain
the original dynamically generated `sent_at`, so that value cannot be independently
reconstructed. No gateway schema or security rule was loosened. Sol validated a
canonical retry template locally; Work retried with a new submission ID/send timestamp.

The corrected real Work call was accepted. Submission ID
`91a389e0-1852-4b47-9c15-75a8b298bf6f`; sender version
`chatgpt_work_phase26a_verification_v1`; auth integration `chatgpt_work_personal_v1`.
It reported the real sourced Geth release above at `REPORTED`, with explicit transport
verification facts and no trading instructions.

Receipt: `ctxgw_657a432202b1f9dd66ccf6aa38eaaf9fd57327c5dda9dcc741964b71d734dc49`.
Logical event: `ctxev_052577876d84c9053c5acfc2c8c6de093dc6980710ff8c37fc940a123992688a`.

| Timestamp / duration | Measured value |
|---|---|
| Work-reported send/discovery | `2026-10-09T18:14:04.778Z` |
| Gateway receipt / new observation knowledge time | `2026-10-09T18:14:06.331268Z` |
| Durable spool | `2026-10-09T18:14:06.341960Z` |
| Ingest started | `2026-10-09T18:14:07.770306Z` |
| Ingest recorded | `2026-10-09T18:14:07.830489Z` |
| Context available, post-commit | `2026-10-09T18:14:07.837434Z` |
| Gateway → durable | **0.010692 seconds** |
| Gateway → context | **1.506166 seconds** |
| Work-reported send → context | **3.059434 seconds** |
| Source publication → receipt processing availability | 92311.837434 seconds |

The source-to-availability value includes the historical release's age, not fresh
real-time discovery. The sender clock is a claim; only gateway and later Prism clocks
are authoritative. This receipt appended **one corroboration, zero new logical events**.
Original event `first_seen_at` remained **2026-10-09T17:11:07.001186Z**. The acknowledgement's
`logical_event_id=null` was correct before authoritative ingest; the completion supplies
the resolved logical event ID.

The ETH snapshot after ingest includes the event. It was already known before the Work
receipt, so it also correctly appears in that earlier snapshot. Its **new Work
corroboration** is absent from the pre-receipt history and present post-ingest, with
`observed_at=2026-10-09T18:14:06.331268Z`, sequence 1, confidence `REPORTED`.
The initial CLI new-event verification separately proved whole-event exclusion before
its first gateway receipt and inclusion afterward. Later Work corroboration did not
backdate that event or revise its original content.

## Failure/recovery and exposure checks

Only the gateway listener and gateway ingest-worker processes were restarted. The
worker was stopped while outside any DB transaction, under the existing runtime lock;
an automatic bounded resume guard was installed. A `test=true` submission was durably
acknowledged while the worker was paused. Both dedicated gateway processes were then
SIGKILLed, leaving the acknowledged receipt pending. Existing supervisors restarted
them, and the retry returned the original receipt and both original timestamps.

Recovery receipt: `ctxgw_fd33306e12fec39d1ea211a678d1d5bbf73a1b7cbbba73a8d467f322f2ae0aba`.
Receipt `2026-10-09T17:12:49.362171Z`; durable `17:12:49.370468Z`;
`TEST_EXCLUDED` completion `17:13:02.588057Z` (13.225886 seconds after receipt).
Final backlog zero, retry errors zero, spool failures zero, listener/worker healthy.
The later full Railway OAuth redeployment also preserved the recovery receipt and the
real Geth receipt, including their original receipt times and completed ingest records.

Public checks: `/healthz` 200; invalid/missing REST auth 401; missing OAuth and even
valid service auth on `/mcp` 401; `/`, `/docs`, `/orders`, `/risk` 404. Malformed JSON
400; unsupported schema, trade direction, unknown entity/category, self-declared
`OFFICIAL` 422; stale unseen submission 409. Diagnostics contain fixed reason codes.
No DB/query/debug/general API route was exposed.

## Health, cost and experiment boundary

Final observed status: running/worker running, `OAUTH_CONFIGURED`, **seven** accepted
receipts (three synthetic), two retries, zero backlog, no alerts, zero error attempts.
Observed real-receipt p50 1.728604 seconds and p95 2.740731 seconds are based on **four**
samples (three CLI, one genuine Work), not a statistically established SLA. Deliberate TLS/auth/schema negative
checks account for rejection counters; they are not ordinary production provider traffic.

Measured gateway RSS: listener 158636 KiB, worker 334100 KiB; combined PSS 396951 KiB.
Before the personal Work connection, spool including audit and initial activation
record was 31281 bytes; later Work receipts and audits add only small JSON records.
At [documented Railway resource rates](https://docs.railway.com/pricing/plans), combined RSS is approximately $5.05/month RAM,
plus an assumed $0.20–$0.60/month CPU; budget **$5–$7/month** pending actual billing
and existing-plan credits. No additional service/base subscription, relay or OpenAI API
calls were added. The newly created Auth0 tenant uses its Free plan; no paid feature was enabled. Work
usage remains part of the user's account/plan and no additional OpenAI API call was made.
Do not claim a measured invoice increase.

Paper v2 before/after: identical run
`paperrun_v2_3811cccc40db4fbe6413ebd018c129d1026748f3cd72e619ec80ba88e47ada95`,
activation `2026-10-09T10:19:06.981566Z`, `ACTIVE`, 28 hypotheses, engine healthy,
failure count zero. Phase 24B specifications, admission, risk, execution and hypothesis
definitions were not modified. Paper/collector processes were not killed during the
targeted recovery verification. The gateway listener has no execution/risk/paper path;
the worker remains the only gateway DB writer under the authoritative runtime lock.

External AI discovery is treated as an information source, never an execution authority.

Prism's authoritative knowledge time begins when Prism receives the event, not when an external model claims to have discovered it.

## Validation

Phase 26A targeted suite: **70 passed**. Full deployed-source regression: **1141 passed**,
including Phase 25A, Phase 24B, Phase 24A, Phase 23 context and runtime tests; one
Starlette deprecation warning. Ruff check, format check
(357 files) and `git diff --check` passed. Production acceptance/rejection, durable ACK,
retry idempotency, snapshot timing, existing-event corroboration, targeted process-crash
recovery and experiment identity checks passed as recorded above.

OAuth acceptance, public authenticated MCP tool listing, genuine Work smoke and real
event delivery, durable acknowledgement, authoritative ingest and point-in-time
context/corroboration checks all passed in production. No Phase 26B expansion was started.

Personal installation follows the [official OpenAI plugin quickstart](https://developers.openai.com/plugins/quickstart);
OAuth uses the [documented predefined-client PKCE contract](https://developers.openai.com/plugins/build/auth).
