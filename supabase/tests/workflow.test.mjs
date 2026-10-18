import assert from 'node:assert/strict';
import {test} from 'node:test';
import {STACKS,alice,bob,action,asRole,count,freshDb,migration,page,workflow} from './helpers.mjs';
for(const [stack,migrations] of Object.entries(STACKS))test(`shared session contract: approvals, outbox, results, retries and devices [${stack}]`,async t=>{
 const db=await freshDb(migrations);
 try{
 // The newest migration must be safe to apply twice on the live project.
 await db.exec('reset role');await db.exec(await migration(migrations.at(-1)));await db.exec('set role service_role');
 const rpc=workflow(db);
 let sid,device,revision;
 await t.test('devices are registered per user and attached to sessions',async()=>{
  device=(await rpc('device')).device_id;assert.ok(device);
  assert.ok((await rpc('device',null,{device_id:device})).last_seen_at);
  assert.equal((await rpc('device',null,{device_id:device},bob)).error.code,'DEVICE_NOT_FOUND');
  assert.equal((await rpc('device',null,{device_id:'not-a-uuid'})).error.code,'DEVICE_NOT_FOUND');
  assert.equal((await rpc('create',null,{device_id:device},bob)).error.code,'DEVICE_NOT_FOUND');
  sid=(await rpc('create',null,{device_id:device})).session_id;
  assert.equal((await db.query('select device_id from public.laai_sessions where id=$1',[sid])).rows[0].device_id,device);
  assert.ok((await rpc('create')).session_id);
 });
 await t.test('page revisions and idempotency survive calls',async()=>{
  const result=await rpc('page',sid,page);revision=result.revision;assert.equal(revision,2);
  assert.deepEqual(await rpc('page',sid,page),result);
  assert.equal((await rpc('page',sid,{...page,title:'Changed'})).error.code,'CONFLICT');
  assert.equal((await rpc('display',sid,{after_revision:revision})).unchanged,true);
  assert.equal((await rpc('display',sid)).page.title,'Headphones');
  assert.equal((await rpc('display',sid,{},bob)).error.code,'SESSION_NOT_FOUND');
 });
 await t.test('action proposals are atomic and registered handlers only',async()=>{
  assert.equal((await rpc('actions',sid,{request_id:'bad-actions',page_id:'page-1',actions:[action,{...action,action_id:'bad',type:'custom'}]})).error.code,'INVALID_PAYLOAD');
  assert.equal((await rpc('display',sid)).actions.length,0);
  revision=(await rpc('actions',sid,{request_id:'a1',page_id:'page-1',actions:[action,{...action,action_id:'act-0'}]})).revision;
  assert.equal((await rpc('result',sid,{request_id:'premature',action_id:action.action_id,status:'completed',message:'not approved'})).error.code,'CONFLICT');
 });
 await t.test('approval and outbox are atomic and idempotent',async()=>{
  assert.equal((await rpc('approve',sid,{request_id:'stale',action_id:'act-1',expected_revision:1})).error.code,'STALE_REVISION');
  assert.equal((await rpc('approve',sid,{request_id:'unknown',action_id:'unknown',expected_revision:revision})).error.code,'ACTION_NOT_FOUND');
  const decision={request_id:'tap-1',action_id:'act-1',expected_revision:revision};
  const result=await rpc('approve',sid,decision);assert.equal(result.status,'approved');
  assert.deepEqual(await rpc('approve',sid,decision),result);
  const events=(await rpc('events',sid)).events;assert.equal(events.length,1);const [event]=events;
  assert.equal(event.event_id,result.event_id);assert.equal(event.page_id,'page-1');
  assert.equal((await rpc('events',sid,{after:event.cursor})).events.length,0);
  assert.equal((await rpc('approve',sid,{...decision,request_id:'other',expected_revision:revision+1})).error.code,'CONFLICT');
 });
 await t.test('executors acknowledge events idempotently without changing display',async()=>{
  const before=(await rpc('display',sid)).revision;
  const approval=await rpc('approve',sid,{request_id:'tap-0',action_id:'act-0',expected_revision:before});
  const event=(await rpc('events',sid)).events.find(e=>e.event_id===approval.event_id);
  assert.equal(event.action_status,'approved');assert.equal(event.consumed_at,null);
  const ack=await rpc('ack',sid,{event_id:approval.event_id});
  assert.equal(ack.event_id,approval.event_id);assert.ok(ack.consumed_at);
  assert.deepEqual(await rpc('ack',sid,{event_id:approval.event_id}),ack);
  assert.equal((await rpc('display',sid)).revision,before+1);
  assert.equal((await rpc('ack',sid,{event_id:'00000000-0000-4000-8000-00000000ffff'})).error.code,'EVENT_NOT_FOUND');
  assert.equal((await rpc('ack',sid,{event_id:'junk'})).error.code,'EVENT_NOT_FOUND');
  assert.equal((await rpc('ack',sid,{event_id:approval.event_id},bob)).error.code,'SESSION_NOT_FOUND');
 });
 await t.test('results, consumption and display revisions persist with safe retries',async()=>{
  const running={request_id:'running-1',action_id:'act-1',status:'running',message:'Researching'};
  await rpc('result',sid,running);assert.equal((await rpc('display',sid)).mode,'loading');
  assert.equal((await rpc('events',sid)).events[0].consumed_at,null);
  const completed={request_id:'result-1',action_id:'act-1',status:'completed',message:'Comparison ready'};
  const result=await rpc('result',sid,completed);const rev=(await rpc('display',sid)).revision;
  assert.deepEqual(await rpc('result',sid,completed),result);
  assert.equal((await rpc('display',sid)).revision,rev);
  assert.equal((await rpc('display',sid)).actions.find(a=>a.action_id==='act-1').result.message,'Comparison ready');
  assert.ok((await rpc('events',sid)).events[0].consumed_at);
  assert.equal((await rpc('result',sid,{...running,request_id:'late-running'})).error.code,'CONFLICT');
 });
 await t.test('page changes invalidate pending and approved actions and close their events',async()=>{
  const rev=(await rpc('actions',sid,{request_id:'a2',page_id:'page-1',actions:[{...action,action_id:'act-2'},{...action,action_id:'act-3'}]})).revision;
  const approval=await rpc('approve',sid,{request_id:'tap-3',action_id:'act-3',expected_revision:rev});
  // Same page ID, new snapshot.
  await rpc('page',sid,{...page,request_id:'p2',title:'Updated page'});
  assert.equal((await rpc('approve',sid,{request_id:'tap-2',action_id:'act-2',expected_revision:rev+2})).error.code,'CONFLICT');
  assert.equal((await rpc('display',sid)).actions.length,0);
  const event=(await rpc('events',sid)).events.find(e=>e.event_id===approval.event_id);
  assert.equal(event.action_status,'invalidated');assert.ok(event.consumed_at);
  assert.equal((await rpc('result',sid,{request_id:'late',action_id:'act-3',status:'completed',message:'x'})).error.code,'CONFLICT');
 });
 await t.test('direct client reads/writes and RPC access are denied',async()=>{
  await asRole(db,'authenticated');
  for(const table of ['laai_sessions','laai_pages','laai_actions','laai_approval_events','laai_idempotency_keys','laai_request_buckets'])
   await assert.rejects(db.query(`select * from public.${table}`),{code:'42501'});
  await assert.rejects(rpc('display',sid),{code:'42501'});
  for(const fn of ["laai_workflow_apply($1,'create',null,'{}')","laai_invalidate_actions($1)","laai_purge_expired_sessions()"])
   await assert.rejects(db.query(`select public.${fn}`,fn.includes('$1')?[alice]:[]),{code:'42501'});
  await asRole(db,'service_role');
 });
 await t.test('rate limits are shared in the database and count failed requests',async()=>{
  const used=async()=>(await db.query('select requests from public.laai_request_buckets where user_id=$1',[bob])).rows[0].requests;
  await db.query("update public.laai_request_buckets set requests=0,window_start=now() where user_id=$1",[bob]);
  for(let i=0;i<3;i++)assert.equal((await rpc('display',sid,{},bob)).error.code,'SESSION_NOT_FOUND');
  assert.equal(await used(),3);
  await db.query('update public.laai_request_buckets set requests=600 where user_id=$1',[bob]);
  const limited=await rpc('create',null,{},bob);
  assert.deepEqual(limited,{error:{code:'RATE_LIMITED',message:'Too many requests; try again in a minute'},status:429});
  assert.equal(await used(),601);
  assert.equal(await count(db,'public.laai_sessions where user_id=$1',[bob]),0);
  await db.query("update public.laai_request_buckets set window_start=now()-interval '2 minutes' where user_id=$1",[bob]);
  assert.ok((await rpc('create',null,{},bob)).session_id);
  assert.equal(await used(),1);
 });
 await t.test('expiry and user deletion cascade without orphan events',async()=>{
  await db.query("update public.laai_sessions set expires_at=now()-interval '31 days' where id=$1",[sid]);
  assert.equal((await rpc('display',sid)).error.code,'SESSION_NOT_FOUND');
  await db.query('select public.laai_purge_expired_sessions()');
  assert.equal(await count(db,'public.laai_approval_events'),0);
  const second=(await rpc('create')).session_id;assert.ok(second);
  await db.exec('reset role');await db.query('delete from auth.users where id=$1',[alice]);
  assert.equal(await count(db,'public.laai_sessions where user_id=$1',[alice]),0);
 });
 }finally{await db.close()}
});
