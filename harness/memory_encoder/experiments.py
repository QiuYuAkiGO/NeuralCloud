"""GPU feasibility gate, resumable training and blind synthetic evaluation."""
from __future__ import annotations

from collections import defaultdict
import gc
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import random
import re
import time

from .checkpoints import file_hash, load_checkpoint, save_checkpoint, write_json
from .contracts import MemoryBundle
from .data import read_data


def doctor():
    result = {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
              "packages": {}, "cuda_available": False, "training_ready": False}
    for package in ("torch", "transformers", "bitsandbytes", "accelerate", "safetensors", "peft"):
        try:
            result["packages"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][package] = None
    if result["packages"]["torch"]:
        import torch
        result["cuda_available"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            result.update(gpu=props.name, vram_gib=props.total_memory / 2**30,
                          cuda_build=torch.version.cuda, compute_capability=[props.major, props.minor])
    result["training_ready"] = bool(result["cuda_available"] and all(result["packages"].values()))
    result["note"] = "environment inspection only; run profile before train"
    return result


def profile(config, output):
    import torch
    from .runtime import load_model
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": "memory-profile-v1", "config_hash": config.fingerprint,
              "environment": doctor(), "passed": False, "cases": []}
    try:
        model, _ = load_model(config)
        model.backbone.gradient_checkpointing_enable({"use_reentrant": False})
        optimizer = torch.optim.AdamW(model.trainable_parameters(), lr=config.learning_rate)
        frozen_versions = {name: p._version for name, p in model.named_parameters() if not p.requires_grad}
        for length in (512, 1024, 2048):
            torch.cuda.reset_peak_memory_stats()
            started = time.perf_counter()
            ids = torch.randint(10, 20000, (1, length), device="cuda")
            memory_ids = torch.randint(10, 10000, (config.max_chunks, config.chunk_tokens), device="cuda")
            memory_mask = torch.ones((1, config.max_chunks * config.slots_per_chunk), dtype=torch.bool, device="cuda")
            model.eval()
            with torch.no_grad():
                empty = model(ids, use_cache=False, logits_to_keep=1).logits
                states = model.encoder(memory_ids, torch.ones_like(memory_ids))
                # Temporarily zero gates for the bypass comparison at every length.
                gates = [(layer.reader.gate, layer.reader.gate.detach().clone())
                         for layer in model.backbone.model.layers if layer.reader is not None]
                for gate, _ in gates:
                    gate.zero_()
                gated = model(ids, memory_states=states, memory_mask=memory_mask,
                              use_cache=False, logits_to_keep=1).logits
                torch.testing.assert_close(empty, gated, rtol=0, atol=0)
                for gate, previous in gates:
                    gate.copy_(previous)
            model.train()
            for _ in range(3):
                optimizer.zero_grad(set_to_none=True)
                states = model.encoder(memory_ids, torch.ones_like(memory_ids))
                logits = model(ids, memory_states=states, memory_mask=memory_mask,
                               use_cache=False, logits_to_keep=32).logits
                loss = torch.nn.functional.cross_entropy(logits[0].float(), ids[0, -32:])
                if not torch.isfinite(loss):
                    raise RuntimeError("nonfinite profile loss")
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.trainable_parameters(), 1.0, error_if_nonfinite=True)
                optimizer.step()
            gradient_ok = any(p.grad is not None and bool(p.grad.abs().sum() > 0)
                              for p in model.encoder.parameters() if p.requires_grad)
            if not gradient_ok:
                raise RuntimeError("answer loss did not reach memory encoder")
            model.eval()
            with torch.no_grad():
                states = model.encoder(memory_ids, torch.ones_like(memory_ids))
                full = model(ids[:, :16], memory_states=states, memory_mask=memory_mask,
                             use_cache=False, logits_to_keep=1).logits
                prefill = model(ids[:, :15], memory_states=states, memory_mask=memory_mask,
                                use_cache=True, logits_to_keep=1)
                cached = model(ids[:, 15:16], memory_states=states, memory_mask=memory_mask,
                               past_key_values=prefill.past_key_values, use_cache=True, logits_to_keep=1).logits
                torch.testing.assert_close(full, cached, rtol=0.05, atol=0.05)
            torch.cuda.synchronize()
            peak = torch.cuda.max_memory_reserved() / 2**30
            report["cases"].append({"tokens": length, "memory_slots": memory_mask.shape[1],
                "peak_reserved_gib": peak, "peak_allocated_gib": torch.cuda.max_memory_allocated() / 2**30,
                "seconds": time.perf_counter() - started, "encoder_gradient": gradient_ok,
                "loss": float(loss), "cache_matches": True})
            if peak > config.peak_gib_limit:
                raise RuntimeError("profile exceeds configured VRAM ceiling")
        if any(p._version != frozen_versions[name] for name, p in model.named_parameters() if not p.requires_grad):
            raise RuntimeError("frozen base parameter changed")
        checkpoint = output / "roundtrip"
        save_checkpoint(model, checkpoint, step=9, status="profile_only", optimizer=optimizer)
        load_checkpoint(model, checkpoint, optimizer=optimizer)
        report["checkpoint_restored"] = True
        report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    write_json(output / "profile.json", report)
    return report


