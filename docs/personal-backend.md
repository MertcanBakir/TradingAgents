# Personal self-hosted backend (single owner)

This mode is **not** a hosted ChatGPT login product for other users. It runs
TradingAgents in the owner's remote environment using that owner's ChatGPT
registration. API-key hosted mode remains available and unchanged by default.
No AWS resource or remote migration is created by these files.

## Configuration and identity

Use `examples/personal-backend-settings.example.json` as a template, not as a
working configuration. All `REPLACE_...` values must be supplied privately.
Replace the example `personal_user_id` with your own Supabase Auth user UUID.
The example UUID is fabricated; it is not an enrolled account or compiled default.
Changing it requires restarting **all** API, worker and maintenance services,
after stopping/draining active work. It never reassigns previous jobs or memory.
Do not auto-enroll new anonymous users. Auth verifies the bearer at Supabase's
`/auth/v1/user`; only this exact owner may use an anonymous authenticated session.
Other users get `ACCESS_DENIED`, even if their session is otherwise valid.

Store settings in a 0600 file owned by runtime UID 1000, with its parent directory
0700. Set `LUCRARI_BACKEND_SETTINGS_FILE` to its absolute path. Remote PostgreSQL
requires `sslmode=verify-full` and a valid trust chain. Never put service keys,
database passwords, encryption keys or OAuth tokens in Git, image layers or logs.
Personal ChatGPT model availability is discovered by the worker at runtime.
The `allowed_models.chatgpt` entry is retained for configuration compatibility;
it does not restrict the dynamic personal catalog. API-key providers still use
their operator-configured model allowlists.

## Database

See [database prerequisites and migrations](../deploy/migrations/README.md).
Apply the following migrations in filename order after preparing the documented
FinBot instrument catalog and Supabase prerequisites:

1. `20261019000000_analysis_backend.sql`
2. `20261022000000_personal_analysis_auth.sql`
3. `20261024000000_dynamic_chatgpt_model_catalog.sql`

The second adds `jobs.auth_type` and permits a null `connection_id` only for
personal ChatGPT jobs with both tiers set to `chatgpt`. Existing API-key jobs
retain their connection ownership FK. No user UID or OAuth secret is put into
the migration. The third adds a private, RLS-protected model catalog containing
model IDs, display names and a refresh timestamp, with no session credentials.

The API commits a reservation and returns 202. `DatabaseDispatcher` deliberately
does not spawn a process: `serve-worker` discovers committed reservations and
claims them atomically. One active reservation/running job is allowed globally
in personal mode; a different concurrent request gets `CAPACITY_FULL` (429),
not an implicit queue. An identical idempotency key replays the original result.
The request must be sent again with a **new** key to explicitly retry a terminal
failure. Owner changes never allow reading a previous owner's history.

## ChatGPT session administration

Official procedure: https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms

The existing upstream `chatgpt_auth` implements OAuth/PKCE, validated identity,
rotation, atomic 0600 writes and cross-process refresh locking. The backend
selects a dedicated absolute `personal_chatgpt_auth_file`; it does not patch
upstream OAuth or mix this registration with another local account.

1. Prepare a dedicated 0700 ChatGPT directory owned by runtime UID 1000 on an
   encrypted, persistent host disk (not the container layer).
2. Run the admin command `session --action init` on that host. This saves only a
   stable host ID; no network or login occurs. Only the worker/admin container
   mounts this directory, never the API or maintenance containers.
3. Complete OAuth **locally** with the same TradingAgents version/client and the
   same selected account/workspace. Transfer the protected credential file over
   SSH to a separate private import directory on the server. Stop using this
   transferred refresh session on the laptop: the server owns later refreshes.
4. Run `session --action import --source /run/import/chatgpt-auth.json`, using a
   one-off worker/admin container with the source directory mounted read-only
   (0700 parent, 0600 file, same UID). It preserves the destination host ID and
   refuses to overwrite a live session. Do not mount the import source into
   normal services. Securely retire the transferred copy after verifying.
5. `session --action status` checks local record structure without network and
   returns booleans only. It is **not** proof of current account permission.
   `session --action models` explicitly contacts OpenAI to discover available
   models for an operator check. The persistent worker publishes the same
   live model list automatically; adding a new personal model requires no
   configuration edit or service restart.

Admin commands use `python -m tradingagents.service.backend.runtime session ...`
with the settings file configured. `session --action login` also supports the
upstream remote terminal/pasted-redirect flow, but must be interactive and must
not be captured in shared logs. Prefer the documented local OAuth/secure import
procedure. `session --action logout` explicitly revokes using upstream behavior;
it is never called automatically by cleanup/restarts.

