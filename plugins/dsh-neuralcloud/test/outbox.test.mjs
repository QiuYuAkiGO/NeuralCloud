import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DurableOutbox, collectCommitted, MemoryClient } from '../src/index.mjs';

test('flush failure never advances the durable consumer cursor', async () => {
  const root = mkdtempSync(join(tmpdir(), 'nc-outbox-'));
  const outbox = new DurableOutbox(join(root, 'outbox.db'));
  const handle = { read: async () => ({ events: [{ seq: 0 }] }), close: async () => {} };
  try {
    await assert.rejects(collectCommitted({ persistence: { open: async () => handle },
      session: { header: { id: 's' }, seq: 1 }, sessions: { flush: async () => { throw new Error('disk full'); } } }, outbox, e => e), /disk full/);
    assert.equal(outbox.cursor('s'), 0);
    assert.equal(outbox.db.prepare('SELECT count(*) AS n FROM queue').get().n, 0);
  } finally { outbox.close(); rmSync(root, { recursive: true }); }
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
  } finally { queue.close(); rmSync(root, { recursive: true }); }
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