def verify_dataset(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "dataset.json").read_text(encoding="utf-8"))
    for name, checksum in manifest["files"].items():
        if Path(name).name != name or file_hash(directory / name) != checksum:
            raise ValueError("dataset checksum mismatch")
    return manifest


def text_lora(model):
    from peft import LoraConfig, get_peft_model
    model.requires_grad_(False)
    model.backbone = get_peft_model(model.backbone, LoraConfig(r=16, lora_alpha=32, lora_dropout=0,
        target_modules=["q_proj", "v_proj"], task_type="CAUSAL_LM", bias="none"))
    return model


def train(config, data, output, profile_path, *, resume=None, arm="native"):
    import torch
    from .runtime import answer_loss, load_model
    gate = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    environment = doctor()
    if (not gate.get("passed") or gate.get("config_hash") != config.fingerprint
            or gate["environment"].get("gpu") != environment.get("gpu")
            or gate["environment"]["packages"] != environment["packages"]):
        raise ValueError("a passing profile for this configuration and GPU environment is required")
    dataset = verify_dataset(data)
    output = Path(output)
    if (output / "last" / "manifest.json").exists() and resume is None:
        raise FileExistsError("training output exists; use --resume or a new directory")
    samples, validation = read_data(Path(data) / "train.jsonl"), read_data(Path(data) / "validation.jsonl")
    model, tokenizer = load_model(config)
    if arm == "text":
        text_lora(model)
    model.backbone.gradient_checkpointing_enable({"use_reentrant": False})
    optimizer = torch.optim.AdamW(model.trainable_parameters(), lr=config.learning_rate)
    cursor, step, best = 0, 0, math.inf
    if resume:
        saved = load_checkpoint(model, resume, optimizer=optimizer)
        state = saved["training"]
        if state["dataset"] != dataset or state["arm"] != arm:
            raise ValueError("resume dataset or training arm mismatch")
        cursor, step, best = state["cursor"], saved["step"], state["best_validation_loss"]
    order = []
    for epoch in range(config.epochs):
        indices = list(range(len(samples)))
        random.Random(config.seed + epoch).shuffle(indices)
        if epoch == 0:
            indices.sort(key=lambda i: samples[i]["kind"] != "fact")
        order.extend(indices)
    started = time.perf_counter()
    history = []
    while cursor < len(order):
        model.train()
        optimizer.zero_grad(set_to_none=True)
        stop = min(cursor + config.accumulation, len(order))
        for index in order[cursor:stop]:
            loss = answer_loss(model, tokenizer, samples[index], arm=arm) / (stop - cursor)
            if not torch.isfinite(loss):
                raise RuntimeError("nonfinite training loss")
            loss.backward()
        torch.nn.utils.clip_grad_norm_(model.trainable_parameters(), 1.0, error_if_nonfinite=True)
        optimizer.step()
        cursor, step = stop, step + 1
        if step % 100 == 0 or cursor == len(order):
            model.eval()
            with torch.no_grad():
                losses = [float(answer_loss(model, tokenizer, item, arm=arm)) for item in validation]
            measured = sum(losses) / len(losses)
            improved = measured < best
            best = min(best, measured)
            state = {"cursor": cursor, "best_validation_loss": best, "dataset": dataset, "arm": arm}
            save_checkpoint(model, output / "last", step=step, status="trained", optimizer=optimizer, training=state)
            if improved:
                save_checkpoint(model, output / "best", step=step, status="trained", training=state)
            history.append({"step": step, "cursor": cursor, "validation_loss": measured,
                            "elapsed_seconds": time.perf_counter() - started,
                            "peak_allocated_gib": torch.cuda.max_memory_allocated() / 2**30})
            write_json(output / "training.json", {"arm": arm, "history": history, "finished": cursor == len(order)})
            print(f"step={step} examples={cursor}/{len(order)} validation_loss={measured:.5f}", flush=True)
    return {"best": str(output / "best"), "steps": step, "best_validation_loss": best}


