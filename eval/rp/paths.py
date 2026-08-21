"""Repo-relative paths for offline RP eval data and reports."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTERNAL_DIR = REPO_ROOT / "eval" / "external"
REPORTS_DIR = REPO_ROOT / "eval" / "reports"
TRACES_DIR = REPORTS_DIR / "traces"

CE_DIR = EXTERNAL_DIR / "charactereval"
RE_DIR = EXTERNAL_DIR / "roleeval"
TC_DIR = EXTERNAL_DIR / "timechara"
CHARRM_DIR = EXTERNAL_DIR / "BaichuanCharRM"

BENCH_DIRS = {
    "charactereval": CE_DIR,
    "roleeval": RE_DIR,
    "timechara": TC_DIR,
}
