# Implementation checkpoints

Baseline: g0-20261024T182731Z. Work only in isolated implementation branches.

1. G0 — verified encrypted source/config/dirty snapshots; source commits recorded in baseline manifest.
2. P0 — pin Harness and Tencent QQ packages; run actual package contracts with synthetic model/input; collect existing production startup observations without restart.
3. P1 — independent WAL persistence, versioned contracts, bounded retrieval, durable idempotency, archive management with fail-closed deletion gates and synthetic restore tests.
4. P2 — authenticated resident memory API, thin Harness integration, context revisions and reconnect behavior.
5. P3/P4 — one character state owner, bounded drift/feedback, durable drive/activity/approval ledgers; no secondary model calls by default; explicit delivery only.
6. P5 — isolated integration evidence first; real QQ and media acceptance require controlled authenticated destination and functioning content path.
7. P6 — remain blocked until consistent production recovery point, independent recovery copy and all rollout gates are verified. Do not stop old writers or start a second normal character before then.

Budgets: tests use temporary synthetic data; no production model calls, test messages, database writes or restarts. Package installation stays inside maintenance worktrees. Each network operation has a bounded timeout; retry at most twice. Synthetic archive batches are bounded by record and byte limits. Cold production eviction remains disabled.

On interruption: read progress.json, inspect both Git states, reuse committed checkpoints and verified archives. Never replay an unknown external effect. Source snapshots are not a consistent live business-data backup.
