import hashlib
import json
from pathlib import Path
import torch
from sentence_transformers import SentenceTransformer
from sentence_transformers.backend import export_dynamic_quantized_onnx_model

root = Path("/var/lib/misty-retrieval-eval/20261027/models/bge-small-zh-v1.5")
torch.set_num_threads(4)
model = SentenceTransformer(str(root), device="cpu", backend="onnx", trust_remote_code=False)
export_dynamic_quantized_onnx_model(
    model, quantization_config="avx2", model_name_or_path=str(root), file_suffix="quantized"
)
target = root / "onnx/model_quantized.onnx"
assert target.is_file()
(root / "quantization-manifest.json").write_text(
    json.dumps(
        {
            "format": "onnx-int8-avx2",
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "source_manifest_sha256": hashlib.sha256((root / "download-manifest.json").read_bytes()).hexdigest(),
        },
        indent=2,
    )
)
print("QUANTIZED", target.stat().st_size)
