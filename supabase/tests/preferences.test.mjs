import assert from 'node:assert/strict';
import {test} from 'node:test';
import {STACKS,alice,bob,asRole,freshDb,workflow} from './helpers.mjs';
for(const [stack,migrations] of Object.entries(STACKS))test(`preferences and free text persist with ownership, defaults, and display revision updates [${stack}]`,async()=>{
 const db=await freshDb(migrations);
 try{
 const prefs=async(uid,values=null)=>(await db.query('select public.laai_preferences($1,$2) value',[uid,values===null?null:JSON.stringify(values)])).rows[0].value;
 const original=await prefs(alice);assert.equal(original.notes,'');assert.equal(original.proactive_enabled,false);
 const sid=(await workflow(db)('create')).session_id;
 const values={display_density:'compact',shopping_priority:'quality',proactive_enabled:true,notes:'Some random text\n香港 <script>not executable</script>'};
 const saved=await prefs(alice,values);assert.equal(saved.notes,values.notes);
 assert.deepEqual(await prefs(alice),saved);assert.equal((await prefs(bob)).notes,'');
 const rev=()=>db.query('select revision from public.laai_sessions where id=$1',[sid]);
 assert.equal((await rev()).rows[0].revision,2);await prefs(alice,values);assert.equal((await rev()).rows[0].revision,2);
 await assert.rejects(prefs(alice,{...values,notes:'a'.repeat(10001)}),{code:'23514'});
 assert.equal((await prefs(alice)).notes,values.notes);
 await asRole(db,'authenticated',bob);
 const rows=(await db.query('select * from public.laai_user_preferences')).rows;assert.equal(rows.length,1);assert.equal(rows[0].user_id,bob);
 await assert.rejects(db.query("update public.laai_user_preferences set notes='spoof'"),{code:'42501'});
 await assert.rejects(prefs(alice),{code:'42501'});
 await db.exec('reset role');await db.query('delete from auth.users where id=$1',[alice]);
 assert.equal((await db.query('select * from public.laai_user_preferences where user_id=$1',[alice])).rows.length,0);
 }finally{await db.close()}
});
