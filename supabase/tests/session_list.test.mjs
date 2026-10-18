import assert from 'node:assert/strict';
import {test} from 'node:test';
import {WATCH,alice,bob,action,freshDb,migration,page,workflow} from './helpers.mjs';

test('sessions lists only the caller\'s unexpired sessions, newest activity first',async()=>{
 const db=await freshDb(WATCH);
 try{
  const rpc=workflow(db);
  const older=(await rpc('create')).session_id,newer=(await rpc('create')).session_id;
  const expired=(await rpc('create')).session_id;
  await rpc('create',null,{},bob);
  await db.query("update public.laai_sessions set expires_at=now()-interval '1 second' where id=$1",[expired]);
  await rpc('page',older,page);
  let {sessions}=await rpc('sessions');
  assert.deepEqual(sessions.map(s=>s.session_id),[older,newer]);
  assert.deepEqual(Object.keys(sessions[0]).sort(),['active_page_id','expires_at','revision','session_id','updated_at']);
  assert.equal(sessions[0].active_page_id,'page-1');assert.equal(sessions[0].revision,2);
  assert.equal(sessions[1].active_page_id,null);assert.equal(sessions[1].revision,1);
  // Every write bumps the revision a watcher compares; reads do not.
  await rpc('actions',older,{request_id:'a1',page_id:'page-1',actions:[action]});
  await rpc('display',older);
  ({sessions}=await rpc('sessions'));
  assert.equal(sessions.find(s=>s.session_id===older).revision,3);
  assert.deepEqual((await rpc('sessions',null,{},bob)).sessions.length,1);
 }finally{await db.close()}
});

test('sessions is rate limited and counted like every operation',async()=>{
 const db=await freshDb(WATCH);
 try{
  const rpc=workflow(db);
  await rpc('sessions');
  assert.equal((await db.query('select requests from public.laai_request_buckets where user_id=$1',[alice])).rows[0].requests,1);
  await db.query('update public.laai_request_buckets set requests=600 where user_id=$1',[alice]);
  assert.equal((await rpc('sessions')).error.code,'RATE_LIMITED');
 }finally{await db.close()}
});

test('006 reruns cleanly and leaves other operations unchanged',async()=>{
 const db=await freshDb(WATCH);
 try{
  await db.exec('reset role');await db.exec(await migration(WATCH.at(-1)));await db.exec('set role service_role');
  const rpc=workflow(db);const sid=(await rpc('create')).session_id;
  assert.equal((await rpc('page',sid,page)).revision,2);
  assert.equal((await rpc('display',sid)).mode,'summary');
  assert.equal((await rpc('nonsense',sid)).error.code,'INVALID_PAYLOAD');
 }finally{await db.close()}
});
