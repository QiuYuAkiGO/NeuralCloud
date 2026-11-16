import test from 'node:test';
import assert from 'node:assert/strict';
import { MemoryClient, bindMemory } from '../src/index.mjs';

test('prepare and tool reuse one recall, and source/index epochs invalidate it', async () => {
  let generation=1, index=1, searches=0;
  const requests=[];
  const client=new MemoryClient('http://127.0.0.1:8765','x'.repeat(32), {fetcher: async (url, options) => {
    if(url.pathname==='/v1/health') return Response.json({generation, workflow:{index:{generation:index},workflow_version:'memory-workflow-v1'}});
    requests.push(JSON.parse(options.body)); searches++;
    await new Promise(r=>setTimeout(r,5));
    return Response.json({status:'OK',generation,items:[{record_id:'r',content:'x'.repeat(3000)}],diagnostics:{stages:[{private:'diagnostic'}]}});
  }});
  const subject={platform:'qq',account_id:'12345'};
  client.setContext('a',{subject,targets:[],turn_id:'turn-1'});
  const tools={};
  bindMemory({id:'a',ctx:{tools:{register:t=>{tools[t.name]=t;return()=>{};}}}},client);
  const a=client.recall('我叫什么',10,undefined,{subject,turn_id:'turn-1'});
  const b=tools.memory_recall.execute({query:'我叫什么'},{signal:new AbortController().signal});
  const results=await Promise.all([a,b]);
  assert.equal(searches,1);
  assert.equal(results[0].items[0].content.length,1024);
  assert.equal(results[0].diagnostics.stages,undefined);
  assert.equal(requests[0].turn_id,undefined);
  generation++;
  await tools.memory_recall.execute({query:'我叫什么'},{signal:new AbortController().signal});
  assert.equal(searches,2);
  index++;
  await tools.memory_recall.execute({query:'我叫什么'},{signal:new AbortController().signal});
  assert.equal(searches,3);
});

test('explicit targets do not replace trusted speaker; no independent retry on empty results',async()=>{
  let received, calls=0;
  const client=new MemoryClient('http://127.0.0.1:8765','x'.repeat(32),{fetcher:async(url,options)=>{
    if(url.pathname==='/v1/health')return Response.json({generation:1});
    calls++;received=JSON.parse(options.body);return Response.json({status:'OK',items:[]});
  }});
  client.setContext('a',{subject:{platform:'qq',account_id:'12345'},turn_id:'t'});
  let tool;
  bindMemory({id:'a',ctx:{tools:{register:t=>{if(t.name==='memory_recall')tool=t;return()=>{};}}}},client);
  await tool.execute({query:'67890叫什么',subject:{platform:'qq',account_id:'99999'},targets:[{platform:'qq',account_id:'67890'}],intent:'identity'},{});
  assert.equal(received.subject.account_id,'12345');
  assert.equal(received.targets[0].account_id,'67890');
  assert.equal(calls,1);
  client.setContext('a',{subject:{platform:'qq',account_id:'12345'},query:'原始提问',targets:[{platform:'qq',account_id:'12345'}],turn_id:'t2'});
  await tool.execute({query:'67890叫什么'},{});
  assert.deepEqual(received.targets,[]);
  assert.equal(received.subject.account_id,'12345');
});
