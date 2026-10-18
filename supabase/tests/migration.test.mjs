import assert from 'node:assert/strict';
import { test } from 'node:test';
import { STACKS, alice, bob, asRole, call, count, freshDb } from './helpers.mjs';

for (const [stack, migrations] of Object.entries(STACKS)) test(`dashboard settings and tasks [${stack}]`, async (t) => {
  const db = await freshDb(migrations);
  try {
    const rpc = (name, args) => call(db, name, args);
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
      const before = await count(db, 'public.laai_contexts');
      await assert.rejects(rpc('laai_create_context', [alice, page, 'rules', JSON.stringify([
        { kind: 'invalid-agent', payload: {} },
      ])]), { code: '23514' });
      assert.equal(await count(db, 'public.laai_contexts'), before);
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
      await asRole(db, 'authenticated', bob);
      assert.equal((await db.query('select * from public.laai_tasks')).rows.length, 0);
      await asRole(db, 'authenticated', alice);
      assert.equal((await db.query('select * from public.laai_tasks')).rows.length, 1);
      await assert.rejects(db.query("update public.laai_tasks set status = 'predicted'"), { code: '42501' });
      await assert.rejects(rpc('laai_claim_task', [alice, task.id, true]), { code: '42501' });
      await asRole(db, 'anon');
      await assert.rejects(db.query('select * from public.laai_tasks'), { code: '42501' });
      await db.exec('reset role');
    });

    await t.test('deleting an Auth user cascades through their saved data', async () => {
      await db.query('delete from auth.users where id = $1', [alice]);
      for (const table of ['laai_settings', 'laai_contexts', 'laai_tasks']) {
        assert.equal(await count(db, `public.${table} where user_id = $1`, [alice]), 0);
      }
    });
  } finally {
    await db.close();
  }
});
