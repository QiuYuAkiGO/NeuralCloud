"""Paired adapters, exact compatibility checks and resumable optimizer state."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import canonical


def file_hash(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def save_checkpoint(model, path, *, step, status, optimizer=None, training=None):
    import torch
    from safetensors.torch import save_file
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    weights = path / "adapter.safetensors"
    save_file(model.trainable_state(), str(weights) + ".tmp")
    Path(str(weights) + ".tmp").replace(weights)
    files = {weights.name: file_hash(weights)}
    if optimizer is not None:
        state = path / "optimizer.pt"
        torch.save({"optimizer": optimizer.state_dict(), "cpu_rng": torch.get_rng_state(),
                    "cuda_rng": torch.cuda.get_rng_state_all() if torch.cuda.is_available() else []}, str(state) + ".tmp")
        Path(str(state) + ".tmp").replace(state)
        files[state.name] = file_hash(state)
    write_json(path / "manifest.json", {"schema_version": "memory-adapter-v1",
        "config": model.config.model_dump(mode="json"), "config_hash": model.config.fingerprint,
        "step": step, "training_status": status, "training": training or {}, "files": files,
        "trainable_names": sorted(model.trainable_state())})


def load_checkpoint(model, path, *, optimizer=None):
    import torch
    from safetensors.torch import load_file
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != "memory-adapter-v1" or manifest["config_hash"] != model.config.fingerprint:
        raise ValueError("adapter and backbone/configuration do not match")
    expected_names = sorted(name for name, p in model.named_parameters() if p.requires_grad)
    if expected_names != manifest["trainable_names"]:
        raise ValueError("adapter parameter inventory mismatch")
    for name, expected in manifest["files"].items():
        if Path(name).name != name or file_hash(path / name) != expected:
            raise ValueError("checkpoint integrity failure")
    state = load_file(str(path / "adapter.safetensors"))
    if sorted(state) != expected_names:
        raise ValueError("incomplete adapter")
    parameters = dict(model.named_parameters())
    with torch.no_grad():
        for name, value in state.items():
            if parameters[name].shape != value.shape:
                raise ValueError("adapter shape mismatch")
            parameters[name].copy_(value)
    if optimizer is not None:
        saved = torch.load(path / "optimizer.pt", map_location="cpu", weights_only=True)
        optimizer.load_state_dict(saved["optimizer"])
        torch.set_rng_state(saved["cpu_rng"])
        if saved["cuda_rng"]:
            torch.cuda.set_rng_state_all(saved["cuda_rng"])
    return manifest


def require_acceptance(path):
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    acceptance = json.loads((path / "acceptance.json").read_text(encoding="utf-8"))
    if (manifest["training_status"] != "trained" or acceptance.get("accepted") is not True
            or acceptance.get("adapter_sha256") != file_hash(path / "adapter.safetensors")):
        raise ValueError("checkpoint has not passed release acceptance")
    return canonical(acceptance)
