// Prepare a NEW source candidate only；never edit the installed service or its data．
import {cpSync,existsSync,mkdirSync,readFileSync,writeFileSync,statSync,realpathSync,lstatSync} from 'node:fs';
import {resolve,join,dirname,relative,isAbsolute} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
const args=process.argv.slice(2),arg=k=>args[args.indexOf(k)+1];
if(!args.includes('--source')||!args.includes('--destination'))throw Error('SOURCE_AND_NEW_DESTINATION_REQUIRED');
const source=realpathSync(resolve(arg('--source'))),dest=resolve(arg('--destination'));
const within=(a,b)=>{const r=relative(a,b);return !r||!r.startsWith('..')&&!isAbsolute(r);};
if(existsSync(dest)||within(source,dest)||within(dest,source))throw Error('NEW_SEPARATE_CANDIDATE_REQUIRED');
for(let parent=dirname(dest);;parent=dirname(parent)){
 if(existsSync(parent)&&(lstatSync(parent).isSymbolicLink()||realpathSync(parent).toLowerCase()!==resolve(parent).toLowerCase()))throw Error('UNSAFE_DESTINATION_ANCESTOR');
 if(parent===dirname(parent))break;
}
const here=dirname(fileURLToPath(import.meta.url));
const pins={
 'src/native/runtime.ts':'9ecf0ec008bb093f51e04f42f20350e3b1a938aee1fd1f4e33d90cc031ec4c11',
 'src/native/http.ts':'9ea7316ecc5d7ff778339422fdd4153a9e6fd4610036f59d5c67e03086781518',
 'src/native/runner.ts':'7394d3856b63b559f071be20146216028ef10b1821043c719276a262e33f92e3',
 'src/native/main.ts':'71d055785dd9692fe5f229bb377d7b95d7758653a7a34bd23b93470284665a99',
 'src/service.ts':'1cf6a1b9688accca399b21efeb0fbdd00299bb553a50a28a2df7a5dd97ce77c4',
 'src/types.ts':'8c9ed6f50390ef85481efb4d6dbb5a09925fde3745b4de460ad0b91beff43fcf',
 'src/discord.ts':'cc7545db42724c05f652a1dd67e2f0d7d1441c69247f05ad64ab00c44526f0ca',
 'src/events.ts':'3bf5082e2893f7be850bb68e3c405aee71717e4eca57b95729c1f8df3c0dcd5b'
};
const hash=b=>createHash('sha256').update(b).digest('hex');
for(const [file,pin] of Object.entries(pins))if(hash(readFileSync(join(source,file)))!==pin)throw Error('REVIEWED_SOURCE_CHANGED:'+file);
mkdirSync(dest,{recursive:true});
for(const name of ['src','migrations','scripts','package.json','package-lock.json','tsconfig.json']){
 cpSync(join(source,name),join(dest,name),{recursive:true,filter:p=>{
  const stat=statSync(p,{throwIfNoEntry:true});if(realpathSync(p).toLowerCase()!==resolve(p).toLowerCase()||!stat.isDirectory()&&stat.size>4*1024*1024)throw Error('UNSAFE_SOURCE_FILE');return true;
 }});
}
const changed={};
function edit(file,fn){const path=join(dest,file),old=readFileSync(path,'utf8'),next=fn(old);if(next===old)throw Error('PATCH_DID_NOT_APPLY:'+file);writeFileSync(path,next);changed[file]={before:hash(Buffer.from(old)),after:hash(Buffer.from(next))};}
function once(text,from,to){if(text.split(from).length!==2)throw Error('PATCH_ANCHOR_CHANGED');return text.replace(from,to);}
edit('src/types.ts',s=>once(s,'export interface Env {',"export interface Env {\n  PARLIAMENT_GATEWAY?:import('./native/parliament').ParliamentGateway;"));
edit('src/native/runtime.ts',s=>{
 s="import {NativeParliament,parliamentConfig} from './parliament';\n"+s;
 s=once(s,'export interface Config {','export interface Config {\n  parliament?:unknown;');
 s=once(s,"'connections','research','information']","'connections','research','information','parliament']");
 s=once(s,'  if(c.information!==undefined)informationConfig.parse(c.information);','  if(c.information!==undefined)informationConfig.parse(c.information);\n  if(c.parliament!==undefined){const p=parliamentConfig.parse(c.parliament);if(p.enabled&&p.port===c.port)throw new Error("SEPARATE_PRIVATE_PORT_REQUIRED");}');
 return once(s,'  const runner=new PersistentRunner(env,config.intervalMs);',"  const hasParliamentLedger=!!db.raw.prepare(\"SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_parliament_requests'\").get();\n  if(hasParliamentLedger||config.parliament!==undefined&&parliamentConfig.parse(config.parliament).enabled)env.PARLIAMENT_GATEWAY=new NativeParliament(db.raw,env,()=>config.parliament??{version:1,enabled:false});\n  const runner=new PersistentRunner(env,config.intervalMs);");
});
edit('src/native/main.ts',s=>{
 s="import {listenParliament,parliamentConfig} from './parliament';\n"+s;
 s=once(s,'  const server=await listen(runtime.env,config.port,createPublicHttpDiagnostic(data));',`  let parliamentServer:Awaited<ReturnType<typeof listen>>|undefined;
  let server:Awaited<ReturnType<typeof listen>>;
  try{
    const privateConfig=config.parliament===undefined?undefined:parliamentConfig.parse(config.parliament);
    if(privateConfig?.enabled&&runtime.env.PARLIAMENT_GATEWAY)parliamentServer=await listenParliament(runtime.env.PARLIAMENT_GATEWAY,privateConfig.port);
    server=await listen(runtime.env,config.port,createPublicHttpDiagnostic(data));
  }catch(e){if(parliamentServer){parliamentServer.closeAllConnections();await new Promise<void>(r=>parliamentServer!.close(()=>r()));}await runtime.close();throw e;}`);
 return once(s,'closing=true;server.closeAllConnections();','closing=true;if(parliamentServer){parliamentServer.closeAllConnections();await new Promise<void>(r=>parliamentServer!.close(()=>r()));}server.closeAllConnections();');
});
edit('src/events.ts',s=>{
 s=once(s,'      try { currentConnection=await this.store.connection(turn.connection_id);','      try { await this.env.PARLIAMENT_GATEWAY?.assertRun(run.id,true);currentConnection=await this.store.connection(turn.connection_id);');
 return once(s,' } catch { allowed=false; }'," } catch(e) { if(e instanceof Fault&&e.code==='OUTBOX_BACKOFF')continue;allowed=false; }");
});
for(const file of ['src/service.ts','src/native/runner.ts','src/discord.ts'])edit(file,s=>{
 const matcher=/(this\.env\.RESEARCH_GATEWAY\?\.assertRun\(([^;]+)\);)/g;
 let count=0;s=s.replace(matcher,(_m,full,params)=>{count++;const run=params.split(',')[0];return full+'\n    await this.env.PARLIAMENT_GATEWAY?.assertRun('+run+');';});
 if(count<1)throw Error('MISSING_LIFECYCLE_GUARDS');
 if(file==='src/service.ts'){
  s=once(s,'userId:string,attachment?:Attachment):Promise<Run>','userId:string,attachment?:Attachment,originCode:"HUMAN_COMMAND"|"HUMAN_GATEWAY_MESSAGE"|"BOT_GATEWAY_MESSAGE"="HUMAN_COMMAND"):Promise<Run>');
  s=once(s,"'run.created','HUMAN_COMMAND'","'run.created',originCode");
  s=once(s,'  async ensureThread(runId:string):Promise<boolean> {','  async ensureThread(runId:string):Promise<boolean> {\n    if(this.env.PARLIAMENT_GATEWAY?.deliveryTarget(runId)){await this.env.PARLIAMENT_GATEWAY.assertRun(runId);return true;}');
  s=s.replaceAll('thread_id:run.thread_id!', 'thread_id:this.env.PARLIAMENT_GATEWAY?.deliveryTarget(run.id)??run.thread_id!');
  s=once(s,'    const claimed=await this.store.exec("INSERT OR IGNORE INTO dispatches',"    if(!await this.store.one('SELECT 1 FROM dispatches WHERE turn_id=?',turn.id))await this.env.PARLIAMENT_GATEWAY?.assertRun(run.id,true);\n    const claimed=await this.store.exec(\"INSERT OR IGNORE INTO dispatches");
  s=once(s,'const chunks=splitMessage(publicContent,1400);','const chunks=splitMessage(this.env.PARLIAMENT_GATEWAY?.publicText(run.id,publicContent)??publicContent,1400);');
  s=once(s,'      const result=await this.outbox.post(`reply-${turn.id}-${i}`','      await this.env.PARLIAMENT_GATEWAY?.assertRun(run.id);\n      const result=await this.outbox.post(`reply-${turn.id}-${i}`');
  s=once(s,'    const result=await this.outbox.post(`complete-${runId}`','    await this.env.PARLIAMENT_GATEWAY?.assertRun(runId);\n    const result=await this.outbox.post(`complete-${runId}`');
 }
 if(file==='src/discord.ts'){
  s=once(s,'payload.thread_id !== run.thread_id','payload.thread_id !== (this.env.PARLIAMENT_GATEWAY?.deliveryTarget(run.id)??run.thread_id)');
  s=once(s,'    const claim=await this.store.exec(',"    await this.env.PARLIAMENT_GATEWAY?.assertRun(run.id,true);\n    const claim=await this.store.exec(");
  s=once(s,'      if (e instanceof Retryable) {',`      if(e instanceof Fault&&e.code==='OUTBOX_BACKOFF'){
        await this.store.exec("UPDATE outbox SET status='PENDING',attempts=attempts-1,lease_until=NULL WHERE id=? AND status='SENDING'",id);throw e;
      }
      if (e instanceof Retryable) {`);
 }
 return s;
});
cpSync(join(here,'parliament.ts'),join(dest,'src/native/parliament.ts'));
cpSync(join(here,'parliament.node.ts'),join(dest,'parliament.node.ts'));
writeFileSync(join(dest,'integration-manifest.json'),JSON.stringify({preparer:join(here,'prepare.mjs'),sourceFiles:pins,changed,newModule:hash(readFileSync(join(here,'parliament.ts'))),productionModified:false},null,2));
// Resolve existing dependencies read-only；no npm install，junction，or package mutation．
const require=createRequire(join(source,'package.json')),esbuild=require('esbuild');
const external={name:'existing-dependencies',setup(build){build.onResolve({filter:/^(?:@|[A-Za-z][^:]*(?:\/|$))/},a=>{
 if(a.path.startsWith('node:'))return;
 const name=a.path==='@cf-wasm/photon/workerd'?'@cf-wasm/photon/node':a.path;
 return {path:pathToFileURL(require.resolve(name)).href,external:true};
});}};
await esbuild.build({entryPoints:[join(dest,'parliament.node.ts')],outfile:join(dest,'parliament-tests.mjs'),bundle:true,platform:'node',target:'node24',format:'esm',plugins:[external]});
await esbuild.build({entryPoints:[join(dest,'src/native/main.ts')],outfile:join(dest,'candidate-server.mjs'),bundle:true,platform:'node',target:'node24',format:'esm',plugins:[external]});
const config=JSON.parse(readFileSync(join(dest,'tsconfig.json'),'utf8'));
config.compilerOptions={...config.compilerOptions,baseUrl:source,paths:{'@cf-wasm/photon/workerd':[join(source,'node_modules/@cf-wasm/photon/dist/workerd.d.ts')],'*':[join(source,'node_modules/*')]},typeRoots:[join(source,'node_modules/@types')],types:[join(source,'node_modules/@cloudflare/workers-types/index.d.ts'),'node']};
config.include=[join(dest,'src/**/*.ts')];config.exclude=[];
writeFileSync(join(dest,'tsconfig.integration.json'),JSON.stringify(config));
console.log(JSON.stringify({prepared:true,productionModified:false,dependenciesInstalled:false,modelCalled:false,candidate:dest}));
