-- Shared workflow v1.1 (DB team). Safe to apply once on top of 002 and 003; every statement
-- is create-or-replace / if-not-exists, so rerunning it is also harmless.
-- * Rate-limit counting moves outside the operation's rollback scope, so failed requests count.
-- * Executors can acknowledge events ('ack'); invalidating an action also closes its event.
-- * Events report the action's current status, so executors can skip invalidated approvals.
-- * Devices can be registered ('device') and attached to new sessions.
begin;

create or replace function public.laai_uuid_or_null(p text) returns uuid
language plpgsql immutable set search_path = public as $$
begin
  return p::uuid;
exception when invalid_text_representation then
  return null;
end $$;
revoke all on function public.laai_uuid_or_null(text) from public, anon, authenticated;
grant execute on function public.laai_uuid_or_null(text) to service_role;

-- Invalidated approvals will never run, so their outbox events need no further delivery.
create or replace function public.laai_invalidate_actions(p_session_id uuid) returns void
language plpgsql set search_path = public as $$
begin
  update public.laai_approval_events e set consumed_at=now()
    from public.laai_actions a
    where a.session_id=p_session_id and a.status <> 'invalidated'
      and e.session_id=a.session_id and e.action_id=a.id and e.consumed_at is null;
  update public.laai_actions set status='invalidated'
    where session_id=p_session_id and status <> 'invalidated';
end $$;
revoke all on function public.laai_invalidate_actions(uuid) from public, anon, authenticated;
grant execute on function public.laai_invalidate_actions(uuid) to service_role;

create index if not exists laai_devices_owner on public.laai_devices(user_id);
create index if not exists laai_events_unconsumed on public.laai_approval_events(session_id, cursor)
  where consumed_at is null;

create or replace function public.laai_workflow_apply(p_user_id uuid, p_operation text, p_session_id uuid, p_payload jsonb)
returns jsonb language plpgsql set search_path = public as $$
declare
  s public.laai_sessions;
  d public.laai_devices;
  a public.laai_actions;
  item jsonb;
  response jsonb;
  previous public.laai_idempotency_keys;
  event_id uuid;
  error_code text; error_detail text; error_status text;
  request_key text := p_payload->>'request_id';
