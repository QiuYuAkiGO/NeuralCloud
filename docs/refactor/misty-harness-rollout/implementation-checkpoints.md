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

后续已完成的人格、旧状态与归档维护隔离检查点见 [05-persona-state-archive.md](05-persona-state-archive.md)。生产切换和本地淘汰仍受门禁约束。

## 2026-10-25 生产准备检查点

固定发布 `resident-20261025-1656288`；两个仓库 CI 通过，83 项每平台验证与真实 QQ / 字幕 / 手机 / 审批通过。旧 writer 已停止，新正式角色未启动，等待 G: 全量独立恢复验证。详见 [06-production-cutover.md](06-production-cutover.md)。
