# Lucrari backend: local code, not a cloud deployment

For the single-owner self-hosted ChatGPT mode, independent database-dispatched
worker and EC2 Compose deployment, see [personal backend](personal-backend.md).
The hosted API-key mode below still defaults to refusing anonymous sessions;
the personal mode accepts only the explicitly configured owner's verified session.

This extends the phase-1 engine. It does **not** change the upstream interactive
CLI, agent prompts/topology, FinBot price jobs, portfolios, FIFO or investment reset.
Canonical migrations are maintained in **FinBot** under `supabase/migrations`.
Copies and installation prerequisites are bundled in
[deploy/migrations](../deploy/migrations/README.md). This backend depends on the
FinBot instrument catalog; the SQL copies do not install that catalog.

## Implemented boundaries

```
Supabase user session -> FastAPI control plane -> atomic DB admission
    -> Dispatcher.start(analysis_id) -> isolated worker supervisor -> graph child
    -> private Storage JSON + final job/decision DB transaction

Scheduled evaluator -> completed US market sessions + adjusted prices
    -> atomic, once-only outcome -> next analysis's Portfolio Manager context
```

No SQS or implicit queue. A fourth live reservation receives HTTP 429
`CAPACITY_FULL`. No request is accepted if a dispatcher is not configured.
The supplied dispatcher launches **local subprocesses only**; a future AWS adapter
must start Step Functions Standard/Fargate using the *same analysis ID*, not
resubmit a new request. It must not retry an entire graph after an uncertain
completion. AWS resources, launch implementation and schedules are not deployed.

## API v1

All `/v1` routes require `Authorization: Bearer <Supabase access token>`.
The configured project's Auth `/auth/v1/user` verifies sessions online. No
decode-only JWT trust, refresh-token acceptance or client-supplied identity.
Anonymous Supabase sessions are rejected for this cost-bearing feature: users
must sign in with a non-anonymous account. Existing portfolio auth is unchanged.

| Method/path | Request | Response |
|---|---|---|
| GET `/healthz` | none | liveness only, no DB details |
| GET `/v1/providers` | none | operator-allowed providers/models and unavailable reasons |
| GET `/v1/connections` | none | owned active IDs, provider/status/creation/verification timestamps |
| POST `/v1/connections` | `provider`, `api_key` | ID/provider/status; verifies with GET models, never a completion |
| POST `/v1/connections/{id}/verify` | none | ID/valid; ownership checked before decryption |
| DELETE `/v1/connections/{id}` | none | idempotent `revoked`; wipes stored ciphertext |
| POST `/v1/analyses` | see below; `Idempotency-Key: UUID` header | 202: job metadata and `replayed` |
| GET `/v1/analyses?limit=20&offset=0` | 1–100 limit; 0–10000 offset | items + total_count, deterministic newest-first order |
| GET `/v1/analyses/{id}` | none | owned job metadata/stage |
| GET `/v1/analyses/{id}/report` | none | owned, integrity-checked version-1 12-section report |

Start body:
```json
{
  "connection_id": "a-user-owned-connection-uuid",
  "quick_model": "an-operator-allowed-model",
  "deep_model": "an-operator-allowed-model",
  "request": {"ticker": "NVDA", "analysis_date": "2026-10-19", "profile": "standard", "language": "tr"}
}
```

Extra properties (user_id, URL, credentials, memory, arbitrary graph config) are
rejected. Both model tiers use this one owned connection; cross-provider tier
mixing is deliberately not exposed yet. Models come from an explicit trusted
allowlist, **not** an assertion that all listed models are enabled for every key.
The actual provider can still return an access/model error. No billing fallback.
The first hosted scope is active `NASDAQ100` stock rows in FinBot `instruments`.
Other markets/funds/crypto require suitable calendars/benchmarks before admission.
The upstream local CLI retains its wider capabilities.

Public job fields: `id, status, stage, created_at, started_at, finished_at,
error_code, ticker, analysis_date`. Status is reserved/running/success/partial/
failed/timed_out. Stage is accepted/analysis/persisting/finished (coarse progress,
not invented agent percentages). No provider responses, credentials, object paths
or owner UUIDs are included. Errors use `{error:{code,retryable}}`, never raw SQL
or SDK text. All responses are no-store; request bodies are bounded to 16 KiB.

## Provider and identity isolation

