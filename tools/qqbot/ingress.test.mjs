import test from 'node:test';
import assert from 'node:assert/strict';
import {QQBot} from '@tencent-connect/qqbot-nodejs';
import {dispatchEvent} from './node_modules/@tencent-connect/qqbot-nodejs/dist/protocol/gateway/event-dispatcher.js';
import {GatewayConnection} from './node_modules/@tencent-connect/qqbot-nodejs/dist/protocol/gateway/gateway-connection.js';
import {bindInbound, ingressTelemetry, ingressKeys} from './ingress.mjs';

test('real pinned SDK dispatch and message callback preserve envelope without network', async () => {
  const telemetry = ingressTelemetry();
  const config = {appId:'123', appSecret:'synthetic-secret', groups:['synthetic_group']};
  const bot = new QQBot({...config, logger:telemetry.logger});
  const packets = [];
  bindInbound(bot, config, item => packets.push(item), telemetry);
  const data = {group_openid:'synthetic_group', author:{member_openid:'synthetic_member'},
    id:'synthetic_msg', timestamp:'2026-10-21T00:00:00Z', content:'private synthetic content'};
  const event = dispatchEvent('GROUP_AT_MESSAGE_CREATE', data, '123', telemetry.logger);
  // Execute the installed SDK's actual callback/middleware path, never start its transport.
  await bot.handleInboundMessage(event.msg);
  assert.deepEqual(packets, [{type:'event', transport:'qqbot', app_id:'123',
    event_type:'GROUP_AT_MESSAGE_CREATE', data}]);
  assert.equal(telemetry.snapshot().group_callback, 1);
  assert.equal(telemetry.snapshot().group_allowed_callback, 1);
  await bot.handleInboundMessage({...event.msg, raw:{...data, group_openid:'outside'}});
  assert.equal(packets.length, 2); // Existing Python scope validation remains the authority.
  assert.equal(telemetry.snapshot().group_allowed_callback, 1);
  await bot.handleInboundMessage({kind:'c2c', senderId:'other', messageId:'x', raw:{}});
  assert.equal(packets.length, 2);
  assert.equal(telemetry.snapshot().other_message_callback, 1);
  assert.equal(JSON.stringify(telemetry.snapshot()).includes('private'), false);
});

test('real SDK dispatch log feeds only fixed counters; raw lifecycle remains unchanged', async () => {
  const telemetry = ingressTelemetry();
  let callbacks = 0;
  const gateway = new GatewayConnection({account:{accountId:'123'},
    abortSignal:new AbortController().signal, log:telemetry.logger,
    getAccessToken:async()=>{throw Error('network forbidden')},
    getGatewayUrl:async()=>{throw Error('network forbidden')}, onMessage:()=>{callbacks++;}});
  const data = {group_openid:'private-group', author:{member_openid:'private-author'},
    id:'private-id', content:'private-body'};
  gateway.handleDispatch({t:'GROUP_AT_MESSAGE_CREATE', d:data, op:0});
  assert.equal(callbacks, 1);
  assert.equal(telemetry.snapshot().dispatch, 1);
  assert.equal(telemetry.snapshot().group_at_dispatch, 1);
  telemetry.logger.error('[123] Message parse error: private payload');
  telemetry.logger.error('private token secret');
  assert.equal(telemetry.snapshot().parse_error, 1);
  assert.equal(telemetry.snapshot().sdk_error, 2);
  assert.deepEqual(Object.keys(telemetry.snapshot()).sort(), [...ingressKeys].sort());
  const values = telemetry.take();
  assert.equal(telemetry.take(), null);
  values.dispatch = -99;
  assert.equal(telemetry.snapshot().dispatch, 1);
  assert.equal(JSON.stringify(values).includes('private'), false);
  assert.throws(()=>telemetry.count('private-key'));

  const bot = new QQBot({appId:'123', appSecret:'synthetic-only'});
  const packets=[];
  bindInbound(bot,{appId:'123', groups:[]},item=>packets.push(item),telemetry);
  await bot.emit('rawEvent',{eventType:'GROUP_MSG_REJECT',data:{group_openid:'synthetic'}});
  await bot.emit('rawEvent',{eventType:'UNSUPPORTED',data:{secret:'never forwarded'}});
  assert.deepEqual(packets,[{type:'lifecycle',app_id:'123',event_type:'GROUP_MSG_REJECT',data:{group_openid:'synthetic'}}]);
});
