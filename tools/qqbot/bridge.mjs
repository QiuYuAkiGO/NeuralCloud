// Private stdio transport. No prompt processing, model calls, arbitrary API paths,
// autonomous sends, shell execution, or console logging of SDK errors/credentials.
import { readFileSync, lstatSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import { QQBot } from '@tencent-connect/qqbot-nodejs';
import { bindInbound, ingressTelemetry } from './ingress.mjs';

const opaque = /^[A-Za-z0-9_-]{1,128}$/;
export function checkedCommand(value, now = Date.now()) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(k => !['id', 'action', 'group_openid', 'user_openid', 'msg_id', 'msg_seq', 'text', 'deadline_ms'].includes(k)) ||
      typeof value.id !== 'string' || !/^[a-f0-9]{32}$/.test(value.id) ||
      value.action !== 'reply' || (Object.hasOwn(value, 'group_openid') === Object.hasOwn(value, 'user_openid')) ||
      typeof (value.group_openid ?? value.user_openid) !== 'string' || !opaque.test(value.group_openid ?? value.user_openid) ||
      typeof value.msg_id !== 'string' || !value.msg_id || Buffer.byteLength(value.msg_id) > 512 ||
      /[\x00-\x20\x7f]/.test(value.msg_id) || !Number.isInteger(value.msg_seq) || value.msg_seq < 1 || value.msg_seq > 5 ||
      typeof value.text !== 'string' || !value.text.trim() || value.text.length > 3000 ||
      !Number.isFinite(value.deadline_ms) || value.deadline_ms <= now || value.deadline_ms > now + 60000) {
    throw new Error('qqbot_command_invalid');
  }
  return value;
}

export async function replyOnce(bot, credentials, command, allowed, used, now = Date.now) {
  const value = checkedCommand(command, now());
  if (!(value.user_openid !== undefined ? new Set(credentials.users ?? []).has(value.user_openid) : allowed.has(value.group_openid)) || used.has(value.id) || used.size >= 65536) throw new Error('qqbot_scope_or_duplicate');
  used.add(value.id); // A failure/timeout never permits a second mutation.
  const token = await bot.tokenManager.getAccessToken(credentials.appId, credentials.appSecret);
  const remaining = value.deadline_ms - now();
  if (remaining <= 0) throw new Error('qqbot_command_expired');
  return bot.apiClient.request(token, 'POST', (value.user_openid !== undefined ? `/v2/users/${value.user_openid}/messages` : `/v2/groups/${value.group_openid}/messages`), {
    content: value.text, msg_type: 0, msg_id: value.msg_id, msg_seq: value.msg_seq,
  }, {timeoutMs: Math.min(10000, remaining)});
}

export async function main(filename) {
  const stat = lstatSync(filename);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 65536 ||
      (process.platform !== 'win32' && (stat.mode & 0o077))) throw new Error('qqbot_private_config_invalid');
  const config = JSON.parse(readFileSync(filename, 'utf8'));
  if (!config || !['appId,appSecret,groups', 'appId,appSecret,groups,users'].includes(Object.keys(config).sort().join()) ||
      typeof config.appId !== 'string' || !/^[1-9][0-9]{0,19}$/.test(config.appId) || typeof config.appSecret !== 'string' ||
      config.appSecret.length < 16 || config.appSecret.length > 4096 || !Array.isArray(config.groups) || config.groups.length > 16 ||
      new Set(config.groups).size !== config.groups.length ||
      config.groups.some(x => typeof x !== 'string' || !opaque.test(x)) ||
      !Array.isArray(config.users ?? []) || (config.users ?? []).length > 16 ||
      new Set(config.users ?? []).size !== (config.users ?? []).length ||
      (config.users ?? []).some(x => typeof x !== 'string' || !opaque.test(x))) throw new Error('qqbot_config_invalid');
  const allowed = new Set(config.groups);
  const used = new Set();
  const emit = value => {
    const line = JSON.stringify(value) + '\n';
    if (Buffer.byteLength(line) > 2 * 1024 * 1024 || process.stdout.writableLength > 4 * 1024 * 1024) {
      process.exitCode = 1; process.exit(1);
    }
    process.stdout.write(line);
  };
  const telemetry = ingressTelemetry();
  const bot = new QQBot({appId: config.appId, appSecret: config.appSecret,
    intents: 1 << 25, markdownSupport: false, tokenPrefetch: 'sync', logger: telemetry.logger});
  bindInbound(bot, config, emit, telemetry);
  bot.on('disconnected', () => emit({type: 'disconnected', code: 'qqbot_gateway_disconnected'}));
  bot.on('error', () => emit({type: 'error', code: 'qqbot_gateway_error'}));
  const publish = () => {
    const counts = telemetry.take();
    if (counts) emit({type: 'telemetry', app_id: config.appId, counts});
  };
  const sampling = setInterval(publish, 2000);
  const input = createInterface({input: process.stdin, crlfDelay: Infinity});
  let stopping = false;
  const stop = async () => { if (!stopping) { stopping = true; clearInterval(sampling); publish(); await bot.stop(); process.exit(0); } };
  input.on('close', () => void stop());
  process.on('SIGTERM', () => void stop());
  process.on('SIGINT', () => void stop());
  let chain = Promise.resolve();
  let queued = 0;
  input.on('line', line => {
    if (Buffer.byteLength(line) > 65536 || ++queued > 16) {
      emit({type: 'error', code: 'qqbot_command_overflow'});
      void stop();
      return;
    }
    chain = chain.then(async () => {
      let command;
      try {
        if (Buffer.byteLength(line) > 65536) throw new Error('qqbot_command_invalid');
        command = checkedCommand(JSON.parse(line));
        const result = await replyOnce(bot, config, command, allowed, used);
        emit({type: 'result', id: command.id, ok: true, result});
      } catch {
        emit({type: 'result', id: /^[a-f0-9]{32}$/.test(command?.id) ? command.id : null,
          ok: false, code: 'qqbot_send_unconfirmed'});
      } finally {
        queued--;
      }
    });
  });
  try {
    await bot.start();
  } catch {
    emit({type: 'error', code: 'qqbot_start_failed'});
    await stop();
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  // Never allow an inherited development setting to disable TLS verification.
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = '1';
  main(process.argv[2]).catch(() => {
    process.stdout.write(JSON.stringify({type: 'error', code: 'qqbot_start_failed'}) + '\n');
    process.exit(1);
  });
}
