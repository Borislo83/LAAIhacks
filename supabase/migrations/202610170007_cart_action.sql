-- Cart action (agents team). Safe to apply on top of 001-006, and to rerun. Registers one
-- more action type, 'add_to_amazon_cart': the page watcher proposes adding one named Amazon
-- listing (title, ASIN, price, condition, seller in the description) to the user's cart. Like
-- every action it requires approval on the glasses; the executor adds only that ASIN, verifies
-- it in the cart and never starts checkout. laai_workflow_apply is 005's body with two
-- changes: the registered-type list, and display's page also carries its `url` (additive),
-- so the watcher can tell an amazon.com /dp/<ASIN> page and add exactly that ASIN.
begin;

alter table public.laai_actions drop constraint if exists laai_actions_type_check;
alter table public.laai_actions add constraint laai_actions_type_check check (type in
  ('compare_prices','find_reviews','add_to_amazon_cart','open_url','read_article','custom'));

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
  -- Writes lock the session to serialize page updates, approval, idempotency and results.
  -- Reads (status, display, events, ack) take no lock, so polls never queue behind a write.
  if p_operation in ('page','actions','approve','reject','result') then
    select * into s from public.laai_sessions where id=p_session_id and user_id=p_user_id for update;
  else
    select * into s from public.laai_sessions where id=p_session_id and user_id=p_user_id;
  end if;
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
      -- The implemented handlers: two research actions and the approval-gated cart add.
      if item->>'type' not in ('compare_prices','find_reviews','add_to_amazon_cart') then
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
    -- One statement, so the revision, page and actions come from the same snapshot even
    -- without a session lock. The mode flags come from the same aggregate as the actions.
    select jsonb_build_object('schema_version','1.0','session_id',ss.id,'revision',ss.revision,
      'page',pg.page,'actions',coalesce(ac.actions,'[]'),'notice',null,'mode',
      case when ac.running then 'loading' when ac.failed then 'error'
        when ac.completed then 'result' when ac.approved then 'loading'
        when ac.total>0 then 'actions' else 'summary' end) into response
    from public.laai_sessions ss
    left join lateral (
      select jsonb_build_object('page_id',p.id,'title',p.title,'kind',p.kind,'summary',p.summary_json,
        'facts',p.facts_json,'source_label',p.source_label,'url',p.source_url) page
      from public.laai_pages p where p.session_id=ss.id and p.id=ss.active_page_id
    ) pg on true
    cross join lateral (
      select jsonb_agg(jsonb_build_object('action_id',x.id,'label',x.label,'type',x.type,
          'description',x.description,'status',x.status,'requires_approval',x.requires_approval,
          'expires_at',x.expires_at,'result',x.result_json) order by x.proposed_at,x.id) actions,
        count(*) total,
        bool_or(x.status='running') running, bool_or(x.status='failed') failed,
        bool_or(x.status='completed') completed, bool_or(x.status='approved') approved
      from public.laai_actions x
      where x.session_id=ss.id and x.page_id=ss.active_page_id and x.status<>'invalidated'
    ) ac
    where ss.id=s.id;
    return response;
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

notify pgrst, 'reload schema';
commit;
