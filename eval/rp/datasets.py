"""Normalize CharacterEval / RoleEval / TimeChara dumps into RPEpisode."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from eval.rp.fetch import hint_for
from eval.rp.paths import BENCH_DIRS
from eval.rp.persona import render_profile
from eval.rp.schema import DatasetMissingError, RPEpisode, Turn

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_NOW = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)

DEFAULT_CE_METRICS: list[tuple[str, str]] = [
    ("fluency", "流畅性"),
    ("coherency", "连贯性"),
    ("consistency", "一致性"),
    ("knowledge_exposure", "知识暴露"),
    ("knowledge_accuracy", "知识准确性"),
    ("knowledge_hallucination", "知识幻觉"),
    ("persona_behavior", "行为"),
    ("persona_utterance", "话语"),
    ("empathy", "共情"),
    ("human_likeness", "拟人"),
    ("engagement", "吸引力"),
    ("diversity", "多样性"),
    ("personality", "人格回溯"),
]

CATEGORY_ZH = {
    "celebrities": "名人",
    "anime_and_comics": "动漫",
    "movies_and_tv_series": "影视",
    "games": "游戏角色",
    "fiction": "小说",
}

_PERIOD_RE = re.compile(
    r"(?:book|year|册)?\s*(\d+)\s*[-_ ]*(?:chapter|chap|ch|章)?\s*(\d+)?",
    re.I,
)


def load_bench(bench: str, root: Path | None = None, *, limit: int | None = None) -> list[RPEpisode]:
    if bench not in BENCH_DIRS:
        raise ValueError(f"unsupported bench: {bench}")
    data_root = root or BENCH_DIRS[bench]
    if bench == "charactereval":
        episodes = load_charactereval(data_root)
    elif bench == "roleeval":
        episodes = load_roleeval(data_root)
    else:
        episodes = load_timechara(data_root)
    if limit is not None:
        return episodes[: max(0, limit)]
    return episodes


def load_charactereval(root: Path) -> list[RPEpisode]:
    data_path = _first_existing(
        root,
        "test_data.jsonl",
        "test_data.json",
        "data/test_data.jsonl",
        "data/test_data.json",
    )
    profile_path = _first_existing(
        root,
        "character_profiles.json",
        "data/character_profiles.json",
    )
    metric_path = _first_existing(
        root,
        "id2metric.jsonl",
        "id2metric.json",
        "data/id2metric.jsonl",
        "data/id2metric.json",
        required=False,
    )
    if data_path is None:
        raise DatasetMissingError("charactereval", hint_for("charactereval"))
    rows = _as_records(read_json_blob(data_path))
    if not rows:
        raise DatasetMissingError("charactereval", hint_for("charactereval"))
    profiles_raw = read_json_blob(profile_path) if profile_path else {}
    profiles = profiles_raw if isinstance(profiles_raw, dict) else {}
    id2metric = read_json_blob(metric_path) if metric_path else {}
    if not isinstance(id2metric, dict):
        id2metric = {}
    episodes: list[RPEpisode] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or row.get("character") or row.get("name") or f"role_{index}")
        profile = _flatten_profile(profiles.get(role, row.get("profile") or {}))
        turns = _character_eval_turns(row)
        if not turns:
            continue
        query_turn = turns[-1]
        history = turns[:-1]
        eid = str(row.get("id", index))
        episodes.append(
            RPEpisode(
                bench="charactereval",
                episode_id=f"ce_{eid}",
                role_id=role,
                profile=profile or {"姓名": role},
                history=history,
                query=_format_named_line(query_turn),
                meta={
                    "source_id": eid,
                    "context": row.get("context") or "",
                    "novel": row.get("novel"),
                    "metrics": _metrics_for(eid, id2metric),
                    "profile_text": render_profile(profile) or str(profiles.get(role, "")),
                },
            )
        )
    return episodes


def load_roleeval(root: Path) -> list[RPEpisode]:
    csv_files = sorted(root.rglob("*.csv")) if root.exists() else []
    json_files = [
        path
        for path in (list(root.rglob("*.jsonl")) + list(root.rglob("*.json")))
        if path.exists() and path.name != "answers.json"
    ]
    episodes: list[RPEpisode] = []
    if csv_files:
        for path in csv_files:
            episodes.extend(_load_roleeval_csv(path))
    for path in json_files:
        blob = read_json_blob(path)
        for index, row in enumerate(_as_records(blob)):
            if isinstance(row, dict):
                episodes.append(_roleeval_from_row(row, index=index, source=path))
    if not episodes:
        raise DatasetMissingError("roleeval", hint_for("roleeval"))
    return episodes


def load_timechara(root: Path) -> list[RPEpisode]:
    files = []
    if root.exists():
        files = sorted(p for p in root.rglob("*") if p.suffix in {".json", ".jsonl"})
    if not files:
        raise DatasetMissingError("timechara", hint_for("timechara"))
    episodes: list[RPEpisode] = []
    for path in files:
        blob = read_json_blob(path)
        for index, row in enumerate(_as_records(blob)):
            if isinstance(row, dict):
                episodes.append(_timechara_from_row(row, index=index, source=path))
    if not episodes:
        raise DatasetMissingError("timechara", hint_for("timechara"))
    return episodes


def read_json_blob(path: Path) -> Any:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    rows: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def parse_story_time(period: str | None, *, fallback: datetime | None = None) -> datetime:
    base = fallback or DEFAULT_NOW
    if not period or period in {"-", "None", "none"}:
        return base
    match = _PERIOD_RE.search(period.replace("–", "-"))
    if not match:
        return base
    book = int(match.group(1) or 1)
    chapter = int(match.group(2) or 1)
    return datetime(1990 + book, 1, 1, tzinfo=TZ) + timedelta(days=max(0, chapter - 1) * 7)


def format_roleeval_query(question: str, choices: dict[str, str], *, category: str) -> str:
    header = f"以下是关于{category}的单项选择题，请选出其中的正确答案。"
    lines = [header, question]
    for letter in ("A", "B", "C", "D"):
        if letter in choices:
            lines.append(f"{letter}. {choices[letter]}")
    lines.append("答案：")
    return "\n".join(lines)


def _load_roleeval_csv(path: Path) -> list[RPEpisode]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    subset, category, split = _roleeval_location(path)
    episodes: list[RPEpisode] = []
    for index, row in enumerate(rows):
        episodes.append(
            _roleeval_from_row(
                row,
                index=index,
                source=path,
                subset=subset,
                category=category,
                split=split,
            )
        )
    return episodes


def _roleeval_from_row(
    row: dict[str, Any],
    *,
    index: int,
    source: Path,
    subset: str | None = None,
    category: str | None = None,
    split: str | None = None,
) -> RPEpisode:
    subset = subset or str(row.get("subset") or "")
    category_key = category or str(row.get("category") or source.stem)
    for suffix in ("_test", "_dev", "_train"):
        if category_key.endswith(suffix):
            category_key = category_key[: -len(suffix)]
    category_zh = CATEGORY_ZH.get(category_key, category_key)
    split = split or str(row.get("split") or "")
    eid = str(row.get("id", index))
    question = str(row.get("question") or row.get("query") or "")
    choices = {
        letter: str(row[letter]).strip()
        for letter in ("A", "B", "C", "D")
        if letter in row and str(row[letter]).strip()
    }
    if not choices and isinstance(row.get("choices"), dict):
        choices = {str(k): str(v) for k, v in row["choices"].items()}
    answer = row.get("answer") or row.get("gold") or row.get("label")
    answer_s = str(answer).strip().upper()[:1] if answer not in (None, "") else None
    if answer_s and answer_s not in {"A", "B", "C", "D"}:
        answer_s = None
    role_id = str(row.get("role") or row.get("character") or category_zh or "roleeval")
    return RPEpisode(
        bench="roleeval",
        episode_id=f"re_{subset or 'x'}_{category_key}_{eid}",
        role_id=role_id,
        profile={"姓名": role_id, "category": category_zh},
        history=[],
        query=format_roleeval_query(question, choices, category=category_zh),
        choices=choices or None,
        answer=answer_s,
        meta={
            "source_id": eid,
            "category": category_zh,
            "category_key": category_key,
            "subset": subset,
            "split": split,
            "source": str(source),
            "question": question,
        },
    )


def _timechara_from_row(row: dict[str, Any], *, index: int, source: Path) -> RPEpisode:
    character = str(row.get("character") or row.get("role") or f"character_{index}")
    period = str(row.get("character_period") or row.get("period") or "")
    question_period = str(row.get("question_period") or "")
    data_type = str(row.get("data_type") or row.get("type") or "")
    story_time = parse_story_time(period)
    event_time = parse_story_time(question_period, fallback=story_time)
    if data_type == "future" and event_time <= story_time:
        event_time = story_time + timedelta(days=365)
    if data_type.startswith("past") and event_time >= story_time:
        event_time = story_time - timedelta(days=30)
    question = str(row.get("question") or row.get("query") or "")
    series = str(row.get("series") or "")
    choices = row.get("choices") if isinstance(row.get("choices"), dict) else None
    answer = row.get("answer")
    answer_s = str(answer).strip() if answer not in (None, "") else None
    return RPEpisode(
        bench="timechara",
        episode_id=f"tc_{index}_{_slug(character)}",
        role_id=character,
        profile={"name": character, "series": series},
        history=[],
        query=question,
        choices={str(k): str(v) for k, v in choices.items()} if choices else None,
        answer=answer_s,
        story_time=story_time,
        meta={
            "source": str(source),
            "series": series,
            "data_type": data_type,
            "question_generation": row.get("question_generation"),
            "event_summary": row.get("event_summary") or "",
            "question_period": question_period,
            "character_period": period,
            "fake_method": row.get("fake_method"),
            "participants": row.get("participants") or "",
            "temporal_label": row.get("temporal_label"),
            "spatial_label": row.get("spatial_label"),
            "gold_response": row.get("gold_response") or "",
            "event_time": event_time.isoformat(),
        },
    )


def _character_eval_turns(row: dict[str, Any]) -> list[Turn]:
    conversations = row.get("conversations") or row.get("dialogue") or row.get("history")
    if isinstance(conversations, list) and conversations:
        turns: list[Turn] = []
        for item in conversations:
            if isinstance(item, dict):
                speaker = str(item.get("from") or item.get("speaker") or item.get("role") or "user")
                text = str(item.get("value") or item.get("text") or item.get("content") or "")
                if text:
                    turns.append(Turn(speaker=speaker, text=text))
        return turns
    context = str(row.get("context") or "")
    return parse_context_lines(context)


def parse_context_lines(context: str) -> list[Turn]:
    turns: list[Turn] = []
    for raw in context.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "：" in line:
            speaker, text = line.split("：", 1)
        elif ":" in line:
            speaker, text = line.split(":", 1)
        else:
            if turns:
                turns[-1].text += "\n" + line
            continue
        speaker = speaker.strip()
        text = text.strip()
        if speaker or text:
            turns.append(Turn(speaker=speaker, text=text))
    return turns


def _format_named_line(turn: Turn) -> str:
    if turn.speaker in {"user", "character"}:
        return turn.text
    return f"{turn.speaker}：{turn.text}"


def _flatten_profile(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {"text": raw}
    if isinstance(raw, dict):
        flat: dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                flat[key] = "；".join(f"{ik}:{_scalar(iv)}" for ik, iv in value.items())
            elif isinstance(value, list):
                flat[key] = "、".join(_scalar(item) for item in value)
            else:
                flat[key] = value
        return flat
    return {"text": str(raw)}


def _scalar(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _metrics_for(eid: str, id2metric: dict[str, Any]) -> list[list[str]]:
    raw = id2metric.get(str(eid), id2metric.get(eid))
    if not raw:
        return [list(pair) for pair in DEFAULT_CE_METRICS]
    out: list[list[str]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append([str(item[0]), str(item[1])])
        elif isinstance(item, dict):
            en = item.get("metric_en") or item.get("en")
            zh = item.get("metric_zh") or item.get("zh")
            if en and zh:
                out.append([str(en), str(zh)])
    return out or [list(pair) for pair in DEFAULT_CE_METRICS]


def _roleeval_location(path: Path) -> tuple[str, str, str]:
    parts = [part.lower() for part in path.parts]
    subset = "chinese" if "chinese" in parts else ("global" if "global" in parts else "")
    split = "dev" if "dev" in parts else ("test" if "test" in parts else "")
    stem = path.stem
    for suffix in ("_test", "_dev", "_train"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return subset, stem, split


def _first_existing(root: Path, *relatives: str, required: bool = True) -> Path | None:
    for rel in relatives:
        candidate = root / rel
        if candidate.exists():
            return candidate
    if required:
        return None
    return None


def _as_records(blob: Any) -> Iterable[Any]:
    if blob is None:
        return []
    if isinstance(blob, list):
        return blob
    if isinstance(blob, dict):
        for key in ("data", "test", "validation", "rows", "items"):
            value = blob.get(key)
            if isinstance(value, list):
                return value
        return [blob]
    return []


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return slug or "role"