The existing provider factory is reused. API-key hosting supports OpenAI,
Anthropic, Google and the upstream fixed-endpoint xAI/DeepSeek/Qwen/GLM/MiniMax/
OpenRouter/Mistral/Kimi/Groq/NVIDIA adapters, including separate regional keys.
Only operator-enabled providers appear as usable. Verification requires a
successful model-list endpoint; vendors returning 404/405 are explicitly
`PROVIDER_VERIFICATION_UNSUPPORTED`, not falsely marked verified.

ChatGPT plan sign-in is **not silently converted into a shared server account**.
The upstream CLI's OAuth is unchanged. Hosted ChatGPT linking remains disabled
with `HOSTED_OAUTH_APPROVAL_REQUIRED` pending hosted-app approval and a registered
per-user redirect/client integration. Do not upload a local auth JSON to this API.
See [OpenAI plan-usage scope](https://developers.openai.com/siwc/token-sharing-open-source).
Azure, Bedrock, Ollama and arbitrary compatible URLs are also unavailable in this
hosted adapter until operator-controlled endpoint/IAM routing exists. The local
CLI still supports them. There is no unvalidated URL field/SSRF escape hatch.

Secrets use AES-256-GCM with random nonce and associated data binding user,
connection and provider. Versioned keys stay in **external backend secrets**, never
in Supabase alongside the ciphertext. Keep old key versions while old ciphertext
exists. New writes use the active version; removing an old key prematurely makes
those connections unreadable. Back up keys separately from the DB. API keys are
never returned. Revocation prevents new analyses/child starts, but does not recall
a key already loaded into a running model request; revoke at the provider too for
urgent compromise. Each graph runs in a separate process; inherited provider keys,
endpoint overrides and tracing keys are cleared before installing its own key.

## Database and Storage

Private `analysis_backend` schema:
- `connections`: owner/provider, encrypted secret, active/revoked timestamps.
- `jobs`: owner, request key/hash, frozen input/model settings, lifecycle, lease,
  hard deadline, safe error code, private object reference/hash.
- `memory`: decision and once-only outcome, point-in-time context fields,
  evaluation lease/attempts/next-at/reason.
- `rate_limits`: bounded per-user/action counters (no token or request contents).

All tables have RLS; PUBLIC/anon/authenticated have **no schema/table grants**.
The NOLOGIN/non-BYPASSRLS `lucrari_analysis_backend` role is for trusted backend
connections only. Every repository transaction explicitly sets that role and
filters the owner where applicable. It can read the US stock catalog, not mutate
it. Backend evaluation/reaping are internal commands, not user HTTP endpoints.
For orphan discovery it has read-only Storage metadata access, constrained to
the analysis bucket by permissive **and restrictive** RLS policies. Metadata is
never deleted directly; object deletion always uses the Storage API.

Provision a dedicated DB login with membership in that role for deployment;
do not give its password or the Supabase Storage service key to the mobile app.
Production DB transport must use verified TLS and the intended Supabase host.
The supplied local tests do not provision production logins or use remote keys.

Private `analysis-reports` bucket: JSON only, max 2 MiB; backend-mediated reads,
no public URL or client Storage grants. Object key is user/job/lease-token.json.
The API validates ownership and SHA-256 before returning the typed report. Partial
reports remain readable but don't create new decision lessons. Investment reset
does not touch any of these records. Deleting the auth user cascades DB rows;
Storage objects are cleaned separately by the internal orphan cleanup command.

Storage and PostgreSQL cannot commit atomically together. Worker uploads first,
then commits terminal job + decision together. On failure it attempts deletion of
only its own unreferenced upload. A crash or ambiguous Storage timeout can leave
an orphan; the bounded `cleanup` command identifies unreferenced objects older
than 24 hours. It defaults to dry-run; `cleanup --apply` explicitly deletes only
verified orphans. It also removes old objects after auth-user DB cascades. No
automatic expiry of still-referenced reports is imposed. Schedule it only after
reviewing retention and obtaining deployment approval. No Storage failure is
converted into successful completion, and uncertain DB commits are checked before
attempting upload deletion.

## Admission, failure and scheduling

Atomic global admission uses a short advisory transaction lock, counting reserved
and running jobs. Replays return the original record before launching; conflicting
payloads under the same key fail 409. Same key never re-executes failed jobs: a
deliberate retry requires a new key. Defaults: 10 accepted analyses/user/UTC day,
120 authenticated API requests/minute, 20 connection probes/24-hour window and
10 active connections/user. Configure API Gateway/WAF pre-auth limits later too.

Worker claims once, heartbeats every <=20 seconds, has a 120-second lease and a
35-minute absolute deadline (settings bounded to <=60 minutes). Expired workers
cannot finalize results or save lessons. Supervisor terminates its child on lost
lease/error/deadline; child watchdog reads leases without extending them, so a
dead supervisor doesn't leave an indefinitely active graph. Admission reaps stale
rows; also schedule `reap` every minute for unattended status cleanup. A cloud
adapter must stop/reconcile ECS tasks as well: three **DB reservations** alone
cannot guarantee three physical containers during a network partition/stop lag.
Provider SDK retries remain bounded; the graph itself is never auto-retried.

## Memory evaluation

Run `evaluate` on a daily backend schedule, not when users start analyses. It
claims due decisions using `FOR UPDATE SKIP LOCKED` and a five-minute evaluation
lease. It settles once or defers missing data for 12 hours, recording a safe code.
No automatic model training and **no extra summarization/reflection LLM call**.

NASDAQ calendar excludes holidays/weekends and today's uncompleted close (one-hour
close buffer). Entry is the adjusted close of the first complete session after
both the requested analysis date and the *actual* decision creation date in New
York. Exit is five trading-session intervals later (six closes). Thus a historical
request created today cannot pretend it predicted last month's movement. Stock
and SPY must have finite positive adjusted closes on **every** required session.
Missing/delisted/suspended data is deferred, not replaced with zeros or neighboring
dates. Returns are observations, not an actual executable backtest or realized P/L.

