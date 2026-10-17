begin;

create table public.laai_settings (
    user_id uuid primary key references auth.users(id) on delete cascade,
    enabled_agents text[] not null default array['price', 'alternatives', 'reviews']
        check (enabled_agents <@ array['price', 'alternatives', 'reviews']::text[]),
    require_confirmation boolean not null default true,
    projection_enabled boolean not null default true,
    text_scale double precision not null default 1 check (text_scale between 0.5 and 3)
);

create table public.laai_contexts (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    page jsonb not null,
    prediction_source text not null check (prediction_source in ('asi_one', 'rules', 'none')),
    created_at timestamptz not null default now(),
    unique (id, user_id)
);

create table public.laai_tasks (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    context_id uuid not null,
    kind text not null check (kind in ('price', 'alternatives', 'reviews')),
    status text not null default 'predicted'
        check (status in ('predicted', 'running', 'succeeded', 'failed')),
    payload jsonb not null,
    result jsonb,
    error text,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz,
    foreign key (context_id, user_id) references public.laai_contexts(id, user_id) on delete cascade,
    unique (context_id, kind)
);

create index laai_contexts_user_created on public.laai_contexts(user_id, created_at desc);
create index laai_tasks_user_created on public.laai_tasks(user_id, created_at desc, id desc);

alter table public.laai_settings enable row level security;
alter table public.laai_contexts enable row level security;
alter table public.laai_tasks enable row level security;

-- Clients can read only their own rows. All writes pass through the backend.
revoke all on public.laai_settings, public.laai_contexts, public.laai_tasks from anon, authenticated;
grant select on public.laai_settings, public.laai_contexts, public.laai_tasks to authenticated;
grant all on public.laai_settings, public.laai_contexts, public.laai_tasks to service_role;
create policy own_settings on public.laai_settings for select to authenticated
    using ((select auth.uid()) = user_id);
create policy own_contexts on public.laai_contexts for select to authenticated
    using ((select auth.uid()) = user_id);
create policy own_tasks on public.laai_tasks for select to authenticated
    using ((select auth.uid()) = user_id);

-- Reads initialize defaults exactly once, without overwriting saved preferences.
create function public.laai_get_settings(p_user_id uuid)
returns jsonb language plpgsql set search_path = '' as $$
declare v_settings public.laai_settings;
begin
    insert into public.laai_settings(user_id) values (p_user_id) on conflict do nothing;
    select * into strict v_settings from public.laai_settings where user_id = p_user_id;
    return to_jsonb(v_settings);
end;
$$;

-- Persist the page and its predictions together, including pages with no tasks.
create function public.laai_create_context(p_user_id uuid, p_page jsonb, p_source text, p_tasks jsonb)
returns jsonb language plpgsql set search_path = '' as $$
declare v_context_id uuid; v_tasks jsonb;
begin
    insert into public.laai_contexts(user_id, page, prediction_source)
        values (p_user_id, p_page, p_source) returning id into v_context_id;
    insert into public.laai_tasks(user_id, context_id, kind, payload)
        select p_user_id, v_context_id, item->>'kind', item->'payload'
        from jsonb_array_elements(p_tasks) item;
    select coalesce(jsonb_agg(to_jsonb(t) order by t.created_at, t.kind), '[]'::jsonb)
        into v_tasks from public.laai_tasks t where t.context_id = v_context_id;
    return jsonb_build_object('context_id', v_context_id, 'prediction_source', p_source, 'tasks', v_tasks);
end;
$$;

-- The row lock serializes dispatch across backend processes. Lock preferences
-- as well so a permission update and dispatch have a definite database order.
create function public.laai_claim_task(p_user_id uuid, p_task_id uuid, p_confirmed boolean)
returns jsonb language plpgsql set search_path = '' as $$
declare v_task public.laai_tasks; v_settings public.laai_settings;
begin
    select * into v_settings from public.laai_settings where user_id = p_user_id for share;
    select * into v_task from public.laai_tasks
        where id = p_task_id and user_id = p_user_id for update;
    if not found then raise sqlstate 'PT404' using message = 'Task not found'; end if;
    if v_settings.user_id is null or not (v_task.kind = any(v_settings.enabled_agents))
        or (v_settings.require_confirmation and not coalesce(p_confirmed, false)) then
        raise sqlstate 'PT403' using message = 'Agent disabled or confirmation required';
    end if;
    if v_task.status <> 'predicted' then
        raise sqlstate 'PT409' using message = 'Task already dispatched';
    end if;
    update public.laai_tasks set status = 'running', started_at = now()
        where id = p_task_id and user_id = p_user_id returning * into v_task;
    return to_jsonb(v_task);
end;
$$;

create function public.laai_finish_task(p_user_id uuid, p_task_id uuid, p_result jsonb, p_error text)
returns jsonb language plpgsql set search_path = '' as $$
declare v_task public.laai_tasks;
begin
    update public.laai_tasks
        set status = case when p_error is null then 'succeeded' else 'failed' end,
            result = p_result, error = p_error, finished_at = now()
        where id = p_task_id and user_id = p_user_id and status = 'running'
        returning * into v_task;
    if not found then raise sqlstate 'PT409' using message = 'Task is not running'; end if;
    return to_jsonb(v_task);
end;
$$;

-- RPCs accept a user id only from our authenticated backend, never from clients.
revoke all on function public.laai_get_settings(uuid) from public, anon, authenticated;
revoke all on function public.laai_create_context(uuid, jsonb, text, jsonb) from public, anon, authenticated;
revoke all on function public.laai_claim_task(uuid, uuid, boolean) from public, anon, authenticated;
revoke all on function public.laai_finish_task(uuid, uuid, jsonb, text) from public, anon, authenticated;
grant execute on function public.laai_get_settings(uuid) to service_role;
grant execute on function public.laai_create_context(uuid, jsonb, text, jsonb) to service_role;
grant execute on function public.laai_claim_task(uuid, uuid, boolean) to service_role;
grant execute on function public.laai_finish_task(uuid, uuid, jsonb, text) to service_role;

notify pgrst, 'reload schema';

commit;
