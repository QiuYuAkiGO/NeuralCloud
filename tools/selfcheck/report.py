"""Write JSON + Markdown self-check reports."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.selfcheck.common import FAIL_STATUSES, ProbeResult, ROOT


def summarize(results: list[ProbeResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def failed(results: list[ProbeResult]) -> list[ProbeResult]:
    return [item for item in results if item.status in FAIL_STATUSES]


def write_report(results: list[ProbeResult], *, destination: Path | None = None,
                 extra: dict[str, Any] | None = None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = destination or (ROOT / "logs" / "self-check" / stamp)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": summarize(results),
        "failed": [item.as_dict() for item in failed(results)],
        "results": [item.as_dict() for item in results],
        **(extra or {}),
    }
    (folder / "report.json").write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    (folder / "report.md").write_text(render_markdown(payload), encoding="utf-8")
    return folder


def render_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Misty 自检报告", "", f"生成时间：{payload.get('generated_at')}", "", "## 摘要", ""]
    summary = payload.get("summary") or {}
    for key, value in sorted(summary.items()):
        lines.append(f"- `{key}`: {value}")
    failed_rows = payload.get("failed") or []
    lines.extend(["", "## 失败项", ""])
    if not failed_rows:
        lines.append("无 fail / unavailable。")
    else:
        for item in failed_rows:
            lines.append(f"### {item['path']} (tier {item['tier']})")
            lines.append(f"{item.get('detail') or ''}")
            for hint in item.get("triage") or []:
                lines.append(f"- {hint}")
            lines.append("")
    lines.extend(["## 全表", "", "| 路径 | 层 | 状态 | 说明 |", "|---|---:|---|---|"])
    for item in payload.get("results") or []:
        detail = (item.get("detail") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{item['path']}` | {item['tier']} | `{item['status']}` | {detail} |")
    lines.append("")
    return "\n".join(lines)
