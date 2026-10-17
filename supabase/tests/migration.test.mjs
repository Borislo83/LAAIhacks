import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { PGlite } from '@electric-sql/pglite';

const alice = '00000000-0000-4000-8000-000000000001';
const bob = '00000000-0000-4000-8000-000000000002';

test('Supabase migration and database invariants', async (t) => {
  const db = new PGlite();
  try {
    // Stub only Supabase's pre-existing Auth objects, not our application SQL.
    await db.exec(`
      create role anon;
      create role authenticated;
      create role service_role bypassrls;
      create schema auth;
      create table auth.users (id uuid primary key);
      create function auth.uid() returns uuid language sql as
        $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
      grant usage on schema public, auth to anon, authenticated, service_role;
      grant execute on function auth.uid() to authenticated;
    `);
    await db.exec(await readFile(new URL('../migrations/202610170001_orchestrator.sql', import.meta.url), 'utf8'));
    await db.query('insert into auth.users(id) values ($1), ($2)', [alice, bob]);
    await db.exec('set role service_role');
    const rpc = async (name, args) => {
      const placeholders = args.map((_, i) => `$${i + 1}`).join(',');
      const { rows } = await db.query(`select to_jsonb(public.${name}(${placeholders})) as value`, args);
      return rows[0].value;
    };
    const page = JSON.stringify({ url: 'https://shop.example', product: 'Sony XM5' });
    const predicted = JSON.stringify([{ kind: 'price', payload: { product: 'Sony XM5' } }]);
    let task;

    await t.test('defaults are saved without overwriting later settings', async () => {
      assert.equal((await rpc('laai_get_settings', [alice])).require_confirmation, true);
      await db.query('update public.laai_settings set text_scale = 1.5 where user_id = $1', [alice]);
      assert.equal((await rpc('laai_get_settings', [alice])).text_scale, 1.5);
      await rpc('laai_get_settings', [bob]);
    });

    await t.test('page and predictions are committed together', async () => {
      const saved = await rpc('laai_create_context', [alice, page, 'rules', predicted]);
      task = saved.tasks[0];
      assert.equal(task.status, 'predicted');
      assert.equal(task.context_id, saved.context_id);
      const before = (await db.query('select count(*)::int as n from public.laai_contexts')).rows[0].n;
      await assert.rejects(rpc('laai_create_context', [alice, page, 'rules', JSON.stringify([
        { kind: 'invalid-agent', payload: {} },
      ])]), { code: '23514' });
      assert.equal((await db.query('select count(*)::int as n from public.laai_contexts')).rows[0].n, before);
    });

    await t.test('ownership, confirmation, and current permissions gate dispatch', async () => {
      await assert.rejects(rpc('laai_claim_task', [bob, task.id, true]), { code: 'PT404' });
      await assert.rejects(rpc('laai_claim_task', [alice, task.id, false]), { code: 'PT403' });
      await db.query("update public.laai_settings set enabled_agents = '{}' where user_id = $1", [alice]);
      await assert.rejects(rpc('laai_claim_task', [alice, task.id, true]), { code: 'PT403' });
      await db.query("update public.laai_settings set enabled_agents = '{price}' where user_id = $1", [alice]);
    });

    await t.test('a task can be claimed once and its result is persistent', async () => {
      assert.equal((await rpc('laai_claim_task', [alice, task.id, true])).status, 'running');
      await assert.rejects(rpc('laai_claim_task', [alice, task.id, true]), { code: 'PT409' });
      const finished = await rpc('laai_finish_task', [alice, task.id, JSON.stringify({ summary: 'Saved $100' }), null]);
      assert.equal(finished.status, 'succeeded');
      assert.equal(finished.result.summary, 'Saved $100');
      assert.ok(finished.finished_at);
      await assert.rejects(rpc('laai_finish_task', [alice, task.id, '{}', null]), { code: 'PT409' });
    });

    await t.test('row-level security hides other users and denies direct writes/RPCs', async () => {
      await db.exec('reset role; set role authenticated');
      await db.query("select set_config('request.jwt.claim.sub', $1, false)", [bob]);
      assert.equal((await db.query('select * from public.laai_tasks')).rows.length, 0);
      await db.query("select set_config('request.jwt.claim.sub', $1, false)", [alice]);
      assert.equal((await db.query('select * from public.laai_tasks')).rows.length, 1);
      await assert.rejects(db.query("update public.laai_tasks set status = 'predicted'"), { code: '42501' });
      await assert.rejects(rpc('laai_claim_task', [alice, task.id, true]), { code: '42501' });
      await db.exec('reset role; set role anon');
      await assert.rejects(db.query('select * from public.laai_tasks'), { code: '42501' });
      await db.exec('reset role');
    });

    await t.test('deleting an Auth user cascades through their saved data', async () => {
      await db.query('delete from auth.users where id = $1', [alice]);
      for (const table of ['laai_settings', 'laai_contexts', 'laai_tasks']) {
        const { rows } = await db.query(`select count(*)::int as n from public.${table} where user_id = $1`, [alice]);
        assert.equal(rows[0].n, 0);
      }
    });
  } finally {
    await db.close();
  }
});
