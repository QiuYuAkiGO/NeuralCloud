import {readFileSync,writeFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const base=process.argv[2],root=process.argv[3];
const require=createRequire(base+'/package.json');
const {Session,SessionId}=await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-session')));
const cases=JSON.parse(readFileSync(root+'/real-cases.json','utf8'));
for(const c of cases){
 const lines=readFileSync('/var/lib/misty/harness/sessions/--var-lib-misty-harness-workspace--/'+c.session+'/session.v4.jsonl','utf8').split('\n').filter(Boolean).map(JSON.parse);
 const header=lines[0],events=lines.slice(1).filter(e=>e.seq<c.anchor_seq);
 try{
 const session=Session.create(SessionId(c.session),events,header);
 c.messages=session.deriveMessages();c.toolHistory=session.toolHistory();c.projection_verified=true;
 c.source_events=events;c.source_header=header;
 }catch(e){c.projection_error=String(e);}
}
writeFileSync(root+'/projected-cases.json',JSON.stringify(cases));
console.log(JSON.stringify(cases.map(c=>({id:c.id,verified:c.projection_verified,error:c.projection_error,messages:c.messages.length}))));
