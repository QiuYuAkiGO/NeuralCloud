"""Offline download guidance and optional fetch for RP benchmark files.

Internet is off during eval. Run this once, then point loaders at eval/external/.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from eval.rp.paths import CE_DIR, CHARRM_DIR, RE_DIR

CE_BASE = "https://raw.githubusercontent.com/morecry/CharacterEval/main/data"
RE_BASE = "https://raw.githubusercontent.com/Magnetic2014/RoleEval/main/zh"
USER_AGENT = "NeuralCloud-RPEval/0.1"

CE_FILES = (
    "test_data.jsonl",
    "character_profiles.json",
    "id2metric.jsonl",
)

RE_SUBSETS = ("chinese", "global")
RE_SPLITS = ("dev", "test")
RE_CATEGORIES = (
    "celebrities",
    "anime_and_comics",
    "movies_and_tv_series",
    "games",
    "fiction",
)

HINTS: dict[str, str] = {
    "charactereval": (
        "Place CharacterEval files under eval/external/charactereval/:\n"
        f"  {CE_BASE}/test_data.jsonl\n"
        f"  {CE_BASE}/character_profiles.json\n"
        f"  {CE_BASE}/id2metric.jsonl\n"
        "Or: python -m eval.rp.fetch --bench charactereval\n"
        "License: see https://github.com/morecry/CharacterEval"
    ),
    "roleeval": (
        "Place RoleEval CSVs under eval/external/roleeval/ "
        "(keep zh/{chinese,global}/{dev,test}/*.csv).\n"
        "Dev CSVs include gold answers; public test CSVs are unlabeled.\n"
        "Or: python -m eval.rp.fetch --bench roleeval\n"
        "Source: https://github.com/Magnetic2014/RoleEval"
    ),
    "timechara": (
        "TimeChara requires accepting the HuggingFace terms "
        "(non-commercial research only), then save JSON/JSONL under "
        "eval/external/timechara/ (e.g. validation.jsonl).\n"
        "  huggingface.co/datasets/ahnpersie/timechara\n"
        "  github.com/ahnjaewoo/timechara\n"
        "Export columns: character, question, data_type, character_period, "
        "question_period, event_summary, gold_response, series, participants, "
        "temporal_label, spatial_label."
    ),
    "characterrm": (
        "Download CharacterRM weights once, then keep them offline:\n"
        "  huggingface.co/morecry/BaichuanCharRM\n"
        f"Place the snapshot at {CHARRM_DIR}\n"
        "pip install -e \".[eval]\"  (torch + transformers)\n"
        "Judge process should share the WSL2 CUDA machine with the LLM server."
    ),
}


def hint_for(bench: str) -> str:
    return HINTS.get(bench, f"Unknown bench {bench}. Expected charactereval / roleeval / timechara.")


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        dest.write_bytes(response.read())


def fetch_charactereval(root: Path | None = None) -> list[Path]:
    dest_root = root or CE_DIR
    written: list[Path] = []
    for name in CE_FILES:
        dest = dest_root / name
        _download(f"{CE_BASE}/{name}", dest)
        written.append(dest)
    return written


def fetch_roleeval(root: Path | None = None) -> list[Path]:
    dest_root = root or RE_DIR
    written: list[Path] = []
    for subset in RE_SUBSETS:
        for split in RE_SPLITS:
            for category in RE_CATEGORIES:
                name = f"{category}_{split}.csv"
                url = f"{RE_BASE}/{subset}/{split}/{name}"
                dest = dest_root / "zh" / subset / split / name
                _download(url, dest)
                written.append(dest)
    return written


def fetch_bench(bench: str) -> list[Path]:
    if bench == "charactereval":
        return fetch_charactereval()
    if bench == "roleeval":
        return fetch_roleeval()
    if bench == "timechara":
        raise DatasetHintError(HINTS["timechara"])
    if bench == "characterrm":
        raise DatasetHintError(HINTS["characterrm"])
    raise ValueError(f"unsupported bench: {bench}")


class DatasetHintError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download RP eval files into eval/external/")
    parser.add_argument(
        "--bench",
        choices=["charactereval", "roleeval", "timechara", "characterrm", "all"],
        default="all",
    )
    args = parser.parse_args(argv)
    targets = ["charactereval", "roleeval"] if args.bench == "all" else [args.bench]
    for bench in targets:
        print(f"== {bench} ==")
        try:
            paths = fetch_bench(bench)
        except (DatasetHintError, URLError, OSError, ValueError) as exc:
            print(hint_for(bench))
            print(f"fetch skipped: {exc}")
            continue
        for path in paths:
            print(f"  wrote {path} ({path.stat().st_size} bytes)")
    if args.bench in {"all", "timechara", "characterrm"}:
        print(hint_for("timechara" if args.bench != "characterrm" else "characterrm"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
