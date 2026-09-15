"""Read-only, provenance-preserving export of retained Misty replies.

Production state directories are explicit: test/eval folders are never crawled.
Later --state-root values supersede earlier snapshots by stable source primary
key. A reply is one decision/turn, with delivered segments kept separate from
unconfirmed generated text. No network or service control is performed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def iso_seconds(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return 0.0


def unique(items):
    return list(dict.fromkeys(items))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def source(path, table=None, key=None):
    return {"path": str(path.resolve()), "table": table, "key": key}


def fetch(path, table):
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return []
        rows = [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]
    return rows


def model_generated(text):
    return isinstance(text, str) and bool(text.strip())


def join_segments(parts):
    return "\n\n".join(x for x in parts if isinstance(x, str) and x.strip())


def delivery_evidence(value, kind):
    names = ("receipt_id", "decision_id", "segment_index", "segment_count", "verification",
             "protocol_message_id", "protocol_message_ids", "segment_receipt_ids", "origin",
             "delivered_at", "evidence_refs", "message_id", "send_id", "status", "external_ref")
    return {"kind": kind, **{k: value[k] for k in names if k in value}, "source": value.get("_source")}


def channel_for(group):
    return "qq_private" if str(group).startswith("private:") else "qq_group"


def context_item(value, own_id=None):
    role = value.get("role", "user")
    if role == "character" or own_id and str(value.get("member_id")) == str(own_id):
        role = "assistant"
    return {
        "id": value.get("record_id") or value.get("event_id"),
        "timestamp": value.get("observed_at") or value.get("message_time") or value.get("timestamp"),
        "speaker": value.get("member_id") or value.get("speaker"),
        "role": role,
        "text": value.get("text", ""),
        "image_analysis": value.get("image_analysis", []),
        "source": value.get("_source"),
    }


def run(roots, output_dir, trace_root=None):
    stores = {name: {} for name in ("decisions", "deliveries", "segments", "observations", "events", "sends", "boards", "turns")}
    mapping = (
        ("qq-session/qq-visual-memory.sqlite3", "qq_reply_decisions", "decisions", "decision_id"),
        ("qq-session/qq-visual-memory.sqlite3", "qq_reply_deliveries", "deliveries", "receipt_id"),
        ("qq-session/qq-visual-memory.sqlite3", "qq_reply_segment_deliveries", "segments", "receipt_id"),
        ("qq-session/qq-visual-memory.sqlite3", "qq_visual_observations", "observations", "record_id"),
        ("qq-direct/direct.sqlite3", "qq_direct_events", "events", "event_id"),
        ("qq-direct/direct.sqlite3", "qq_direct_sends", "sends", "send_id"),
        ("board/board.sqlite3", "board_entries", "boards", "entry_id"),
        ("runtime/board-turns.sqlite3", "turns", "turns", "entry_id"),
    )
    inventory = {"schema_version": "misty-history-inventory-v1", "created_at": datetime.now(timezone.utc).isoformat(),
                 "state_roots": [str(p.resolve()) for p in roots], "sources": [], "exclusions": {}, "notes": []}
    prepared = {}
    sessions = {}
    journal_deliveries = {}
    journal_events = {}
    journal_prepared = {}
    api_runs = {}
    for root in roots:
        path = root / "runs.json"
        if path.exists():
            for run_record in read_json(path):
                stages = {v.get("stage"): v.get("data", {}) for v in run_record.get("stages", [])}
                persona = stages.get("state_build", {}).get("snapshot", {}).get("persona", {})
                if "misty" in str(persona.get("persona_id", "")).lower():
                    api_runs[run_record.get("event_id")] = {
                        "final": stages.get("final", {}), "event": stages.get("ingest", {}),
                        "_source": source(path, "runs", run_record.get("run_id")),
                    }
        for relative, table, name, key in mapping:
            path = root / relative
            rows = fetch(path, table)
            if not path.exists():
                continue
            inventory["sources"].append({"path": str(path.resolve()), "table": table, "rows": len(rows)})
            for row in rows:
                record = json.loads(row["record_json"]) if "record_json" in row else row.copy()
                record["_source"] = source(path, table, row[key])
                record["_row_status"] = row.get("status")
                stores[name][row[key]] = record
        path = root / "cognitive/state.json"
        if path.exists():
            value = read_json(path)
            inventory["sources"].append({"path": str(path.resolve()), "prepared": len(value.get("prepared", {})),
                                         "session_messages": sum(len(x) for x in value.get("sessions", {}).values())})
            for key, entry in value.get("prepared", {}).items():
                prepared[key] = {**entry, "_source": source(path, "prepared", key)}
            for sid, messages in value.get("sessions", {}).items():
                for message in messages:
                    sessions[(sid, message.get("event_id"))] = {**message, "_source": source(path, "sessions." + sid, message.get("event_id"))}
        for path in sorted((root / "runtime/journal").glob("*.jsonl")):
            records = 0
            with path.open(encoding="utf-8-sig") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    records += 1
                    if value.get("record_type") == "reply_delivery":
                        value["_source"] = source(path, "reply_delivery", value.get("record_seq"))
                        journal_deliveries[value["data"].get("prepare_id") or value.get("event_id")] = value
                    elif value.get("record_type") == "event_accepted":
                        event = value.get("data", {}).get("event", {})
                        if event.get("event_type") == "user_message":
                            journal_events[value.get("event_id")] = event
                    elif value.get("record_type") == "domain_command_result":
                        envelope = value.get("data", {}).get("result") or {}
                        if envelope.get("command_type") == "PrepareResponse" and envelope.get("status") == "applied":
                            prep = envelope.get("result", {})
                            if (prep.get("session_id") == "sess_misty" or prep.get("event_id") in api_runs) and prep.get("prepare_id"):
                                journal_prepared[prep["prepare_id"]] = {
                                    **prep, "_source": source(path, "domain_command_result", value.get("record_seq"))}
                        elif envelope.get("command_type") == "PrepareQQResponse" and envelope.get("status") == "applied":
                            decision = (envelope.get("result") or {}).get("decision") or {}
                            if decision.get("decision_id"):
                                # Prefer the source DB when present; journal supplies
                                # recently generated decisions absent from an old snapshot.
                                stores["decisions"].setdefault(decision["decision_id"], {
                                    **decision, "_source": source(path, "domain_command_result", value.get("record_seq"))})
            inventory["sources"].append({"path": str(path.resolve()), "records": records})

    # Monitor API journal exports do not expose cognitive/state.json, but carry
    # both the scoped PrepareResponse result and its ingress event.
    for pid, prep in journal_prepared.items():
        if pid in prepared:
            continue
        decision = prep.get("decision", {})
        if not model_generated(decision.get("text")) or prep.get("model_version") == "mock":
            continue
        ingress = journal_events.get(prep.get("event_id"), {})
        payload = ingress.get("payload", {})
        prepared[pid] = {
            "prepared": prep,
            "event": {"session_id": prep["session_id"], "event_id": prep.get("event_id"),
                      "raw_text": payload.get("text", ""), "channel": payload.get("channel", "chat"),
                      "timestamp": ingress.get("source_time") or ingress.get("received_at") or prep.get("prepared_at")},
            "final": api_runs.get(prep.get("event_id"), {}).get("final") or {
                "response_id": "runtime-prepare:" + pid, "text": decision["text"],
                "segments": decision.get("segments") or [decision["text"]]},
            "_source": prep["_source"],
            "_misty_verified": prep.get("event_id") in api_runs,
            "_persona_source": api_runs.get(prep.get("event_id"), {}).get("_source"),
        }
    inventory["journal_prepared_responses"] = len(journal_prepared)

    decisions, deliveries, segments = stores["decisions"], stores["deliveries"], stores["segments"]
    observations, events = stores["observations"], stores["events"]
    deliveries_by_decision = defaultdict(list)
    segments_by_decision = defaultdict(list)
    sends_by_decision = defaultdict(list)
    for value in deliveries.values():
        deliveries_by_decision[value.get("decision_id")].append(value)
    for value in segments.values():
        segments_by_decision[value["decision_id"]].append(value)
    for value in stores["sends"].values():
        sends_by_decision[value["decision_id"]].append(value)
    records = []
    covered_protocol_messages = set()
    delivered_text_index = defaultdict(list)
    for decision_id, decision in decisions.items():
        if decision.get("preset_id") != "misty":
            inventory["exclusions"]["qq_other_preset"] = inventory["exclusions"].get("qq_other_preset", 0) + 1
            continue
        planned_segments = decision.get("reply_segments") or ([decision["reply"]] if decision.get("reply") else [])
        generated = decision.get("reply") or join_segments(planned_segments)
        if not model_generated(generated):
            inventory["exclusions"]["qq_no_reply_wait"] = inventory["exclusions"].get("qq_no_reply_wait", 0) + 1
            continue
        whole = sorted(deliveries_by_decision[decision_id], key=lambda v: iso_seconds(v.get("delivered_at")))
        parts = sorted(segments_by_decision[decision_id], key=lambda v: v["segment_index"])
        direct = sends_by_decision[decision_id]
        evidence = [delivery_evidence(v, "reply_receipt") for v in whole] + [delivery_evidence(v, "segment_receipt") for v in parts]
        evidence += [delivery_evidence(v, "direct_send") for v in direct]
        if whole:
            reply = whole[-1]["delivered_text"]
            reply_segments = whole[-1].get("delivered_segments") or [p["delivered_text"] for p in parts] or [reply]
            status = "confirmed_delivered"
            stamp = whole[-1]["delivered_at"]
        elif parts:
            reply_segments = [v["delivered_text"] for v in parts]
            reply = join_segments(reply_segments)
            status = "confirmed_partial"
            stamp = max((p["delivered_at"] for p in parts), key=iso_seconds)
        else:
            reply, reply_segments = generated, planned_segments
            status = "generated_unconfirmed"
            stamp = decision.get("completed_at") or decision.get("created_at")
        group, self_id = decision.get("group_id"), decision.get("self_member_id")
        refs = unique(decision.get("source_record_ids", []) + decision.get("new_observation_record_ids", []))
        context = [context_item(observations[key], self_id) for key in refs if key in observations]
        context.sort(key=lambda v: (iso_seconds(v.get("timestamp")), str(v.get("id"))))
        new_refs = set(decision.get("new_observation_record_ids", []))
        missing_new_refs = sorted(new_refs - observations.keys())
        triggers = [x for x in context if x["id"] in new_refs and x["role"] != "assistant"]
        effective_query = decision.get("effective_query") if isinstance(decision.get("effective_query"), str) else ""
        if not triggers and not effective_query and not new_refs:
            triggers = [x for x in context if x.get("speaker") == decision.get("trigger_member_id")][-1:]
        user_text = "\n".join(x["text"] for x in triggers)
        use_effective_query = bool(effective_query) and (bool(missing_new_refs) or not user_text)
        if use_effective_query:
            user_text = effective_query
        record = {
            "id": decision_id, "timestamp": stamp, "generated_at": decision.get("created_at"),
            "channel": channel_for(group), "scope": group, "user": user_text,
            "trigger_messages": triggers, "context": context, "reply": reply, "reply_segments": reply_segments,
            "generated_reply": generated, "generated_segments": planned_segments, "delivery_status": status,
            "attribution": "misty_model", "model": decision.get("model"),
            "persona_version": decision.get("persona_version"), "prompt_version": decision.get("prompt_version"),
            "delivery_evidence": evidence, "sources": [decision["_source"]],
            "missing_context_ids": [key for key in refs if key not in observations],
            "user_text_source": "decision_effective_query" if use_effective_query else "trigger_observations" if triggers else "unavailable",
            "decision_effective_query": effective_query,
            "trigger_observations_status": "partial" if missing_new_refs else "complete" if new_refs else "no_new_observations",
            "missing_new_observation_ids": missing_new_refs,
        }
        records.append(record)
        for value in direct + parts + whole:
            mids = value.get("protocol_message_ids", []) + [value.get("protocol_message_id"), value.get("message_id")]
            covered_protocol_messages.update((str(group), str(mid)) for mid in mids if mid is not None)
        for value in parts + whole:
            for text in value.get("delivered_segments", []) or [value["delivered_text"]]:
                delivered_text_index[(str(group), text)].append((iso_seconds(value.get("delivered_at")), record))

    # A historical event is evidence of account output, not automatically model authorship.
    unattributed = 0
    matched_echoes = 0
    for event in sorted(events.values(), key=lambda v: iso_seconds(v.get("message_time"))):
        if not event.get("own") or not model_generated(event.get("text")):
            continue
        key = (str(event.get("group_id")), str(event.get("message_id")))
        if key in covered_protocol_messages:
            continue
        stamp = iso_seconds(event.get("message_time"))
        matches = [record for when, record in delivered_text_index[(key[0], event["text"])] if abs(when - stamp) <= 120]
        if len(matches) == 1:
            matches[0]["delivery_evidence"].append({"kind": "matching_protocol_history_echo", "message_id": event.get("message_id"),
                                                     "timestamp": event.get("message_time"), "source": event["_source"]})
            covered_protocol_messages.add(key)
            matched_echoes += 1
            continue
        peers = [v for v in events.values() if v.get("group_id") == event.get("group_id")
                 and iso_seconds(v.get("message_time")) < stamp]
        peers.sort(key=lambda v: iso_seconds(v.get("message_time")))
        context = [context_item(v, event.get("self_member_id")) for v in peers[-12:]]
        records.append({"id": "qq-account-history:" + hashlib.sha256("|".join(key).encode()).hexdigest()[:24],
                        "timestamp": event.get("message_time"), "channel": channel_for(event.get("group_id")),
                        "scope": event.get("group_id"), "user": "", "trigger_messages": [], "context": context,
                        "reply": event["text"], "reply_segments": [event["text"]], "delivery_status": "account_history_unattributed",
                        "attribution": "account_output_model_authorship_unconfirmed", "model": None,
                        "delivery_evidence": [{"kind": "own_protocol_history", "message_id": event.get("message_id"),
                                               "evidence_ref": event.get("event_evidence_ref"), "source": event["_source"]}],
                        "sources": [event["_source"]], "missing_context_ids": []})
        unattributed += 1
    for receipt in deliveries_by_decision[None]:
        refs = receipt.get("source_record_ids", [])
        context = [context_item(observations[k], receipt.get("self_member_id")) for k in refs if k in observations]
        records.append({"id": receipt["receipt_id"], "timestamp": receipt["delivered_at"], "channel": channel_for(receipt.get("group_id")),
                        "scope": receipt.get("group_id"), "user": "\n".join(x["text"] for x in context),
                        "trigger_messages": context, "context": context, "reply": receipt["delivered_text"],
                        "reply_segments": receipt.get("delivered_segments") or [receipt["delivered_text"]],
                        "delivery_status": "external_verified_unattributed", "attribution": "misty_account_external_verification",
                        "model": None, "delivery_evidence": [delivery_evidence(receipt, "external_reply_receipt")],
                        "sources": [receipt["_source"]], "missing_context_ids": [k for k in refs if k not in observations]})

    response_ids = set()
    for pid, value in prepared.items():
        event, final, prep = value.get("event", {}), value.get("final", {}), value.get("prepared", {})
        if (event.get("session_id") != "sess_misty" and not value.get("_misty_verified")) or prep.get("model_version") == "mock" or not model_generated(final.get("text")):
            continue
        rid = final.get("response_id") or pid
        response_ids.add(rid)
        delivered = journal_deliveries.get(pid)
        result = delivered.get("data", {}).get("result", {}) if delivered else {}
        success = result.get("success") is True
        session_id = event.get("session_id")
        session_items = [msg for (sid, _), msg in sessions.items() if sid == session_id]
        current_positions = [i for i, msg in enumerate(session_items) if msg.get("event_id") == event.get("event_id")]
        if current_positions:
            prior_items = session_items[:current_positions[0] + 1]
        else:
            prior_items = [msg for msg in session_items if iso_seconds(msg.get("timestamp")) < iso_seconds(event.get("timestamp"))]
        context = [context_item(msg) for msg in prior_items if not str(msg.get("event_id")).startswith(rid + ":")]
        committed = any(str(key).startswith(rid + ":") for (sid, key) in sessions if sid == session_id)
        evidence = []
        if delivered:
            evidence.append({"kind": "runtime_reply_delivery", "success": success, "external_ref": result.get("external_ref"), "source": delivered["_source"]})
        if committed:
            evidence.append({"kind": "cognitive_session_committed", "response_id": rid, "source": value["_source"]})
        records.append({"id": rid, "timestamp": delivered.get("recorded_at") if success else prep.get("prepared_at") or event.get("timestamp"),
                        "channel": "web_chat", "scope": session_id, "user": event.get("raw_text", ""), "context": context,
                        "trigger_messages": [{"id": event.get("event_id"), "role": "user", "text": event.get("raw_text", ""), "timestamp": event.get("timestamp")}],
                        "reply": join_segments(result["delivered_segments"]) if success else final["text"],
                        "reply_segments": result.get("delivered_segments") if success else final.get("segments") or [final["text"]],
                        "generated_reply": final["text"], "generated_segments": final.get("segments") or [final["text"]],
                        "delivery_status": "runtime_delivery_recorded" if success else "session_committed" if committed else "generated_unconfirmed",
                        "attribution": "misty_model", "model": prep.get("model_version"), "delivery_evidence": evidence,
                        "sources": [value["_source"]] + ([value["_persona_source"]] if value.get("_persona_source") else []), "missing_context_ids": [],
                        "delivery_caveat": "Runtime memory-delivery or session commit is not proof of a browser receiving/rendering the reply."})
    for board in stores["boards"].values():
        turn = stores["turns"].get(board["entry_id"], {})
        records.append({"id": board["entry_id"], "timestamp": board["created_at"], "channel": "board", "scope": "misty-thoughts",
                        "user": turn.get("request_text", ""), "context": [], "trigger_messages": [], "title": board["title"],
                        "reply": board["text"], "reply_segments": [board["text"]], "delivery_status": "board_persisted",
                        "attribution": "misty_model", "model": None,
                        "delivery_evidence": [{"kind": "board_entry_persisted", "entry_id": board["entry_id"], "evidence_ref": board["evidence_ref"], "source": board["_source"]}],
                        "sources": [board["_source"]], "missing_context_ids": [],
                        "context_source_ids": json.loads(board.get("source_event_ids_json", "[]"))})

    # Trace files contain many mock experiments. Retain only live Misty final
    # responses and deduplicate against cognitive response IDs.
    trace_counts = Counter()
    if trace_root and trace_root.exists():
        for path in sorted(trace_root.glob("*/run_*.json")):
            value = read_json(path)
            if value.get("model_version") == "mock":
                trace_counts["mock_runs_excluded"] += 1
                continue
            if value.get("input", {}).get("session_id") != "sess_misty":
                trace_counts["other_session_runs_excluded"] += 1
                continue
            final = value.get("result") or {}
            if not model_generated(final.get("text")):
                trace_counts["no_final_text"] += 1
                continue
            rid = final.get("response_id") or value["call_id"]
            if rid in response_ids:
                trace_counts["live_final_duplicate"] += 1
                continue
            response_ids.add(rid)
            records.append({"id": rid, "timestamp": value.get("completed_at") or value.get("ts"), "channel": "web_chat",
                            "scope": "sess_misty", "user": value.get("input", {}).get("text", ""), "context": [],
                            "trigger_messages": [], "reply": final["text"], "reply_segments": final.get("segments") or [final["text"]],
                            "delivery_status": "generated_unconfirmed", "attribution": "misty_model", "model": value.get("model_version"),
                            "delivery_evidence": [], "sources": [source(path, "result", rid)], "missing_context_ids": []})
            trace_counts["live_final_added"] += 1
    inventory["trace_inventory"] = dict(trace_counts)
    inventory["matched_protocol_history_echoes"] = matched_echoes
    inventory["merged_source_counts"] = {name: len(value) for name, value in stores.items()}
    records.sort(key=lambda v: (iso_seconds(v.get("timestamp")), v["id"]))
    assert len({v["id"] for v in records}) == len(records), "duplicate exported primary key"
    inventory["record_count"] = len(records)
    inventory["by_delivery_status"] = dict(Counter(v["delivery_status"] for v in records))
    inventory["by_channel"] = dict(Counter(v["channel"] for v in records))
    inventory["by_attribution"] = dict(Counter(v["attribution"] for v in records))
    inventory["reply_characters"] = sum(len(v["reply"]) for v in records)
    inventory["timestamp_start"] = records[0]["timestamp"] if records else None
    inventory["timestamp_end"] = records[-1]["timestamp"] if records else None
    inventory["notes"] = [
        "Coverage is all retained replies in explicit production states, not proof that deleted/uncollected historical conversations are complete.",
        "One QQ record is one decision; confirmed_partial includes delivered text only, while generated_reply preserves the full draft.",
        "QQ context is the complete available set of decision source_record_ids plus new_observation_record_ids; replies are never truncated.",
        "Own protocol history without a model decision/receipt match remains unattributed and must not enter the main model-quality denominator.",
        "External verified account replies without model provenance remain separate from model-attributed responses.",
        "Cognitive session commits and memory-delivery success prove application persistence, not browser reception.",
        "Board entries are published monologues and should be scored separately from conversational replies.",
        "Directories containing tests, eval outputs, mock traces, alternate characters, credentials, and QQ encrypted login archives are excluded.",
        "No approximate text deduplication is used. An existing delivered segment can absorb one same-scope exact-text protocol echo within 120 seconds.",
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    corpus = "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records)
    # Use fixed LF bytes so the recorded digest also matches the Windows file.
    (output_dir / "corpus.jsonl").write_bytes(corpus.encode("utf-8"))
    inventory["corpus_sha256"] = hashlib.sha256(corpus.encode("utf-8")).hexdigest()
    (output_dir / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: inventory[k] for k in ("record_count", "by_delivery_status", "by_channel", "reply_characters", "timestamp_start", "timestamp_end", "corpus_sha256")}, ensure_ascii=True))
    return records, inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, action="append", required=True,
                        help="Production state dir; repeat old-to-new, latest stable keys win")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--trace-root", type=Path, default=Path("traces/calls"))
    args = parser.parse_args()
    for root in args.state_root:
        if not root.is_dir():
            parser.error(f"state root does not exist: {root}")
    run(args.state_root, args.output_dir, args.trace_root)


if __name__ == "__main__":
    main()
