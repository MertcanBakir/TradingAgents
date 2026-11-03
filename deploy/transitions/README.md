# Manual transition: multi-user provider connections (schema v2)

This is a breaking, destructive transition for the TradingAgents analysis backend.
It is prepared for review only. Do not run it automatically during CI, image
publication, container startup or ordinary additive migration delivery.

## What changes

The transition deletes all analysis_backend jobs, memory, connections (including
ciphertext), and rate-limit rows. It removes the single jobs.connection_id and
its old auth constraint. It adds connection names, per-tier job_model_bindings,
owner-bound composite foreign keys, immutable configuration/binding identities,
deferred binding completeness/provider checks, and a private schema revision.
The personal ChatGPT file and protected runtime configuration are not touched.

Only validated UUID-based objects in the private analysis-reports bucket are
removed through the Storage API. Other buckets, auth.users, public instruments,
FinBot portfolio data, ChatGPT model metadata, Supabase migration history and the
existing deployment checksum ledger are preserved. The tool refuses unexpected
report paths and any reserved/running jobs. It records the new migration version
in Supabase history after applying the SQL successfully.

`20261102000000_multi_user_provider_connections.sql` is deliberately outside
`deploy/migrations/manifest.json`. Never mark it backwards_compatible or weaken
the additive runner to make it auto-apply. Coordinate this exact version/file
with canonical FinBot supabase/migrations before its next migration deployment;
this PR does not silently edit that separate repository or reset its history.

## Merge behavior and guards

Merging runs CI and publishes the tested image normally. Production delivery is
held until BOTH PRODUCTION_DEPLOY_ENABLED=true and BACKEND_SCHEMA_VERSION=2.
A missing schema variable means deploy is skipped, with a transition notice.
The image-packaged migration checker independently requires the schema-v2 marker.
Setting a GitHub variable does not migrate the database.

The new host helper must be installed root-owned under
`/usr/local/libexec/finbot-tradingagents/versions/<SHA256>.py` after reviewing its
source. Calculate SHA256 from deploy/personal/deploy_host.py; use the existing
hash-checking SSM document. Keep the old helper. This PR does not install or
activate a host helper, change production variables or execute the reset.

Images declare io.finbot.backend-schema=2. The updated helper checks compatibility
against the DB revision. If the first v2 rollout fails, it refuses to run the old
v1 image against v2 and requires forward recovery under maintenance. Later v2-to-v2
image rollbacks retain normal verified rollback behavior. A successful DB reset
is not reversed by changing LUCRARI_IMAGE or restoring .env.

## Reviewed cutover sequence

1. Keep BACKEND_SCHEMA_VERSION unset while reviewing/merging. Ensure the actual
   GitHub variables agree before merge. Stop concurrent release activity. Obtain
   a successful current-main CI image/digest (artifacts expire after one day).
2. Schedule a brief maintenance window. Block new private API admissions at the
   edge; stop outside writers. Drain all reserved/running analyses, and quiesce
   worker/maintenance activity for the reset. Keep management access available.
   The root-owned deploy helper later expects the previous API/worker/maintenance
   containers to be running and their images to match .env, so restore those
   idle containers under the admission block before invoking delivery. The v1
   services must not accept new requests after schema reset.
3. Protect existing settings, encryption keys, ChatGPT session and Caddy volumes.
   If retaining a route back to v1 is required, take an appropriate database/schema
   backup before reset; .env backups cannot restore the schema. Deleted reports
   are intentionally discarded and cannot be recovered by a SQL rollback.
4. Install the reviewed versioned host helper. Pull the exact tested candidate
   image using the instance role. Run its reset_provider_backend.py in an
   ephemeral non-root, read-only container, reusing ONLY the API settings/CA
   mounts (the same --volumes-from <api-id>:ro pattern as deployment). Do not
   pass ChatGPT mounts, Docker socket or private credentials on the command line.
5. First invoke `/opt/finbot-transitions/reset_provider_backend.py` without flags.
   Inspect safe counts and plan_hash. This is a read-only transaction and performs
   no Storage requests. Snapshot includes target DB/project identity, SQL checksum,
   job/connection IDs, report paths and counts without exposing secrets.
6. After reviewing the plan, invoke it with:

   ```text
   --apply --confirm reset-analysis-backend-v2 --plan-hash <reviewed hash> --services-quiesced
   ```

   --services-quiesced is an explicit OPERATOR assertion, not automatic proof that
   every external service was stopped. The tool also takes DB locks and refuses
   active jobs or a changed plan. SQL changes and history are one transaction.
   Storage deletion is external and not transactional: if a later deletion/SQL
   operation fails, some report files may already be gone while SQL rolls back.
   Keep maintenance on, inspect the failure and generate a fresh dry-run; do not
   blindly retry. An already-completed transition is rejected rather than wiping
   new users' data a second time.
7. Prepare a separate protected copy of settings with real invited Supabase UUIDs in allowed_user_ids
   and operator-approved models for the chosen API providers in allowed_models.
   Do not replace the active settings yet: the old image rejects the new
   allowed_user_ids field, including during the helper preflight. The first v2
   rollout uses the existing owner-only settings. Keep personal_user_id,
   personal_chatgpt_enabled, personal_chatgpt_auth_file,
   encryption keys, DSN and CA mount intact. Example non-secret changes:

   ```json
   {
     "allowed_user_ids": ["<invited Supabase user UUID>"],
     "allowed_models": {
       "chatgpt": ["<existing owner catalog placeholder>"],
       "openai": ["<approved model ID>"],
       "anthropic": ["<approved model ID>"]
     }
   }
   ```

   This is a partial settings illustration, not a complete valid settings file.
   Do not put users' API keys in this file. They add keys via the authenticated API.
8. Restore idle services needed for the helper's preflight while admissions remain
   blocked. Set BACKEND_SCHEMA_VERSION=2 and enable production delivery, then run
   CD for the successful current-main push CI attempt. The new migration checker
   verifies v2; it does not execute this reset. If the candidate is stale/expired,
   refresh main CI first. Keep maintenance until the intended image, HTTPS health,
   fresh personal model catalog, owner-only ChatGPT access and invited API routes
   are verified. Once all containers run v2, install the prepared settings and
   recreate API/worker/maintenance under the admission block; verify invited API
   routes and owner behavior again. Paid provider calls require deliberate test
   credentials/approval.
9. Reopen admissions only after backend checks and the frontend contract are ready.
   See ../../docs/provider-connections-api.md. No SQS or per-user queue is installed
   by this transition; global active capacity stays one.

## Recovery and tests

Do not enable the schema gate before the marker and helper are correct. On failure,
keep admissions blocked and inspect the exact SSM command and safe tool output.
Never retry a runner timeout without checking whether its remote command remains
active. Prefer forward recovery; restoring v1 needs a matching database/schema
recovery plan as well as its image. Do not delete or repair historical migration
rows to bypass the version checks.

The required PostgreSQL CI job tests the baseline, transition, RLS, cross-owner
FKs, deferred constraints, immutable selections, account deletion, mixed providers,
idempotency, revocation and admission races. Dry-run, stale-plan, active-job,
unexpected-path and Storage-failure tests never use production services. The
broader FinBot acceptance suite can additionally run locally with FINBOT_ROOT;
its v1 personal SQL contract is checked before this intentionally breaking reset.
