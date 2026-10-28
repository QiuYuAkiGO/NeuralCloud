"""Opt-in service; authenticated partitions never come from request JSON."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
from threading import Lock

from fastapi import FastAPI, Header, HTTPException
from .contracts import ChatRequest, ReadContext
from .sources import HyMemorySource, HySubprocess, NeuralCloudSource, open_neuralcloud


def create_app(engine, sources, principals):
    if not principals or any(not token for token in principals):
        raise ValueError("explicit nonempty bearer credentials required")
    app, lock = FastAPI(title="NeuralCloud Memory Encoder"), Lock()

    @app.get("/health")
    def health():
        return {"status": "ready", "providers": sorted(sources)}

    @app.post("/v1/memory/chat")
    def chat(request: ChatRequest, authorization: str = Header(default="")):
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        context = next((value for key, value in principals.items() if secrets.compare_digest(key, token)), None)
        if context is None:
            raise HTTPException(401, "authentication required")
        source = sources.get(request.memory.provider)
        if source is None:
            raise HTTPException(422, "memory provider is not configured")
        with lock:
            context = context.model_copy(update={"now": datetime.now(timezone.utc)})
            try:
                bundle = source.read(request.memory, context)
                if not source.validate(bundle, context):
                    raise HTTPException(409, "memory changed before encoding; retry")
                response = engine([m.model_dump() for m in request.messages], bundle, request.max_new_tokens)
                current = context.model_copy(update={"now": datetime.now(timezone.utc)})
                if not source.validate(bundle, current):
                    raise HTTPException(409, "memory changed during generation; retry")
                return response
            except ValueError:
                raise HTTPException(422, "invalid memory or conversation budget") from None
            except (RuntimeError, TimeoutError, subprocess.SubprocessError):
                raise HTTPException(503, "memory runtime unavailable") from None
    return app


def configured_app(config_path, checkpoint, config):
    from .checkpoints import load_checkpoint, require_acceptance
    from .runtime import generate, load_model
    require_acceptance(checkpoint)
    settings = json.loads(Path(config_path).read_text(encoding="utf-8"))
    sources, principals = {}, {}
    if item := settings.get("neuralcloud"):
        sources["neuralcloud"] = NeuralCloudSource(open_neuralcloud(**item))
    if item := settings.get("hy_memory"):
        sources["hy_memory"] = HyMemorySource(
            HySubprocess(item["python"], item["config"], item["checkout"]), timezone=item["timezone"])
    for item in settings["principals"]:
        token = os.environ[item["token_env"]]
        if token in principals:
            raise ValueError("duplicate service credential")
        principals[token] = ReadContext.model_validate(item["context"])
    model, tokenizer = load_model(config)
    load_checkpoint(model, checkpoint)
    return create_app(lambda messages, bundle, maximum: generate(model, tokenizer, messages, bundle,
                      max_new_tokens=maximum), sources, principals)
