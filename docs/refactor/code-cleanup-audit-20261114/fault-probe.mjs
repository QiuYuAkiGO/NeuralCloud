// Synthetic baseline probe: no network requests, production data or external actions.
// Run from the repository: node docs/refactor/code-cleanup-audit-20261114/fault-probe.mjs
import { MemoryClient, DurableOutbox } from '../../../plugins/dsh-neuralcloud/src/index.mjs';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const outcomes = {};
for (const [name, fetcher] of Object.entries({
  invalid_json: async () => new Response('not json'),
  unauthorized: async () => new Response('{}', { status: 401 }),
  invalid_shape: async () => new Response('{}'),
})) {
  const client = new MemoryClient('http://127.0.0.1:8765', 'x'.repeat(32), { fetcher });
  try { outcomes[name] = await client.recall('synthetic'); }
  catch (error) { outcomes[name] = { error: error.message }; }
}
const root = mkdtempSync(join(tmpdir(), 'nc-cleanup-audit-'));
const queue = new DurableOutbox(join(root, 'queue.db'));
try {
  queue.stage('s', 1, [{ source_instance: 'audit', event_id: '1' }]);
  await queue.flush({ request: async () => { throw new Error('SERVICE_401'); } });
  outcomes.outbox_401 = { ...queue.status(), attempts: queue.db.prepare('SELECT attempts FROM queue').get().attempts };
} finally {
  queue.close();
  rmSync(root, { recursive: true });
}
console.log(JSON.stringify(outcomes, null, 2));
