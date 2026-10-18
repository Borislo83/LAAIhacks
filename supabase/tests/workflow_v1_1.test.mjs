import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
import {PGlite} from '@electric-sql/pglite';
const alice='00000000-0000-4000-8000-000000000001',bob='00000000-0000-4000-8000-000000000002';
const migrations=['202610170002_shared_workflow.sql','202610170003_user_preferences.sql','202610170004_workflow_v1_1.sql'];
test('workflow v1.1: ack, invalidation outbox, rate limits and devices',async t=>{
 const db=new PGlite();
 try{
 await db.exec(`create role anon;create role authenticated;create role service_role bypassrls;
 create schema auth;create table auth.users(id uuid primary key);
 create function auth.uid() returns uuid language sql as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
 grant usage on schema public,auth to anon,authenticated,service_role;
 grant execute on function auth.uid() to authenticated;`);
 for(const name of migrations)await db.exec(await readFile(new URL('../migrations/'+name,import.meta.url),'utf8'));
 // 004 must be safe to apply twice on the live project.
 await db.exec(await readFile(new URL('../migrations/'+migrations[2],import.meta.url),'utf8'));
 await db.query('insert into auth.users values ($1),($2)',[alice,bob]);
 await db.exec('set role service_role');
 const rpc=async(op,sid=null,p={},user=alice)=>(await db.query('select public.laai_workflow($1,$2,$3,$4) value',[user,op,sid,JSON.stringify(p)])).rows[0].value;
 const page={request_id:'p1',page_id:'page-1',url:'https://example.com/product',kind:'product',title:'Headphones',summary:[],facts:[],source_label:'Test',updated_at:new Date().toISOString()};
 const action={action_id:'act-1',type:'compare_prices',label:'Compare',description:'Research prices',expires_at:new Date(Date.now()+3600000).toISOString(),requires_approval:true};
 let sid,device;
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
 await t.test('executors acknowledge events idempotently without changing display',async()=>{
  await rpc('page',sid,page);
  const revision=(await rpc('actions',sid,{request_id:'a1',page_id:'page-1',actions:[action]})).revision;
  const approval=await rpc('approve',sid,{request_id:'tap-1',action_id:'act-1',expected_revision:revision});
  const [event]=(await rpc('events',sid)).events;
  assert.equal(event.action_status,'approved');assert.equal(event.consumed_at,null);
  const ack=await rpc('ack',sid,{event_id:approval.event_id});
  assert.equal(ack.event_id,approval.event_id);assert.ok(ack.consumed_at);
  assert.deepEqual(await rpc('ack',sid,{event_id:approval.event_id}),ack);
  assert.equal((await rpc('display',sid)).revision,revision+1);
  assert.equal((await rpc('ack',sid,{event_id:'00000000-0000-4000-8000-00000000ffff'})).error.code,'EVENT_NOT_FOUND');
  assert.equal((await rpc('ack',sid,{event_id:'junk'})).error.code,'EVENT_NOT_FOUND');
  assert.equal((await rpc('ack',sid,{event_id:approval.event_id},bob)).error.code,'SESSION_NOT_FOUND');
 });
 await t.test('invalidated approvals close their outbox event and report their status',async()=>{
  const act={...action,action_id:'act-2'};
  const revision=(await rpc('actions',sid,{request_id:'a2',page_id:'page-1',actions:[act]})).revision;
  const approval=await rpc('approve',sid,{request_id:'tap-2',action_id:'act-2',expected_revision:revision});
  await rpc('page',sid,{...page,request_id:'p2',title:'Changed'});
  const event=(await rpc('events',sid)).events.find(e=>e.event_id===approval.event_id);
  assert.equal(event.action_status,'invalidated');assert.ok(event.consumed_at);
  assert.equal((await rpc('result',sid,{request_id:'late',action_id:'act-2',status:'completed',message:'x'})).error.code,'CONFLICT');
 });
 await t.test('failed requests count toward the shared rate limit',async()=>{
  await db.query("update public.laai_request_buckets set requests=0,window_start=now() where user_id=$1",[bob]);
  for(let i=0;i<3;i++)assert.equal((await rpc('display',sid,{},bob)).error.code,'SESSION_NOT_FOUND');
  assert.equal((await db.query('select requests from public.laai_request_buckets where user_id=$1',[bob])).rows[0].requests,3);
  await db.query('update public.laai_request_buckets set requests=600 where user_id=$1',[bob]);
  const limited=await rpc('create',null,{},bob);
  assert.equal(limited.error.code,'RATE_LIMITED');assert.equal(limited.status,429);
  assert.equal((await db.query('select count(*)::int n from public.laai_sessions where user_id=$1',[bob])).rows[0].n,0);
 });
 await t.test('direct clients still cannot call workflow functions',async()=>{
  await db.exec('reset role; set role authenticated');
  for(const fn of ["laai_workflow_apply($1,'create',null,'{}')","laai_invalidate_actions($1)"])
   await assert.rejects(db.query(`select public.${fn}`,[alice]),{code:'42501'});
  await db.exec('reset role; set role service_role');
 });
 }finally{await db.close();}
});