Never paste credential file contents into chat, mobile requests or dashboard SQL.
No HTTP endpoint imports, signs in, signs out or returns ChatGPT credentials.
Session expiry/revocation results in `CHATGPT_LOGIN_REQUIRED`; temporary refresh
outages result in `CHATGPT_TEMPORARILY_UNAVAILABLE`. No API-key billing fallback.

## API contract

Authenticate with the owner's Supabase bearer session and a UUID Idempotency-Key:

```json
{
  "auth_type": "personal_chatgpt",
  "request": {"ticker": "NVDA", "analysis_date": "2026-10-20", "language": "tr"},
  "quick_model": "REPLACE_WITH_AVAILABLE_CHATGPT_MODEL",
  "deep_model": "REPLACE_WITH_AVAILABLE_CHATGPT_MODEL"
}
```

Do not send `connection_id` for this auth type. API-key requests still require
it and default to `auth_type: api_key`. `/v1/providers` advertises the worker's
dynamic catalog with `auth_type: personal_chatgpt`. It preserves the `models`
array of IDs and adds `model_options` containing `{id, name}` objects for display.
Clients must use the returned IDs rather than hardcoding model lists.
API-key connection CRUD never accepts ChatGPT tokens.
Status/history/report endpoints remain ownership-scoped and private.

The worker refreshes the catalog at startup and every five minutes between
analyses. A long-running analysis may delay that refresh until it finishes.
Refresh failures retry after one minute; an existing catalog expires after
15 minutes. Missing or stale catalogs omit ChatGPT from `providers` and report
`unavailable.chatgpt: MODEL_CATALOG_UNAVAILABLE`. Admission checks the catalog;
the analysis child checks the live session again before execution. A listed
model can still fail upstream due to quotas or a later access change.
Only the worker mounts the session directory; the API reads non-secret catalog
metadata from PostgreSQL.

## Deployment packaging (not executed automatically)

The optional [CD pipeline](../deploy/personal/CD.md) documents image handoff,
GitHub OIDC, restricted SSM deployment and rollback. Live deployment remains
disabled until explicitly enabled after first-release approval.

`compose.personal.yml` runs API, worker, maintenance and Caddy HTTPS on one EC2.
Set `LUCRARI_IMAGE` to a versioned ECR image/tag (prefer a digest),
`LUCRARI_PRIVATE_DIR` to your protected host directory and `LUCRARI_DOMAIN` to
your DNS hostname. Supply valid DNS and restrict inbound traffic to HTTPS/ACME
requirements; never expose port 8000 publicly. ECR credentials stay on the host.
Before production pin the Caddy image to a reviewed immutable digest as well.

The runtime image remains `Dockerfile.worker`, with entrypoints overridden by
Compose. API/worker/maintenance run non-root with read-only root filesystems,
temporary scratch space and bounded Docker logs. ChatGPT storage is mounted only
into worker. `/tmp/market-cache` is ephemeral; business state stays in Supabase.
Docker health or restart alone does not guarantee job health: monitor leases and
safe job error codes. EC2 is a single point of failure; no HA claim is made.

`serve-worker` polls every 3 seconds, keeps a host-level daemon lock, and runs
one supervisor/child at a time. A child also retains an execution lock if its
parent dies. A second child fails safely instead of overlapping it. The child
watchdog stops after lease loss; the supervisor enforces the 35-minute deadline.
`maintain` reaps expired leases every 30 seconds and runs deterministic memory
evaluation hourly. It mounts no ChatGPT session, makes no LLM calls and never
starts analyses. Its price evaluation may contact Yahoo for completed sessions.
Orphan report cleanup remains explicit (`cleanup` dry-run; `cleanup --apply`
only with operator authorization); maintenance does not silently delete reports.

If the API restarts before delivery, a still-valid reservation is rediscovered.
If the worker is unavailable longer than the 120-second admission lease, the job
times out rather than running unexpectedly much later. Running analyses are
not resumed mid-graph or automatically rerun after crashes. DB/storage persistence
uses lease fencing and compensating cleanup; it is not a distributed transaction.

## Tests and next gates

Use offline unit tests and the real temporary PostgreSQL acceptance suite. Never
run `session login/models/import`, real analysis, `supabase db push`, AWS create
commands or Compose deployment merely as an automated unit test. After explicit
deployment approval, still verify real Supabase Auth/Storage, Linux bind-mount
permissions, a real ChatGPT registration, model access and one end-to-end report.

