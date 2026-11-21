-- Keep current scope authoritative even when historical comparison cannot finish.
create or replace function phinix_private.sync_program(p_id text,current_scope jsonb,checked_at double precision)
returns void language plpgsql security invoker set search_path='' as $$
begin
 if jsonb_typeof(current_scope->'domains') is distinct from 'object'
   or coalesce(current_scope->>'status','') not in ('open','not_open') then
   raise exception 'invalid_current_scope';
 end if;
 insert into public.phinix_live_state values('program:'||p_id,'program',
  jsonb_build_object('program_id',p_id,'last_checked',checked_at,'status',current_scope->>'status',
   'domains',current_scope->'domains','scope_url',current_scope->>'scope_url'))
 on conflict(id) do update set data=excluded.data;
 update public.phinix_live_state set data=data||jsonb_build_object('active',(current_scope->'domains') ? (data->>'domain'))||
  case when (current_scope->'domains') ? (data->>'domain') then
   jsonb_build_object('endpoints',current_scope->'domains'->(data->>'domain'),'scope_url',current_scope->>'scope_url')
  else '{}'::jsonb end
 where kind='addition' and data->>'program_id'=p_id;
 -- A successful current-scope read resolves earlier status-only permission checks.
 update public.phinix_live_state set data=data||'{"status":"done","error_code":null,"next_attempt":0}'::jsonb
 where kind='job' and data->>'program_id'=p_id and data->>'kind'='status'
 and data->>'status'='error' and data->>'error_code'='scope_access_denied';
end $$;
revoke all on function phinix_private.sync_program(text,jsonb,double precision) from public,anon,authenticated;
