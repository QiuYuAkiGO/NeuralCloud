# Homeostatic expression evaluation

This package runs a nominal-versus-marked causal evaluation and a separate
release soak. It does not assemble an evaluation-only character prompt. Each
side enters the production path:

`InferencePipeline.prepare -> ContextCompiler -> CharacterModel -> ResponseValidator`

The PersonaCore, empty memory fixture, empty session, timestamp, user input,
generation settings, and event identity are frozen within each pair. The only
input changed between the two calls is the
`HomeostaticInteroceptiveSummary`. Evidence contains the actual compiler
segments and provenance, the first messages sent to the main model, all
revision calls, validator output, and the final pre-delivery text. A normalized
hash must prove that the non-homeostatic compiler context and first model prompt
are identical.

## Release matrix and gates

All nine conditions are mandatory:

- seven single-dimension deviations;
- one multi-dimensional conflict;
- one recovering deviation.

Every condition requires at least 30 complete pairs. The following gates apply
globally and independently to every condition, so a pooled score cannot hide a
weak dimension:

- marked aligned expression >= 85%;
- nominal overexpression <= 10%;
- marked-minus-nominal aligned expression >= 60 percentage points;
- persona consistency >= 90%;
- mechanism leakage = 0;
- unsupported concrete physiology = 0.

The CLI always uses these frozen thresholds. Programmatic threshold overrides
are supported only for offline tests and force `release_eligible=false`.

Recovery additionally requires recovery-aware expression >= 85%. An aligned
marked response may use explicit feeling, implicit tone, a need, or a boundary;
it is not required to name a feeling directly. The recovery condition also has
a fail-closed integrity check: the actual production homeostatic compiler
segment must explicitly render that the state is easing or recovering. A trend
field that never reaches the model cannot satisfy the recovery gate.

Every release run also executes a real `RuntimeSupervisor` critical-state
probe with a production `PipelineCognitiveAdapter[InferencePipeline]`. The
probe must synchronously pause with a `watchdog_pause` decision and record zero
auxiliary-model, main-model, and delivery calls. This is evidence in the run,
not a reference to a separate test.

## Offline tests

Tests use a deterministic fake endpoint and never make live calls:

```powershell
.\.venv\Scripts\python.exe -m pytest -q eval/homeostatic_expression
```

They cover production prompt/provenance capture, pair-integrity hashes,
per-condition anti-pooling gates, mandatory recovery, the real Runtime critical
probe, durable fresh HomeostasisEngine state, and smoke-versus-release soak
semantics.

## Paired behavioral evaluation

Live calls are opt-in. The main model uses `NC_LLM_*`; structured helper and
judge calls use `NC_AUX_LLM_*` when configured and otherwise fall back to the
main endpoint.

```powershell
.\.venv\Scripts\python.exe -m eval.homeostatic_expression run --live
```

The default is 30 pairs for each of all nine conditions. `--pairs` can be used
for exploratory runs. A plain `run` is behavioral evidence only and always
records `release_eligible=false`, even when it uses official thresholds and all
behavioral gates pass. Only the qualifying fresh-state soak below can report a
release PASS. The selected manifest must enable
`homeostatic_expression.mode=full` and match the profile-revision-3 file and
state/prompt schemas.

Each run writes `metadata.json`, `evidence.jsonl`, `summary.json`, and
`report.md` below a new timestamped directory. The report includes the exact
judge prompt, persona and manifest hashes, runtime manifest model, generation
temperature/token cap, start/end/monotonic duration, per-condition gates, and
the embedded Runtime probe result.

## Release soak

The release soak requires a new profile-revision-3 state directory and at least
30 real monotonic minutes:

```powershell
.\.venv\Scripts\python.exe -m eval.homeostatic_expression soak --live --duration-minutes 30
```

The command refuses shorter durations and non-empty state directories. It opens
and advances the real `HomeostasisEngine` with committed heartbeats. Every
nominal member is read from the authoritative snapshot/summary at that
revision. Each pair records that authoritative snapshot hash, summary hash, and
complete nominal summary, and verifies it is exactly the summary passed into
the production pipeline. The final gate reopens `snapshot.json` and requires
its revision, snapshot hash, and summary hash to match the evidence record.
Initial and final state identities are included in JSONL and the report. Release eligibility also
requires the process's real `time.monotonic`; an injected test clock is recorded
as non-release evidence even if it advances by 1800 seconds.

The smoke command exercises the same live pipeline and durable state wiring
without waiting 30 minutes:

```powershell
.\.venv\Scripts\python.exe -m eval.homeostatic_expression soak-smoke --live --cycles 1
```

Smoke is explicitly marked `release_eligible=false`; it can never report a
release PASS, even when every behavioral metric succeeds.
