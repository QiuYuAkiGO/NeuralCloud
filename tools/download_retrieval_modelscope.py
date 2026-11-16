"""Pinned, SHA256-verified public mirror download, with no remote code execution."""

import hashlib
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

root = Path("/var/lib/misty-retrieval-eval/20261027/models")
root.mkdir(exist_ok=True)
models = ["AI-ModelScope/bge-small-zh-v1.5", "BAAI/bge-m3", "Qwen/Qwen3-Embedding-0.6B"]
for name in models:
    destination = root / name.split("/")[-1]
    destination.mkdir(exist_ok=True)
    manifest = destination / "download-manifest.json"
    if manifest.exists():
        files = json.loads(manifest.read_text())
    else:
        url = "https://modelscope.cn/api/v1/models/" + name + "/repo/files?Revision=master&Recursive=true"
        data = json.load(urllib.request.urlopen(url, timeout=30))
        files = [
            f
            for f in data["Data"]["Files"]
            if f["Type"] == "blob"
            and ("/" not in f["Path"] or f["Path"].startswith("1_Pooling/"))
            and f["Path"].endswith((".json", ".txt", ".model", ".safetensors"))
        ]
        manifest.write_text(json.dumps(files, indent=2))
    if not any(f["Path"].endswith(".safetensors") for f in files):
        data = json.load(
            urllib.request.urlopen(
                "https://modelscope.cn/api/v1/models/" + name + "/repo/files?Revision=master&Recursive=true", timeout=30
            )
        )
        files += [f for f in data["Data"]["Files"] if f["Path"] == "pytorch_model.bin"]
        manifest.write_text(json.dumps(files, indent=2))
    assert any(f["Path"].endswith((".safetensors", ".bin")) for f in files), "MODEL_WEIGHTS_REQUIRED"

    def download(f):
        target = destination / f["Path"]
        assert target.resolve().is_relative_to(destination.resolve())
        target.parent.mkdir(exist_ok=True)
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == f["Sha256"]:
            return
        params = urllib.parse.urlencode({"Revision": f["Revision"], "FilePath": f["Path"]})
        url = "https://modelscope.cn/api/v1/models/" + name + "/repo?" + params
        tmp = target.with_suffix(target.suffix + ".part")
        for attempt in range(3):
            try:
                h = hashlib.sha256()
                size = 0
                with urllib.request.urlopen(url, timeout=60) as source, tmp.open("wb") as out:
                    while block := source.read(1024 * 1024):
                        out.write(block)
                        h.update(block)
                        size += len(block)
                if size != f["Size"] or h.hexdigest() != f["Sha256"]:
                    raise ValueError("MODEL_HASH_MISMATCH")
                tmp.replace(target)
                print("VERIFIED", name, f["Path"], size, flush=True)
                return
            except Exception as e:
                if attempt == 2:
                    raise
                print("RETRY", name, f["Path"], type(e).__name__, flush=True)
                time.sleep(2)

    print("MODEL", name, "bytes", sum(f["Size"] for f in files), flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(download, files))
    (destination / "READY").write_text(hashlib.sha256(manifest.read_bytes()).hexdigest())
    print("READY", destination, flush=True)
