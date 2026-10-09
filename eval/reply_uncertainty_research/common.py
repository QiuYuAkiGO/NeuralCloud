import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval.qq_brief.common import canonical, digest, file_hash, read, save, source_hashes  # noqa: E402,F401


def verify_frozen(root, arm="control"):
    manifest = read(root / "manifest.json")
    for name, expected in manifest["evaluator"].items():
        if file_hash(Path(__file__).with_name(name)) != expected:
            raise ValueError("frozen_evaluator_changed")
    for name, expected in manifest["shared_evaluator"].items():
        if file_hash(Path(__file__).resolve().parents[2] / name) != expected:
            raise ValueError("shared_evaluator_changed")
    for name, expected in manifest["inputs"].items():
        if file_hash(root / name) != expected:
            raise ValueError("frozen_input_changed: " + name)
    source = Path(manifest["sources"][arm]["root"])
    if source_hashes(source) != manifest["sources"][arm]["files"]:
        raise ValueError("frozen_source_changed")
    return source
