# Analysis backend database migrations

These SQL files are copies of the analysis migrations maintained in the FinBot
application repository. They are included so this fork documents and ships its
backend schema dependencies. Keep the copies synchronized with FinBot.

## Prerequisites

This is a FinBot integration, not a standalone empty-database installer.
The migrations expect:

- Supabase PostgreSQL with its `auth` and `storage` schemas, Auth roles and
  Storage tables already provisioned.
- FinBot's `public.instruments` catalog, including `id`, `symbol`, `market`,
  `asset_type` and `is_active` columns. The backend queries active `NASDAQ100`
  stock instruments. Prepare the real FinBot catalog and its RLS configuration
  first; these files do not create or populate it.
- A database administrator able to create the private schema and role, grant
  privileges, define RLS policies, and configure the private report bucket.

The API-key hosted backend also requires the first migration. Personal ChatGPT
mode requires all three, including the dynamic catalog migration.

## Installation order

1. `20261019000000_analysis_backend.sql`: private backend schema, service role,
   connections, jobs, memory, rate limits and report bucket permissions.
2. `20261022000000_personal_analysis_auth.sql`: personal authentication contract
   for jobs without an API-key connection.
3. `20261024000000_dynamic_chatgpt_model_catalog.sql`: private runtime model
   catalog shared between worker and API.

Apply each migration once in filename order with your database migration tool.
Existing FinBot deployments should use their canonical migration history and
apply only pending migrations. Do not rerun these copies against a database
where the same migrations are already installed. The files deliberately do
not mask duplicate-installation errors.

Apply the catalog migration before deploying the dynamic-model backend image.
The worker populates the catalog after startup; the API cannot advertise personal
models until the worker has successfully discovered and published them.

No account UUID, database password, API key or ChatGPT session belongs in these
SQL files. Set the owner's UUID and credentials privately in runtime settings.
See [personal deployment](../../docs/personal-backend.md) for configuration.

## Verification

Verify all five `analysis_backend` tables have RLS enabled and that `anon` and
`authenticated` have no schema/table access or backend-role membership. The
dedicated backend role must remain NOLOGIN, non-superuser and non-BYPASSRLS.
The `analysis-reports` bucket must remain private.

The real PostgreSQL integration suite currently expects a full FinBot checkout:

```sh
FINBOT_ROOT=/absolute/path/to/FinBot python -m pytest -q -m integration tests/test_hosted_backend_postgres.py
```

It creates an ephemeral local database and uses the canonical FinBot migration
chain. These bundled files alone do not replace that test prerequisite.
