/** Synthetic integration tests against the actual normal runtime，runner，MCP and outbox． */
import {test} from 'node:test';
import {request as httpRequest} from 'node:http';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,readFileSync,symlinkSync,unlinkSync,lstatSync,existsSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {join,dirname} from 'node:path';
import {createHash} from 'node:crypto';
import {generateKeyPair,exportJWK,SignJWT} from 'jose';
import {createRuntime,DEFAULT_CONFIG} from './src/native/runtime';
import {NativeParliament,listenParliament} from './src/native/parliament';
import {Fault} from './src/types';
import {Store} from './src/store';
import {Orchestrator} from './src/service';
import {route} from './src/native/http';
import {Events,EVENT_NAME} from './src/events';
import {base64} from './src/crypto';

const issuer='https://synthetic-issuer.example/',audience='https://synthetic-service.example/mcp';
const keys=await generateKeyPair('RS256'),jwk=await exportJWK(keys.publicKey);jwk.kid='synthetic-parliament';
const principal=(id:string)=>'oauth:'+JSON.stringify([issuer,id+'-account',id+'-client']);
const digest=(s:string)=>createHash('sha256').update(s).digest('hex');
globalThis.fetch=(async()=>{throw Error('EXTERNAL_NETWORK_DISABLED_IN_SYNTHETIC_TEST');}) as typeof fetch;
const snowflake=()=>String((BigInt(Date.now())-1420070400000n)<<22n);
async function fixture(recipient='1000'){
 mkdirSync('.cache/parliament-tests',{recursive:true});const directory=mkdtempSync(join(process.cwd(),'.cache/parliament-tests/case-'));
 const p={version:1,enabled:true,port:18641,guild_id:'100',channel_id:'200',project_id:'parliament-test',registered_bot_ids:['1000','2000','4000'],
  recipients:{'1000':{connection_id:'grok',identity_sha256:digest(principal('grok'))},'2000':{connection_id:'mia',identity_sha256:digest(principal('mia'))}}};
 const config={...DEFAULT_CONFIG,parliament:p,env:{...DEFAULT_CONFIG.env,ALLOWED_GUILD_IDS:'100',ALLOWED_USER_IDS:'300,4000'}};
 const rt=createRuntime(process.cwd(),directory,config),store=new Store(rt.env),service=new Orchestrator(rt.env);
 Object.assign(rt.env,{DISCORD_MODE:'live',DISCORD_RELAY_MODE:'bot',LIVE_CONNECTORS:'true',MCP_OAUTH_ISSUER:issuer,MCP_OAUTH_AUDIENCE:audience,MCP_RESOURCE_URL:audience,MCP_OAUTH_JWKS_JSON:JSON.stringify({keys:[jwk]}),
  GROK_ROUTINE_URL:'https://routine.example/synthetic-only',GROK_ROUTINE_KEY:'synthetic-existing-routine-key',GROK_ROUTINE_ALLOWED_HOSTS:'routine.example',
  SUBSCRIPTION_ENCRYPTION_KEY:base64(new Uint8Array(32).fill(9)),CALLBACK_ALLOWED_HOSTS:'callback.example',DISCORD_BOT_TOKEN:'synthetic-existing-service-bot'});
 await store.exec("INSERT INTO projects(id,guild_id,channel_id,enabled) VALUES('parliament-test','100','200',1)");
 for(const who of ['300','4000'])await store.exec("INSERT INTO project_members VALUES('parliament-test',?)",who);
 for(const id of ['grok','mia']){
  await store.exec('UPDATE agent_connections SET principal=?,mode=?,enabled=1,verified=1 WHERE id=?',principal(id),id==='grok'?'routine':'events',id);
  await store.exec("INSERT INTO connection_projects VALUES(?,'parliament-test')",id);
  rt.db.raw.prepare('INSERT INTO native_enrollment_bindings VALUES(?,?,?,?,?)').run(id,digest(principal(id)),'synthetic-proof','synthetic-window',Date.now());
 }
 const q={version:1,job_id:digest(directory),guild_id:'100',channel_id:'201',requester_id:'300',recipient_id:recipient,message_id:snowflake(),kind:'ask',text:'Synthetic public question.'};
 const message:any={id:q.message_id,channel_id:q.channel_id,author:{id:'300',bot:false},type:0,content:`<@${recipient}> ask\n${q.text}`,attachments:[]};
 const channel:any={id:'201',guild_id:'100',parent_id:'200',type:11,thread_metadata:{archived:false}};
 let memberPresent=true;const calls:string[]=[],routine:any[]=[],callbacks:any[]=[],posts:any[]=[];
 globalThis.fetch=(async(url:any,init:any)=>{
  assert.match(String(url),/^https:\/\/discord.com\/api\/v10\/channels\/[0-9]+\/messages(?:\/[0-9]+\/threads)?$/);
  assert.equal(init.method,'POST');assert.equal(init.headers.Authorization,'Bot synthetic-existing-service-bot');
  posts.push({url:String(url),body:JSON.parse(init.body)});return Response.json({id:String(90000+posts.length)});
 }) as typeof fetch;
 const parents=new Map<string,any>();
 const discord=async(path:string)=>{
  calls.push(path);
  if(path==='users/@me')return{id:'5500',bot:true};
  if(path==='guilds/100')return{id:'100'};
  if(path==='channels/201')return channel;
  if(path.startsWith('channels/201/messages/'))return parents.get(path.split('/').at(-1)!)??message;
  if(path.startsWith('guilds/100/members/')){const id=path.split('/').at(-1)!;if(!memberPresent&&id===q.requester_id)throw new Fault('MEMBER_CHANGED',403);return{user:{id,bot:id!=='300'}};}
  throw Error('unexpected synthetic REST path');
 };
 const bridge=rt.env.PARLIAMENT_GATEWAY as NativeParliament;bridge.discord=discord;
 rt.env.GROK_ROUTINE_EGRESS={fetch:async(url:any,init:any)=>{routine.push({url,body:JSON.parse(init.body)});return Response.json({});}} as any;
 rt.env.CALLBACK_EGRESS={fetch:async(_url:any,init:any)=>{const b=JSON.parse(init.body);callbacks.push(b);return b.type==='verification'?Response.json({challenge:b.challenge}):new Response(null,{status:204});}} as any;
 async function ingress(request:Request){try{return await bridge.handle(request);}catch(e){return Response.json({error:e instanceof Fault?e.code:'INVALID_INPUT'},{status:e instanceof Fault?e.httpStatus:400});}}
 const start=async(value:any=q)=>ingress(new Request('http://127.0.0.1:18641/v1/parliament',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(value)}));
 const result=async()=>ingress(new Request('http://127.0.0.1:18641/v1/parliament?job_id='+q.job_id));
 async function rpc(id:string,name:string,args:any){
  const token=await new SignJWT({scope:'discussions:turn',azp:id+'-client'}).setProtectedHeader({alg:'RS256',kid:jwk.kid}).setIssuer(issuer).setAudience(audience).setSubject(id+'-account').setIssuedAt().setExpirationTime('5m').sign(keys.privateKey);
  const response=await route(new Request('http://127.0.0.1:18640/mcp',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json',Accept:'application/json','MCP-Protocol-Version':'2026-07-28','Mcp-Method':'tools/call','Mcp-Name':name},body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args,_meta:{'io.modelcontextprotocol/protocolVersion':'2026-07-28','io.modelcontextprotocol/clientCapabilities':{},'io.modelcontextprotocol/clientInfo':{name:'synthetic',version:'1'}}}})}),rt.env,()=>{});
  return{status:response.status,body:await response.json() as any};
 }
 const binding=async()=>{
  rt.db.raw.prepare("INSERT INTO native_mia_event_epoch VALUES('mia',?,1)").run(digest(principal('mia')));
  const c=await store.connection('mia');await new Events(rt.env).subscribe(c,{name:EVENT_NAME,arguments:{project_id:'parliament-test'},delivery:{mode:'webhook',url:'https://callback.example/synthetic-only',secret:'whsec_'+base64(new Uint8Array(32).fill(37))}});
 };
 return{rt,store,service,p,q,message,channel,calls,routine,callbacks,posts,parents,discord,bridge,start,result,rpc,binding,removeMember:()=>{memberPresent=false;}};
}

for(const recipient of ['1000','2000'])test('REST message → '+(recipient==='1000'?'existing Grok Routine':'existing Dot Events')+' → OAuth MCP answer → original thread',async()=>{
 const x=await fixture(recipient);try{
  if(recipient==='2000')await x.binding();
  const response=await x.start();assert.equal(response.status,200);const receipt:any=await response.json();assert.equal(receipt.status,'pending');
  const run=await x.store.run(receipt.run_id);assert.equal(run.thread_id,null);assert.equal(x.bridge.deliveryTarget(run.id),'201');assert.equal(run.created_by,'300');assert.equal(run.max_calls,1);assert.ok(run.deadline-Date.now()<=120000);
  assert.equal((await x.start()).status,200);await x.rt.runner.tick();await x.rt.runner.tick();
  const who=recipient==='1000'?'grok':'mia';assert.equal(x.routine.length,who==='grok'?1:0);assert.equal(x.callbacks.filter(b=>b.name===EVENT_NAME).length,who==='mia'?1:0);
  const args={run_id:receipt.run_id,turn_id:receipt.turn_id};
  const wrong=await x.rpc(who==='grok'?'mia':'grok','get_turn',args);assert.ok(wrong.body.result?.isError||wrong.body.error);
  const task=await x.rpc(who,'get_turn',args);assert.equal(task.status,200);assert.equal(task.body.result.structuredContent.objective,x.q.text);assert.equal(task.body.result.structuredContent.can_submit,true);assert.equal(task.body.result.structuredContent.task_kind,undefined);
  assert.equal((await x.result()).status,200);assert.equal(((await (await x.result()).json()) as any).authenticated_answer,false);
  const submitted=await x.rpc(who,'submit_turn',{...args,request_key:'synthetic-complete',status:'completed',text:'Synthetic actual authenticated connection answer. '+principal(who),citations:[],asset_ids:[],image_perception:'none'});
  assert.equal(submitted.body.result.isError,undefined);await x.rt.runner.tick();
  const result:any=await (await x.result()).json();assert.equal(result.status,'completed');assert.equal(result.authenticated_answer,true);assert.equal(result.discord_display,'posted');assert.match(result.message_url,/^https:\/\/discord.com\/channels\/100\/201\/[0-9]+$/);
  const messages=await x.store.all<any>("SELECT payload,status FROM outbox WHERE kind='DISCORD'");assert.equal(messages.length,2);for(const m of messages){const payload=JSON.parse(m.payload);assert.equal(m.status,'SENT');assert.equal(payload.thread_id,'201');assert.ok(!payload.content.includes(principal(who)));assert.notEqual(payload.action,'parent');assert.notEqual(payload.action,'thread');}
  assert.equal(x.posts.length,2);assert.ok(x.posts.every(v=>v.url==='https://discord.com/api/v10/channels/201/messages'));
  assert.equal((await x.start()).status,200);await x.rt.runner.tick();assert.equal(x.posts.length,2);
  assert.equal((await x.store.all<any>("SELECT detail_code FROM audit_logs WHERE action='run.created'"))[0].detail_code,'HUMAN_GATEWAY_MESSAGE');
  assert.equal((await x.store.all('SELECT * FROM native_information_requests')).length,0);assert.equal((await x.store.all('SELECT * FROM native_research_requests')).length,0);
 }finally{await x.rt.close();}
});

for(const invalid of ['webhook','wrong_author','quoted_command','other_bot','changed_text','archived','wrong_thread','stale','number_id','implementation','private_secret'])test('rejects '+invalid+' before dispatch',async()=>{
 const x=await fixture();try{
  if(invalid==='webhook')x.message.webhook_id='999';
  if(invalid==='wrong_author')x.message.author.id='999';
  if(invalid==='quoted_command')x.message.content='> '+x.message.content;
  if(invalid==='other_bot')x.message.content=x.message.content.replace('1000','2000');
  if(invalid==='changed_text')x.q.text='Changed caller claim';
  if(invalid==='archived')x.channel.thread_metadata.archived=true;
  if(invalid==='wrong_thread')x.channel.parent_id='999';
  if(invalid==='stale')x.q.message_id=x.message.id='1';
  if(invalid==='number_id')(x.q as any).recipient_id=1000;
  if(invalid==='implementation')(x.q as any).kind='implement';
  if(invalid==='private_secret')x.q.text= x.rt.env.GROK_ROUTINE_KEY!;
  assert.notEqual((await x.start()).status,200);await x.rt.runner.tick();assert.equal(x.routine.length,0);assert.equal((await x.store.all('SELECT * FROM runs')).length,0);
 }finally{await x.rt.close();}
});

for(const change of ['binding','grant','message','membership','policy','transport'])test('rechecks '+change+' before dispatch',async()=>{
 const x=await fixture();try{
  assert.equal((await x.start()).status,200);
  if(change==='binding')await x.store.exec("UPDATE agent_connections SET principal='oauth:changed' WHERE id='grok'");
  if(change==='grant')await x.store.exec("DELETE FROM connection_projects WHERE connection_id='grok'");
  if(change==='message')x.message.content='Changed message';
  if(change==='membership')x.removeMember();
  if(change==='policy')x.p.recipients['1000'].identity_sha256='b'.repeat(64);
  if(change==='transport')x.rt.env.GROK_ROUTINE_KEY='changed-synthetic-key';
  await x.rt.runner.tick();assert.equal(x.routine.length,0);const response=await x.result();if(response.status===200)assert.notEqual((await response.json() as any).status,'completed');
 }finally{await x.rt.close();}
});

test('registered Bot is the real source actor and cannot replay a correlation or delegate from a reply',async()=>{
 const x=await fixture();try{
  x.q.requester_id='4000';x.message.author={id:'4000',bot:true};x.message.content=`<@1000> ask request=synthetic-correlation hop=0\n${x.q.text}`;
  assert.equal((await x.start()).status,200);const created=await x.store.all<any>("SELECT created_by FROM runs");assert.equal(created[0].created_by,'4000');
  const other={...x.q,job_id:'b'.repeat(64)};assert.equal((await x.start(other)).status,409);assert.equal((await x.store.all('SELECT * FROM runs')).length,1);
  assert.equal((await x.store.all<any>("SELECT detail_code FROM audit_logs WHERE action='run.created'"))[0].detail_code,'BOT_GATEWAY_MESSAGE');
 }finally{await x.rt.close();}
});

test('pending and unknown delivery never become a successful answer or another Routine POST',async()=>{
 const x=await fixture();try{
  const receipt:any=await (await x.start()).json();await x.rt.runner.tick();let state:any=await (await x.result()).json();assert.equal(state.status,'pending');assert.equal(state.authenticated_answer,false);
  await x.store.exec("UPDATE dispatches SET status='UNKNOWN' WHERE turn_id=?",receipt.turn_id);await x.rt.runner.tick();await x.rt.runner.tick();assert.equal(x.routine.length,1);
  const binding=x.bridge;x.rt.env.PARLIAMENT_GATEWAY=binding;assert.equal((await x.start()).status,200);assert.equal((await x.store.all('SELECT * FROM runs')).length,1);
 }finally{await x.rt.close();}
});

test('disabled bridge leaves existing HTTP/MCP routing and ordinary discussion unchanged',async()=>{
 const x=await fixture();try{
  delete x.rt.env.PARLIAMENT_GATEWAY;
  assert.equal((await route(new Request('http://127.0.0.1:18640/v1/parliament'),x.rt.env,()=>{})).status,404);
  const run=await x.service.create({project:'parliament-test',topic:'ordinary',objective:'Synthetic ordinary discussion',agents:['grok']},'ordinary-synthetic','100','200','300');
  await x.rt.runner.tick();assert.equal(x.routine.length,1);assert.notEqual((await x.store.run(run.id)).thread_id,'201');
 }finally{await x.rt.close();}
});

test('private listener rejects public forwarding headers and the public listener has no bridge route',async()=>{
 const x=await fixture();const server=await listenParliament(x.bridge,0);try{
  const port=(server.address() as any).port;
  const send=(headers:Record<string,string>)=>new Promise<number>((resolve,reject)=>{const req=httpRequest({hostname:'127.0.0.1',port,path:'/v1/parliament',method:'POST',headers:{'content-type':'application/json',...headers}},res=>{res.resume();res.on('end',()=>resolve(res.statusCode!));});req.on('error',reject);req.end(JSON.stringify(x.q));});
  for(const header of ['x-forwarded-for','forwarded','cf-connecting-ip','via','origin','x-real-ip'])assert.equal(await send({[header]:'synthetic-public-forwarder'}),403);
  assert.equal(await send({host:'parliament.example'}),403);
  const publicResponse=await route(new Request('http://127.0.0.1:18640/v1/parliament',{method:'POST',headers:{'content-type':'application/json','x-forwarded-host':'parliament.example'},body:JSON.stringify(x.q)}),x.rt.env,()=>{});assert.equal(publicResponse.status,404);
  assert.equal((await x.store.all('SELECT * FROM runs')).length,0);
  assert.equal(await send({}),200);assert.equal((await x.store.all('SELECT * FROM runs')).length,1);
 }finally{server.closeAllConnections();await new Promise<void>(r=>server.close(()=>r()));await x.rt.close();}
});

for(const failure of ['429','body_reset'])test('REST '+failure+' during pending observation backs off and later accepts the same authenticated run',async()=>{
 const x=await fixture();try{
  let now=Date.now(),limited=false; x.bridge.clock=()=>now;
  const postFetch=globalThis.fetch;
  globalThis.fetch=(async(url:any,init:any)=>{
   if(init.method==='GET'){
    assert.equal(init.headers.Authorization,'Bot synthetic-existing-service-bot');assert.equal(init.redirect,'error');
    if(limited){limited=false;if(failure==='body_reset')return new Response(new ReadableStream({start(controller){controller.error(new TypeError('synthetic body reset'));}}),{status:200});return Response.json({retry_after:1},{status:429});}
    const path=String(url).replace('https://discord.com/api/v10/','');return Response.json(await x.discord(path));
   }
   return postFetch(url,init);
  }) as typeof fetch;
  x.bridge.discord=path=>(x.bridge as any).discordGet(path);
  const receipt:any=await (await x.start()).json();await x.rt.runner.tick();assert.equal(x.routine.length,1);
  now+=4000;limited=true;await x.rt.runner.tick();assert.equal((await x.store.run(receipt.run_id)).status,'WAITING_REPLY');assert.equal((await x.result()).status,503);
  now+=2000;const observed:any=await (await x.result()).json();assert.equal(observed.run_id,receipt.run_id);assert.equal(observed.status,'pending');
  const answered=await x.rpc('grok','submit_turn',{run_id:receipt.run_id,turn_id:receipt.turn_id,request_key:'after-backoff',status:'completed',text:'Synthetic recovered answer',citations:[],asset_ids:[],image_perception:'none'});assert.equal(answered.body.result.isError,undefined);
  await x.rt.runner.tick();assert.equal((await (await x.result()).json() as any).status,'completed');assert.equal(x.routine.length,1);assert.equal((await x.store.all('SELECT * FROM runs')).length,1);
 }finally{await x.rt.close();}
});

test('membership revoked after an authenticated answer prevents the actual outbox post',async()=>{
 const x=await fixture();try{
  const q:any=await (await x.start()).json();await x.rt.runner.tick();
  await x.rpc('grok','submit_turn',{run_id:q.run_id,turn_id:q.turn_id,request_key:'before-revoke',status:'completed',text:'Synthetic answer',citations:[],asset_ids:[],image_perception:'none'});
  x.removeMember();await x.rt.runner.tick();assert.equal(x.posts.length,0);assert.notEqual((await x.store.run(q.run_id)).status,'COMPLETED');
 }finally{await x.rt.close();}
});

test('a human reply to the actual target Bot is admitted；a registered Bot reply chain is not',async()=>{
 const x=await fixture();try{
  x.parents.set('1234',{id:'1234',channel_id:'201',author:{id:'1000',bot:true},content:'[parliament-result v1 synthetic] Receipt'});
  x.message.message_reference={message_id:'1234',channel_id:'201'};x.message.content=x.q.text;assert.equal((await x.start()).status,200);
 }finally{await x.rt.close();}
 const y=await fixture();try{
  y.q.requester_id='4000';y.message.author={id:'4000',bot:true};y.message.content=`<@1000> ask request=synthetic-correlation hop=0\n${y.q.text}`;
  y.message.message_reference={message_id:'1234',channel_id:'201'};y.parents.set('1234',{id:'1234',channel_id:'201',author:{id:'1000',bot:true},content:'Synthetic Bot reply'});
  assert.equal((await y.start()).status,403);assert.equal((await y.store.all('SELECT * FROM runs')).length,0);
 }finally{await y.rt.close();}
});

test('source preparation rejects a destination with a junction ancestor before any copy',()=>{
 const temp=mkdtempSync(join(process.cwd(),'.cache/prepare-boundary-')),source=join(temp,'fake-source'),alias=join(temp,'alias');mkdirSync(source);
 symlinkSync(source,alias,process.platform==='win32'?'junction':'dir');
 try{
  const manifest=JSON.parse(readFileSync('integration-manifest.json','utf8'));
  const result=spawnSync(process.execPath,[manifest.preparer,'--source',source,'--destination',join(alias,'new-child')],{encoding:'utf8',env:{SystemRoot:process.env.SystemRoot,SystemDrive:process.env.SystemDrive,TEMP:temp,TMP:temp}});
  assert.notEqual(result.status,0);assert.match(result.stderr,/UNSAFE_DESTINATION_ANCESTOR/);assert.equal(existsSync(join(source,'new-child')),false);
 }finally{assert.equal(lstatSync(alias).isSymbolicLink(),true);unlinkSync(alias);}
});

test('human follows the authenticated relay answer without mistaking its shared sender for the recipient',async()=>{
 const x=await fixture();try{
  const oldId=x.q.message_id,oldMessage=structuredClone(x.message),first:any=await (await x.start()).json();await x.rt.runner.tick();
  await x.rpc('grok','submit_turn',{run_id:first.run_id,turn_id:first.turn_id,request_key:'first-actual-answer',status:'completed',text:'Synthetic answer to follow up.',citations:[],asset_ids:[],image_perception:'none'});await x.rt.runner.tick();
  const url=new URL((await (await x.result()).json() as any).message_url),answerId=url.pathname.split('/').at(-1)!;
  const answer={id:answerId,channel_id:'201',author:{id:'5500',bot:true},content:x.posts[0].body.content};x.parents.set(answerId,answer);x.parents.set(oldId,oldMessage);
  const owned:any=await (await x.bridge.handle(new Request(`http://127.0.0.1:18641/v1/parliament?channel_id=201&recipient_id=1000&reply_to=${answerId}`))).json();assert.equal(owned.owned,true);
  const wrong:any=await (await x.bridge.handle(new Request(`http://127.0.0.1:18641/v1/parliament?channel_id=201&recipient_id=2000&reply_to=${answerId}`))).json();assert.equal(wrong.owned,false);
  x.q.job_id='c'.repeat(64);x.q.message_id=String(BigInt(snowflake())+1234n);x.q.text='Synthetic plain follow-up';Object.assign(x.message,{id:x.q.message_id,content:x.q.text,message_reference:{message_id:answerId,channel_id:'201'}});
  const follow:any=await (await x.bridge.handle(new Request('http://127.0.0.1:18641/v1/parliament',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(x.q)}))).json();assert.equal(follow.status,'pending',JSON.stringify(follow));assert.notEqual(follow.run_id,first.run_id);await x.rt.runner.tick();assert.equal(x.routine.length,2);
  const question=await x.rpc('grok','get_turn',{run_id:follow.run_id,turn_id:follow.turn_id});assert.equal(question.body.result.structuredContent.objective,x.q.text);
  await x.rpc('grok','submit_turn',{run_id:follow.run_id,turn_id:follow.turn_id,request_key:'follow-up-actual-answer',status:'completed',text:'Synthetic authenticated follow-up answer.',citations:[],asset_ids:[],image_perception:'none'});await x.rt.runner.tick();
  assert.equal((await (await x.result()).json() as any).status,'completed');assert.equal(x.posts.length,4);assert.ok(x.posts.every(v=>v.url==='https://discord.com/api/v10/channels/201/messages'));
 }finally{await x.rt.close();}
});