begin
  if p_operation = 'device' then
    -- Register a new device, or refresh last_seen_at for one this user already owns.
    if p_payload ? 'device_id' then
      update public.laai_devices set last_seen_at=now()
        where id=public.laai_uuid_or_null(p_payload->>'device_id') and user_id=p_user_id returning * into d;
      if not found then perform public.laai_workflow_error('DEVICE_NOT_FOUND','Device not found',404); end if;
    else
      insert into public.laai_devices(user_id) values(p_user_id) returning * into d;
    end if;
    return jsonb_build_object('device_id',d.id,'last_seen_at',d.last_seen_at);
  end if;
  if p_operation = 'create' then
    if p_payload ? 'device_id' then
      select * into d from public.laai_devices
        where id=public.laai_uuid_or_null(p_payload->>'device_id') and user_id=p_user_id;
      if not found then perform public.laai_workflow_error('DEVICE_NOT_FOUND','Device not found',404); end if;
    end if;
    insert into public.laai_sessions(user_id,device_id) values(p_user_id,d.id) returning * into s;
    return jsonb_build_object('schema_version','1.0','session_id',s.id,'revision',s.revision);
  end if;
  -- Every operation is scoped to the verified user, even with a service-role connection.
  -- This lock serializes page updates, approval, idempotency and result transitions.
  select * into s from public.laai_sessions where id=p_session_id and user_id=p_user_id for update;
  if not found then perform public.laai_workflow_error('SESSION_NOT_FOUND','Session not found',404); end if;
  if s.expires_at <= now() then perform public.laai_workflow_error('SESSION_NOT_FOUND','Session expired',404); end if;
  if p_operation in ('page','actions','approve','reject','result') then
    if request_key is null or length(request_key) not between 1 and 160 then
      perform public.laai_workflow_error('INVALID_PAYLOAD','A request_id is required',422);
    end if;
    select * into previous from public.laai_idempotency_keys
      where session_id=s.id and request_id=request_key and operation=p_operation;
    if found then
      if previous.request_json <> p_payload then
        perform public.laai_workflow_error('CONFLICT','request_id was used with different input',409);
      end if;
      return previous.response_json;
    end if;
  end if;
  if p_operation = 'status' then
    return jsonb_build_object('schema_version','1.0','session_id',s.id,'revision',s.revision,
      'active_page_id',s.active_page_id,'expires_at',s.expires_at);
  elsif p_operation = 'page' then
    -- A page snapshot change invalidates all old actions, even when its stable ID is reused.
    perform public.laai_invalidate_actions(s.id);
    insert into public.laai_pages(id,session_id,source_url,kind,title,summary_json,facts_json,source_label,updated_at)
      values(p_payload->>'page_id',s.id,p_payload->>'url',p_payload->>'kind',p_payload->>'title',
        p_payload->'summary',p_payload->'facts',p_payload->>'source_label',(p_payload->>'updated_at')::timestamptz)
      on conflict(session_id,id) do update set source_url=excluded.source_url,kind=excluded.kind,
        title=excluded.title,summary_json=excluded.summary_json,facts_json=excluded.facts_json,
        source_label=excluded.source_label,updated_at=excluded.updated_at;
    s.active_page_id := p_payload->>'page_id';
    update public.laai_sessions set active_page_id=s.active_page_id where id=s.id;
  elsif p_operation = 'actions' then
    if p_payload->>'page_id' is distinct from s.active_page_id or s.active_page_id is null then
      perform public.laai_workflow_error('CONFLICT','Page is no longer active',409);
    end if;
    -- Action IDs cannot be reused or resurrected after replacement/approval.
    if exists(select 1 from public.laai_actions x join jsonb_array_elements(p_payload->'actions') j
      on x.id=j->>'action_id' where x.session_id=s.id) then
      perform public.laai_workflow_error('CONFLICT','Use new action IDs for replacement proposals',409);
    end if;
    perform public.laai_invalidate_actions(s.id);
    for item in select * from jsonb_array_elements(p_payload->'actions') loop
      if (item->>'expires_at')::timestamptz <= now() then
        perform public.laai_workflow_error('ACTION_EXPIRED','Proposed action already expired',409);
      end if;
      -- Only the two implemented research handlers are registered for this release.
      if item->>'type' not in ('compare_prices','find_reviews') then
        perform public.laai_workflow_error('INVALID_PAYLOAD','No registered handler for this action type',422);
      end if;
      insert into public.laai_actions(id,session_id,page_id,type,label,description,expires_at)
        values(item->>'action_id',s.id,s.active_page_id,item->>'type',item->>'label',
          item->>'description',(item->>'expires_at')::timestamptz);
    end loop;
  elsif p_operation = 'display' then
    if s.revision <= coalesce((p_payload->>'after_revision')::bigint,0) then
      return jsonb_build_object('unchanged',true);
    end if;
    select jsonb_build_object('page_id',id,'title',title,'kind',kind,'summary',summary_json,
      'facts',facts_json,'source_label',source_label) into item
      from public.laai_pages where session_id=s.id and id=s.active_page_id;
    select coalesce(jsonb_agg(jsonb_build_object('action_id',id,'label',label,'type',type,
      'description',description,'status',status,'requires_approval',requires_approval,
      'expires_at',expires_at,'result',result_json) order by proposed_at,id),'[]') into response
      from public.laai_actions where session_id=s.id and page_id=s.active_page_id and status<>'invalidated';
    return jsonb_build_object('schema_version','1.0','session_id',s.id,'revision',s.revision,
      'page',item,'actions',response,'notice',null,'mode',
      case when exists(select 1 from public.laai_actions where session_id=s.id and page_id=s.active_page_id and status='running') then 'loading'
      when exists(select 1 from public.laai_actions where session_id=s.id and page_id=s.active_page_id and status='failed') then 'error'
      when exists(select 1 from public.laai_actions where session_id=s.id and page_id=s.active_page_id and status='completed') then 'result'
      when exists(select 1 from public.laai_actions where session_id=s.id and page_id=s.active_page_id and status='approved') then 'loading'
      when jsonb_array_length(response)>0 then 'actions' else 'summary' end);
  elsif p_operation = 'ack' then
    -- Consumer acknowledgment: no further delivery is needed. Idempotent; no display change.
    update public.laai_approval_events set consumed_at=coalesce(consumed_at,now())
      where session_id=s.id and id=public.laai_uuid_or_null(p_payload->>'event_id')
      returning jsonb_build_object('event_id',id,'cursor',lpad(cursor::text,19,'0'),'consumed_at',consumed_at)
      into response;
    if response is null then perform public.laai_workflow_error('EVENT_NOT_FOUND','Event not found',404); end if;
    return response;
  elsif p_operation = 'events' then
    select coalesce(jsonb_agg(e.value order by e.cursor),'[]') into response from (
      select cursor,jsonb_build_object('event_id',id,'cursor',lpad(cursor::text,19,'0'),
        'type',event_type,'session_id',session_id,'action_id',action_id,'page_id',page_id,
        'occurred_at',emitted_at,'consumed_at',consumed_at,
        'action_status',(select status from public.laai_actions x where x.session_id=e2.session_id and x.id=e2.action_id)) value
      from public.laai_approval_events e2 where session_id=s.id
        and cursor > coalesce(nullif(p_payload->>'after','')::bigint,0)
      order by cursor limit 100
    ) e;
    return jsonb_build_object('events',response);
  elsif p_operation in ('approve','reject','result') then
    select * into a from public.laai_actions where session_id=s.id and id=p_payload->>'action_id' for update;
    if not found then perform public.laai_workflow_error('ACTION_NOT_FOUND','Action not found',404); end if;
    if a.page_id is distinct from s.active_page_id or a.status='invalidated' then
      perform public.laai_workflow_error('CONFLICT','Action belongs to an old page or proposal',409);
    end if;
    if p_operation in ('approve','reject') then
      if s.revision <> (p_payload->>'expected_revision')::bigint then
        perform public.laai_workflow_error('STALE_REVISION','Display changed; refresh and confirm again.',409);
      end if;
      if a.expires_at <= now() then perform public.laai_workflow_error('ACTION_EXPIRED','Action expired',409); end if;
      if a.status <> 'proposed' then perform public.laai_workflow_error('CONFLICT','Action already decided or consumed',409); end if;
      a.status := case when p_operation='approve' then 'approved' else 'rejected' end;
      update public.laai_actions set status=a.status,
        approved_at=case when p_operation='approve' then now() else null end where session_id=s.id and id=a.id;
      if p_operation='approve' then
        -- State change and outbox insert commit in the same transaction.
        insert into public.laai_approval_events(session_id,action_id,page_id)
          values(s.id,a.id,a.page_id) returning id into event_id;
      end if;
      response := jsonb_build_object('action_id',a.id,'status',a.status,'event_id',event_id);
    else
      if a.status not in ('approved','running') then
        perform public.laai_workflow_error('CONFLICT','Only approved actions accept execution results',409);
      end if;
      if p_payload->>'status' not in ('running','completed','failed') then
        perform public.laai_workflow_error('INVALID_PAYLOAD','Invalid result status',422);
      end if;
      if a.expires_at <= now() and a.status='approved' and p_payload->>'status'<>'failed' then
        perform public.laai_workflow_error('ACTION_EXPIRED','Action expired before execution',409);
      end if;
      update public.laai_actions set status=p_payload->>'status',result_json=p_payload
        where session_id=s.id and id=a.id;
      if p_payload->>'status' in ('completed','failed') then
        update public.laai_approval_events set consumed_at=now() where session_id=s.id and action_id=a.id;
      end if;
      response := jsonb_build_object('action_id',a.id,'status',p_payload->>'status');
    end if;
  else
    perform public.laai_workflow_error('INVALID_PAYLOAD','Unknown operation',422);
  end if;
  update public.laai_sessions set revision=revision+1,updated_at=now() where id=s.id returning * into s;
  if response is null then response:=jsonb_build_object('schema_version','1.0','session_id',s.id,'revision',s.revision); end if;
  insert into public.laai_idempotency_keys(session_id,request_id,operation,request_json,response_json)
    values(s.id,request_key,p_operation,p_payload,response);
  return response;
