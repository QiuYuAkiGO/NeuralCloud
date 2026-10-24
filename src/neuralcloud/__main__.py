from pathlib import Path
import argparse
import json

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

                uvicorn.run(
                    create_app(store, args.token_file.read_text().strip()), host="127.0.0.1", port=args.port, workers=1
                )
        finally:
            store.close()


if __name__ == "__main__":
    main()
