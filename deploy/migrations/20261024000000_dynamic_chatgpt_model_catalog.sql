-- Runtime-discovered ChatGPT model catalog. OAuth credentials remain on the
-- worker host; only non-secret display metadata is shared with the API.
begin;

create table analysis_backend.provider_model_catalog (
    provider text primary key,
    models jsonb not null,
    refreshed_at timestamptz not null default clock_timestamp(),
    check (jsonb_typeof(models) = 'array')
);

alter table analysis_backend.provider_model_catalog enable row level security;
create policy backend_provider_model_catalog
    on analysis_backend.provider_model_catalog
    to lucrari_analysis_backend
    using (true)
    with check (true);

revoke all on analysis_backend.provider_model_catalog from public, anon, authenticated;
grant select, insert, update, delete
    on analysis_backend.provider_model_catalog
    to lucrari_analysis_backend;

comment on table analysis_backend.provider_model_catalog is
    'Non-secret model IDs and display names discovered by the trusted personal worker. OAuth credentials never enter PostgreSQL.';

commit;
