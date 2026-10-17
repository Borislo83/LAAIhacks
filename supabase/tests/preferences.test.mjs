import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
import {PGlite} from '@electric-sql/pglite';
const alice='00000000-0000-4000-8000-000000000001',bob='00000000-0000-4000-8000-000000000002';
test('preferences and free text persist with ownership, defaults, and display revision updates',async()=>{
 const db=new PGlite();
 try{
 await db.exec(`create role anon;create role authenticated;create role service_role bypassrls;
 create schema auth;create table auth.users(id uuid primary key);
 create function auth.uid() returns uuid language sql as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
 grant usage on schema public,auth to anon,authenticated,service_role;
 grant execute on function auth.uid() to authenticated;`);
 for(const name of ['202610170002_shared_workflow.sql','202610170003_user_preferences.sql'])await db.exec(await readFile(new URL('../migrations/'+name,import.meta.url),'utf8'));
 await db.query('insert into auth.users values ($1),($2)',[alice,bob]);await db.exec('set role service_role');
 const prefs=async(uid,values=null)=>(await db.query('select public.laai_preferences($1,$2) value',[uid,values===null?null:JSON.stringify(values)])).rows[0].value;
 const original=await prefs(alice);assert.equal(original.notes,'');assert.equal(original.proactive_enabled,false);
 const sid=(await db.query("select public.laai_workflow($1,'create',null,'{}') value",[alice])).rows[0].value.session_id;
 const values={display_density:'compact',shopping_priority:'quality',proactive_enabled:true,notes:'Some random text\n香港 <script>not executable</script>'};
 const saved=await prefs(alice,values);assert.equal(saved.notes,values.notes);
 assert.deepEqual(await prefs(alice),saved);assert.equal((await prefs(bob)).notes,'');
 const rev=()=>db.query('select revision from public.laai_sessions where id=$1',[sid]);
 assert.equal((await rev()).rows[0].revision,2);await prefs(alice,values);assert.equal((await rev()).rows[0].revision,2);
 await assert.rejects(prefs(alice,{...values,notes:'a'.repeat(10001)}),{code:'23514'});
 assert.equal((await prefs(alice)).notes,values.notes);
 await db.exec('reset role;set role authenticated');await db.query("select set_config('request.jwt.claim.sub',$1,false)",[bob]);
 const rows=(await db.query('select * from public.laai_user_preferences')).rows;assert.equal(rows.length,1);assert.equal(rows[0].user_id,bob);
 await assert.rejects(db.query("update public.laai_user_preferences set notes='spoof'"),{code:'42501'});
 await assert.rejects(prefs(alice),{code:'42501'});
 await db.exec('reset role');await db.query('delete from auth.users where id=$1',[alice]);
 assert.equal((await db.query('select * from public.laai_user_preferences where user_id=$1',[alice])).rows.length,0);
 }finally{await db.close()}
});
