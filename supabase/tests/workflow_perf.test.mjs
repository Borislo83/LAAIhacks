import assert from 'node:assert/strict';
import {test} from 'node:test';
import {LIVE,NEXT,STACKS,alice,action,asRole,call,count,freshDb,migration,page,workflow} from './helpers.mjs';

// A row lock stamps the tuple's xmax. PGlite has one connection, so probe xmax instead of
// blocking a second one. Earlier lockers (FK checks, rolled-back probes) also leave xmax set,
// so each probe starts from a freshly written, unlocked row version.
const unlock=(db,sid)=>db.query('update public.laai_sessions set updated_at=updated_at where id=$1',[sid]);
const lockedByMe=async(db,sid)=>(await db.query(
 "select xmax::text<>'0' locked from public.laai_sessions where id=$1",[sid])).rows[0].locked;

// Display modes must match 004's EXISTS probes exactly, including their precedence.
for(const [stack,migrations] of Object.entries(STACKS))test(`display mode precedence [${stack}]`,async()=>{
 const db=await freshDb(migrations);
 try{
  const rpc=workflow(db);const sid=(await rpc('create')).session_id;
  const mode=async()=>(await rpc('display',sid)).mode;
  const display=await rpc('display',sid);
  assert.equal(display.mode,'summary');assert.equal(display.page,null);assert.deepEqual(display.actions,[]);
  await rpc('page',sid,page);assert.equal(await mode(),'summary');
  const ids=['a-run','a-fail','a-done','a-ok'];
  await rpc('actions',sid,{request_id:'a1',page_id:'page-1',actions:ids.map(action_id=>({...action,action_id}))});
  assert.equal(await mode(),'actions');
  const set=status=>id=>db.query('update public.laai_actions set status=$1 where id=$2',[status,id]);
  await set('approved')('a-ok');assert.equal(await mode(),'loading');
  await set('completed')('a-done');assert.equal(await mode(),'result');
  await set('failed')('a-fail');assert.equal(await mode(),'error');
  await set('running')('a-run');assert.equal(await mode(),'loading');
  for(const id of ids)await set('invalidated')(id);
  const cleared=await rpc('display',sid);assert.equal(cleared.mode,'summary');assert.deepEqual(cleared.actions,[]);
 }finally{await db.close()}
});

test('005 is idempotent, swaps indexes and runs on top of the live schema',async()=>{
 const db=await freshDb(NEXT);
 try{
  await db.exec('reset role');await db.exec(await migration(NEXT.at(-1)));
  const indexes=(await db.query("select indexname from pg_indexes where schemaname='public'")).rows.map(r=>r.indexname);
  assert.ok(!indexes.includes('laai_session_owner_revision'));
  assert.ok(!indexes.includes('laai_events_unconsumed'));
  assert.ok(indexes.includes('laai_sessions_user_expiry'));
  // Preference saves bump revisions by (user_id, expires_at); the new index serves them.
  await db.exec('set enable_seqscan=off');
  const plan=(await db.query('explain select 1 from public.laai_sessions where user_id=$1 and expires_at>now()',[alice])).rows.map(r=>r['QUERY PLAN']).join('\n');
  assert.match(plan,/laai_sessions_user_expiry/);
 }finally{await db.close()}
});

test('read operations leave the session row unlocked; writes still lock it',async()=>{
 for(const [migrations,readsLock] of [[LIVE,true],[NEXT,false]]){
  const db=await freshDb(migrations);
  try{
   const rpc=workflow(db);const sid=(await rpc('create')).session_id;
   await rpc('page',sid,page);
   const rev=(await rpc('actions',sid,{request_id:'a1',page_id:'page-1',actions:[action]})).revision;
   const {event_id}=await rpc('approve',sid,{request_id:'t1',action_id:'act-1',expected_revision:rev});
   for(const [op,p] of [['status',{}],['display',{}],['events',{}],['ack',{event_id}]]){
    await unlock(db,sid);await db.exec('begin');
    try{
     assert.equal(await lockedByMe(db,sid),false);
     assert.ok(!(await rpc(op,sid,p)).error,op);
     assert.equal(await lockedByMe(db,sid),readsLock,`${op} lock on ${migrations.length} migrations`);
    }finally{await db.exec('rollback')}
   }
   // The probe does see locks: a rejected write still locked the row before rejecting.
   await unlock(db,sid);await db.exec('begin');
   try{
    assert.equal((await rpc('approve',sid,{request_id:'t2',action_id:'act-1',expected_revision:1})).error.code,'STALE_REVISION');
    assert.equal(await lockedByMe(db,sid),true);
   }finally{await db.exec('rollback')}
  }finally{await db.close()}
 }
});

