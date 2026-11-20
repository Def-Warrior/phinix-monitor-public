-- Supabase may create this event trigger when automatic RLS is enabled.
-- It is administrative and must not be callable through client API roles.
do $$
begin
    if to_regprocedure('public.rls_auto_enable()') is not null then
        revoke execute on function public.rls_auto_enable() from public, anon, authenticated;
    end if;
end;
$$;
