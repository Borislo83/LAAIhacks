begin;

create table public.laai_devices (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  last_seen_at timestamptz not null default now(),
  unique (id, user_id)
);
create table public.laai_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  device_id uuid,
  active_page_id text,
  revision bigint not null default 1 check (revision > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  expires_at timestamptz not null default now() + interval '24 hours',
  foreign key (device_id, user_id) references public.laai_devices(id, user_id)
);
create table public.laai_pages (
  id text not null,
  session_id uuid not null references public.laai_sessions(id) on delete cascade,
  source_url text not null,
  kind text not null,
  title text not null,
  summary_json jsonb not null default '[]' check (jsonb_typeof(summary_json) = 'array'),
  facts_json jsonb not null default '[]' check (jsonb_typeof(facts_json) = 'array'),
  source_label text not null,
  updated_at timestamptz not null,
  primary key (session_id, id)
);
create table public.laai_actions (
  id text not null,
  session_id uuid not null references public.laai_sessions(id) on delete cascade,
  page_id text not null,
  type text not null check (type in ('compare_prices','find_reviews','open_url','read_article','custom')),
  label text not null,
  description text not null,
  status text not null default 'proposed' check (status in ('proposed','approved','rejected','running','completed','failed','invalidated')),
  requires_approval boolean not null default true check (requires_approval),
  expires_at timestamptz not null,
  proposed_at timestamptz not null default now(),
  approved_at timestamptz,
  result_json jsonb,
  primary key (session_id, id),
  foreign key (session_id, page_id) references public.laai_pages(session_id, id)
);
create table public.laai_approval_events (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null,
  action_id text not null,
  page_id text not null,
  cursor bigint generated always as identity unique,
  event_type text not null default 'action.approved' check (event_type = 'action.approved'),
  emitted_at timestamptz not null default now(),
  consumed_at timestamptz,
  unique (session_id, action_id),
  foreign key (session_id, action_id) references public.laai_actions(session_id, id) on delete cascade
);
create table public.laai_idempotency_keys (
  session_id uuid not null references public.laai_sessions(id) on delete cascade,
  request_id text not null,
  operation text not null,
  request_json jsonb not null,
  response_json jsonb not null,
  created_at timestamptz not null default now(),
  primary key (session_id, request_id, operation)
);
alter table public.laai_sessions add constraint laai_active_page
  foreign key (id, active_page_id) references public.laai_pages(session_id,id) deferrable initially deferred;

create table public.laai_request_buckets (
  user_id uuid primary key references auth.users(id) on delete cascade,
  window_start timestamptz not null default now(),
  requests int not null default 1
);
alter table public.laai_request_buckets enable row level security;
revoke all on public.laai_request_buckets from anon, authenticated;
grant all on public.laai_request_buckets to service_role;

create index laai_session_owner_revision on public.laai_sessions(user_id, id, revision);
create index laai_session_expiry on public.laai_sessions(expires_at);
create index laai_actions_page_status on public.laai_actions(session_id, page_id, status);
create index laai_events_session_cursor on public.laai_approval_events(session_id, cursor);

-- Supabase Auth is the users table; do not create a second user identity registry.
alter table public.laai_devices enable row level security;
alter table public.laai_sessions enable row level security;
alter table public.laai_pages enable row level security;
alter table public.laai_actions enable row level security;
alter table public.laai_approval_events enable row level security;
alter table public.laai_idempotency_keys enable row level security;
-- All shared-workflow reads go through the role-checked API. Direct clients get no grants.
revoke all on public.laai_devices, public.laai_sessions, public.laai_pages, public.laai_actions,
  public.laai_approval_events, public.laai_idempotency_keys from anon, authenticated;
grant all on public.laai_devices, public.laai_sessions, public.laai_pages, public.laai_actions,
  public.laai_approval_events, public.laai_idempotency_keys to service_role;
grant usage, select on sequence public.laai_approval_events_cursor_seq to service_role;

-- Raising internally rolls back all writes in an operation before returning the safe error.
create function public.laai_workflow_error(p_code text, p_message text, p_status int)
returns void language plpgsql set search_path = public as $$
begin
  raise exception using errcode = 'LW001', message = p_code,
    detail = p_message, hint = p_status::text;
end $$;
revoke all on function public.laai_workflow_error(text,text,int) from public, anon, authenticated;
grant execute on function public.laai_workflow_error(text,text,int) to service_role;

create function public.laai_workflow(p_user_id uuid, p_operation text, p_session_id uuid, p_payload jsonb)
returns jsonb language plpgsql set search_path = public as $$
declare
  s public.laai_sessions;
  a public.laai_actions;
  item jsonb;
  response jsonb;
  previous public.laai_idempotency_keys;
  event_id uuid;
  error_code text; error_detail text; error_status text;
  request_key text := p_payload->>'request_id';
begin
  -- A shared database bucket applies across API processes and restarts.
  insert into public.laai_request_buckets(user_id) values(p_user_id)
  on conflict(user_id) do update set
    requests=case when laai_request_buckets.window_start < now()-interval '1 minute' then 1 else laai_request_buckets.requests+1 end,
    window_start=case when laai_request_buckets.window_start < now()-interval '1 minute' then now() else laai_request_buckets.window_start end;
  if (select requests from public.laai_request_buckets where user_id=p_user_id)>600 then
    perform public.laai_workflow_error('RATE_LIMITED','Too many requests; try again in a minute',429);
  end if;
  if p_operation = 'create' then
    insert into public.laai_sessions(user_id) values(p_user_id) returning * into s;
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
    update public.laai_actions set status='invalidated'
      where session_id=s.id and status <> 'invalidated';
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
    update public.laai_actions set status='invalidated'
      where session_id=s.id and status <> 'invalidated';
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
  elsif p_operation = 'events' then
    select coalesce(jsonb_agg(e.value order by e.cursor),'[]') into response from (
      select cursor,jsonb_build_object('event_id',id,'cursor',lpad(cursor::text,19,'0'),
        'type',event_type,'session_id',session_id,'action_id',action_id,'page_id',page_id,
        'occurred_at',emitted_at,'consumed_at',consumed_at) value
      from public.laai_approval_events where session_id=s.id
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
revoke all on function public.laai_workflow(uuid,text,uuid,jsonb) from public, anon, authenticated;
grant execute on function public.laai_workflow(uuid,text,uuid,jsonb) to service_role;

-- Explicit operator-run retention; no hidden timer. Expired sessions are retained 30 days,
-- then cascading deletion removes their pages, actions, events and idempotency responses.
create function public.laai_purge_expired_sessions() returns bigint language plpgsql set search_path=public as $$
declare removed bigint;
begin
  delete from public.laai_sessions where expires_at < now() - interval '30 days';
  get diagnostics removed = row_count;
  return removed;
end $$;
revoke all on function public.laai_purge_expired_sessions() from public, anon, authenticated;
grant execute on function public.laai_purge_expired_sessions() to service_role;
notify pgrst, 'reload schema';
commit;