for(const disable of ['false','removed'])test('restart with bridge '+disable+' never reinterprets saved jobs as a legacy discussion',async()=>{
 const x=await fixture();let oldClosed=false;
 try{
  const q:any=await (await x.start()).json(),directory=dirname(x.rt.db.file),oldEnv=x.rt.env;await x.rt.close();oldClosed=true;
  const config:any={...DEFAULT_CONFIG,env:{...DEFAULT_CONFIG.env,ALLOWED_GUILD_IDS:'100',ALLOWED_USER_IDS:'300,4000'}};
  if(disable==='false')config.parliament={version:1,enabled:false};
  const restarted=createRuntime(process.cwd(),directory,config);
  try{
   for(const key of ['LIVE_CONNECTORS','GROK_ROUTINE_URL','GROK_ROUTINE_KEY','GROK_ROUTINE_ALLOWED_HOSTS','GROK_ROUTINE_EGRESS','MCP_OAUTH_ISSUER','DISCORD_BOT_TOKEN']) (restarted.env as any)[key]=(oldEnv as any)[key];
   assert.ok(restarted.env.PARLIAMENT_GATEWAY);await restarted.runner.tick();assert.equal(x.routine.length,0);assert.equal(x.posts.length,0);assert.equal(x.callbacks.length,0);
   const store=new Store(restarted.env);assert.equal((await store.all("SELECT * FROM outbox WHERE kind='DISCORD'")).length,0);assert.equal((await store.run(q.run_id)).thread_id,null);assert.equal((await store.run(q.run_id)).status,'FAILED');
  }finally{await restarted.close();}
 }finally{if(!oldClosed)await x.rt.close();}
});
