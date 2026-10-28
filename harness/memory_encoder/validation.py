"""Source checks, persistent history replay and explicit release evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import zipfile

from .checkpoints import file_hash, load_checkpoint, write_json
from .contracts import MemoryBundle, MemoryQuery, ReadContext
from .data import read_data
from .sources import HyMemorySource, HySubprocess, NeuralCloudSource, open_neuralcloud


def replay_timeline(data, output):
    from harness.core.models import MemoryScope
    from harness.memory.production import Experience, ProductionMemoryStore
    from .experiments import verify_dataset
    dataset = verify_dataset(data)
    events = read_data(Path(data) / "timeline.jsonl")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    database = output / "timeline.sqlite3"
    if database.exists():
        raise FileExistsError("replay requires a new isolated output directory")
    store = ProductionMemoryStore(database, owner_id="synthetic", preset_id="encoder-replay")
    source = NeuralCloudSource(store)
    heads, stale_checks, scope_checks, restarts = {}, 0, 0, 0
    for event in events:
        audience, subject = event["audience"], event["subject"]
        now = datetime.fromisoformat(event["at"])
        context = ReadContext(audience=audience, principal="synthetic-replay", now=now)
        key = (audience, subject)
        if previous := heads.pop(key, None):
            store.delete_source(previous.records[0].record_id, event_id=f"retract:{event['event_id']}")
            if source.validate(previous, context):
                raise AssertionError("stale record survived a correction/deletion")
            stale_checks += 1
        if event["operation"] == "upsert":
            identifier = f"event:{event['event_id']}"
            term = f"snapshot{event['event_id']:05d}"
            store.ingest([Experience(record_id=identifier, author_id=subject,
                text=f"{term} {event['content']}", occurred_at=now,
                scope=MemoryScope(owner_id="synthetic", channel="local",
                    conversation_id=audience, visibility=[audience]),
                independent_key=identifier, source_refs=(identifier,))])
            query = MemoryQuery(provider="neuralcloud", query=term, limit=1)
            current = source.read(query, context)
            if [r.record_id for r in current.records] != [identifier]:
                raise AssertionError("latest replay event not recalled")
            heads[key] = current
            other = context.model_copy(update={"audience": "synthetic:unauthorized"})
            if source.read(query, other).records:
                raise AssertionError("audience leak in replay")
            scope_checks += 1
        if event.get("restart_after"):
            store = ProductionMemoryStore(database, owner_id="synthetic", preset_id="encoder-replay")
            source = NeuralCloudSource(store)
            if not all(source.validate(value, ReadContext(audience=scope, principal="synthetic-replay", now=now))
                       for (scope, _), value in heads.items()):
                raise AssertionError("memory state failed restart recovery")
            restarts += 1
    report = {"schema_version": "memory-replay-v1", "passed": True, "events": len(events),
        "span_days": (datetime.fromisoformat(events[-1]["at"]) - datetime.fromisoformat(events[0]["at"])).days,
        "stale_checks": stale_checks, "scope_checks": scope_checks, "unauthorized_records": 0,
        "restarts": restarts, "dataset": dataset, "coverage": "NeuralCloud persistence and adapter; no LLM quality claim"}
    write_json(output / "replay.json", report)
    return report


def check_sources(config, settings_path, cases_path, checkpoint, output):
    from .runtime import generate, load_model
    settings = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    contexts = {item["context"]["principal"]: ReadContext.model_validate(item["context"])
                for item in settings["principals"]}
    if any(not context.audience.startswith("synthetic:") for context in contexts.values()):
        raise ValueError("source checks require explicitly synthetic test partitions")
    sources = {}
    if entry := settings.get("neuralcloud"):
        sources["neuralcloud"] = NeuralCloudSource(open_neuralcloud(**entry))
    if entry := settings.get("hy_memory"):
        sources["hy_memory"] = HyMemorySource(
            HySubprocess(entry["python"], entry["config"], entry["checkout"]), timezone=entry["timezone"])
    model, tokenizer = load_model(config)
    load_checkpoint(model, checkpoint)
    results = []
    for case in read_data(cases_path):
        context = contexts[case["principal"]].model_copy(update={"now": datetime.now(timezone.utc)})
        query = MemoryQuery.model_validate(case["memory"])
        source = sources[query.provider]
        bundle = source.read(query, context)
        valid = source.validate(bundle, context)
        stale_rejected = None
        if case.get("invalidated_bundle"):
            stale_rejected = not source.validate(MemoryBundle.model_validate(case["invalidated_bundle"]), context)
        result = generate(model, tokenizer, case["messages"], bundle, max_new_tokens=256)
        admitted = {chunk["record_id"] for chunk in result["manifest"]["chunks"]}
        passed = (valid and source.validate(bundle, context.model_copy(update={"now": datetime.now(timezone.utc)}))
            and set(case.get("required_records", ())).issubset(admitted)
            and not set(case.get("forbidden_records", ())) & admitted
            and case["expected_answer"] in result["answer"]
            and not any(text in result["answer"] for text in case.get("forbidden_answers", ())))
        kind = case["kind"]
        if kind in {"update", "delete"}:
            passed = passed and stale_rejected is True
        if kind == "scope":
            passed = passed and bool(case.get("forbidden_records"))
        results.append({"case_id": case["id"], "provider": query.provider, "kind": kind,
                        "passed": passed, "stale_rejected": stale_rejected, "result": result})
    coverage = {(r["provider"], r["kind"]) for r in results if r["passed"]}
    required = {(provider, kind) for provider in ("neuralcloud", "hy_memory")
                for kind in ("fact", "update", "delete", "scope")}
    report = {"schema_version": "memory-source-check-v1", "passed": bool(results) and all(r["passed"] for r in results)
        and required.issubset(coverage), "config_hash": config.fingerprint,
        "adapter_sha256": file_hash(Path(checkpoint) / "adapter.safetensors"), "cases": results,
        "missing_coverage": sorted(required - coverage), "fixtures_sha256": file_hash(cases_path)}
    write_json(Path(output) / "sources.json", report)
    return report


def accept_release(checkpoint, profile, evaluation, sources, replay):
    checkpoint = Path(checkpoint)
    metadata = json.loads((checkpoint / "manifest.json").read_text(encoding="utf-8"))
    paths = {"profile": profile, "evaluation": evaluation, "sources": sources, "replay": replay}
    reports = {key: json.loads(Path(path).read_text(encoding="utf-8")) for key, path in paths.items()}
    adapter_hash = file_hash(checkpoint / "adapter.safetensors")
    conditions = [
        metadata["training_status"] == "trained", metadata.get("training", {}).get("arm") == "native",
        reports["profile"].get("passed") is True,
        reports["evaluation"].get("quality_passed") is True,
        reports["sources"].get("passed") is True,
        reports["replay"].get("passed") is True,
        reports["replay"].get("events", 0) >= 10000, reports["replay"].get("span_days", 0) >= 90,
        reports["replay"].get("restarts", 0) >= 2, reports["replay"].get("unauthorized_records") == 0,
        reports["evaluation"].get("adapter_sha256") == adapter_hash,
        reports["sources"].get("adapter_sha256") == adapter_hash,
        all(reports[key].get("config_hash") == metadata["config_hash"] for key in ("profile", "evaluation", "sources")),
        reports["evaluation"].get("dataset") == metadata.get("training", {}).get("dataset"),
        reports["replay"].get("dataset") == metadata.get("training", {}).get("dataset"),
    ]
    accepted = all(conditions)
    report = {"schema_version": "memory-release-v1", "accepted": accepted, "adapter_sha256": adapter_hash,
              "config_hash": metadata["config_hash"], "evidence": {}}
    for key, original in paths.items():
        report["evidence"][key] = file_hash(original)
        if accepted:
            write_json(checkpoint / "evidence" / f"{key}.json", reports[key])
    write_json(checkpoint / "acceptance.json", report)
    return report


def export_bundle(output):
    """Allowlist source files, never copy a data directory, .env, or virtualenv."""
    root = Path(__file__).resolve().parents[2]
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("handoff archive already exists")
    paths = set()
    for folder in ("harness", "infra", "tests"):
        paths.update(path for path in (root / folder).rglob("*.py") if "__pycache__" not in path.parts)
    for folder in ("harness/llm/data", "eval/memory_architecture"):
        paths.update(path for path in (root / folder).rglob("*")
                     if path.is_file() and path.suffix in (
                         {".py", ".md", ".txt", ".json"} if folder == "harness/llm/data" else {".py", ".md"})
                     and "__pycache__" not in path.parts)
    for name in ("requirements-gpu.txt", "service.example.json", "cpu-verification.json", "requirements-cpu-verified.txt"):
        path = root / "tools/memory_encoder" / name
        if path.exists():
            paths.add(path)
    paths.add(root / "pyproject.toml")
    paths.add(root / "README.md")
    for name in ("2026-10-19-memory-encoder-qwen38-plan.md", "memory-encoder-handoff.md",
                 "2026-10-19-hy-memory-assessment.html", "2026-10-17-external-memory-review.html"):
        path = root / "docs/research/agent-memory" / name
        if path.exists():
            paths.add(path)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    manifest = {"schema_version": "memory-code-handoff-v1", "git_base": head,
        "contains_trained_weights": False, "files": {str(path.relative_to(root)).replace("\\", "/"): file_hash(path)
                                                  for path in sorted(paths)}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, str(path.relative_to(root)))
        archive.writestr("HANDOFF-MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"archive": str(output), "sha256": file_hash(output), "files": len(paths),
            "contains_trained_weights": False}
