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
