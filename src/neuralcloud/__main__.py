from pathlib import Path
import argparse
import json
import os

from .contracts import SourceEvent, Correction, Recall
from .storage import MemoryStore, initialize


def main():
    parser = argparse.ArgumentParser(description="Resident online memory; maintenance must be explicit")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "serve", "backup"):
        command = commands.add_parser(name)
        command.add_argument("--database", type=Path, required=True)
        command.add_argument("--character", required=True)
        if name == "serve":
            command.add_argument("--token-file", type=Path, required=True)
            command.add_argument("--port", type=int, default=8765)
        if name == "backup":
            command.add_argument("--target", type=Path, required=True)
    schema = commands.add_parser("export-contracts")
    schema.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.database, args.character)
    elif args.command == "export-contracts":
        args.target.mkdir(parents=True, exist_ok=True)
        for model in (SourceEvent, Correction, Recall):
            (args.target / (model.__name__ + ".v1.schema.json")).write_text(
                json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8"
            )
    else:
        store = MemoryStore(args.database, args.character)
        try:
            if args.command == "backup":
                store.backup(args.target)
            else:
                import uvicorn
                from .api import create_app

                retriever = None
                if os.environ.get("NC_RETRIEVAL_V2") == "1":
                    from .retrieval import Retriever, IdentityIndex
                    from .semantic import SemanticClient

                    identity_path = os.environ.get("NC_IDENTITY_INDEX")
                    semantic_url = os.environ.get("NC_SEMANTIC_URL")
                    semantic_token = (
                        Path(os.environ["NC_SEMANTIC_TOKEN_FILE"]).read_text().strip() if semantic_url else None
                    )
                    if identity_path and not Path(identity_path).is_file():
                        raise ValueError("IDENTITY_INDEX_MISSING")
                    database_id = store.db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
                    retriever = Retriever(
                        store,
                        IdentityIndex(identity_path) if identity_path else None,
                        SemanticClient(semantic_url, database_id, semantic_token) if semantic_url else None,
                        hybrid=os.environ.get("NC_HYBRID_ENABLED") == "1",
                        threshold=float(os.environ.get("NC_SEMANTIC_THRESHOLD", "0")),
                        keyword_threshold=float(os.environ.get("NC_KEYWORD_THRESHOLD", "0")),
                    )

                if retriever is not None and os.environ.get("NC_MEMORY_WORKFLOW") == "1":
                    from .evidence_index import EvidenceIndex
                    from .workflow import MemoryWorkflow

                    index = EvidenceIndex(args.database, Path(os.environ["NC_EVIDENCE_INDEX"]))
                    retriever = MemoryWorkflow(
                        retriever, index, deadline_ms=int(os.environ.get("NC_WORKFLOW_DEADLINE_MS", "1800"))
                    )

                uvicorn.run(
                    create_app(store, args.token_file.read_text().strip(), retriever=retriever),
                    host="127.0.0.1",
                    port=args.port,
                    workers=1,
                )
        finally:
            store.close()


if __name__ == "__main__":
    main()