The stored lesson explicitly records returns, benchmark-relative performance and
the prior bullish/bearish/neutral view, with no invented causal explanation. A
five-session observation cannot prove a six-month thesis. Only resolved lessons
owned by the same user and known by the analysis cutoff reach the final Portfolio
Manager (five same-ticker + three cross-ticker, bounded context). New decisions
and outcomes cannot be supplied by the mobile client.

## Local configuration and verification

Install: `pip install -c constraints/worker.txt '.[backend,dev]'`.
Provide **server-only** `LUCRARI_BACKEND_SETTINGS` JSON via secure environment:
`database_url`, `supabase_url`, `supabase_publishable_key`,
`supabase_service_key`, `encryption_keys` (version -> base64 32-byte key),
`active_encryption_key`, `allowed_models` (provider -> allowed model IDs).
Generate your own keys; do not reuse test fixture keys. Do not commit env files.
Local HTTP Supabase is allowed only with explicit `allow_local_http=true` on
localhost/127.0.0.1/host.docker.internal. There is no default production DSN/key.

For local subprocess mode only, set `LUCRARI_DISPATCHER=local-process`, then run:
```
uvicorn tradingagents.service.backend.runtime:application --factory --host 127.0.0.1 --port 8000 --no-access-log
python -m tradingagents.service.backend.runtime reap
python -m tradingagents.service.backend.runtime evaluate
python -m tradingagents.service.backend.runtime cleanup
```
`evaluate` uses real market HTTP in an operational run; don't run it against live
data/DB until approved. Merely importing modules or generating OpenAPI does not
connect to any service or start a graph. The factory fails closed without an
explicit dispatcher. Do not run the local subprocess dispatcher inside Lambda.

Tests:
```
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q tests/test_hosted_backend.py
FINBOT_ROOT=/path/to/FinBot TEST_PG_BIN=/path/to/postgres/bin \
  PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q -m integration tests/test_hosted_backend_postgres.py
```
The PostgreSQL fixture creates/stops a new local cluster on a random loopback
port. It refuses external DSNs by having no DSN input. It applies the complete
FinBot migration chain, with minimal auth/storage platform metadata stubs, then
exercises real roles, transactions, concurrent admission, memory and investment
reset. This does **not** emulate actual hosted GoTrue or Storage HTTP servers;
those HTTP boundaries are tested separately with deterministic transports.

Remaining external gates: approved hosted ChatGPT OAuth registration/flow;
production secrets/TLS; Supabase migration deployment and real Storage/Auth smoke;
AWS dispatcher/orchestration and schedules; task reconciliation, cleanup scheduling,
pre-auth edge limits and frontend integration. No deployment is implied by tests.
