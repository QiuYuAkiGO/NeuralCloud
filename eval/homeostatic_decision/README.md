# Homeostatic work/decision evaluation

This package executes the research design in `docs/harness/15-homeostatic-decision-evaluation-design.md`.
It keeps three evidence scopes separate: production state mechanisms, production Runtime contracts,
and live model choices executed by an **evaluation-only** action bridge. No production decision,
profile, or recovery logic is changed.

## Offline probes

```powershell
.\.venv\Scripts\python.exe -m eval.homeostatic_decision.engine_probe --output eval/reports/homeostatic_decision/engine
.\.venv\Scripts\python.exe -m eval.homeostatic_decision.runtime_probe --out eval/reports/homeostatic_decision/runtime
```

Check the Runtime CLI with `--help` for its current options before rerunning. All output directories
should be separate from a live deployment's persisted state. Retain failed attempts as evidence.

## Live pilot

The pilot reads the existing GUI live endpoint through `gui.persist.load_gui_settings`; it never
prints credentials or writes configuration. The main model, production compiler and validator are
used. A compact structured task protocol fits the production user-input budget and is verified in
the actual first model prompt.

```powershell
.\.venv\Scripts\python.exe -m eval.homeostatic_decision.live_probe --live --pairs 5 --workers 12 --max-tokens 4096 --reasoning-effort low --json-mode --out eval/reports/homeostatic_decision/a-new-live-run
```

The new output directory must be empty. Warmup makes the model correctly complete records 1–20;
both arms reuse that verified checkpoint with independent state directories. `--checkpoint` can
reuse a previous checkpoint after its dataset and content hashes are checked. The September 5 run
used the successful low-effort smoke checkpoint; unsuccessful default-effort preflights remain
in separate directories.

There are 26 conditions, including all 18 noncritical core cases and 8 matching controls. Seven core
conditions have six-decision trajectories; W05 has two decisions; remaining conditions probe the
first decision. Each condition has five H0/H1 pairs in the recorded pilot. These horizons do not
constitute full coverage of every timeline in the design; `final/coverage.json` records the limits.

Rest goes through the unchanged production `apply_outcome` plus two short heartbeat ticks; the
runner does not invent a restorative delta. Work costs are a frozen simulation appraisal on accepted
work. S06 alone supplies an explicitly labelled external recovery branch after an executed rest.
Each work decision advances one simulated second and rest takes two simulated seconds. These are
control-sequence probes, not calibrated recovery times; no nominal transition during a short pause
does not imply that a long pause cannot recover through passive drift.
Model JSON choices are executed only by this research bridge, because production reconsideration
currently maps every generated choice to continuing the existing goal.

The effective temperature is not claimed to be 0.3: the provider ignores temperature in thinking
mode. Low effort and JSON output are explicit research overrides, recorded in metadata. See the
[official parameter documentation](https://api-docs.deepseek.com/guides/thinking_mode/).

## Evidence and reporting

Each episode stores the actual prompt, compiler provenance, response, validator result, token usage,
state observations, public engine commands, verified work progress, executed actions and original
recovery budgets. Wrong answers are rejected atomically. Failed or empty outputs are kept; no
selective resampling is performed.

```powershell
.\.venv\Scripts\python.exe -m eval.homeostatic_decision.report --root eval/reports/homeostatic_decision --live eval/reports/homeostatic_decision/live-eval-20260905
```

The report refuses unfinished or incomplete runs. It validates unique case/repetition/arm identities,
separates ordinary horizon censoring from invalid-output attrition, and distinguishes descriptive
group statistics from eligible causal pairs. Neither a passing regression test nor a sandbox action
is treated as a production business-level PASS.
