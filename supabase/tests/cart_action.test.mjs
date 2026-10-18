import assert from 'node:assert/strict';
import {test} from 'node:test';
import {CART,WATCH,action,freshDb,migration,page,workflow} from './helpers.mjs';

const cart={...action,action_id:'act-cart',type:'add_to_amazon_cart',label:'Add to Amazon cart',
 description:'Add Sony WH-1000XM5 (ASIN B09XS7JWHH), $328.00, new, sold by Amazon.com'};

test('006 refuses the cart action; 007 registers it alongside the research actions',async()=>{
 for(const [stack,accepted] of [[WATCH,false],[CART,true]]){
  const db=await freshDb(stack);
  try{
   const rpc=workflow(db);const sid=(await rpc('create')).session_id;
   await rpc('page',sid,page);
   const result=await rpc('actions',sid,{request_id:'a1',page_id:'page-1',actions:[cart,action]});
   if(!accepted){assert.equal(result.error.code,'INVALID_PAYLOAD');continue}
   assert.equal(result.error,undefined);
   const shown=(await rpc('display',sid)).actions;
   assert.deepEqual(shown.map(a=>a.type).sort(),['add_to_amazon_cart','compare_prices']);
   assert.equal(shown.find(a=>a.type==='add_to_amazon_cart').requires_approval,true);
   // The watcher reads the page URL from display to recognise an amazon.com /dp/ page.
   assert.equal((await rpc('display',sid)).page.url,page.url);
   // Still only registered handlers: the reserved types stay refused.
   await rpc('page',sid,{...page,request_id:'p2',page_id:'page-2'});
   const custom=await rpc('actions',sid,{request_id:'a2',page_id:'page-2',actions:[{...action,action_id:'act-x',type:'custom'}]});
   assert.equal(custom.error.code,'INVALID_PAYLOAD');
  }finally{await db.close()}
 }
});

test('007 reruns cleanly',async()=>{
 const db=await freshDb(CART);
 try{
  await db.exec('reset role');await db.exec(await migration(CART.at(-1)));await db.exec('set role service_role');
  const rpc=workflow(db);const sid=(await rpc('create')).session_id;
  await rpc('page',sid,page);
  assert.equal((await rpc('actions',sid,{request_id:'a1',page_id:'page-1',actions:[cart]})).error,undefined);
 }finally{await db.close()}
});
