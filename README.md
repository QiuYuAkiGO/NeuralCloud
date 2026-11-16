# NeuralCloud resident memory

This branch includes the running memory-service source recovered on 2026-11-16. Memory, evidence, semantic indexing, state and Harness services were verified active. See [source provenance and validation limits](docs/refactor/production-source-sync-20261116.md). This recovery does not deploy changes or authorize cold eviction.

The v1 service uses SQLite WAL, explicit initialization, OS-held single-instance ownership, bounded Chinese text retrieval, durable idempotent ingestion, revision-aware corrections and dependency invalidation. The ordinary API cannot open archives, restore cold history, or execute tools. The recovered implementation also contains semantic retrieval, evidence indexing and memory workflows; these require their explicitly configured services and optional dependencies.

```sh
uv sync --locked --extra dev --extra archive
uv run neuralcloud-memory init --database /isolated/misty/neuralcloud/memory.sqlite3 --character synthetic-misty
uv run neuralcloud-memory serve --database /isolated/misty/neuralcloud/memory.sqlite3 --character synthetic-misty --token-file /isolated/private/memory.token
uv run pytest -q
npm --prefix plugins/dsh-neuralcloud test
```

Create the token file privately with a random credential of at least 32 characters. The server binds only to 127.0.0.1. Never initialize over an existing database; unsupported formats require explicit maintenance.

The management-only archive library implements bounded encrypted export, hash/reference checks, signed destination restoration receipts, independent recovery-domain checks, and revision-guarded online detachment. Local eviction is intentionally blocked until the deployment's complete copy inventory and recovery gates are accepted. I: is maintenance staging; H: and I: are the same physical disk.

`python -m neuralcloud.maintenance --help` provides explicit, bounded export jobs, capacity reporting and audited retries in an isolated maintenance root. `TemporaryViews` adds signed scoped reads with expiry, encrypted temporary copies and restart revocation; it is absent from role routes. The [persona/state and archive checkpoint](docs/refactor/misty-harness-rollout/05-persona-state-archive.md) records limits, commands, real frozen-input rehearsal and the remaining production gates.

The thin Harness integration provides scoped memory tools, a bounded persistent outbox, actual SessionStore durability barriers and conservative disconnected-cache behavior. Install it only for trusted Misty handles. It does not install a replacement agent loop or grant every development agent global memory.

- [Implementation plan](docs/refactor/misty-harness-refactor-20261025.md)
- [Verified baseline](docs/refactor/misty-harness-rollout/00-remote-progress-sync.md)
- [Execution status](docs/refactor/misty-harness-rollout/progress.json)
- [Checkpoints](docs/refactor/misty-harness-rollout/implementation-checkpoints.md)
- [Isolated implementation and validation](docs/refactor/misty-harness-rollout/03-isolated-implementation.md)

Explicit legacy import is available through `python -m neuralcloud.migration --help`. Owner contracts are generated with `python tools/export_resident_contracts.py` and checked with `--check` (requires the archive extra). The migration and archive management modules never run during ordinary service startup.

Legacy GUI, inference, QQ, body and voice integrations remain recoverable in the source history but are excluded from the new default wheel and test path. Their production services have not been removed or restarted by this refactor.
