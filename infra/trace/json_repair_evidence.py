"""Private, bounded evidence for model JSON anomalies; no request or reasoning capture."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from infra.trace.request_spans import current_trace_context

logger = logging.getLogger(__name__)
_NAME = re.compile(r"repair-[0-9a-f]{32}\.json$")


def record_repair(settings, original, effective, schema, actions, error, metadata=None):
    if not settings.json_repair_evidence_enabled:
        return {"evidence_status": "disabled"}
    root = Path(settings.trace_dir) / "json-repair"
    temp = None
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink():
            raise OSError("evidence_directory_symlink")
        root.chmod(0o700)
        raw = original.encode("utf-8")
        limit = 1024 * 1024
        context = current_trace_context()
        context.update(metadata or {})
        allowed = ("correlation_id", "event_id", "command_id", "span_id", "response_id", "phase", "qq_trace_id")
        record = {"schema_version": "json-repair-evidence-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
                  "original": raw[:limit].decode("utf-8", errors="replace"), "original_bytes": len(raw),
                  "original_sha256": hashlib.sha256(raw).hexdigest(), "original_truncated": len(raw) > limit,
                  "effective": effective, "actions": actions, "schema": schema,
                  "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                  "context": {k: context[k] for k in allowed if context.get(k) is not None},
                  "error_code": getattr(error, "error_code", "json_output_deadline" if isinstance(error, TimeoutError) else None),
                  "diagnostics": getattr(error, "diagnostics", [])}
        name = "repair-" + uuid4().hex + ".json"
        temp = root / (name + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
        os.replace(temp, root / name)
        now = time.time()
        entries = []
        for path in root.iterdir():
            if not _NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
                if now - stat.st_mtime > settings.json_repair_retention_days * 86400:
                    path.unlink()
                else:
                    entries.append((stat.st_mtime, stat.st_size, path))
            except FileNotFoundError:
                pass
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= settings.json_repair_evidence_max_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size
        return {"evidence_status": "saved", "evidence_ref": name}
    except (OSError, ValueError, TypeError):
        logger.warning("json_repair_evidence_write_failed")
        return {"evidence_status": "failed"}
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
