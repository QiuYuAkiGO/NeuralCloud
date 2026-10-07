const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const src=readFileSync('gui/static/app.js','utf8');
let calls=0, painted=0, resolve, listener;
const timers=new Map(); let tid=0;
const env={document:{hidden:true,addEventListener:(name,fn)=>listener=fn},
 setTimeout:fn=>{timers.set(++tid,fn);return tid},clearTimeout:id=>timers.delete(id),
 fetch:()=>{calls++;return new Promise(r=>resolve=r)},applySnapshot:()=>painted++};
vm.createContext(env);
vm.runInContext(src.slice(src.indexOf('let memoryRefreshTimer'),src.indexOf('const state ='))+
 src.slice(src.indexOf('async function fetchStatus'),src.indexOf('async function sendChat')),env);
(async()=>{
 await env.fetchStatus();env.scheduleStatusRefresh();assert.equal(calls,0);assert.equal(timers.size,0);
 env.document.hidden=false; listener(); assert.equal(timers.size,1);
 env.scheduleStatusRefresh(); assert.equal(timers.size,1);
 const a=env.fetchStatus(), b=env.fetchStatus(); assert.equal(calls,1);
 env.document.hidden=true;listener();assert.equal(timers.size,0);
 resolve({ok:true,json:async()=>({})});await Promise.all([a,b]);assert.equal(painted,0);
 env.document.hidden=false;listener();assert.equal(timers.size,1);
 const c=env.fetchStatus();resolve({ok:true,json:async()=>({})});await c;
 assert.equal(calls,2);assert.equal(painted,1);console.log('demand/visibility/coalescing checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
