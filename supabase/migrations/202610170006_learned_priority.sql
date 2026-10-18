-- Learned shopping priority (services/orchestrator/src/laai_orchestrator/learning.py).
-- The priority itself is computed from laai_tasks / laai_actions on read; nothing learned is
-- stored. This only records when the user last reset learning, so older decisions are ignored.
begin;
alter table public.laai_user_preferences add column if not exists learned_since timestamptz;

create or replace function public.laai_reset_learned_priority(p_user_id uuid)
returns jsonb language plpgsql set search_path=public as $$
declare saved public.laai_user_preferences;
begin
  insert into public.laai_user_preferences(user_id,learned_since) values(p_user_id,now())
    on conflict(user_id) do update set learned_since=now(),updated_at=now()
    returning * into saved;
  -- Open glasses sessions re-read preferences, as after any other preference change.
  update public.laai_sessions set revision=revision+1,updated_at=now()
    where user_id=p_user_id and expires_at>now();
  return to_jsonb(saved)-'user_id';
end $$;
revoke all on function public.laai_reset_learned_priority(uuid) from public,anon,authenticated;
grant execute on function public.laai_reset_learned_priority(uuid) to service_role;
notify pgrst, 'reload schema';
commit;
