"""Reuse the established atomic evidence writer, with no runtime imports."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval.qq_brief.common import canonical, digest, file_hash, read, save, source_hashes  # noqa: E402,F401
