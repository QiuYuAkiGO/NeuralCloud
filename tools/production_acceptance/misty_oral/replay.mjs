// Frozen-payload replay through the pinned production AgentLoop and PiAi adapter.
// No production service client, channel plugin, or external-action executor is mounted.
import {readFileSync,writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {createHash,randomUUID} from 'node:crypto';
const [base,root,mode='pilot']=process.argv.slice(2);
const req=createRequire(base+'/package.json');
const load=async n=>import(pathToFileURL(req.resolve('@deepseek-ai/'+n)));
const {Context}=await load('cordis');
const {default:AgentRegistry}=await load('dsh-agent');
const {default:AgentLoop}=await load('dsh-agent-loop');
const {default:LlmRuntime,createUserMessage}=await load('dsh-llm');
const {default:SessionStore,SessionId}=await load('dsh-session');
const {default:Projection}=await load('dsh-session-projection');
const {default:SystemPrompt}=await load('dsh-system-prompt');
const {default:Tools}=await load('dsh-tools');
const {default:Attachments}=await load('dsh-attachment-local');
const {mountExistingModel}=await import(pathToFileURL(base+'/resident/composition.mjs'));
const config=JSON.parse(readFileSync('/var/lib/misty/config/harness.json'));
const cases=JSON.parse(readFileSync(root+'/cases.json'));
const design=JSON.parse(readFileSync(root+'/design.json'));
const digest=x=>createHash('sha256').update(JSON.stringify(x)).digest('hex');
mkdirSync(root+'/results',{recursive:true,mode:0o700});
if(mode==='formal'){
 const gate=JSON.parse(readFileSync(root+'/gate.json'));
 if(!gate.passed||gate.cases_sha256!==digest(cases)||gate.design_sha256!==digest(design))throw Error('CONTRACT_GATE_NOT_PASSED');
}
async function run(job){
 const output=root+'/results/'+job.run_id+'.json';if(existsSync(output))return;
 const c=cases.find(c=>c.id===job.case_id);const ctx=new Context();
 const record={...job,status:'running',case_kind:c.kind,input_sha256:digest(c),calls:[],actions:[],missing_evidence:[],started_at:new Date().toISOString()};
 writeFileSync(output,JSON.stringify(record));
 let handle;
 try{
 for(const p of [LlmRuntime,SessionStore,Projection,SystemPrompt,Tools,AgentRegistry])await ctx.plugin(p);
 await ctx.plugin(AgentLoop,{agents:[],maxParallelToolCalls:1});
 await ctx.plugin(Attachments,{dshHome:root+'/frozen-home',maxImageBytes:10*1024*1024,maxImagesPerMessage:4,maxMessageImageBytes:20*1024*1024,normalizedImageMaxPixels:2097152,normalizedImageMaxBytes:2*1024*1024});
 ctx.on('llm/stream', (options, next) => (async function*(){
   if(record.calls.length>=6)throw Error('EXPERIMENT_STEP_LIMIT');
   const generated=options.messages.filter(m=>m.role!=='system'&&!(m.role==='user'&&m.content?.some(b=>b.text==='EXPERIMENT_TRIGGER')));
   const messages=structuredClone(c.messages);
   const system=messages.find(m=>m.role==='system');
   system.content=[{type:'text',text:design.prompts[c.id][job.group].text}];
   const frozen={...options,...c.header.config,messages:[...messages,...generated],tools:c.header.tools,toolHistory:c.toolHistory};
   record.calls.push({request:JSON.parse(JSON.stringify({...frozen,signal:undefined})),usage:[],finish:null});
   const call=record.calls.at(-1);
   for await(const chunk of ctx.llm.adapterStream(frozen)){
    if(chunk.type==='usage')call.usage.push(chunk.usage);
    if(chunk.type==='finish')call.finish=chunk.reason;
    yield chunk;
   }
 })());
 await mountExistingModel(ctx,config.model,readFileSync(config.model.key_file,'utf8').trim());
 for(const schema of c.header.tools)ctx.tools.register({...schema,
  output:{schema:{type:'object',additionalProperties:true},render:(_a,v)=>[{type:'text',text:JSON.stringify(v)}]},
  execute:async(args,exec)=>{
   record.actions.push({name:schema.name,args});
   if(schema.name==='misty_wait'){exec.concludeTurn();return{status:'WAIT',simulation:true};}
   if(schema.name==='misty_send')return{status:'SENT',external_id:'SIMULATED:'+randomUUID(),simulation:true,ledger_confirmation:'SIMULATED_ONLY'};
   const match=c.frozen_tools?.find(x=>x.name===schema.name&&JSON.stringify(x.arguments)===JSON.stringify(args));
   if(match)return structuredClone(match.result);
   record.missing_evidence.push({name:schema.name,args});exec.concludeTurn();return{status:'UNAVAILABLE',simulation:true,error:'FROZEN_EVIDENCE_NOT_AVAILABLE'};
  }});
 handle=await ctx.agents.create({sessionId:SessionId('experiment-'+randomUUID()),agentOptions:c.header.config});
 handle.agent.followup(createUserMessage({content:[{type:'text',text:'EXPERIMENT_TRIGGER'}],source:{kind:'user'}}));
 await Promise.race([handle.agent.whenIdle(),new Promise((_,reject)=>{const t=setTimeout(()=>reject(Error('TRIAL_TIMEOUT')),180000);t.unref();})]);
 record.status=!record.calls.length?'contract_failed':record.missing_evidence.length?'unevaluable':record.calls.some(x=>['error','aborted'].includes(x.finish?.kind))?'model_error':'completed';
 record.events=handle.agent.session.snapshotEvents();
 }catch(e){record.status='failed';record.error=String(e);}
 finally{await handle?.dispose();await ctx.fiber.dispose();record.finished_at=new Date().toISOString();writeFileSync(output,JSON.stringify(record));}
 console.log(JSON.stringify({run_id:job.run_id,status:record.status,calls:record.calls.length}));
}
const jobs=mode==='pilot'?design.pilot:mode==='image-pilot'?design.schedule.filter(j=>j.group==='A'&&j.repetition===0&&cases.find(c=>c.id===j.case_id).messages.some(m=>m.content.some(b=>b.type==='image'))).slice(0,6).map((j,i)=>({...j,run_id:'pilot-image-'+i})):design.schedule;
let cursor=0;
await Promise.all(Array.from({length:mode==='pilot'?1:3},async()=>{while(cursor<jobs.length){const job=jobs[cursor++];await run(job);}}));