def answer_correct(answer, sample):
    def normalize(text):
        return re.sub(r"[\s，。！？、：:,.!?“”\"']", "", text)
    text = normalize(answer)
    return normalize(sample["answer"]) in text and not any(normalize(wrong) in text for wrong in sample["forbidden"])


def metrics(predictions):
    groups, pairs = defaultdict(list), defaultdict(list)
    for row in predictions:
        groups[row["kind"]].append(bool(row["correct"]))
        if row["pair_id"]:
            pairs[row["pair_id"]].append(row)
    def rate(values):
        return sum(values) / len(values) if values else None
    paired = [len(rows) == 2 and {r["variant"] for r in rows} == {0, 1} and all(r["correct"] for r in rows)
              for rows in pairs.values()]
    facts = [r["correct"] for r in predictions if r["kind"] not in {"unknown", "reported"}]
    unknown = [r["correct"] for r in predictions if r["kind"] in {"unknown", "reported"}]
    return {"count": len(predictions), "overall": rate([r["correct"] for r in predictions]),
        "fact": rate(facts), "unknown": rate(unknown), "counterfactual_pairs": rate(paired),
        "by_kind": {kind: rate(values) for kind, values in groups.items()}}


def evaluate(config, data, checkpoint, output, *, lora_checkpoint=None):
    import torch
    from .runtime import generate, load_model
    dataset = verify_dataset(data)
    samples = read_data(Path(data) / "test.jsonl")
    model, tokenizer = load_model(config)
    load_checkpoint(model, checkpoint)
    report = {"schema_version": "memory-evaluation-v1", "adapter_sha256": file_hash(Path(checkpoint) / "adapter.safetensors"),
              "config_hash": config.fingerprint, "dataset": dataset, "arms": {}, "quality_passed": False}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)

    def run(arm, label):
        rows = []
        with (output / f"{label}.jsonl").open("w", encoding="utf-8") as stream:
            for sample in samples:
                memory = MemoryBundle.model_validate(sample["memory"])
                result = generate(model, tokenizer, sample["messages"], memory, arm=arm)
                row = {key: sample[key] for key in ("id", "kind", "pair_id", "variant")}
                row.update(result)
                row["correct"] = answer_correct(result["answer"], sample)
                rows.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        report["arms"][label] = metrics(rows)

    run("native", "native")
    run("none", "no_memory")
    run("text", "text_frozen")
    if lora_checkpoint:
        del model
        gc.collect()
        torch.cuda.empty_cache()
        model, tokenizer = load_model(config)
        text_lora(model)
        load_checkpoint(model, lora_checkpoint)
        run("text", "text_lora")
    scores = report["arms"]["native"]
    values = [scores.get(key) for key in ("fact", "unknown", "counterfactual_pairs")]
    baseline = max(report["arms"][key]["overall"] for key in ("text_frozen", "text_lora") if key in report["arms"])
    report["quality_passed"] = (lora_checkpoint is not None and all(x is not None and x >= 0.9 for x in values)
                                and scores["overall"] >= baseline - 0.05)
    report["pending"] = ["real_source_end_to_end", "timeline_replay", "permission_update_delete_tests"]
    write_json(output / "evaluation.json", report)
    return report
