"""Public model artifacts only; sends no memory text to any remote service."""

import json
from pathlib import Path
from huggingface_hub import HfApi, snapshot_download

root = Path("/var/lib/misty-retrieval-eval/20261027")
models = ["BAAI/bge-small-zh-v1.5", "BAAI/bge-m3", "Qwen/Qwen3-Embedding-0.6B"]
lock = {}
for name in models:
    revision = HfApi().model_info(name).sha
    lock[name] = revision
    (root / "models.lock.json").write_text(json.dumps(lock, indent=2))
    print("DOWNLOAD", name, revision, flush=True)
    path = snapshot_download(
        name,
        revision=revision,
        allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "1_Pooling/*"],
        max_workers=3,
    )
    print("READY", name, path, flush=True)
