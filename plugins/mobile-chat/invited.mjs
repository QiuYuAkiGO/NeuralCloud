import {createHash, randomUUID} from 'node:crypto';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

export function invitedGroup({baseUrl, invite, nickname = 'AI 成员', stateDir}) {
  if (!invite || !stateDir) throw new Error('invite and stateDir required');
  const base = new URL(baseUrl);
  if (base.protocol !== 'https:' && !(base.protocol === 'http:' && ['localhost','127.0.0.1','[::1]'].includes(base.hostname)))
    throw new Error('use the HTTPS server URL (including its path)');
  if (base.search || base.hash || base.username || base.password) throw new Error('invalid server URL');
  const endpoint = base.href.replace(/\/$/, '') + '/v1/group/';
  mkdirSync(stateDir, {recursive:true,mode:0o700});
  const identity = join(stateDir, 'member-'+createHash('sha256').update(endpoint).digest('hex').slice(0,20));
  try { writeFileSync(identity,randomUUID(),{flag:'wx',mode:0o600}); }
  catch (err) { if (err.code !== 'EEXIST') throw err; }
  const token = readFileSync(identity,'utf8');
  if (token.length !== 36) throw new Error('invalid local member identity');
  async function call(route,body) {
    const result = await fetch(endpoint+route,{method:body?'POST':'GET',redirect:'error',signal:AbortSignal.timeout(10000),
      headers:{'Content-Type':'application/json','X-Member':token},body:body && JSON.stringify(body)});
    if (!result.ok) throw new Error('group '+route.split('?')[0]+' HTTP '+result.status);
    return result.json();
  }
  return {
    login: () => call('profile',{nickname,invite,agent:true}),
    read: after => call('messages?after='+after),
    send: (text,request_id) => call('messages',{text,request_id}),
  };
}
