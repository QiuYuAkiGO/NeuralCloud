import test from 'node:test';
import assert from 'node:assert/strict';
import {checkedCommand, replyOnce} from './bridge.mjs';
const valid = () => ({id:'a'.repeat(32), action:'reply', group_openid:'synthetic_group',
  msg_id:'ROBOT1.0-synthetic', msg_seq:1, text:'合成回复', deadline_ms:2000});

test('only a bounded passive group reply is accepted', () => {
  assert.equal(checkedCommand(valid(),1000).action,'reply');
  for (const patch of [{action:'push'}, {msg_id:''}, {group_openid:'../users'}, {msg_seq:0},
    {deadline_ms:999}, {deadline_ms:1000000}, {text:''}, {api_path:'/any'}, {token:'secret'}]) {
    assert.throws(()=>checkedCommand({...valid(),...patch},1000));
  }
});

test('exactly one authenticated POST, bound path, trigger and segment sequence', async () => {
  const calls=[];
  const bot={tokenManager:{getAccessToken:async()=> 'synthetic'},apiClient:{request:async(...a)=>{
    calls.push(a);return {id:'ack'};
  }}};
  const used=new Set();
  assert.deepEqual(await replyOnce(bot,{appId:'1',appSecret:'test'},valid(),new Set(['synthetic_group']),used,()=>1000),{id:'ack'});
  assert.equal(calls.length,1);
  assert.equal(calls[0][2],'/v2/groups/synthetic_group/messages');
  assert.deepEqual(calls[0][3],{content:'合成回复',msg_type:0,msg_id:'ROBOT1.0-synthetic',msg_seq:1});
  await assert.rejects(replyOnce(bot,{},valid(),new Set(['synthetic_group']),used,()=>1000));
  assert.equal(calls.length,1);
});

test('timeout never retries, and token delay cannot extend deadline', async () => {
  let calls=0, now=1000;
  const bot={tokenManager:{getAccessToken:async()=> 'synthetic'},apiClient:{request:async()=>{calls++;throw Error('unknown')}}};
  const used=new Set();
  await assert.rejects(replyOnce(bot,{},valid(),new Set(['synthetic_group']),used,()=>now));
  await assert.rejects(replyOnce(bot,{},valid(),new Set(['synthetic_group']),used,()=>now));
  assert.equal(calls,1);
  bot.tokenManager.getAccessToken=async()=>{now=3000;return 'synthetic'};
  await assert.rejects(replyOnce(bot,{},valid(),new Set(['synthetic_group']),new Set(),()=>1000+(now-1000)));
  assert.equal(calls,1);
});

test('unauthorized destination fails before authentication or POST', async () => {
  let touched=false;
  await assert.rejects(replyOnce({tokenManager:{getAccessToken:async()=>{touched=true}}},
    {},valid(),new Set(),new Set(),()=>1000));
  assert.equal(touched,false);
});
