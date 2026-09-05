from __future__ import annotations

import asyncio
import subprocess
import sys

from eval.grounded_agency.evidence import normalized_trial_evidence, validate_run
from eval.grounded_agency.runner import run_experiment
from eval.grounded_agency.scenarios import load_scenario


def test_same_seed_produces_byte_stable_normalized_evidence(tmp_path):
    async def run():
        first = await run_experiment(tmp_path / "first", seed=9)
        second = await run_experiment(tmp_path / "second", seed=9)
        assert first.integrity.passed, first.trials[0].error
        assert second.integrity.passed, second.trials[0].error
        assert normalized_trial_evidence(first.trials[0]) == normalized_trial_evidence(second.trials[0])
    asyncio.run(run())


def test_all_scenarios_execute_real_runtime_actions(tmp_path):
    async def run():
        scenarios = [load_scenario(name).model_copy(update={"horizon_steps": 3, "initial_resource_levels": ()})
            for name in ("state_sensitivity_v1", "anticipation_v1", "opportunity_cost_v1", "action_only_v1")]
        result = await run_experiment(tmp_path / "all", scenarios=scenarios)
        assert result.integrity.passed, [(trial.error, trial.integrity) for trial in result.trials]
        assert all(trial.action_outcomes for trial in result.trials)
        assert all(trial.runtime_evidence for trial in result.trials)
    asyncio.run(run())


def test_cli_two_condition_two_repetition_smoke(tmp_path):
    root = tmp_path / "cli"
    result = subprocess.run([sys.executable, "-m", "eval.grounded_agency", "run", "--scenario", "action_only_v1",
        "--condition", "no_body", "--condition", "opaque_current", "--repetitions", "2", "--output-dir", str(root)],
        capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert validate_run(root).trial_count == 4
    result = subprocess.run([sys.executable, "-m", "eval.grounded_agency", "validate-run", str(root)],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_cli_requires_explicit_plugins(tmp_path):
    result = subprocess.run([sys.executable, "-m", "eval.grounded_agency", "run", "--live", "--output-dir", str(tmp_path / "live")],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "explicit model factory" in result.stderr
