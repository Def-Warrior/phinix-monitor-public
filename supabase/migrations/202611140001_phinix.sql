-- Run once in the intended Supabase project. Only server-side keys can access data.
begin;
create table if not exists public.phinix_checkpoint (
    id integer primary key check (id = 1),
    revision uuid not null,
    body text not null check (length(body) <= 35000000),
    sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    byte_count integer not null check (byte_count between 1 and 25000000),
    updated_at timestamptz not null default now()
);
create table if not exists public.phinix_feed (
    id integer primary key check (id = 1),
    payload jsonb not null,
    updated_at timestamptz not null
);
alter table public.phinix_checkpoint enable row level security;
alter table public.phinix_feed enable row level security;
revoke all on public.phinix_checkpoint, public.phinix_feed from public, anon, authenticated;
grant select, insert, update on public.phinix_checkpoint, public.phinix_feed to service_role;

create or replace function public.phinix_commit(
    expected_revision uuid, next_revision uuid, snapshot_body text,
    snapshot_sha256 text, snapshot_bytes integer, domain_feed jsonb
) returns uuid language plpgsql security invoker set search_path = '' as $$
declare current_revision uuid;
begin
    -- Also serialize the first insert, when there is no row to lock yet.
    perform pg_advisory_xact_lock(72819403);
    select revision into current_revision from public.phinix_checkpoint where id = 1 for update;
    if current_revision is distinct from expected_revision then
        raise sqlstate 'PT409' using message = 'checkpoint_conflict';
    end if;
    if next_revision is null or next_revision = current_revision
       or jsonb_typeof(domain_feed->'items') is distinct from 'array'
       or (domain_feed->>'schema') is distinct from '1'
       or domain_feed->>'updated_at' is null then
        raise exception 'invalid_checkpoint';
    end if;
    if exists(select 1 from public.phinix_feed where id = 1
        and updated_at > (domain_feed->>'updated_at')::timestamptz) then
        raise sqlstate 'PT409' using message = 'stale_snapshot';
    end if;
    insert into public.phinix_checkpoint(id, revision, body, sha256, byte_count)
    values(1, next_revision, snapshot_body, snapshot_sha256, snapshot_bytes)
    on conflict(id) do update set revision=excluded.revision, body=excluded.body,
        sha256=excluded.sha256, byte_count=excluded.byte_count, updated_at=now();
    insert into public.phinix_feed(id, payload, updated_at)
    values(1, domain_feed, (domain_feed->>'updated_at')::timestamptz)
    on conflict(id) do update set payload=excluded.payload, updated_at=excluded.updated_at;
    return next_revision;
end;
$$;
revoke all on function public.phinix_commit(uuid, uuid, text, text, integer, jsonb)
    from public, anon, authenticated;
grant execute on function public.phinix_commit(uuid, uuid, text, text, integer, jsonb) to service_role;
commit;
