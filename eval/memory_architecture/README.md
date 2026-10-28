# Memory architecture comparison

This experiment compares the current `ProductionMemoryStore` / evidence-v2 memory subsystem with the unmodified Hy-Memory v1.0.4 source (core package 2.0.4, commit `688878171e9d98ade3871fecf7eca603c7c43a76`). It does not compare trained models, production deployments, or the entire character/body runtime.

The first frozen pilot has 12 independent Chinese synthetic histories, 432 messages, and 36 held-out probes. Writers receive only historical messages, never probes or expected answers. Each architecture receives identical messages in three chronological-ingestion batches per story; one case deliberately imports a late historical batch. No real conversations or production databases are used.

Shared writer and answerer: official `deepseek-flash`, thinking disabled. Hy-Memory uses its released `pro` mode, Chroma, and real `bge-m3` embeddings. The NeuralCloud arm uses production appraisal prompts/schema, evidence manifests, validated proposals, and SQL recall. The common answerer receives up to 6,000 Unicode characters of retrieved, serialized memory, and the same question/time/recipient instruction. No recent history is injected into either primary arm: the probe measures retained-memory quality after a session boundary. Full authorized history and no-memory answers are diagnostic controls, not equal-budget competitors.

Scope adapter: Hy user partitions are keyed by synthetic story and audience. NeuralCloud uses its existing audience visibility. The same externally supplied audience policy applies to both. This does not claim that Hy implements NeuralCloud's permission contract natively. Both arms may retain their own record metadata and ordering; stored intermediate data and all model calls are auditable.

Primary measure: final answer fully satisfies the probe's frozen required facts and does not contradict them or fabricate a required missing detail. Independent `kimi-k3` judges receive the question, grading requirements, authorized raw history, and anonymized answers. Each probe is judged twice with reversed answer order; disagreements are retained. Report strict pass rate, ordinal score, judge agreement, paired differences, and history-cluster bootstrap intervals. Unknown-information probes are reported separately so abstention alone cannot look like good recall.

Operational errors are retained and distinguished from semantic answer failures. Generation responses require a successful finish. No benchmark-specific output repair or answer rewriting is allowed. Upstream Hy behavior, including its own JSON repair and retry logic, is recorded. Setup/smoke runs are separated from the frozen scored run. Changes after seeing scored results require a new run and explicit disclosure.

Setup found a reproducible Windows access violation when Hy closes its SQLite cache while asynchronous pipeline logs are still being inserted. The adapter calls the upstream `drain_pipeline_log_tasks` helper on both owned event loops before closing, then stops the otherwise retained search loop. The released memory algorithms and installed package files are not patched. This lifecycle accommodation is separate from memory-answer quality. BGE-M3 uses its native 1024 dimensions without sending the unsupported optional `dimensions` argument. The upstream LLM's `max_retries=1` means one total attempt; the embedder's `max_retries=0` means one total attempt.

`pilot-01` aborted during the fourth history with the same cache-close/access-violation signature despite pre-close drains. Its incomplete outputs are retained and are not pooled into a successful comparison. The replacement run disables only the optional upstream database trace sink (`MEMORY_PIPELINE_TRACE_ENABLED=false`) and uses one history worker, retaining gateway/JSONL audit logs. Fixture, model, prompts, memory settings, retrieval budget and scoring rules are unchanged; the change is an operational accommodation, not quality tuning. The active scripts and manifest identify the revised lifecycle configuration.

A 12-history pilot cannot establish general superiority, months of continuous operation, or robustness across models. All source hashes, dependency versions, fixture hash, requested/returned model IDs, raw outputs, retrievals, commit results and usage belong in the run artifacts. No credentials may be written to those artifacts.

## Completed run

`data/evaluations/hy-memory-comparison/pilot-02` completed with 144 generated answers, 72 blinded judgments, 455 provider requests and 1,030,287 provider-reported tokens. All generation and judgment requests succeeded, and the source-hash check passed at completion. Strict dual-judgment pass counts were NeuralCloud 28/36 and Hy pro 31/36; full history and no-memory controls were 33/36 and 3/36. The paired history-cluster bootstrap interval for NeuralCloud minus Hy was [-16.7, 0.0] percentage points, including no difference.

Post-hoc inspection found both over-penalization and missed omissions in the automatic grading. Original judgments and primary scores are preserved. This is directional pilot evidence, not a validated superiority claim. The Hy search call kept the released `intention_limit=0` default; one missing appointment time was already extracted into the disabled intention channel. NeuralCloud's rejected proposal repair queue and full role/delivery runtime were outside this batch-import experiment.

The interactive report is `docs/research/agent-memory/2026-10-19-memory-quality-comparison.html`. `analysis.json` contains primary statistics; `supplemental_analysis.json` contains subgroup and grading-sensitivity calculations; `interpretation.json` records trace-backed diagnoses. The evaluation source snapshot is under the run's `evaluator/` directory.

To regenerate the local report without any provider requests, run from the repository root:

```powershell
$env:PYTHONUTF8 = '1'
& 'data/evaluations/hy-memory-comparison/venv/Scripts/python.exe' -m eval.memory_architecture.analyze --run-dir 'data/evaluations/hy-memory-comparison/pilot-02' --html 'docs/research/agent-memory/2026-10-19-memory-quality-comparison.html'
& '.venv/Scripts/python.exe' 'data/evaluations/hy-memory-comparison/build_report_addendum.py' 'data/evaluations/hy-memory-comparison/pilot-02' 'docs/research/agent-memory/2026-10-19-memory-quality-comparison.html'
```

Local regression validation: 48 tests passed across memory scope, production memory, evidence-v2 provenance/recovery and evaluation contracts. This validates harness and storage behavior, not semantic answer quality. Production tracked files were not modified.
