# Emotional expression diagnostics

This package implements the isolated diagnostic stage of
`docs/harness/16-emotional-expression-diagnostic-research-plan.md`.
Every artifact records `release_eligible=false`. It has no delivery, production
memory writer, action backend, or automatic repair/deployment path.

## Offline workflow

From the repository root, using a new private output directory:

```powershell
.\.venv\Scripts\python.exe -m eval.emotional_expression_diagnostics preflight --create-spec eval/reports/emotional_expression_diagnostics/private/pilot-spec.json
.\.venv\Scripts\python.exe -m eval.emotional_expression_diagnostics audit --spec eval/reports/emotional_expression_diagnostics/private/pilot-spec.json --out eval/reports/emotional_expression_diagnostics/private/audit
.\.venv\Scripts\python.exe -m eval.emotional_expression_diagnostics pilot --spec eval/reports/emotional_expression_diagnostics/private/pilot-spec.json --out eval/reports/emotional_expression_diagnostics/private/offline-run
.\.venv\Scripts\python.exe -m eval.emotional_expression_diagnostics judge --run eval/reports/emotional_expression_diagnostics/private/offline-run
.\.venv\Scripts\python.exe -m eval.emotional_expression_diagnostics analyze --run eval/reports/emotional_expression_diagnostics/private/offline-run
```

The default cases are **synthetic QQ diagnostic fixtures**, not captured QQ
conversation requests. `A-synthetic-proxy` uses the production homeostatic
renderer and is explicitly not `A-prod`. The six selected scenario families
have two variants each. Five conditions, three repetitions, and independent
B0/B1/C probes plan 180 generation calls plus 36 probes. Repeated outputs do not
increase independent sample size. Identical model-visible contrasts are marked
`unobservable_contrast` and skipped; consequently executed calls can be fewer
than the planned 216. Optional `--extra H2`, `--extra E-style`, and `--extra mode`
add separately counted conditions; they do not alter the main comparisons.

Specs are immutable JSON envelopes with a SHA-256. Changing one requires a new
spec path/run. A confirmation spec must identify its sealed pilot and list pilot
case fingerprints; renamed duplicates are rejected. Independent information
review and a deployment latency budget are necessary for confirmation.

## Actual requests and live execution

Import actual P0 requests using `ActualRequest` inside a `CaseSpec`. Required
fields include the exact UTF-8 HTTP body and its SHA-256, endpoint/request IDs,
evidence reference, verified identity and explicit cloud-replay authorization.
The A body is sent byte-for-byte. Reconstructed compiler output must not be
labeled A-prod. A is a product reference, not the B1−B0 controlled H1 comparison;
the audit reports differences in user text and generation options. Transformed
B/C/D payloads remain `manipulated_pending_http` until actually sent.

No live call occurs unless the frozen spec enables it **and** `pilot --live` is
used. `preflight --live` only prepares/validates configuration; it does not call
a model. Live specs need an exact endpoint/model and dated, sourced input/output
prices in CNY. `pilot --gui-settings data/gui_settings.json` reads the existing
GUI endpoint/model/key, checks identity against the spec, and never writes the
settings. Credentials are never stored in artifacts or printed. In the absence
of that option, the configured `NC_LLM_*` endpoint is used.

All live runs share the default private `deepseek-budget.json` ledger. The hard
budget is 50 CNY; admission stops at 45 CNY including outstanding worst-case
reservations. Each call reserves the full configured input-token envelope plus
its bounded output cap, at the frozen conservative prices. Unknown charges or
interrupted requests retain their reservations. Four independent HTTP requests
at most may run concurrently; reservation and settlement are coordinated
serially. Do not create another ledger to bypass this budget. A stale lock or
unsettled request needs billing reconciliation, not automatic deletion.

Only text chat payloads with known billing fields are accepted. Multiple
completions, ambiguous output caps, tools/audio and unknown payload extensions
are refused. The input envelope covers the complete serialized body. HTTP hooks
and custom auth that could mutate a budgeted request are refused. Missing or
incompatible caps in A-prod cause refusal rather than silently changing the
captured request. No generation retries or validator revisions are performed by
the replay runner; each result says `raw_replay_no_validator`.

The HTTP evidence observer records actual serialized request bytes and visible
content, response identity, finish reason, numeric usage and latency. It excludes
headers, credentials, hidden reasoning, tool arguments and exception text. A
sink failure is an experiment failure. Interrupted calls stay visible through
the durable ledger and request evidence. Non-streaming TTFT is always null.

## Independent ratings and analysis

`judge` seals anonymous `blind-samples.jsonl`; `blind-map.json` is a separate
analysis-only mapping. Models never receive it. Human scoring instructions are
written to `rating-instructions.json`. Import ratings using
`judge --import-ratings <jsonl>`; an original rating cannot be overwritten.

Each human row identifies a sample and rater, declares calibration on non-test
examples, supplies integer 1–5 `scores` for `adaptation`, `recognizability`,
`persona`, and `naturalness`, boolean `violations` for `fact_action`,
`mechanism_leakage`, and `unsupported_physiology`, an `overexpression` boolean,
and evidence references. Two independent calibrated humans are required.
Model ratings do not fill missing human scores.

`judge --cursor-live --max-samples N` invokes the optional Cursor adapter using
separate blind sessions. It also requires fresh proof that on-demand charges are
disabled and existing quota only is used. The adapter verifies each sample's
`review-authorizations.json` entry; a private sample requires its separate
`external_review_authorized` permission. Auxiliary outputs stay in
`aux-ratings.jsonl`. Model availability, identity limits and blocked reviews are
recorded without silently substituting models.

Analysis averages humans, then repetitions within each variant, then variants
within each independent episode. It reports cluster bootstrap 95% intervals,
separate paired sign-randomization p-values and Holm correction. Adaptation is
primary; personality/naturalness noninferiority and negative-control uncertainty
are explicit gates. Zero observed rare errors do not establish zero risk.
Original disagreements and high-risk flags remain available for human review.
Missing human ratings, pilot data, fake data, uncertain HTTP/backend identity,
insufficient sample sizes or unestablished noninferiority produce
`inconclusive`, never a release pass.

Final artifacts include the frozen manifest, visibility audit, request and HTTP
evidence, blind packets, ratings, `analysis.json`, and `report.md`. Private and
redacted artifacts need independent hashes; a redacted hash is not the original
request hash. Production candidates must still pass the original nine-condition
expression suite, fresh-state soak, critical-state zero-call probe and relevant
QQ/GA regressions after being integrated into the real pipeline.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q eval/emotional_expression_diagnostics/test_core.py
```

Tests use deterministic fake clients or `httpx.MockTransport`; no live model is
called.
