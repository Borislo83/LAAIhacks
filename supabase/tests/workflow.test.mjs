import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
import {PGlite} from '@electric-sql/pglite';
const alice='00000000-0000-4000-8000-000000000001',bob='00000000-0000-4000-8000-000000000002';
test('shared session contract persists approvals, outbox, results and retries',async t=>{
 const db=new PGlite();
 try{
 await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
 create schema auth; create table auth.users(id uuid primary key);
 grant usage on schema public,auth to anon,authenticated,service_role;`);
 // Production state is 002 followed by 004; 004 replaces the workflow entry point.
 for(const name of ['202610170002_shared_workflow.sql','202610170004_workflow_v1_1.sql'])await db.exec(await readFile(new URL('../migrations/'+name,import.meta.url),'utf8'));
 await db.query('insert into auth.users values ($1),($2)',[alice,bob]);
 await db.exec('set role service_role');
 const rpc=async(op,sid=null,p={},user=alice)=>(await db.query('select public.laai_workflow($1,$2,$3,$4) value',[user,op,sid,JSON.stringify(p)])).rows[0].value;
 const sid=(await rpc('create')).session_id;
 const page={request_id:'p1',page_id:'page-1',url:'https://example.com/product',kind:'product',title:'Headphones',summary:[],facts:[],source_label:'Test',updated_at:new Date().toISOString()};
 const action={action_id:'act-1',type:'compare_prices',label:'Compare',description:'Research prices',expires_at:new Date(Date.now()+3600000).toISOString(),requires_approval:true};
 let revision,event,decision;
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
 revision=(await rpc('actions',sid,{request_id:'a1',page_id:'page-1',actions:[action]})).revision;
 assert.equal((await rpc('result',sid,{request_id:'premature',action_id:action.action_id,status:'completed',message:'not approved'})).error.code,'CONFLICT');
 });
 await t.test('approval and outbox are atomic and idempotent',async()=>{
 assert.equal((await rpc('approve',sid,{request_id:'stale',action_id:'act-1',expected_revision:1})).error.code,'STALE_REVISION');
 assert.equal((await rpc('approve',sid,{request_id:'unknown',action_id:'unknown',expected_revision:revision})).error.code,'ACTION_NOT_FOUND');
 decision={request_id:'tap-1',action_id:'act-1',expected_revision:revision};
 const result=await rpc('approve',sid,decision);assert.equal(result.status,'approved');
 assert.deepEqual(await rpc('approve',sid,decision),result);
 const events=(await rpc('events',sid)).events;assert.equal(events.length,1);event=events[0];
 assert.equal(event.event_id,result.event_id);assert.equal(event.page_id,'page-1');
 assert.equal((await rpc('events',sid,{after:event.cursor})).events.length,0);
 assert.equal((await rpc('approve',sid,{...decision,request_id:'other',expected_revision:revision+1})).error.code,'CONFLICT');
 });
 await t.test('results, consumption and display revisions persist with safe retries',async()=>{
 const running={request_id:'running-1',action_id:'act-1',status:'running',message:'Researching'};
 await rpc('result',sid,running);assert.equal((await rpc('display',sid)).mode,'loading');
 assert.equal((await rpc('events',sid)).events[0].consumed_at,null);
 const completed={request_id:'result-1',action_id:'act-1',status:'completed',message:'Comparison ready'};
 const result=await rpc('result',sid,completed);const rev=(await rpc('display',sid)).revision;
 assert.deepEqual(await rpc('result',sid,completed),result);
 assert.equal((await rpc('display',sid)).revision,rev);
 assert.equal((await rpc('display',sid)).actions[0].result.message,'Comparison ready');
 assert.ok((await rpc('events',sid)).events[0].consumed_at);
 assert.equal((await rpc('result',sid,{...running,request_id:'late-running'})).error.code,'CONFLICT');
 });
 await t.test('page changes invalidate pending approvals, including same page ID',async()=>{
 const act={...action,action_id:'act-2'};await rpc('actions',sid,{request_id:'a2',page_id:'page-1',actions:[act]});
 const rev=(await rpc('display',sid)).revision;
 await rpc('page',sid,{...page,request_id:'p2',title:'Updated page'});
 assert.equal((await rpc('approve',sid,{request_id:'tap-2',action_id:'act-2',expected_revision:rev+1})).error.code,'CONFLICT');
 assert.equal((await rpc('display',sid)).actions.length,0);
 });
 await t.test('direct client reads/writes and RPC access are denied',async()=>{
 await db.exec('reset role; set role authenticated');
 for(const table of ['laai_sessions','laai_pages','laai_actions','laai_approval_events','laai_idempotency_keys'])await assert.rejects(db.query(`select * from public.${table}`),{code:'42501'});
 await assert.rejects(rpc('display',sid),{code:'42501'});await db.exec('reset role; set role service_role');
 });
 await t.test('rate limits are shared in the database',async()=>{
 await db.query('update public.laai_request_buckets set requests=600 where user_id=$1',[alice]);
 assert.equal((await rpc('display',sid)).error.code,'RATE_LIMITED');
 await db.query("update public.laai_request_buckets set window_start=now()-interval '2 minutes' where user_id=$1",[alice]);
 assert.ok((await rpc('display',sid)).session_id);
 });
 await t.test('expiry and user deletion cascade without orphan events',async()=>{
 await db.query("update public.laai_sessions set expires_at=now()-interval '31 days' where id=$1",[sid]);
 assert.equal((await rpc('display',sid)).error.code,'SESSION_NOT_FOUND');
 await db.query('select public.laai_purge_expired_sessions()');
 assert.equal((await db.query('select count(*)::int n from public.laai_approval_events')).rows[0].n,0);
 const second=(await rpc('create')).session_id;assert.ok(second);
 await db.exec('reset role');await db.query('delete from auth.users where id=$1',[alice]);
 assert.equal((await db.query('select count(*)::int n from public.laai_sessions')).rows[0].n,0);
 });
 }finally{await db.close()}
});
