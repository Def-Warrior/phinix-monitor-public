create extension if not exists pg_cron;
create extension if not exists pg_net;
revoke all on schema cron, net from public, anon, authenticated;
revoke all on all tables in schema cron, net from public, anon, authenticated;

select cron.schedule('phinix-poll-six-seconds', '6 seconds', $job$
 select net.http_post(
   url := 'https://udhrobgpevbumoozesay.supabase.co/functions/v1/phinix-poll',
   headers := jsonb_build_object('Content-Type','application/json','X-Phinix-Key',
     (select decrypted_secret from vault.decrypted_secrets where name='phinix_cron_key')),
   body := '{}'::jsonb, timeout_milliseconds := 25000
 )
 from public.phinix_runtime
 where id=1 and enabled and configured and lease_until < extract(epoch from now());
$job$);

-- Bound only this monitor's disposable scheduler logs; preserve monitoring history.
select cron.schedule('phinix-prune-cron-logs', '17 * * * *', $job$
 delete from cron.job_run_details
 where jobid in (select jobid from cron.job where jobname in
   ('phinix-poll-six-seconds','phinix-prune-cron-logs'))
 and end_time < now() - interval '1 day';
$job$);
