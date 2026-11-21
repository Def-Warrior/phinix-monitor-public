-- pg_net is not relocatable. Recreate only its transient network queue
-- under extensions; domain state, outbox and Vault credentials are untouched.
do $$ begin
 if exists(select 1 from public.phinix_runtime where lease_until>extract(epoch from now())) then
   raise exception 'monitor_busy_retry_migration';
 end if;
end $$;
lock table public.phinix_runtime in access exclusive mode;
drop extension pg_net;
create extension pg_net with schema extensions;
revoke all on schema net from public, anon, authenticated;
revoke all on all tables in schema net from public, anon, authenticated;
