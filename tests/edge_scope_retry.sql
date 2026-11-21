begin;
do $test$
declare owner_id uuid:=gen_random_uuid(); a text:=gen_random_uuid()::text; b text:=gen_random_uuid()::text;
 ja text:=gen_random_uuid()::text; jh text:=gen_random_uuid()::text; aid text:=gen_random_uuid()::text; result jsonb;
begin
 perform 1 from public.phinix_runtime where id=1 for update;
 if exists(select 1 from public.phinix_runtime where lease_until>extract(epoch from now())) then raise exception 'monitor_busy_retry_test'; end if;
 update public.phinix_runtime set lease_owner=owner_id,lease_until=extract(epoch from now())+30,discord_cooldown=0 where id=1;
 perform phinix_private.sync_program(a,'{"status":"open","domains":{"removed.invalid":["removed.invalid"]},"scope_url":"https://app.intigriti.com/researcher/dashboard"}',0);
 perform phinix_private.sync_program(b,'{"status":"open","domains":{},"scope_url":"https://app.intigriti.com/researcher/dashboard"}',1);
 insert into public.phinix_live_state values('job:'||ja,'job',jsonb_build_object('id',ja,'program_id',a,'kind','status','notify',false,'status','error','occurred_at',0,'next_attempt',extract(epoch from now())+3600));
 perform public.phinix_edge_rpc('ingest',jsonb_build_object('owner',owner_id,'jobs','[]'::jsonb,'hash','rollback-test'));
 if not exists(select 1 from public.phinix_live_state where kind='job' and data->>'program_id'=b and data->>'status'='pending') then raise exception 'stale_program_starvation'; end if;
 perform public.phinix_edge_rpc('fail_job',jsonb_build_object('owner',owner_id,'job_id',ja,'code','scope_access_denied','delay',3600,'unavailable',true));
 if not exists(select 1 from public.phinix_live_state where id='program:'||a and (data->>'last_checked')::double precision>extract(epoch from now())-10) then raise exception 'unavailable_program_timestamp_stale'; end if;
 perform phinix_private.sync_program(a,'{"status":"open","domains":{"removed.invalid":["removed.invalid"]},"scope_url":"https://app.intigriti.com/researcher/dashboard"}',extract(epoch from now()));
 insert into public.phinix_live_state values('addition:'||aid,'addition',jsonb_build_object('id',aid,'program_id',a,'domain','removed.invalid','active',true,'occurred_at',0,'endpoints','["removed.invalid"]'::jsonb));
 insert into public.phinix_live_state values('job:'||jh,'job',jsonb_build_object('id',jh,'program_id',a,'kind','scope','notify',true,'status','pending','occurred_at',1,'next_attempt',0));
 insert into public.phinix_live_state values('outbox:'||aid,'outbox',jsonb_build_object('id',aid,'state','pending','due_at',0,'created_at',now(),'payload',jsonb_build_object('kind','domain_added','program_id',a,'domain','removed.invalid')));
 perform public.phinix_edge_rpc('fail_job',jsonb_build_object('owner',owner_id,'job_id',jh,'code','scope_access_denied','delay',3600,'unavailable',false,'current','{"status":"open","domains":{"kept.invalid":["kept.invalid"]},"scope_url":"https://app.intigriti.com/researcher/dashboard"}'::jsonb));
 if exists(select 1 from public.phinix_live_state where id='addition:'||aid and (data->>'active')::boolean) then raise exception 'removed_domain_still_active'; end if;
 perform public.phinix_edge_rpc('claim_delivery',jsonb_build_object('owner',owner_id));
 if not exists(select 1 from public.phinix_live_state where id='outbox:'||aid and data->>'state'='suppressed') then raise exception 'stale_scope_alert_not_suppressed'; end if;
 if (select (payload->'stats'->>'pending_alerts')::bigint from public.phinix_feed where id=1)
    <> (select count(*) from public.phinix_live_state where kind='outbox' and data->>'state' in ('pending','sending') and data->'payload'->>'kind'='domain_added') then
  raise exception 'suppressed_alert_count_stale';
 end if;
 update public.phinix_live_state set data=data||'{"status":"done"}'::jsonb where id='job:'||jh;
 result:=public.phinix_edge_rpc('fail_job',jsonb_build_object('owner',owner_id,'job_id',jh,'code','network_error'));
 if result<>'false'::jsonb then raise exception 'completed_job_downgraded'; end if;
end $test$;
rollback;
select 'passed: stale checks, removed-domain suppression, completed-job protection; rolled back' as scope_retry_test;
