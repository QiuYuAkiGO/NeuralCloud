import test from 'node:test';
import assert from 'node:assert/strict';
import {QQBot} from '@tencent-connect/qqbot-nodejs';
import {dispatchEvent} from './node_modules/@tencent-connect/qqbot-nodejs/dist/protocol/gateway/event-dispatcher.js';
import {bindInbound, ingressTelemetry} from './ingress.mjs';
import {checkedCommand, replyOnce} from './bridge.mjs';

test('real SDK C2C callbacks reach Python with authenticated envelope', async () => {
  const telemetry=ingressTelemetry(), packets=[];
  const config={appId:'123',appSecret:'synthetic',groups:[],users:['peer']};
  const bot=new QQBot({...config,logger:telemetry.logger});
  bindInbound(bot,config,x=>packets.push(x),telemetry);
  const data={author:{user_openid:'peer'},id:'c2c1',timestamp:new Date().toISOString(),content:'hello'};
  const event=dispatchEvent('C2C_MESSAGE_CREATE',data,'123',telemetry.logger);
  await bot.handleInboundMessage(event.msg);
  assert.deepEqual(packets,[{type:'event',transport:'qqbot',app_id:'123',event_type:'C2C_MESSAGE_CREATE',data}]);
  await bot.emit('rawEvent',{eventType:'FRIEND_DEL',data:{openid:'peer'}});
  assert.equal(packets[1].event_type,'FRIEND_DEL');
});

test('private reply is allowlisted, exclusive, passive, and never retried', async () => {
  const command={id:'b'.repeat(32),action:'reply',user_openid:'peer',msg_id:'c2c1',msg_seq:1,text:'ok',deadline_ms:2000};
  const calls=[], used=new Set();
  const bot={tokenManager:{getAccessToken:async()=> 'test'},apiClient:{request:async(...args)=>{calls.push(args);return {id:'ack'};}}};
  assert.throws(()=>checkedCommand({...command,group_openid:'peer'},1000));
  assert.throws(()=>checkedCommand({...command,user_openid:'../groups/peer'},1000));
  await assert.rejects(replyOnce(bot,{users:[]},command,new Set(['peer']),used,()=>1000));
  assert.equal(calls.length,0);
  await replyOnce(bot,{users:['peer']},command,new Set(),used,()=>1000);
  assert.equal(calls[0][2],'/v2/users/peer/messages');
  assert.equal(calls[0][3].msg_id,'c2c1');
  await assert.rejects(replyOnce(bot,{users:['peer']},command,new Set(),used,()=>1000));
  assert.equal(calls.length,1);
});
