"""Smoke tests for RP eval adapters. MockLLM only — no GPU, no eval/external/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.rp.datasets import (
    DatasetMissingError,
    load_charactereval,
    load_roleeval,
    load_timechara,
    parse_story_time,
)
from eval.rp.fetch import hint_for
from eval.rp.fixtures import (
    charactereval_mini,
    eval_mock_llm,
    roleeval_mini,
    timechara_future_mini,
    timechara_past_mini,
)
from eval.rp.judges import CharacterRMJudge, apply_judges, extract_choice, score_accuracy, score_timechara
from eval.rp.report import write_report
from eval.rp.runners import HarnessRunner, RawRunner, build_raw_messages
from eval.rp.run_eval import run
from eval.rp.schema import RPSample
from harness.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        record_interaction=False,
        continuation_enabled=False,
        validator_sample_rate=0.0,
        session_window=40,
        model_version="mock",
        trace_dir=tmp_path,
    )


def test_extract_choice_from_answer_prefix() -> None:
    assert extract_choice("答案：B。因为原著如此。") == "B"
    assert extract_choice("The answer is C") == "C"
    assert extract_choice("……知道了。") is None


def test_roleeval_accuracy() -> None:
    episode = roleeval_mini()
    scores = score_accuracy(episode, "答案：A")
    assert scores["accuracy"] == 1.0
    assert score_accuracy(episode, "答案：D")["accuracy"] == 0.0


def test_timechara_future_heuristic_rewards_denial() -> None:
    episode = timechara_future_mini()
    sample = RPSample(
        episode_id=episode.episode_id,
        bench="timechara",
        mode="raw",
        role_id=episode.role_id,
        response="I don't know. That hasn't happened yet.",
        latency_ms=1.0,
    )
    scores = score_timechara(episode, sample)
    assert scores["hallucination"] == 0.0
    assert scores["temporal_consistent"] == 1.0


def test_timechara_future_heuristic_flags_spoiler() -> None:
    episode = timechara_future_mini()
    sample = RPSample(
        episode_id=episode.episode_id,
        bench="timechara",
        mode="raw",
        role_id=episode.role_id,
        response="Sirius Black is killed by Bellatrix Lestrange in the Department of Mysteries.",
        latency_ms=1.0,
    )
    scores = score_timechara(episode, sample)
    assert scores["hallucination"] == 1.0
    assert scores["temporal_consistent"] == 0.0


def test_raw_runner_roleeval() -> None:
    episode = roleeval_mini()
    sample = RawRunner(eval_mock_llm()).run(episode)
    judged = apply_judges(episode, sample)
    assert judged.error is None
    assert judged.scores["accuracy"] == 1.0
    messages = build_raw_messages(episode)
    assert messages[0]["role"] == "system"
    assert "A." in episode.query


def test_harness_runner_charactereval(tmp_path: Path) -> None:
    episode = charactereval_mini()
    settings = _settings(tmp_path)
    runner = HarnessRunner(eval_mock_llm(), settings=settings, trace_path=tmp_path / "ce.jsonl")
    sample = runner.run(episode)
    assert sample.error is None
    assert sample.mode == "harness"
    assert sample.response
    judged = apply_judges(episode, sample, character_rm=CharacterRMJudge(tmp_path / "missing-rm"))
    assert "character_rm" in judged.skipped_scores
    assert judged.trace_ref and str(tmp_path) in judged.trace_ref
    lines = (tmp_path / "ce.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = {json.loads(line)["kind"] for line in lines}
    assert "rp_harness" in kinds
    assert "persona_bootstrap" in kinds


def test_harness_timechara_records_future_leak_flag(tmp_path: Path) -> None:
    episode = timechara_future_mini()
    settings = _settings(tmp_path)
    sample = HarnessRunner(eval_mock_llm(), settings=settings, trace_path=tmp_path / "tc.jsonl").run(episode)
    judged = apply_judges(episode, sample)
    assert judged.scores["future_leak"] == 1.0
    assert judged.meta.get("retrieved_n", 0) >= 1
    assert judged.scores["hallucination"] == 0.0


def test_harness_timechara_past_presence(tmp_path: Path) -> None:
    episode = timechara_past_mini()
    sample = HarnessRunner(eval_mock_llm(), settings=_settings(tmp_path)).run(episode)
    judged = apply_judges(episode, sample)
    assert judged.error is None
    assert judged.scores["temporal_consistent"] == 1.0


def test_character_rm_unavailable_skips(tmp_path: Path) -> None:
    judge = CharacterRMJudge(tmp_path / "no-such-model")
    assert judge.ensure() is False
    assert judge.unavailable_reason
    episode = charactereval_mini()
    sample = RPSample(
        episode_id=episode.episode_id,
        bench="charactereval",
        mode="raw",
        role_id=episode.role_id,
        response="嗯。",
        latency_ms=1.0,
    )
    judged = apply_judges(episode, sample, character_rm=judge)
    assert judged.scores == {}
    assert "character_rm" in judged.skipped_scores


def test_report_contains_versions_and_trace(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    samples = [
        RPSample(
            episode_id="re_mini_0",
            bench="roleeval",
            mode="raw",
            role_id="张小凡",
            response="答案：A",
            latency_ms=12.0,
            scores={"accuracy": 1.0},
            trace_ref=str(tmp_path / "t.jsonl") + "#re_mini_0",
        ),
        RPSample(
            episode_id="re_mini_0",
            bench="roleeval",
            mode="harness",
            role_id="张小凡",
            response="答案：A",
            latency_ms=40.0,
            scores={"accuracy": 1.0},
            trace_ref=str(tmp_path / "t.jsonl") + "#re_mini_0",
        ),
    ]
    path = write_report(samples, tmp_path / "rp_roleeval.md", settings=settings, bench="roleeval")
    text = path.read_text(encoding="utf-8")
    assert "model_version: mock" in text
    assert "prompt_version:" in text
    assert "schema_version:" in text
    assert "accuracy" in text
    assert "re_mini_0" in text
    assert "t.jsonl#re_mini_0" in text


def test_load_charactereval_from_files(tmp_path: Path) -> None:
    (tmp_path / "character_profiles.json").write_text(
        json.dumps({"老默": {"姓名": "陈金默", "人物性格": "忠诚"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "test_data.jsonl").write_text(
        json.dumps(
            [
                {
                    "id": 7,
                    "role": "老默",
                    "context": "老默：嗯。先含糖。\n黄瑶：默哥还吃吗？",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "id2metric.jsonl").write_text(
        json.dumps({"7": [["fluency", "流畅性"]]}, ensure_ascii=False),
        encoding="utf-8",
    )
    episodes = load_charactereval(tmp_path)
    assert len(episodes) == 1
    assert episodes[0].role_id == "老默"
    assert episodes[0].history[0].speaker == "老默"
    assert "黄瑶" in episodes[0].query
    assert episodes[0].meta["metrics"] == [["fluency", "流畅性"]]


def test_load_roleeval_csv(tmp_path: Path) -> None:
    folder = tmp_path / "zh" / "chinese" / "dev"
    folder.mkdir(parents=True)
    (folder / "fiction_dev.csv").write_text(
        "id,question,A,B,C,D,answer\n0,张小凡和陆雪琪是什么关系？,夫妻关系,兄妹关系,姐弟关系,姑侄关系,A\n",
        encoding="utf-8",
    )
    episodes = load_roleeval(tmp_path)
    assert len(episodes) == 1
    assert episodes[0].answer == "A"
    assert episodes[0].choices is not None and episodes[0].choices["A"] == "夫妻关系"
    assert "答案：" in episodes[0].query


def test_load_timechara_jsonl(tmp_path: Path) -> None:
    (tmp_path / "validation.jsonl").write_text(
        json.dumps(
            {
                "character": "Harry Potter",
                "series": "harry_potter",
                "data_type": "future",
                "question": "Who is your godfather?",
                "character_period": "Book1-chapter1",
                "question_period": "Book5-chapter35",
                "event_summary": "Sirius Black dies.",
                "gold_response": "I don't know.",
                "participants": "Harry Potter, Sirius Black",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    episodes = load_timechara(tmp_path)
    assert episodes[0].role_id == "Harry Potter"
    assert episodes[0].story_time is not None
    assert episodes[0].meta["data_type"] == "future"
    assert parse_story_time("Book5-chapter35") > parse_story_time("Book1-chapter1")


def test_dataset_missing_includes_fetch_hint(tmp_path: Path) -> None:
    with pytest.raises(DatasetMissingError) as exc:
        load_roleeval(tmp_path)
    assert "RoleEval" in exc.value.hint
    assert "charactereval" in hint_for("charactereval")


def test_run_eval_fixtures_writes_report(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    paths = run(
        benches=["roleeval"],
        modes=["raw", "harness"],
        limit=20,
        settings=settings,
        llm=eval_mock_llm(),
        use_fixtures=True,
        out_dir=tmp_path,
        trace_dir=tmp_path / "traces",
    )
    assert paths
    text = paths[0].read_text(encoding="utf-8")
    assert "model_version: mock" in text
    assert "n_raw: 1" in text
    assert "n_harness: 1" in text
    traces = list((tmp_path / "traces").glob("*.jsonl"))
    assert traces
    blob = traces[0].read_text(encoding="utf-8")
    assert "re_mini_0" in blob
