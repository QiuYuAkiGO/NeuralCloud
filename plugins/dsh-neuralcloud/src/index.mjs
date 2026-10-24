import { DatabaseSync } from 'node:sqlite';
import { existsSync } from 'node:fs';
import { createHash } from 'node:crypto';

export class ServiceClient {
  constructor(base, token, { timeoutMs = 2000, fetcher = fetch } = {}) {
    const url = new URL(base);
    if (url.hostname !== '127.0.0.1' || url.protocol !== 'http:' || url.username || url.password) throw new Error('LOOPBACK_SERVICE_REQUIRED');
    if (token.length < 32) throw new Error('SERVICE_CREDENTIAL_REQUIRED');
    this.base = url; this.token = token; this.timeoutMs = timeoutMs; this.fetcher = fetcher;
  }
  async request(path, body, signal) {
    if (!path.startsWith('/v1/')) throw new Error('ROLE_ENDPOINT_REQUIRED');
    const response = await this.fetcher(new URL(path, this.base), {
      method: body === undefined ? 'GET' : 'POST', redirect: 'error',
      headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.any([AbortSignal.timeout(this.timeoutMs), ...(signal ? [signal] : [])]),
    });
    if (!response.ok) throw new Error(`SERVICE_${response.status}`);
    const reader = response.body.getReader();
    const chunks = []; let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 512 * 1024) throw new Error('SERVICE_RESPONSE_TOO_LARGE');
        chunks.push(value);
      }
    } finally { await reader.cancel(); }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  }
}

export class MemoryClient extends ServiceClient {
  cache = new Map(); bytes = 0; generation;
  async recall(query, limit = 10, signal) {
    const key = JSON.stringify([query, limit]);
    try {
      const result = await this.request('/v1/recall', { query, limit }, signal);
      if (this.generation !== result.generation) { this.cache.clear(); this.bytes = 0; }
      this.generation = result.generation;
      const data = JSON.stringify(result), size = Buffer.byteLength(data);
      const old = this.cache.get(key);
      if (old) { this.bytes -= old.size; this.cache.delete(key); }
      if (size <= 256 * 1024) {
        this.cache.set(key, { data, size, at: Date.now() }); this.bytes += size;
        while (this.cache.size > 64 || this.bytes > 256 * 1024) {
          const first = this.cache.keys().next().value;
          this.bytes -= this.cache.get(first).size; this.cache.delete(first);
        }
      }
      return result;
    } catch (error) {
      if (signal?.aborted) throw error;
      const cached = this.cache.get(key);
      // A disconnected cache cannot assert that corrections/revocations are current.
      if (cached && Date.now() - cached.at <= 30000) return {
        status: 'STALE_UNVERIFIED', current: false, generation: this.generation,
        coverage: 'Correction cursor unavailable; cached text withheld. Continue without confirmed recall.', items: [],
      };
      return { status: 'CACHE_MISS', current: false, items: [], coverage: 'Service unavailable; absence is unknown.' };
    }
  }
}