### Local verification — 2026-10-22

- `python -m pytest -p no:cacheprovider -q tests/test_personal_backend.py
  tests/test_hosted_backend.py tests/test_analysis_service.py`: 134 passed.
- `FINBOT_ROOT=... python -m pytest -p no:cacheprovider -q -m integration
  tests/test_hosted_backend_postgres.py`: 28 passed on an ephemeral loopback
  PostgreSQL 17 cluster, applying the complete FinBot migration chain and SQL
  acceptance files. Auth/Storage HTTP and graph generation remain mocked.
- FinBot full Python suite: 918 passed, 1 warning.
- Full TradingAgents suite on macOS/Python 3.14: 1493 passed, 3 failed, 1 skipped
  before the additional personal roundtrip parameter was added (integration
  tests are deselected in this command). The three failures are unchanged
  upstream Windows-lock simulation tests referencing `errno.EDEADLOCK`, which
  macOS does not define. Neither that implementation nor those tests was edited.
- Ruff checks and formatting, migration prefix uniqueness, Compose configuration
  validation and `git diff --check` passed. FinBot production jobs unchanged.
- Docker was initially unavailable. Later the same day, launching the installed
  nested Desktop app with the required local execution permission succeeded;
  Linux container verification below supersedes that earlier limitation.
- No real OAuth login/refresh, OpenAI generation, remote Supabase writes, AWS
  resource creation, commit or push was performed.

### Linux production-container verification — 2026-10-22

- Built `tradingagents-personal-test:20261022` from `Dockerfile.worker --target test`:
  **1496 passed, 1 skipped, 29 deselected, 100 subtests passed** on Linux ARM64 /
  Python 3.13. The skip is optional `langchain_aws`; PostgreSQL/integration tests
  require the separate real-database test command above. The three macOS
  Windows-lock simulation failures do not occur on Linux.
- Re-ran that full suite with `docker run --rm --network none`: same result.
- Built `tradingagents-personal-worker:20261022`, the actual non-root runtime
  image. `python -m pip check` reports no broken requirements.
- Ran `deploy/personal/container_smoke.py` against that runtime with
  `--network none --read-only --tmpfs /tmp:mode=1777,size=256m --cap-drop ALL
  --security-opt no-new-privileges:true`. It passed with UID 1000, a read-only
  root filesystem, private 0600 host-ID initialization, empty-session status,
  real Uvicorn `/healthz`, and a 401/no-store response on a private route without
  authentication. The API boots with its worker-only credential path absent.
- The smoke test uses fabricated settings and no OAuth credentials; only
  container loopback HTTP is used. It does not validate a real ChatGPT account
  or remote Supabase Auth/Storage. Temporary containers remove themselves.
- Images remain local; nothing was pushed to ECR. Tested architecture is
  **linux/arm64**. Choose matching EC2 architecture or rebuild and test for
  `linux/amd64` before deploying to an x86 instance.

Repeat the production smoke test after building the runtime image:

```sh
docker run --rm --network none --read-only \
  --tmpfs /tmp:mode=1777,size=256m --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=bind,source=$PWD/deploy/personal/container_smoke.py,target=/opt/container_smoke.py,readonly" \
  --entrypoint python tradingagents-personal-worker:20261022 /opt/container_smoke.py
```

### Remote Supabase deployment — 2026-10-22 (explicitly authorized)

After the local/container acceptance checks, the owner explicitly authorized the
Supabase deployment. On the operator's linked FinBot project, only these
two pending migrations were applied with `supabase db push --yes`:

- `20261019000000_analysis_backend.sql`
- `20261022000000_personal_analysis_auth.sql`

Read-only remote verification confirmed all four analysis tables have RLS,
`anon`/`authenticated` have no schema usage, table privileges or backend-role
membership, and the dedicated backend role is NOLOGIN/non-superuser/non-BYPASSRLS.
The `analysis-reports` bucket is private, JSON-only, with a 2 MiB file limit.
The personal-auth check constraint and reservation discovery index exist. The
configured owner's Auth UID exists; no user was created or modified. Job and
memory tables were empty after deployment, and a subsequent push dry-run reported
`Remote database is up to date.` No fixture writes or SQL acceptance fixtures
were run against production. No portfolio data, ChatGPT credentials or AWS
resources were changed, and no commit/push was performed.

The CLI's two role-membership revoke warnings were benign: neither mobile role
had the membership to begin with; remote privilege checks confirmed it is absent.
AWS deployment and a real ChatGPT/Supabase Storage roundtrip remain separate gates.
