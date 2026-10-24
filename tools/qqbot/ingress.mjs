// Bounded metadata only. SDK log text/payloads, identifiers and errors are never retained.
export const ingressKeys = Object.freeze([
  'dispatch', 'group_at_dispatch', 'group_dispatch', 'lifecycle_dispatch', 'other_dispatch',
  'group_callback', 'group_allowed_callback', 'other_message_callback', 'raw_callback',
  'parse_error', 'sdk_error', 'ready', 'resumed',
]);
const lifecycleEvents = new Set(['GROUP_ADD_ROBOT', 'GROUP_DEL_ROBOT', 'GROUP_MSG_REJECT', 'GROUP_MSG_RECEIVE', 'FRIEND_ADD', 'FRIEND_DEL', 'C2C_MSG_REJECT', 'C2C_MSG_RECEIVE']);

export function ingressTelemetry() {
  const counts = Object.fromEntries(ingressKeys.map(key => [key, 0]));
  let dirty = true;
  const count = key => {
    if (!Object.hasOwn(counts, key)) throw new Error('qqbot_telemetry_key_invalid');
    counts[key] = Math.min(2147483647, counts[key] + 1);
    dirty = true;
  };
  return {
    count,
    snapshot: () => ({...counts}),
    take: () => { if (!dirty) return null; dirty = false; return {...counts}; },
    logger: {
      debug(value) {
        if (typeof value !== 'string') return;
        // Match only the fixed SDK prefix. Never output or retain the payload suffix.
        const match = /^\[[^\]\r\n]{1,128}\] Dispatch event: t=([A-Z_]+) payload=/.exec(value.slice(0, 220));
        if (!match) return;
        count('dispatch');
        count(match[1] === 'GROUP_AT_MESSAGE_CREATE' ? 'group_at_dispatch'
          : match[1] === 'GROUP_MESSAGE_CREATE' ? 'group_dispatch'
          : lifecycleEvents.has(match[1]) ? 'lifecycle_dispatch' : 'other_dispatch');
      },
      error(value) {
        count('sdk_error');
        if (typeof value === 'string' && /^\[[^\]\r\n]{1,128}\] Message parse error:/.test(value.slice(0, 180))) {
          count('parse_error');
        }
      },
      info() {}, warn() {},
    },
  };
}

export function bindInbound(bot, config, emit, telemetry) {
  const allowed = new Set(config.groups);
  bot.on('ready', data => {
    telemetry.count('ready');
    emit({type: 'ready', app_id: config.appId, data});
  });
  bot.on('resumed', () => {
    telemetry.count('resumed');
    emit({type: 'resumed', app_id: config.appId});
  });
  bot.on('message', (_, msg) => {
    if (msg.kind !== 'group') {
      telemetry.count('other_message_callback');
      if (msg.kind === 'c2c' && msg.rawEventType === 'C2C_MESSAGE_CREATE')
        emit({type: 'event', transport: 'qqbot', app_id: config.appId, event_type: msg.rawEventType, data: msg.raw});
      return;
    }
    telemetry.count('group_callback');
    if (allowed.has(msg.raw?.group_openid)) telemetry.count('group_allowed_callback');
    // Preserve the existing raw envelope and scope validation in Python.
    emit({type: 'event', transport: 'qqbot', app_id: config.appId,
      event_type: msg.rawEventType, data: msg.raw});
  });
  bot.on('rawEvent', ctx => {
    telemetry.count('raw_callback');
    if (lifecycleEvents.has(ctx.eventType)) {
      emit({type: 'lifecycle', app_id: config.appId, event_type: ctx.eventType, data: ctx.data});
    }
  });
}