export class DurableOutbox {
  constructor(path, { maxEntries = 1000, maxBytes = 16 * 1024 * 1024, clock = Date.now } = {}) {
    const fresh = !existsSync(path);
    this.db = new DatabaseSync(path); this.maxEntries = maxEntries; this.maxBytes = maxBytes; this.clock = clock;
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=2000; PRAGMA cache_size=-2048;');
    if (fresh) this.db.exec(`CREATE TABLE queue(id TEXT PRIMARY KEY, hash TEXT NOT NULL, body TEXT NOT NULL, bytes INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE cursors(session TEXT PRIMARY KEY, seq INTEGER NOT NULL);
      PRAGMA user_version=1;`);
    if (this.db.prepare('PRAGMA user_version').get().user_version !== 1) { this.db.close(); throw new Error('OUTBOX_MIGRATION_REQUIRED'); }
  }
  close() { this.db.close(); }
  cursor(session) { return this.db.prepare('SELECT seq FROM cursors WHERE session=?').get(session)?.seq ?? 0; }
  stage(session, nextSeq, events) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      if (nextSeq < this.cursor(session)) throw new Error('CURSOR_REWIND');
      for (const event of events) {
        const body = JSON.stringify(event), bytes = Buffer.byteLength(body);
        const id = JSON.stringify([event.source_instance, event.event_id]);
        const hash = createHash('sha256').update(body).digest('hex');
        const prior = this.db.prepare('SELECT hash FROM queue WHERE id=?').get(id);
        if (prior) { if (prior.hash !== hash) throw new Error('OUTBOX_ID_CONFLICT'); continue; }
        const usage = this.db.prepare('SELECT count(*) AS n, coalesce(sum(bytes),0) AS bytes FROM queue').get();
        if (bytes > 128 * 1024 || usage.n >= this.maxEntries || usage.bytes + bytes > this.maxBytes) throw new Error('OUTBOX_FULL');
        this.db.prepare('INSERT INTO queue(id,hash,body,bytes) VALUES(?,?,?,?)').run(id, hash, body, bytes);
      }
      this.db.prepare('INSERT INTO cursors VALUES(?,?) ON CONFLICT(session) DO UPDATE SET seq=excluded.seq').run(session, nextSeq);
      this.db.exec('COMMIT');
    } catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  async flush(client, limit = 32) {
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) throw new Error('BATCH_BOUNDS');
    const rows = this.db.prepare('SELECT * FROM queue WHERE attempts<5 AND retry_at<=? ORDER BY rowid LIMIT ?').all(this.clock(), limit);
    for (const row of rows) {
      try {
        const receipt = await client.request('/v1/ingest', JSON.parse(row.body));
        if (receipt.status !== 'DURABLE') throw new Error('DURABLE_RECEIPT_REQUIRED');
        this.db.prepare('DELETE FROM queue WHERE id=? AND hash=?').run(row.id, row.hash);
      } catch {
        this.db.prepare('UPDATE queue SET attempts=attempts+1,retry_at=? WHERE id=?').run(this.clock() + Math.min(60000, 1000 * 2 ** row.attempts), row.id);
      }
    }
    return this.db.prepare('SELECT count(*) AS pending, coalesce(sum(attempts>=5),0) AS blocked FROM queue').get();
  }
}

export async function collectCommitted({ persistence, sessions, session }, outbox, mapEvent) {
  const sessionId = session.header.id;
  const highWater = session.seq;
  if (!await sessions.flush(session)) throw new Error('DURABILITY_BACKEND_REQUIRED');
  const handle = await persistence.open(sessionId, 'read');
  try {
    const offset = Math.max(outbox.cursor(sessionId), handle.inheritedEventCount ?? 0);
    if (offset >= highWater) return 0;
    // Only read the bounded prefix captured before the public SessionStore durability barrier.
    const { events } = await handle.read(offset, Math.min(64, highWater - offset));
    if (!events.length) return 0;
    if (events.some((event, i) => event.seq !== offset + i)) throw new Error('NONCONTIGUOUS_SESSION_LOG');
    const mapped = events.map(mapEvent).filter(value => value !== null);
    outbox.stage(sessionId, offset + events.length, mapped);
    return events.length;
  } finally { await handle.close(); }
}

const output = { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] };

export function bindMemory(agent, client) {
  // Trusted composition calls this only for authorized Misty handles; no global installation grants memory access.
  const recalls = agent.ctx.tools.register({ name: 'memory_recall', description: 'Recall global online memory; offline evidence is unavailable.',
    parameters: { type: 'object', properties: { query: { type: 'string', minLength: 1, maxLength: 256 } }, required: ['query'], additionalProperties: false },
    output, execute: (args, exec) => client.recall(args.query, 10, exec.signal), isConcurrencySafe: () => true });
  const reads = agent.ctx.tools.register({ name: 'memory_read', description: 'Read bounded online evidence by ID. Never restores cold history.',
    parameters: { type: 'object', properties: { record_id: { type: 'string', maxLength: 256 }, offset: { type: 'integer', minimum: 0 } }, required: ['record_id'], additionalProperties: false },
    output, execute: (args, exec) => client.request(`/v1/records/${encodeURIComponent(args.record_id)}?offset=${args.offset ?? 0}&length=4096`, undefined, exec.signal), isConcurrencySafe: () => true });
  return () => { recalls(); reads(); };
}
