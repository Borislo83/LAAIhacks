-- Session listing for agent-side watchers (agents team). Safe to apply on top of 001-005, and
-- to rerun. Adds one read operation, 'sessions', to the public laai_workflow entry point:
-- the caller's unexpired sessions with their revision and active page, newest activity first
-- (at most 100). A watcher compares revisions and reads /display only for sessions that
-- changed, so it notices every page PUT without a per-page event. laai_workflow_apply and
-- every existing operation's response are unchanged; 'sessions' is rate limited and counted
-- like every other operation, and takes no lock.
begin;

create or replace function public.laai_workflow_sessions(p_user_id uuid) returns jsonb
language sql stable set search_path = public as $$
  select jsonb_build_object('sessions',coalesce(jsonb_agg(jsonb_build_object(
      'session_id',x.id,'revision',x.revision,'active_page_id',x.active_page_id,
      'updated_at',x.updated_at,'expires_at',x.expires_at) order by x.updated_at desc,x.id),'[]'))
  from (select * from public.laai_sessions where user_id=p_user_id and expires_at>now()
        order by updated_at desc,id limit 100) x;
$$;
revoke all on function public.laai_workflow_sessions(uuid) from public, anon, authenticated;
grant execute on function public.laai_workflow_sessions(uuid) to service_role;

create or replace function public.laai_workflow(p_user_id uuid, p_operation text, p_session_id uuid, p_payload jsonb)
returns jsonb language plpgsql set search_path = public as $$
declare used int; response jsonb;
begin
  select case when window_start < now()-interval '1 minute' then 0 else requests end into used
    from public.laai_request_buckets where user_id=p_user_id;
  if coalesce(used,0) >= 600 then
    perform public.laai_count_request(p_user_id);
    return jsonb_build_object('error',jsonb_build_object('code','RATE_LIMITED',
      'message','Too many requests; try again in a minute'),'status',429);
  end if;
  if p_operation = 'sessions' then
    response := public.laai_workflow_sessions(p_user_id);
  else
    response := public.laai_workflow_apply(p_user_id, p_operation, p_session_id, p_payload);
  end if;
  perform public.laai_count_request(p_user_id);
  return response;
end $$;
revoke all on function public.laai_workflow(uuid,text,uuid,jsonb) from public, anon, authenticated;
grant execute on function public.laai_workflow(uuid,text,uuid,jsonb) to service_role;

notify pgrst, 'reload schema';
commit;
