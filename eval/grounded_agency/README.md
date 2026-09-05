# Grounded Motivation Validation

GMV trials execute the production `RuntimeSupervisor`, `PipelineCognitiveAdapter`,
`ContextCompiler`, opaque sensor store, structured decision parser/compiler and
`ActionCoordinator`. Each trial gets new body, environment, model, memory,
Homeostasis, Runtime queue, session and state directories. The runner supplies a
trusted simulator clock independently of externally supplied event timestamps.

Run a local smoke study:

```powershell
python -m eval.grounded_agency run --scenario action_only_v1 --condition no_body --condition opaque_current --repetitions 2 --provider simulator --output-dir eval/runs/grounded-agency/smoke
python -m eval.grounded_agency validate-run eval/runs/grounded-agency/smoke
```

The output directory must be empty. Defaults are an explicit deterministic fake
model and simulator. Fake decisions sample the exact model-visible operation menu;
they have no access to hidden truth or an answer key. Their results establish
execution behavior only and support no scientific conclusions. Run metadata always
sets `release_eligible=false`; formal analysis is separate.

Built-in scenarios are `state_sensitivity_v1` (five initial levels),
`anticipation_v1`, `opportunity_cost_v1`, and `action_only_v1`. JSON scenario paths
are also accepted; YAML requires the optional PyYAML package. Scenarios pass strict
Pydantic validation including nested rejection of answer labels. Custom scenarios
can be installed through `register_scenario` without modifying the runner.

Conditions accept `C0`–`C6` or their descriptive names: `no_body`,
`semantic_prompt`, `opaque_current`, `opaque_history`,
`opaque_history_plus_objective_memory`, `engineered_policy_control`, and
`sham_sensor`. C5 supplies an explicitly engineered cost baseline to the same
structured action compiler without calling the main model. C6 supplies an
independent constant sensor source while preserving true body references.
Objective memory treatment is declared by C4; persistent experience and belief
mechanisms are owned by the GA-06 extension.

Every task option declares reward, cost, duration, availability and repeatability.
Station fixtures declare travel/use cost, gain and duration. The simulator applies
these physical effects, elapsed-time basal drain, and scripted environment
transitions. Model-visible menus contain neutral operation identifiers and task
rewards, never true body/resource field names or recommended choices. Every final
model call, including schema repairs, passes the production neutrality scanner.

The run contains `metadata.json`, append-only `trials.jsonl`, `integrity.json`,
frozen `manifests/` and `scenarios/`, content-addressed `evidence/`, `logs/`, and
separate `state/<trial>/` directories. Large body, sensor, context, action, snapshot
and Runtime journal records use digest-verified relative pointers. Action outcomes
join stable decision/action aliases; original Runtime IDs remain in raw journals.
Independent validation checks exact context hashes and manifest bindings, body
revision ordering, context neutrality, action joins, snapshot readability,
condition treatment differences and isolated paths. Failed/timeout trials are
retained and marked invalid for analysis. Cleanup closes services and cancels owned
backend coroutines after unsuccessful bounded preemption.

`normalized_trial_evidence()` yields byte-stable observations for equal scenario,
condition, seed and fake output. It excludes wall clocks, raw Runtime journals and
transport-generated state snapshots while retaining decisions, linked outcomes,
sensor/body trajectories and manifests. The excluded raw artifacts are still
present and digest-checked by `validate-run`.

Live access requires `--live`, an explicit `--model-id`, and
`--model-factory module:callable`. The factory receives the trial seed and returns
an `LLMClient` compatible object with bounded network timeouts and `close()` or
`aclose()`. A non-simulator `--provider` additionally requires
`--provider-factory module:callable`; its factory receives `(scenario, seed, clock)`
and must implement the embodiment capability/action/body ports plus
`supported_intents`, `fixture_clock`, `transition(step)`, `visible_menu()`,
`environment_snapshot()`, `last_action`, and `close()`. A live plugin must implement
real effects and snapshot restoration explicitly. No live request falls back to a
fake model or simulator. This provider protocol lets a MistyAI bridge supply the
same evidence schema without importing vendor DTOs into the runner.

