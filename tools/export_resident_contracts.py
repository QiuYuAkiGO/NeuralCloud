"""Generate owner contracts and TypeScript declarations; never hand-maintain DTO copies."""

import argparse
import json
from pathlib import Path

from pydantic import BaseModel


def ts_type(schema):
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        return " | ".join(ts_type(item) for item in schema["anyOf"])
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    if "enum" in schema:
        return " | ".join(json.dumps(item, ensure_ascii=False) for item in schema["enum"])
    kind = schema.get("type")
    if kind == "array":
        return "(" + ts_type(schema["items"]) + ")[]"
    if kind == "object":
        fields = []
        for name, child in schema.get("properties", {}).items():
            optional = "" if name in schema.get("required", []) else "?"
            fields.append(json.dumps(name) + optional + ": " + ts_type(child) + ";")
        additional = schema.get("additionalProperties", False)
        if additional:
            fields.append(
                "[key: string]: " + (ts_type(additional) if isinstance(additional, dict) else "unknown") + ";"
            )
        return "{ " + " ".join(fields) + " }" if fields else "Record<string, never>"
    if kind in {"integer", "number"}:
        return "number"
    if kind in {"string", "boolean", "null"}:
        return kind
    if not schema:
        return "unknown"
    raise ValueError("Unsupported generated schema: " + str(schema))


def export(modules, output, types_path, *, check=False, openapi=None):
    definitions = {}
    artifacts = {}
    for module in modules:
        for name, model in vars(module).items():
            if not isinstance(model, type) or not issubclass(model, BaseModel) or name == "Contract":
                continue
            if model.__module__ != module.__name__:
                continue
            schema = model.model_json_schema()
            definitions.update(schema.get("$defs", {}))
            definitions[name] = schema
            artifacts[output / (name + ".v1.schema.json")] = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    declarations = "// Generated from owner Pydantic schemas. Run tools/export_resident_contracts.py.\n"
    declarations += "// Runtime bounds and authorization remain enforced by the owner service.\n"
    for name, schema in sorted(definitions.items()):
        declarations += "export type " + name + " = " + ts_type(schema) + ";\n"
    artifacts[types_path] = declarations
    if openapi is not None:
        artifacts[output / "openapi.v1.json"] = json.dumps(openapi, indent=2, ensure_ascii=False) + "\n"
    for path, text in artifacts.items():
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                raise SystemExit("Generated contract drift: " + str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    print(json.dumps({"generated_contracts": len(artifacts), "check": check}))


if __name__ == "__main__":
    from neuralcloud import archive, contracts
    from neuralcloud.api import create_app

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    export(
        [contracts, archive],
        Path("contracts/resident/v1"),
        Path("plugins/dsh-neuralcloud/src/contracts.d.ts"),
        check=args.check,
        openapi=create_app(None, "schema-export-only" * 2).openapi(),
    )
