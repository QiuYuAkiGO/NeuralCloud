import {readFileSync,writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
const root=process.argv[2];const read=p=>JSON.parse(readFileSync(root+'/'+p));
const hash=v=>createHash('sha256').update(JSON.stringify(v)).digest('hex');
const cases=read('cases.json'),design=read('design.json'),checks=[];
for(const job of design.pilot){
 const row=read('results/'+job.run_id+'.json');const c=cases.find(c=>c.id===job.case_id);
 const expected=structuredClone(c.messages);expected.find(m=>m.role==='system').content=[{type:'text',text:design.prompts[c.id][job.group].text}];
 const errors=[];
 if(!['completed','unevaluable'].includes(row.status))errors.push('trial_failed');
 if(!row.calls.length)errors.push('no_request_capture');
 if(hash(row.calls[0]?.request.messages)!==hash(expected))errors.push('frozen_input_mismatch');
 if(hash(row.calls[0]?.request.tools)!==hash(c.header.tools))errors.push('tool_schema_mismatch');
 for(const call of row.calls)for(const [k,v]of Object.entries(c.header.config))if(call.request[k]!==v)errors.push('model_config_mismatch:'+k);
 checks.push({id:job.run_id,passed:!errors.length,errors,status:row.status});
}
const result={passed:checks.length===6&&checks.every(c=>c.passed),checks,cases_sha256:hash(cases),design_sha256:hash(design),note:'Reconstructed historical contexts using production Session projection; pinned current prompt, production AgentLoop and adapter, cloned tool schemas and isolated simulations. No production integration handlers are executed.'};
writeFileSync(root+'/gate.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result));
