import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { DurableOutbox, collectCommitted, MemoryClient, bindMemory } from '../src/index.mjs';

test('recall tool takes its subject only from channel state and preserves mode and limit', async () => {
  const definitions = new Map();
  const agent = { id: 'current-agent', ctx: { tools: { register: definition => {
    definitions.set(definition.name, definition); return () => definitions.delete(definition.name);
  } } } };
  const client = new MemoryClient('http://127.0.0.1:8765', 'x'.repeat(32));
  const actor = {platform:'qq', account_id:'2285027817'};
  client.setSubject(agent.id, actor);
  let called;
  client.recall = async (...args) => { called=args; return {status:'OK',items:[]}; };
  const dispose = bindMemory(agent,client);
  const recall = definitions.get('memory_recall');
  assert.equal(recall.parameters.additionalProperties,false);
  assert.equal(recall.parameters.properties.subject,undefined);
  await recall.execute({query:'称呼',limit:3,mode:'keyword',subject:{platform:'qq',account_id:'other'}},{signal:undefined});
  assert.deepEqual(called,['称呼',3,undefined,{subject:actor,mode:'keyword'}]);
  dispose();
  assert.equal(client.subjects.size,0);
});

function cleanup(root) {
  assert.equal(dirname(resolve(root)), resolve(tmpdir()));
  assert.ok(resolve(root).startsWith(resolve(join(tmpdir(), 'nc-outbox-'))));
  rmSync(root, { recursive: true });
}

test('flush failure never advances the durable consumer cursor', async () => {
  const root = mkdtempSync(join(tmpdir(), 'nc-outbox-'));
  const outbox = new DurableOutbox(join(root, 'outbox.db'));
  const handle = { read: async () => ({ events: [{ seq: 0 }] }), close: async () => {} };
  try {
    await assert.rejects(collectCommitted({ persistence: { open: async () => handle },
      session: { header: { id: 's' }, seq: 1 }, sessions: { flush: async () => { throw new Error('disk full'); } } }, outbox, e => e), /disk full/);
    assert.equal(outbox.cursor('s'), 0);
    assert.equal(outbox.db.prepare('SELECT count(*) AS n FROM queue').get().n, 0);
  } finally { outbox.close(); cleanup(root); }
});

test('outbox survives restart, refuses overflow, and only removes durable acknowledgments', async () => {
  const root = mkdtempSync(join(tmpdir(), 'nc-outbox-'));
  const path = join(root, 'queue.db');
  let queue = new DurableOutbox(path, { maxEntries: 1 });
  try {
    queue.stage('s', 1, [{ source_instance: 'fixture', event_id: 'one', content: 'history' }]);
    assert.throws(() => queue.stage('s', 2, [{ source_instance: 'fixture', event_id: 'two' }]), /FULL/);
    assert.equal(queue.cursor('s'), 1);
    queue.close(); queue = new DurableOutbox(path, { clock: () => 1e15 });
    const waiting = await queue.flush({ request: async () => ({ status: 'RECEIVED_ONLY' }) });
    assert.equal(waiting.pending, 1);
    queue.db.exec('UPDATE queue SET retry_at=0');
    assert.equal((await queue.flush({ request: async () => ({ status: 'DURABLE' }) })).pending, 0);
  } finally { queue.close(); cleanup(root); }
});

test('disconnected cache withholds possibly corrected text and does not claim absence', async () => {
  let offline = false;
  const client = new MemoryClient('http://127.0.0.1:8765', 'x'.repeat(32), { fetcher: async () => {
    if (offline) throw new Error('offline');
    return new Response(JSON.stringify({ status: 'OK', generation: 1, items: [{ content: 'private old text' }] }));
  } });
  assert.equal((await client.recall('name')).items.length, 1);
  offline = true;
  assert.equal((await client.recall('name')).status, 'STALE_UNVERIFIED');
  assert.deepEqual((await client.recall('name')).items, []);
  assert.equal((await client.recall('unknown')).status, 'CACHE_MISS');
});

test('five failed deliveries stop; hash-bound operator retry preserves the exact memory event', async () => {
  const root = mkdtempSync(join(tmpdir(), 'nc-outbox-'));
  let now = 0, calls = 0;
  const queue = new DurableOutbox(join(root, 'queue.db'), { clock: () => now });
  try {
    const event = { source_instance: 'fixture', event_id: 'retry', content: 'unchanged evidence' };
    queue.stage('s', 1, [event]);
    const offline = { request: async () => { calls++; throw new Error('offline'); } };
    for (let attempt = 0; attempt < 6; attempt++) { now += 60000; await queue.flush(offline); }
    assert.equal(calls, 5); assert.equal(queue.status().blocked, 1);
    const [blocked] = queue.blocked();
    assert.equal(blocked.body, undefined);
    assert.throws(() => queue.retry(blocked.id, '0'.repeat(64)), /CONFLICT/);
    queue.retry(blocked.id, blocked.hash);
    assert.equal((await queue.flush({ request: async (_path, body) => { assert.deepEqual(body, event); return { status: 'DURABLE' }; } })).pending, 0);
    assert.equal(queue.cursor('s'), 1);
  } finally { queue.close(); cleanup(root); }
});
