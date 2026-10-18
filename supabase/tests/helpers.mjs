import {readFile} from 'node:fs/promises';
import {PGlite} from '@electric-sql/pglite';

export const alice='00000000-0000-4000-8000-000000000001',bob='00000000-0000-4000-8000-000000000002';

export const M={
 orchestrator:'202610170001_orchestrator.sql',shared:'202610170002_shared_workflow.sql',
 preferences:'202610170003_user_preferences.sql',v1_1:'202610170004_workflow_v1_1.sql',perf:'202610170005_workflow_perf.sql',
 sessions:'202610170006_session_list.sql',cart:'202610170007_cart_action.sql',
};
// Every behavior test runs against what is live today and against the next migration on top.
export const LIVE=[M.orchestrator,M.shared,M.preferences,M.v1_1];
export const NEXT=[...LIVE,M.perf];
export const WATCH=[...NEXT,M.sessions];
export const CART=[...WATCH,M.cart];
export const STACKS={'001-004 (live)':LIVE,'001-005':NEXT};

export const migration=name=>readFile(new URL('../migrations/'+name,import.meta.url),'utf8');

// Stub only Supabase's pre-existing Auth objects, not our application SQL. Leaves the
// connection as service_role, with alice and bob registered in auth.users.
export async function freshDb(migrations){
 const db=new PGlite();
 await db.exec(`create role anon;create role authenticated;create role service_role bypassrls;
 create schema auth;create table auth.users(id uuid primary key);
 create function auth.uid() returns uuid language sql as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
 grant usage on schema public,auth to anon,authenticated,service_role;
 grant execute on function auth.uid() to authenticated;`);
 for(const name of migrations)await db.exec(await migration(name));
 await db.query('insert into auth.users values ($1),($2)',[alice,bob]);
 await db.exec('set role service_role');
 return db;
}

export const asRole=async(db,role,sub=null)=>{
 await db.exec(`reset role;set role ${role}`);
 if(sub)await db.query("select set_config('request.jwt.claim.sub',$1,false)",[sub]);
};

// Calls any public function by name with positional arguments.
export const call=async(db,name,args)=>{
 const placeholders=args.map((_,i)=>`$${i+1}`).join(',');
 return (await db.query(`select to_jsonb(public.${name}(${placeholders})) as value`,args)).rows[0].value;
};
export const workflow=db=>(op,sid=null,p={},user=alice)=>call(db,'laai_workflow',[user,op,sid,JSON.stringify(p)]);
export const count=async(db,sql,args=[])=>(await db.query(`select count(*)::int n from ${sql}`,args)).rows[0].n;

export const page={request_id:'p1',page_id:'page-1',url:'https://example.com/product',kind:'product',title:'Headphones',summary:[],facts:[],source_label:'Test',updated_at:new Date().toISOString()};
export const action={action_id:'act-1',type:'compare_prices',label:'Compare',description:'Research prices',expires_at:new Date(Date.now()+3600000).toISOString(),requires_approval:true};