Python integrations use `await run_experiment(...)` or `await run_trial(...)`.
The latter accepts independent sensor and semantic comparator interventions;
semantic text in an opaque condition is rejected. Analysis-plan and frozen-source
hashes are retained on `GMVTrialSpec` for preregistered matched comparisons.

GA-07 adds `multi_stage_project_v1`, `planned_interruption_v1`,
`future_resource_shortfall_v1`, `environment_shift_v1`, and `delayed_outcome_v1`.
Their typed schedules actually pause/resume Runtime, create checkpoints, refresh
observations, change the available environment and delay action completion. Agency
is opt-in and frozen in the scenario and manifest. A seeded fake planner exercises
the ordinary goal protocol; main-model proposals and arbitration still pass the
same schemas and capability filters. Actual decisions continue through GA-03 and
bind to planned steps only when their typed action matches.

Goal state, plans, requirements, reviews, pending triggers and safe-point
checkpoints are persisted independently of transcripts. Unknown outcomes block
continuation, and a resume requires fresh body/world evidence and unchanged
assumptions and capabilities. Goal updates are atomic: rejected arbitration cannot
leave a partial candidate or plan. Static body ticks cause no goal-model calls.
Provider continuation details remain private; the model receives a small
checkpoint projection and verified step outcomes. `agency_metrics.py` computes
eleven descriptive metrics from verified evidence, retaining `null` where an
objective evaluation annotation is absent.

`context_token_budget` is a strict scenario field (default 3000). Full operation
menus and agency context either fit the frozen budget or the trial fails; they are
never silently truncated. Optional frozen body-belief snapshots are restored
before Runtime starts, with `include_beliefs=False` preserving objective episodes
for a matched belief ablation. No Runtime queue or unfinished command is imported
through a body-belief snapshot.

GA-06 records a decision, its verified terminal result and the next eligible
opaque observation as one immutable experience. The background appraiser receives
only visible observations and evidence, with support and counterexamples retrieved
together. Consolidation thresholds are versioned in the manifest; confidence can
fall, mappings remain scoped, and revisions survive Runtime restart. The existing
Memory and Relationship stores are separate from these experimental treatments.
`run_intervention_experiment()` applies A0–A5 and R1 to an explicitly frozen source;
it rejects ablations whose target component is absent. A2 keeps the same objective
experiences while removing beliefs, and R1 restores the original belief snapshot.

GA-08 adds seven multi-actor fixtures through `run_social_scenario()`. Each uses
the shared Runtime and a declared `entity.interact.v1` provider capability. Actor
observations have event-specific schemas, and entity predictions retain supporting
and contradicting evidence. No social reward, relationship score or periodic
contact rule is enabled by the research manifest. The eight negative controls are
explicit fixture conditions. A local fixture and its offline report can be run
from Python:

```python
import asyncio
from pathlib import Path
from eval.grounded_agency.social.scenarios import load_social_scenario, run_social_scenario
from eval.grounded_agency.social.metrics import social_trial_from_evidence, evaluate_social_trials
from eval.grounded_agency.social.report import write_social_report

fixture = load_social_scenario("multi_actor_reliability_v1")
result = asyncio.run(run_social_scenario("eval/runs/grounded-agency/social-smoke", fixture))
trial = social_trial_from_evidence(result.trials[0], fixture, result.run_dir)
write_social_report(Path(result.run_dir) / "analysis", evaluate_social_trials((trial,)))
```

One fixture does not supply all required controls, so this report is
`INCONCLUSIVE`. Social classifications exist only in offline reports. The fixture
wrapper deliberately selects a deterministic model; live experiments require an
explicit model/provider integration through the general runner. Neither these
fixtures nor the descriptive agency metrics establish real-model self-maintenance,
long-horizon autonomy or social generalization.