test('rate limit is checked before work and counted after it',async()=>{
 const db=await freshDb(NEXT);
 try{
  const rpc=workflow(db);
  const bucket=async()=>(await db.query('select requests from public.laai_request_buckets where user_id=$1',[alice])).rows[0]?.requests;
  assert.equal(await bucket(),undefined);
  const sid=(await rpc('create')).session_id;assert.equal(await bucket(),1);
  await rpc('display',sid);await rpc('status',sid);assert.equal(await bucket(),3);
  // Unknown operations and other errors count too.
  assert.equal((await rpc('nonsense',sid)).error.code,'INVALID_PAYLOAD');assert.equal(await bucket(),4);
  await db.query('update public.laai_request_buckets set requests=599 where user_id=$1',[alice]);
  assert.equal((await rpc('page',sid,page)).revision,2);assert.equal(await bucket(),600);
  // The 601st request is refused before touching the session, and still counts.
  const refused=await rpc('page',sid,{...page,request_id:'p2',title:'Refused'});
  assert.equal(refused.error.code,'RATE_LIMITED');assert.equal(refused.status,429);
  assert.equal(await bucket(),601);
  assert.equal((await db.query('select revision from public.laai_sessions where id=$1',[sid])).rows[0].revision,2);
  assert.equal(await count(db,"public.laai_idempotency_keys where request_id='p2'"),0);
 }finally{await db.close()}
});

test('retention purges expired sessions and 30-day-old dashboard history',async()=>{
 const db=await freshDb(NEXT);
 try{
  const rpc=workflow(db);
  const old=(await rpc('create')).session_id,fresh=(await rpc('create')).session_id;
  await db.query("update public.laai_sessions set expires_at=now()-interval '31 days' where id=$1",[old]);
  const ctx=async()=>(await call(db,'laai_create_context',[alice,'{}','rules',JSON.stringify([{kind:'price',payload:{}},{kind:'reviews',payload:{}}])])).context_id;
  const stale=await ctx(),recent=await ctx();
  await db.query("update public.laai_contexts set created_at=now()-interval '31 days' where id=$1",[stale]);
  await db.query("update public.laai_tasks set created_at=now()-interval '31 days' where context_id=$1",[stale]);
  assert.deepEqual(await call(db,'laai_purge_expired',[]),{sessions:1,contexts:1,tasks:2});
  assert.equal(await count(db,'public.laai_sessions where id=$1',[fresh]),1);
  assert.equal(await count(db,'public.laai_contexts where id=$1',[recent]),1);
  assert.equal(await count(db,'public.laai_tasks where context_id=$1',[recent]),2);
  assert.deepEqual(await call(db,'laai_purge_expired',[]),{sessions:0,contexts:0,tasks:0});
  await asRole(db,'authenticated');
  for(const fn of ['laai_purge_expired()',"laai_count_request('"+alice+"')"])
   await assert.rejects(db.query(`select public.${fn}`),{code:'42501'});
 }finally{await db.close()}
});

test('purge is scheduled only when pg_cron is installed',async()=>{
 // PGlite has no pg_cron: freshDb already applied 005 without it. Stub cron.schedule to see the call.
 const db=await freshDb(NEXT);
 try{
  await db.exec(`reset role;create schema cron;create table cron.jobs(name text primary key,schedule text,command text);
  create function cron.schedule(n text,s text,c text) returns bigint language sql as
   $$ insert into cron.jobs values(n,s,c) on conflict(name) do update set schedule=excluded.schedule,command=excluded.command returning 1::bigint $$;`);
  assert.equal(await count(db,'cron.jobs'),0);
  for(let i=0;i<2;i++)await db.exec(await migration(NEXT.at(-1)));
  assert.deepEqual((await db.query('select * from cron.jobs')).rows,[{name:'laai-purge-expired',schedule:'17 3 * * *',command:'select public.laai_purge_expired()'}]);
 }finally{await db.close()}
});
