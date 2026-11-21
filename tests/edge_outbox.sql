begin;
do $test$
declare owner_id uuid:=gen_random_uuid(); test_program text:=gen_random_uuid()::text;
 test_job text:=gen_random_uuid()::text; test_addition text:=gen_random_uuid()::text; result jsonb; claimed jsonb;
begin
 perform 1 from public.phinix_runtime where id=1 for update;
 if exists(select 1 from public.phinix_runtime where lease_until>extract(epoch from now())) then raise exception 'monitor_busy_retry_test'; end if;
 update public.phinix_runtime set lease_owner=owner_id,lease_until=extract(epoch from now())+30,discord_cooldown=0 where id=1;
 insert into public.phinix_live_state values('job:'||test_job,'job',jsonb_build_object('id',test_job,'program_id',test_program,'notify',true,'status','pending','occurred_at',extract(epoch from now())::bigint));
 result:=public.phinix_edge_rpc('finish_job',jsonb_build_object('owner',owner_id,'job_id',test_job,'status','open','scope_url','https://app.intigriti.com/researcher/dashboard','domains','{"edge-test.invalid":["https://edge-test.invalid"]}'::jsonb,'additions',jsonb_build_array(jsonb_build_object('id',test_addition,'change_id',test_job,'domain','edge-test.invalid','endpoints','["https://edge-test.invalid"]'::jsonb,'occurred_at',extract(epoch from now())::bigint))));
 if result<>'true'::jsonb then raise exception 'finish_failed'; end if;
 claimed:=public.phinix_edge_rpc('claim_delivery',jsonb_build_object('owner',owner_id));
 if not exists(select 1 from jsonb_array_elements(claimed) x where x->>'id'=test_addition) then raise exception 'claim_failed'; end if;
 perform public.phinix_edge_rpc('delivery_result',jsonb_build_object('owner',owner_id,'ids',jsonb_build_array(test_addition),'code','rate_limited','delay',12));
 if not exists(select 1 from public.phinix_live_state where id='outbox:'||test_addition and data->>'state'='pending' and data->>'failures'='0') then raise exception 'rate_limit_retry_failed'; end if;
 if public.phinix_edge_rpc('claim_delivery',jsonb_build_object('owner',owner_id))<>'[]'::jsonb then raise exception 'cooldown_ignored'; end if;
 update public.phinix_runtime set discord_cooldown=0 where id=1;
 update public.phinix_live_state set data=data||'{"due_at":0}'::jsonb where id='outbox:'||test_addition;
 claimed:=public.phinix_edge_rpc('claim_delivery',jsonb_build_object('owner',owner_id));
 perform public.phinix_edge_rpc('delivery_result',jsonb_build_object('owner',owner_id,'ids',jsonb_build_array(test_addition),'code','sent','message_id','simulated-rollback-only'));
 if not exists(select 1 from public.phinix_live_state where id='outbox:'||test_addition and data->>'state'='sent') then raise exception 'ack_failed'; end if;
 if exists(select 1 from jsonb_array_elements(public.phinix_edge_rpc('claim_delivery',jsonb_build_object('owner',owner_id))) x where x->>'id'=test_addition) then raise exception 'duplicate_delivery'; end if;
end $test$;
rollback;
select 'passed: transaction rolled back; no external messages sent' as outbox_test;
