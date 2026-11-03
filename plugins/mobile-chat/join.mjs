#!/usr/bin/env node
import {homedir} from 'node:os';
import {join} from 'node:path';
import {invitedGroup} from './invited.mjs';

try {
  const {MISTY_SERVER:baseUrl,MISTY_INVITE:invite,MISTY_NAME:nickname,MISTY_STATE_DIR} = process.env;
  if (!baseUrl || !invite) throw new Error('set MISTY_SERVER to the full server URL and MISTY_INVITE to the invitation code');
  const client = invitedGroup({baseUrl,invite,nickname,stateDir:MISTY_STATE_DIR || join(homedir(),'.misty-mobile')});
  const profile = await client.login();
  if (!Number.isSafeInteger(profile.start_after)) throw new Error('server does not support invited agents');
  await client.read(profile.start_after);
  console.log(JSON.stringify({connected:true,channel:'mobile:group',member:profile.id,nickname:profile.nickname}));
} catch(error) { console.error(error.message); process.exitCode=1; }
