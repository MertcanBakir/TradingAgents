# Local analysis service — phase 1

Headless Python service for a future Lambda/Fargate integration. This phase does
not deploy AWS, connect to Supabase, provide public HTTP endpoints, verify JWTs,
enforce the distributed three-worker limit, or create a queue. Existing CLI
behavior is preserved. The final public API contract is a separate phase.

## Safe validation (no providers, database or browser)

    .venv/bin/python -m tradingagents.service.cli \
      --request examples/service-request.json \
      --settings examples/service-settings.json \
      --user-id 11111111-1111-4111-8111-111111111111

The output says execution_started=false. The default never instantiates a graph
or creates a memory DB. Example model IDs are the locally tested choices, not an
account-availability guarantee. Server settings select models; requests cannot
supply credentials, endpoints or paths.

## Explicit execution (uses your plan/API and fetches market data)

Configure credentials using the upstream provider setup. ChatGPT plan users sign
in through the existing tradingagents login chatgpt command. Never put tokens in
source control/images. Multi-user cloud authorization is a separate decision;
local OAuth success does not establish permission to serve other users.

    mkdir -p local-data
    .venv/bin/python -m tradingagents.service.cli \
      --request examples/service-request.json \
      --settings examples/service-settings.json \
      --user-id 11111111-1111-4111-8111-111111111111 \
      --memory-db local-data/memory.sqlite3 \
      --output local-data/nvda-report.json \
      --execute

User ID is trusted LOCAL context, not authentication. Do not expose this command
as an unauthenticated API. The future HTTP adapter must verify JWTs and resolve
instrument_id to a trusted provider ticker. This local stock adapter accepts a
validated provider ticker and does not change the instrument catalog.

Output files must be new; existing reports are not overwritten. Otherwise JSON
goes to stdout. Optional --analysis-id identifies the memory decision. Identical
memory writes are idempotent; conflicting reuse is rejected. This does NOT
suppress a second expensive graph run: admission/idempotency must happen in the
phase 2 job repository, before graph construction.

## Isolation and execution

The standard profile fixes research/risk rounds to one, independent of CLI
preferences and environment round overrides. Every run owns a deep-copied config
and temporary results/cache directories, removed on success/failure. Endpoint
overrides, filesystem memory, checkpoints and global config mutation are disabled
for this adapter. Data tools still use the upstream run-scoped ContextVar config.
Agent prompts/graph shape and interactive CLI are not redesigned.

SDK retries are bounded by llm_max_retries; the service never retries the entire
graph. The local engine does not promise a hard wall-clock timeout or crash
resume. Phase 2 must enforce deadlines and worker termination externally.

## JSON contract, schema_version 1

AnalysisResult: analysis_id, ticker, analysis_date, final_rating, trade_action,
quality, twelve ordered sections, effective_config, upstream_version, UTC
started_at/finished_at, duration_seconds, warning_codes and memory_saved.

final_rating: Buy / Overweight / Hold / Underweight / Sell / REVIEW.
trade_action: Buy / Hold / Sell / null. Unparseable output never silently becomes
Hold. Missing requested analyst output is not considered complete.

Section codes: executive_summary, technical, fundamentals, news, sentiment, bull,
bear, trade_plan, risk, final_assessment, provenance, disclaimer. Each has title,
nullable content_markdown, and parts. Risk parts preserve aggressive/conservative/
neutral text; trade-plan parts include research_manager. Executive summary comes
from the existing final-decision header; no additional summarizer/model call.
Missing text remains null and produces a warning when required. Provenance does
not claim independently verified data freshness. Treat Markdown as untrusted text.

Raw state/messages/prompts, endpoints, paths, user IDs and credentials are not in
the output contract. Known environment secrets and common token formats are
additionally redacted from report text. Errors use domain codes, not raw traces.
This does not certify every upstream library log safe; production logging sinks
must also be sanitized and provider debug logging kept disabled.

## Memory: durable local adapter, replaceable by Supabase

MemoryRepository is the port. SQLiteMemoryRepository is a real persistent local
adapter, initially creating its DB with 0600 permissions. BoundMemory binds user,
ticker and point-in-time cutoff before the graph runs. It feeds the real graph's
past_context path, read by the Portfolio Manager.

Only complete, parseable decisions become pending memories. Partial/review reports
are returned but are not learned from. Memory failures add warnings; inspect
memory_saved and warning_codes. Future production publication and pending memory
must share a DB transaction. Local output files and SQLite are NOT a distributed
transaction: a file-write failure may leave a pending memory decision.

Retrieval: at most five same-ticker and three cross-ticker resolved decisions,
only for the bound user, under a character budget. Future resolutions are excluded
even in historical analyses. Lessons are context, not instructions or established
causal facts. Pending decisions are never injected. Different analysis_id values
allow multiple analyses of one ticker/date without overwriting the first.

No automatic settlement happens during analysis. A separate worker can call
resolve with deterministic returns, configured evaluation window, entry/exit dates
and a validated reflection. Phase 1 does not fetch settlement prices or call a
reflection LLM. Invalid windows, cross-user writes and conflicting settlements are
rejected. Five trading days in the sample is an explicit evaluation period, NOT
proof that a 3–6 month thesis has been validated. Preserve this distinction in the
future reflection worker; benchmark/window are stored per decision.

## Container

The existing Dockerfile remains the CLI. Dockerfile.worker is one-shot, runs this
service and exits, exposing no HTTP port.

    docker build -f Dockerfile.worker -t tradingagents-worker .
    docker run --rm tradingagents-worker \
      --request examples/service-request.json \
      --settings examples/service-settings.json \
      --user-id 11111111-1111-4111-8111-111111111111

This validates only. Execution requires explicit credentials and a writable memory
volume. In production replace SQLite with Supabase, not Fargate local disk.

Worker dependencies are pinned by constraints/worker.txt to the tested versions;
the upstream CLI Dockerfile and dependencies remain separately managed. Run the
real Python 3.13/Linux offline test stage with:

    docker build -f Dockerfile.worker --target test -t tradingagents-worker-test .

Provenance records configured vendor chains from the actual run config; it does
not assert that every configured source was successfully queried. Configure an
appropriate benchmark before evaluating non-US instruments; SPY in the sample
is for the NVDA example, not a universal baseline.

## Tests

    .venv/bin/python -m pytest -q tests/test_analysis_service.py

Tests use fake graph/provider boundaries and real temporary SQLite databases;
no LLM, Supabase or SMTP calls. The upstream full suite on macOS has three existing
Windows-lock simulation failures involving errno.EDEADLOCK. Do not hide those or
claim the entire upstream suite is green.
## Hosted backend extension

The later API/Supabase implementation is documented in [hosted-backend.md](hosted-backend.md).
The local SQLite CLI documented above remains available; hosted workers use the separate
Supabase memory adapter and never share a CLI login across users.

