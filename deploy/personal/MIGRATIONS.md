# Database delivery

Deployment runs the tested image's migration runner **before** changing `.env`
or recreating any service. Both SQL and runner are inside the CI-tested ARM64
image; the deployment runner never downloads live SQL from another branch/repo.
The `production` job covers migrations and application delivery together.

## Existing production database

FinBot's `supabase/migrations/` remains canonical. The three historical files in
`deploy/migrations/` are byte-identical copies, not a complete Supabase bootstrap.
Their versions/names were verified in the live database's
`supabase_migrations.schema_migrations` using a read-only transaction.

The runner **requires** all three versions with matching names. It never runs
their SQL, creates their tables, infers application from table existence, resets
the database or repairs Supabase history. Missing history requires operator
reconciliation against FinBot. The historical hashes are pinned in the runner.
This history check is not a proof that the entire live schema is free of drift.

On the first deployment, it creates a private `tradingagents_deploy.migrations`
checksum ledger and adopts these three verified historical versions. It does not
reapply them or alter their Supabase history records. No new application-schema
migration is introduced by this change. Subsequent runs detect changes/removals
of previously recorded files; fixes always need a new timestamped migration.

## Adding a migration

1. Author the SQL in canonical FinBot `supabase/migrations/`; coordinate releases
   so only **one** pipeline executes that version against the shared database.
2. Copy the exact file to this repo's `deploy/migrations/`. Use a unique 14-digit
   timestamp newer than all current shared Supabase history. Do not edit an
   already-applied migration or reuse its version.
3. Add an entry to `manifest.json`'s `migrations` array, in timestamp order:

   ```json
   {
     "file": "20261030000000_add_analysis_note.sql",
     "sha256": "<SHA-256 of the exact SQL file bytes>",
     "backwards_compatible": true
   }
   ```

4. Add real PostgreSQL acceptance tests for the new behavior and confirm the old
   deployed image also works with the expanded schema. Review SQL and this
   compatibility assertion in the PR. The flag is a reviewer assertion, not an
   automatic proof of application compatibility.
5. Merge only after `CI passed`. CD checks history read-only, backs up `.env`,
   applies the batch, then recreates the three backend services and verifies them.

Example supported SQL:

```sql
BEGIN;
ALTER TABLE analysis_backend.jobs ADD COLUMN note text;
COMMIT;
```

One outer `BEGIN`/`COMMIT` pair is optional and removed by the PostgreSQL parser;
the runner owns the real transaction. Embedded transaction control is rejected.
The automated subset permits new permanent `analysis_backend` tables, nullable
columns without defaults/constraints on existing tables, nonunique ordinary
indexes, backend-only policies/grants, RLS enablement and comments. All backend
tables must have RLS and no effective mobile grants before the batch commits.

Destructive changes, data backfills, required/defaulted columns, type changes,
functions, changes to other schemas and nontransactional commands (including
concurrent indexes) are rejected. They require a separately reviewed rollout.
SQL parsing catches unsupported statements; it is not a security sandbox and
does not replace review of expressions, constraints, privileges or lock impact.

## Atomicity, history and recovery

- One transaction covers the entire pending batch, the checksum ledger and
  Supabase history inserts. A failed SQL file rolls back the whole new batch.
- A transaction-scoped advisory lock rejects concurrent TradingAgents migration
  runners. Applying also locks the Supabase history table against concurrent
  writers. Coordinate FinBot deployments; the advisory lock is not shared by
  Supabase CLI or arbitrary administrator SQL.
- Connection timeout is 10 seconds; statement timeout 120 seconds; lock timeout
  5 seconds. Schema locks can briefly affect existing requests. Deployments may
  interrupt active analyses, as with the existing delivery flow.
- The check phase is read-only. A failure in either check/apply stops before any
  service restart or runtime-image change. A successful batch is not undone if
  the new image fails readiness: the previous image is restored and must remain
  compatible with the additive schema. There is no automatic schema downgrade.
- New versions already applied outside this runner without its checksum record
  are rejected, even when their names match. Reconcile deliberately; do not
  blindly insert ledger rows, repair history or rerun SQL to make CD green.
- Other FinBot migration versions are allowed in shared history. The runner does
  not own or apply that repository's unrelated migrations. Pending local versions
  older than the shared history tip fail rather than silently run out of order.

## Credentials and host preparation

The one-off non-root, read-only migration container uses only the existing API
container's settings/CA mounts. It does not receive worker ChatGPT mounts or
GitHub secrets. No new database credential is created. The existing underlying
database login is `postgres`; normal application queries already use
`SET LOCAL ROLE lucrari_analysis_backend`. Migrations intentionally use the
underlying privileged connection. A separate migration credential and further
runtime privilege separation are future hardening, not implemented here.

SQL diagnostics/settings/DSNs are suppressed from deployment output. Only safe
status, version and count metadata is returned. The ledger schema/table has no
permissions for `PUBLIC`, `anon`, `authenticated` or the backend role.

The host helper changes in this PR must be installed root-owned as
`/usr/local/libexec/finbot-tradingagents/versions/<SHA256>.py` before merging. The
updated restricted SSM document selects that hash-named file and verifies its
bytes. It falls back to the original helper only when its hash matches, so old
CD runs remain usable. Installing the versioned helper and updating the document
does not run migrations or restart application services. Do not remove older
helpers while a deployment can still reference them.

## Verification

CI adds a required PostgreSQL 17 service job with disposable databases. It tests
baseline adoption, read-only planning, idempotency, atomic multi-file failure,
concurrent-run rejection, checksum drift, new-table RLS and mobile denial. Unit
and deployment tests additionally cover SQL-policy rejection, manifest integrity,
failure before restart and preserving migrations during image rollback. The
production Docker build validates the actual packaged manifest and parser.

These tests use minimal Supabase auth/storage/catalog stubs. They do not test
managed Supabase HTTP behavior or replace FinBot's full canonical migration suite.
Run locally with PostgreSQL 17 binaries (or a disposable loopback service):

```sh
python deploy/personal/migrate.py --directory deploy/migrations --validate
python -m pytest -m integration tests/test_database_migrations_postgres.py
```

`TEST_MIGRATION_DATABASE_URL` may point only to a disposable loopback PostgreSQL
server with permission to create/drop test databases and roles. Never use a live
database or a tunnel to production for acceptance tests.
