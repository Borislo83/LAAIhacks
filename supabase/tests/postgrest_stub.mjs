// A PostgREST-shaped HTTP front for an embedded PGlite database that runs our real
// migration SQL. It lets end-to-end tests drive the unmodified orchestrator process
// (SUPABASE_URL=http://127.0.0.1:<port>) against the same RPCs Supabase would run.
//
//   node postgrest_stub.mjs [port]     prints "listening <port>" once migrations are applied
//
// Only POST /rest/v1/rpc/<function> is implemented (named JSON arguments, like PostgREST).
// Auth is not emulated: run the orchestrator with DEMO_AUTH_MODE=true and DEMO_USER_ID=alice.
import {createServer} from 'node:http';
import {readdir} from 'node:fs/promises';
import {alice,bob,freshDb} from './helpers.mjs';

const migrations=(await readdir(new URL('../migrations/',import.meta.url))).filter(f=>f.endsWith('.sql')).sort();
const db=await freshDb(migrations);
let queue=Promise.resolve();
const serial=work=>(queue=queue.then(work,work));

const server=createServer((req,res)=>{
 let body='';
 req.on('data',chunk=>body+=chunk);
 req.on('end',()=>serial(async()=>{
  const send=(status,value)=>{res.writeHead(status,{'content-type':'application/json'});res.end(JSON.stringify(value));};
  const match=/^\/rest\/v1\/rpc\/([a-z_]+)$/.exec(req.url.split('?')[0]);
  if(req.method==='GET'&&req.url==='/users')return send(200,{alice,bob});
  // No real identities exist here, so every non-fixture token is invalid, as Auth would say.
  if(req.url==='/auth/v1/user')return send(401,{code:401,msg:'invalid JWT'});
  if(req.method!=='POST'||!match)return send(404,{code:'PGRST404',message:'Not emulated'});
  try{
   const args=Object.entries(JSON.parse(body||'{}'));
   const named=args.map(([k],i)=>`${k} => $${i+1}`).join(',');
   const values=args.map(([,v])=>v!==null&&typeof v==='object'?JSON.stringify(v):v);
   const {rows}=await db.query(`select to_jsonb(public.${match[1]}(${named})) as value`,values);
   send(200,rows[0].value);
  }catch(err){
   // PostgREST reports SQL errors as 400 with the SQLSTATE in `code`.
   send(400,{code:err.code??'P0001',message:err.message});
  }
 }));
});
server.listen(Number(process.argv[2]??0),'127.0.0.1',()=>console.log(`listening ${server.address().port}`));