exception when sqlstate 'LW001' then
  get stacked diagnostics error_code=message_text,error_detail=pg_exception_detail,error_status=pg_exception_hint;
  return jsonb_build_object('error',jsonb_build_object('code',error_code,'message',error_detail), 'status',error_status::int);
end $$;
revoke all on function public.laai_workflow_apply(uuid,text,uuid,jsonb) from public, anon, authenticated;
grant execute on function public.laai_workflow_apply(uuid,text,uuid,jsonb) to service_role;

-- Public entry point. The request counter commits even when the operation fails and rolls back.
create or replace function public.laai_workflow(p_user_id uuid, p_operation text, p_session_id uuid, p_payload jsonb)
returns jsonb language plpgsql set search_path = public as $$
declare used int;
begin
  insert into public.laai_request_buckets(user_id) values(p_user_id)
  on conflict(user_id) do update set
    requests=case when laai_request_buckets.window_start < now()-interval '1 minute' then 1 else laai_request_buckets.requests+1 end,
    window_start=case when laai_request_buckets.window_start < now()-interval '1 minute' then now() else laai_request_buckets.window_start end
  returning requests into used;
  if used > 600 then
    return jsonb_build_object('error',jsonb_build_object('code','RATE_LIMITED',
      'message','Too many requests; try again in a minute'),'status',429);
  end if;
  return public.laai_workflow_apply(p_user_id, p_operation, p_session_id, p_payload);
end $$;
revoke all on function public.laai_workflow(uuid,text,uuid,jsonb) from public, anon, authenticated;
grant execute on function public.laai_workflow(uuid,text,uuid,jsonb) to service_role;
notify pgrst, 'reload schema';
commit;
