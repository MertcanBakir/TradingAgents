-- Personal self-hosted ChatGPT sessions remain on the owner's host, never in DB.
-- Preserve the API-key connection contract and its composite ownership FK.
begin;
alter table analysis_backend.jobs
    add column auth_type text not null default 'api_key';
alter table analysis_backend.jobs alter column connection_id drop not null;
alter table analysis_backend.jobs add constraint analysis_jobs_auth_contract check (
    (auth_type = 'api_key' and connection_id is not null
        and model_settings->'quick'->>'provider' is distinct from 'chatgpt'
        and model_settings->'deep'->>'provider' is distinct from 'chatgpt')
    or
    (auth_type = 'personal_chatgpt' and connection_id is null
        and (model_settings->'quick'->>'provider' = 'chatgpt') is true
        and (model_settings->'deep'->>'provider' = 'chatgpt') is true)
);
create index analysis_jobs_reserved_created
    on analysis_backend.jobs(created_at,id) where status = 'reserved';
comment on column analysis_backend.jobs.auth_type is
    'Credential selector only. personal_chatgpt requires trusted runtime owner allowlist; no OAuth tokens are stored here.';
commit;
