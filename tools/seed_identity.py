"""Offline maintenance: import a reviewed evidence manifest; no role endpoint."""

import argparse
import json
from pathlib import Path
from neuralcloud.storage import MemoryStore
from neuralcloud.retrieval import IdentityIndex

parser = argparse.ArgumentParser()
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--index", type=Path, required=True)
parser.add_argument("--manifest", type=Path, required=True)
parser.add_argument("--character", default="misty")
args = parser.parse_args()
store = MemoryStore(args.database, args.character)
index = IdentityIndex(args.index)
try:
    index.seed(json.loads(args.manifest.read_text()), store)
finally:
    index.close()
    store.close()
