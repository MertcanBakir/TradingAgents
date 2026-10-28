begin;

-- Not exposed through PostgREST. Only the trusted analysis backend may use it.
create schema analysis_backend;
revoke all on schema analysis_backend from public, anon, authenticated;
do $$ begin
    if not exists (select 1 from pg_roles where rolname = 'lucrari_analysis_backend') then
        create role lucrari_analysis_backend nologin;
    end if;
    if exists (select 1 from pg_roles where rolname = 'lucrari_analysis_backend'
        and (rolcanlogin or rolbypassrls or rolsuper)) then
        raise exception 'analysis_backend_role_has_unsafe_attributes';
    end if;
end $$;
revoke lucrari_analysis_backend from anon, authenticated;
grant lucrari_analysis_backend to postgres;
grant usage on schema analysis_backend to lucrari_analysis_backend;
grant usage on schema public to lucrari_analysis_backend;
grant select on public.instruments to lucrari_analysis_backend;
create policy analysis_backend_read_us_catalog on public.instruments
    for select to lucrari_analysis_backend
    using (market = 'NASDAQ100' and asset_type = 'stock');

create table analysis_backend.connections (
    id uuid primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    provider text not null,
    secret_ciphertext text,
    status text not null check (status in ('active', 'revoked')),
    created_at timestamptz not null default clock_timestamp(),
    verified_at timestamptz not null,
    unique (user_id, id),
    check ((status = 'active') = (secret_ciphertext is not null))
);
create index analysis_connections_user on analysis_backend.connections(user_id, created_at desc);

create table analysis_backend.jobs (
    id uuid primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    connection_id uuid not null,
    request_key uuid not null,
    request_hash text not null,
    request jsonb not null,
    model_settings jsonb not null,
    status text not null check (status in ('reserved', 'running', 'success', 'partial', 'failed', 'timed_out')),
    stage text not null default 'accepted' check (stage in ('accepted','analysis','persisting','finished')),
    created_at timestamptz not null default clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    lease_token uuid,
    lease_until timestamptz not null,
    hard_deadline timestamptz not null,
    report_path text,
    report_sha256 text,
    error_code text,
    unique (user_id, request_key),
    unique (user_id, id),
    foreign key (user_id, connection_id) references analysis_backend.connections(user_id, id),
    check ((status in ('success','partial','failed','timed_out')) = (finished_at is not null)),
    check (status not in ('success','partial') or (report_path is not null and report_sha256 is not null)),
    check (hard_deadline > created_at),
    check (lease_until <= hard_deadline)
);
create index analysis_jobs_user_created on analysis_backend.jobs(user_id, created_at desc, id desc);
create index analysis_jobs_active on analysis_backend.jobs(lease_until) where status in ('reserved','running');
create unique index analysis_jobs_report_path on analysis_backend.jobs(report_path) where report_path is not null;

create table analysis_backend.memory (
    analysis_id uuid primary key,
    user_id uuid not null,
    ticker text not null,
    analysis_date date not null,
    created_at timestamptz not null,
    decision jsonb not null,
    outcome jsonb,
    resolved_at timestamptz,
    evaluation_token uuid,
    evaluation_until timestamptz,
    next_evaluation_at timestamptz not null default clock_timestamp(),
    evaluation_attempts integer not null default 0 check (evaluation_attempts >= 0),
    last_evaluation_code text,
    foreign key (user_id, analysis_id) references analysis_backend.jobs(user_id, id) on delete cascade,
    check ((outcome is null) = (resolved_at is null))
);
create index analysis_memory_context on analysis_backend.memory(user_id, ticker, resolved_at desc, analysis_id desc);
create index analysis_memory_due on analysis_backend.memory(next_evaluation_at) where outcome is null;

create table analysis_backend.rate_limits (
    user_id uuid not null references auth.users(id) on delete cascade,
    action text not null,
    window_start timestamptz not null,
    hits integer not null check (hits > 0),
    primary key (user_id, action)
);

alter table analysis_backend.connections enable row level security;
alter table analysis_backend.jobs enable row level security;
alter table analysis_backend.memory enable row level security;
alter table analysis_backend.rate_limits enable row level security;
create policy backend_connections on analysis_backend.connections to lucrari_analysis_backend using (true) with check (true);
create policy backend_jobs on analysis_backend.jobs to lucrari_analysis_backend using (true) with check (true);
create policy backend_memory on analysis_backend.memory to lucrari_analysis_backend using (true) with check (true);
create policy backend_limits on analysis_backend.rate_limits to lucrari_analysis_backend using (true) with check (true);
revoke all on all tables in schema analysis_backend from public, anon, authenticated;
grant select, insert, update, delete on all tables in schema analysis_backend to lucrari_analysis_backend;
alter default privileges in schema analysis_backend revoke all on tables from public, anon, authenticated;
alter default privileges in schema analysis_backend revoke execute on functions from public;

-- Private reports: no client storage policies; backend proxies owned objects.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('analysis-reports', 'analysis-reports', false, 2097152, array['application/json'])
on conflict (id) do nothing;
do $$ begin
    if exists (select 1 from storage.buckets where id = 'analysis-reports' and public) then
        raise exception 'analysis_reports_bucket_must_be_private';
    end if;
end $$;

-- Read metadata only for safe orphan discovery. Delete objects via Storage API,
-- never by deleting storage.objects rows. No grants to any mobile role.
grant usage on schema storage to lucrari_analysis_backend;
grant select on storage.objects to lucrari_analysis_backend;
create policy analysis_backend_report_metadata on storage.objects
    for select to lucrari_analysis_backend using (bucket_id = 'analysis-reports');
create policy analysis_backend_report_metadata_limit on storage.objects as restrictive
    for select to lucrari_analysis_backend using (bucket_id = 'analysis-reports');

comment on schema analysis_backend is 'TradingAgents private backend. No mobile grants. Does not participate in investment reset.';
comment on table analysis_backend.connections is 'AES-GCM ciphertext only; encryption keys stay outside this database.';
comment on table analysis_backend.jobs is 'Admission serialized by advisory transaction lock 710191. Leases fence late workers; external supervisor must terminate expired processes.';
commit;
