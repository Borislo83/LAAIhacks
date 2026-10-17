begin;
create table public.laai_user_preferences (
  user_id uuid primary key references auth.users(id) on delete cascade,
  display_density text not null default 'comfortable' check(display_density in ('comfortable','compact')),
  shopping_priority text not null default 'balanced' check(shopping_priority in ('balanced','price','quality')),
  proactive_enabled boolean not null default false,
  notes text not null default '' check(char_length(notes)<=10000),
  updated_at timestamptz not null default now()
);
alter table public.laai_user_preferences enable row level security;
create policy laai_preferences_owner_read on public.laai_user_preferences for select
  to authenticated using(user_id=auth.uid());
revoke all on public.laai_user_preferences from anon, authenticated;
grant select on public.laai_user_preferences to authenticated;
grant all on public.laai_user_preferences to service_role;

create function public.laai_preferences(p_user_id uuid,p_preferences jsonb default null)
returns jsonb language plpgsql set search_path=public as $$
declare saved public.laai_user_preferences;
begin
  if p_preferences is null then
    insert into public.laai_user_preferences(user_id) values(p_user_id) on conflict(user_id) do nothing;
    select * into saved from public.laai_user_preferences where user_id=p_user_id;
  else
    insert into public.laai_user_preferences(user_id,display_density,shopping_priority,proactive_enabled,notes)
      values(p_user_id,p_preferences->>'display_density',p_preferences->>'shopping_priority',
        (p_preferences->>'proactive_enabled')::boolean,p_preferences->>'notes')
      on conflict(user_id) do update set display_density=excluded.display_density,
        shopping_priority=excluded.shopping_priority,proactive_enabled=excluded.proactive_enabled,
        notes=excluded.notes,updated_at=now()
      where (laai_user_preferences.display_density,laai_user_preferences.shopping_priority,
        laai_user_preferences.proactive_enabled,laai_user_preferences.notes) is distinct from
        (excluded.display_density,excluded.shopping_priority,excluded.proactive_enabled,excluded.notes)
      returning * into saved;
    if found then
      update public.laai_sessions set revision=revision+1,updated_at=now()
        where user_id=p_user_id and expires_at>now();
    else
      select * into saved from public.laai_user_preferences where user_id=p_user_id;
    end if;
  end if;
  return to_jsonb(saved)-'user_id';
end $$;
revoke all on function public.laai_preferences(uuid,jsonb) from public,anon,authenticated;
grant execute on function public.laai_preferences(uuid,jsonb) to service_role;
notify pgrst, 'reload schema';
commit;
