create or replace function phinix_private.edge_rpc(op text,args jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare r public.phinix_runtime; t double precision:=extract(epoch from clock_timestamp());
 owner_id uuid; x jsonb; j jsonb; k text; secret_id uuid; cfg jsonb; result jsonb; rows_found integer; row_id text;
begin
 select * into r from public.phinix_runtime where id=1 for update;
 if op='configure' then
  if r.enabled then raise exception 'disable_before_configure'; end if;
  if length(coalesce(args->>'intigriti',''))<10 or (args->>'discord') !~ '^https://discord.com/api(/v10)?/webhooks/[0-9]+/[A-Za-z0-9_-]+$' then raise exception 'invalid_credentials'; end if;
  foreach k in array array['intigriti','discord','site_url','site_token'] loop
   if length(coalesce(args->>k,''))>0 then
    select id into secret_id from vault.secrets where name='phinix_'||k;
    if secret_id is null then perform vault.create_secret(args->>k,'phinix_'||k);
    else perform vault.update_secret(secret_id,args->>k); end if;
   end if;
  end loop;
  update public.phinix_runtime set configured=true where id=1;
  return jsonb_build_object('configured',true);
 elsif op='import' then
  if r.enabled or exists(select 1 from public.phinix_live_state limit 1) then raise exception 'import_requires_empty_disabled_monitor'; end if;
  for x in select value from jsonb_array_elements(args->'rows') loop
   insert into public.phinix_live_state values((x->>'kind')||':'||(x->>'id'),x->>'kind',x->'data');
  end loop;
  update public.phinix_runtime set started_at=(args->>'started_at')::double precision,
   last_poll=(args->>'last_poll')::timestamptz,discord_cooldown=coalesce((args->>'discord_cooldown')::double precision,0),status='healthy' where id=1;
  perform phinix_private.refresh_feed();
  return (select payload->'stats' from public.phinix_feed where id=1);
 elsif op in ('acquire','probe') then
  if coalesce(args->>'key','') <> coalesce((select decrypted_secret from vault.decrypted_secrets where name='phinix_cron_key'),'missing') then
   return jsonb_build_object('authorized',false);
  end if;
  if not r.configured then return jsonb_build_object('authorized',true,'configured',false); end if;
  if op='acquire' then
   if not r.enabled or r.lease_until>t then return jsonb_build_object('authorized',true,'busy',true); end if;
   owner_id:=(args->>'owner')::uuid;
   update public.phinix_runtime set lease_owner=owner_id,lease_until=t+30 where id=1;
  end if;
  select jsonb_object_agg(substr(name,8),decrypted_secret) into cfg from vault.decrypted_secrets
   where name in ('phinix_intigriti','phinix_discord','phinix_site_url','phinix_site_token');
  return jsonb_build_object('authorized',true,'configured',true,'credentials',cfg,'provider_cooldown',r.provider_cooldown,
   'discord_cooldown',r.discord_cooldown,'activities_hash',r.activities_hash,'started_at',r.started_at);
 end if;
 owner_id:=(args->>'owner')::uuid;
 if owner_id is null or owner_id is distinct from r.lease_owner or r.lease_until<t then raise exception 'lease_lost'; end if;
 if op='ingest' then
  if r.activities_hash is distinct from args->>'hash' then
   for x in select value from jsonb_array_elements(args->'jobs') loop
    insert into public.phinix_live_state values('job:'||(x->>'id'),'job',x||jsonb_build_object('status','pending','next_attempt',0,'notify',(x->>'occurred_at')::double precision>=r.started_at)) on conflict(id) do nothing;
   end loop;
   update public.phinix_runtime set activities_hash=args->>'hash' where id=1;
  end if;
  select data into x from public.phinix_live_state p where kind='program' and (data->>'last_checked')::double precision<t-3600
   and not exists(select 1 from public.phinix_live_state j where j.kind='job'
    and j.data->>'program_id'=p.data->>'program_id' and j.data->>'kind'='status'
    and j.data->>'status' in ('pending','error'))
   order by (data->>'last_checked')::double precision limit 1;
  if x is not null then
   k:='refresh:'||(x->>'program_id')||':'||floor(t/3600)::text;
   insert into public.phinix_live_state values('job:'||k,'job',jsonb_build_object('id',k,'program_id',x->>'program_id','kind','status','occurred_at',floor(t),'notify',false,'status','pending','next_attempt',0)) on conflict(id) do nothing;
  end if;
  update public.phinix_runtime set last_poll=clock_timestamp(),status='healthy',last_error=null,provider_cooldown=0 where id=1;
  select coalesce(jsonb_agg(data),'[]'::jsonb) into result from (select data from public.phinix_live_state where kind='job' and data->>'status' in ('pending','error') and (data->>'next_attempt')::double precision<=t order by (data->>'notify')::boolean desc,(data->>'occurred_at')::bigint desc limit 4) q;
  return result;
 elsif op='finish_job' then
  select data into j from public.phinix_live_state where id='job:'||(args->>'job_id');
  if j is null or j->>'status'='done' then return 'false'; end if;
  k:=j->>'program_id';
  perform phinix_private.sync_program(k,args,t);
  for x in select value from jsonb_array_elements(args->'additions') loop
   insert into public.phinix_live_state values('addition:'||(x->>'id'),'addition',x||jsonb_build_object('program_id',k,'scope_url',args->>'scope_url','detected_at',clock_timestamp(),'active',true)) on conflict(id) do nothing;
   get diagnostics rows_found = row_count;
   if rows_found>0 and (j->>'notify')::boolean and (x->>'occurred_at')::bigint>0 then
    row_id:=x->>'id';
    insert into public.phinix_live_state values('outbox:'||row_id,'outbox',jsonb_build_object('id',row_id,'dedupe_key','domain:'||row_id,'state','pending','attempts',0,'failures',0,'due_at',0,'created_at',clock_timestamp(),
     'payload',jsonb_build_object('kind','domain_added','program_id',k,'domain',x->>'domain','endpoints',x->'endpoints','scope_url',args->>'scope_url','occurred_at',to_timestamp((x->>'occurred_at')::double precision)))) on conflict(id) do nothing;
   end if;
  end loop;
  update public.phinix_live_state set data=data||jsonb_build_object('status','done','error_code',null,'next_attempt',0) where id='job:'||(args->>'job_id');
  perform phinix_private.refresh_feed(); return 'true';
 elsif op='fail_job' then
  select data into j from public.phinix_live_state where id='job:'||(args->>'job_id');
  if j is null or j->>'status'='done' then return 'false'; end if;
  k:=j->>'program_id';
  update public.phinix_live_state set data=data||jsonb_build_object('status','error','error_code',args->>'code','next_attempt',t+coalesce((args->>'delay')::double precision,60)) where id='job:'||(args->>'job_id');
  if jsonb_typeof(args->'current')='object' then
   perform phinix_private.sync_program(k,args->'current',t);
  elsif coalesce((args->>'unavailable')::boolean,false) then
   update public.phinix_live_state set data=data||'{"active":false}'::jsonb where kind='addition' and data->>'program_id'=k;
   update public.phinix_live_state set data=data||jsonb_build_object('status','unavailable','domains','{}'::jsonb,'last_checked',t) where id='program:'||k;
  end if;
  perform phinix_private.refresh_feed(); return 'true';
 elsif op='provider_error' then
  update public.phinix_runtime set status=args->>'code',last_error=args->>'code',provider_cooldown=t+coalesce((args->>'delay')::double precision,60) where id=1;
  return 'true';
 elsif op='claim_delivery' then
  if r.discord_cooldown>t then return '[]'; end if;
  update public.phinix_live_state set data=data||'{"state":"pending"}'::jsonb where kind='outbox' and data->>'state'='sending' and (data->>'lease_until')::double precision<t;
  result:='[]'; rows_found:=0;
  for x in select data from public.phinix_live_state where kind='outbox' and data->>'state'='pending' and (data->>'due_at')::double precision<=t order by data->>'created_at' desc limit 40 loop
   select data into j from public.phinix_live_state where id='program:'||(x->'payload'->>'program_id');
   if x->'payload'->>'kind'<>'domain_added' or j is null or j->>'status'<>'open' or not ((j->'domains') ? (x->'payload'->>'domain')) then
    update public.phinix_live_state set data=data||'{"state":"suppressed","error_code":"domain_only_mode"}'::jsonb where id='outbox:'||(x->>'id');
    rows_found:=rows_found+1;
    continue;
   end if;
   x:=jsonb_set(x,'{payload,endpoints}',j->'domains'->(x->'payload'->>'domain'));
   update public.phinix_live_state set data=x||jsonb_build_object('state','sending','owner',owner_id,'lease_until',t+120,'attempts',coalesce((x->>'attempts')::int,0)+1) where id='outbox:'||(x->>'id');
   result:=result||jsonb_build_array(x);
   exit when jsonb_array_length(result)>=5;
  end loop;
  if rows_found>0 then perform phinix_private.refresh_feed(); end if;
  return result;
 elsif op='delivery_result' then
  for k in select value from jsonb_array_elements_text(args->'ids') loop
   select data into j from public.phinix_live_state where id='outbox:'||k and data->>'owner'=owner_id::text;
   if j is null then continue; end if;
   if args->>'code'='sent' then
    j:=j||jsonb_build_object('state','sent','sent_at',clock_timestamp(),'error_code',null,'discord_message_id',args->>'message_id');
    update public.phinix_runtime set last_sent_at=clock_timestamp(),last_delivery_latency_ms=greatest(0,(extract(epoch from clock_timestamp()-(j->>'created_at')::timestamptz)*1000)::bigint) where id=1;
   else
    j:=j||jsonb_build_object('failures',coalesce((j->>'failures')::int,0)+case when args->>'code'='rate_limited' then 0 else 1 end);
    j:=j||jsonb_build_object('state',case when coalesce((args->>'permanent')::boolean,false) or (j->>'failures')::int>3 then 'failed' else 'pending' end,
     'error_code',args->>'code','due_at',t+coalesce((args->>'delay')::double precision,30));
   end if;
   update public.phinix_live_state set data=j-'owner'-'lease_until' where id='outbox:'||k;
  end loop;
  if args->>'code'='rate_limited' then update public.phinix_runtime set discord_cooldown=t+(args->>'delay')::double precision where id=1; end if;
  perform phinix_private.refresh_feed(); return 'true';
 elsif op='release' then
  update public.phinix_runtime set lease_owner=null,lease_until=0,last_duration_ms=(args->>'duration_ms')::integer where id=1;
  return 'true';
 end if;
 raise exception 'unsupported_operation';
end $$;
revoke all on function phinix_private.edge_rpc(text,jsonb) from public,anon,authenticated;
grant execute on function phinix_private.edge_rpc(text,jsonb) to service_role;
