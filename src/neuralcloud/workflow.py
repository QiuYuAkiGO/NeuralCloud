"""One bounded evidence workflow for preparation and explicit recall tools."""

from __future__ import annotations
import json
import hashlib
import re
import threading
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

from .contracts import RecallSubject
from .retrieval import query_terms, quoted_phrases
from .evidence_index import author_of, account

VERSION = "memory-workflow-v1"
LOADED_IMPLEMENTATION = {
    "module": str(Path(__file__).resolve()),
    "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
}
RULES = json.loads((Path(__file__).parent / "workflows" / "memory-v1.json").read_text(encoding="utf-8"))
IDENTITY = re.compile(
    r"名字|姓名|全名|昵称|称呼|叫啥|叫什么|我是谁|他是谁|她是谁|哪位|这个.*(?:QQ|账号)|身份|认得|自称|我们的关系|创造.*你|创造者"
)
PREDICATES = {
    "不喜欢": ["dislikes"],
    "讨厌": ["dislikes"],
    "喜欢": ["likes", "preference"],
    "偏好": ["preference", "likes", "dislikes"],
    "决定": ["decision"],
    "约定": ["decision"],
    "推荐": ["recommendation"],
    "为什么": ["reason"],
    "原因": ["reason"],
    "关系": ["relationship"],
}


def dated_constraints(query, constraints):
    """Explicit calendar dates in event questions are hard bounds; quotes remain literal."""
    text = query
    for phrase in quoted_phrases(query):
        text = text.replace(phrase, "")
    if not re.search(r"当天|那天|期间|发生|聊|说|回忆|记录|到|至", text):
        return constraints
    dates = re.findall(r"(?<![0-9])(20[0-9]{2})[-年]([0-9]{1,2})[-月]([0-9]{1,2})(?:日)?(?![0-9])", text)
    if not dates or len(dates) > 2:
        return constraints
    try:
        values = [datetime(*map(int, date), tzinfo=timezone(timedelta(hours=8))).timestamp() for date in dates]
    except ValueError:
        return constraints
    c = dict(constraints)
    start, end = min(values), max(values) + 86400 - 0.000001
    c["since"] = max(c.get("since", start), start)
    c["until"] = min(c.get("until", end), end)
    return c


class MemoryWorkflow:
    def __init__(self, retriever, index, *, deadline_ms=1800):
        self.base, self.store, self.index = retriever, retriever.store, index
        self.deadline_ms = deadline_ms
        self.cache = {}
        self.lock = threading.RLock()

    def status(self):
        return {
            "workflow_version": VERSION,
            "index": self.index.status(),
            "online_judge": False,
            "max_rounds": 3,
            "deadline_ms": self.deadline_ms,
            "implementation": LOADED_IMPLEMENTATION,
        }

    def plan(self, query, subject, intent, targets):
        explicit = [f"{t.platform}:{t.account_id}" for t in targets]
        ids = re.findall(r"(?<![0-9])[1-9][0-9]{4,15}(?![0-9])", query)
        relationship_question = bool(re.search(r"朋友|同事|同学|家人|父亲|母亲|伴侣|爱人", query))
        # A bare account lookup is identity retrieval; dates in ordinary text are not identities.
        bare = bool(re.fullmatch(r"\s*(?:(?:qq:member:|qq:|QQ[:： ]*|@)?[1-9][0-9]{4,15})\s*", query))
        if intent == "auto":
            intent = (
                "fact"
                if relationship_question
                else "identity"
                if IDENTITY.search(query) or bare
                else "cause"
                if re.search("为什么|原因", query)
                else "fact"
                if any(k in query for k in PREDICATES)
                else "event"
                if re.search("上次|那次|昨天|之前.*(?:说|聊)|发生", query)
                else "topic"
            )
        account_relation = re.search(
            r"[1-9][0-9]{4,15}(?:的)?(?:最喜欢|喜欢|不喜欢|偏好|讨厌|决定|约定|推荐|为什么|上次|上个月|去年|昨天|之前|有没有|是否|说过|拥有|平时|朋友|同事|家人|和|与)", query
        )
        if ids and not explicit and (intent == "identity" or re.search(r"QQ|qq:|账号|@", query) or account_relation):
            explicit.extend("qq:" + i for i in ids)
        entities = list(dict.fromkeys(explicit))
        if (
            not entities
            and subject
            and (
                intent == "identity"
                and re.search("我|称呼|身份|这个.*(?:QQ|账号)|认得", query)
                or intent in ["fact", "cause"]
                and re.search("我", query)
            )
        ):
            entities = [f"{subject.platform}:{subject.account_id}"]
        if (
            not entities
            and intent == "identity"
            and self.base.identities
            and re.search(r"创造(?:了)?你|你的.*创造者|谁.*创造", query)
        ):
            entities = [
                f"{r['platform']}:{r['account_id']}" for r in self.base.identities.role_accounts("创造者", self.store)
            ]
        ambiguous = False
        if not entities and intent != "topic":
            entities, ambiguous = self.index.alias_targets(query)
        predicates = (
            ["name"]
            if intent == "identity"
            else ["reason"]
            if intent == "cause"
            else ["event", "decision"]
            if intent == "event"
            else []
        )
        if intent == "fact":
            if relationship_question:
                return intent, entities, ["relationship"], ambiguous
            for word, values in PREDICATES.items():
                if word in query:
                    predicates = values
                    break
        return intent, entities, predicates, ambiguous

    def _allowed(self, record, constraints, phrases, entities=()):
        if record.get("status") != "ONLINE":
            return False
        source = self.index.read_source(record["record_id"])
        if not source or source[1] != "ONLINE" or source[0] != record["revision"]:
            return False
        p = source[2]
        c = constraints or {}
        if c.get("conversation_id") and p.get("conversation_id") != c["conversation_id"]:
            return False
        if c.get("since") is not None and p["occurred_at"] < c["since"]:
            return False
        if c.get("until") is not None and p["occurred_at"] > c["until"]:
            return False
        if not all(phrase.casefold() in p["content"].casefold() for phrase in phrases):
            return False
        if entities:
            linked = {author_of(p)}
            linked.update(account(x) for x in p.get("participants", []))
            ch = p.get("channel") or {}
            linked.update(account(x, ch.get("platform", "qq")) for x in ch.get("mentions", []))
            # Textual numeric mentions establish only QQ clues, never QQBot identity.
            linked.update("qq:" + x for x in re.findall(r"(?<![0-9])[1-9][0-9]{4,15}(?![0-9])", p["content"]))
            if not any(e in linked for e in entities):
                return False
        return True

    @staticmethod
    def _answers_value(query, intent, fact):
        if intent == "identity" and re.search("全名|真实姓名", query):
            return fact["confirmation"] in ["self_reported_full_name", "operator_confirmed"]
        if intent == "fact":
            match = re.search(r"(?:不喜欢|喜欢|讨厌|推荐)(.+?)(?:吗|呢|[？?]|$)", query)
            if match:
                value = re.sub(r"^(?:的|过)|什么|哪些|哪种|啥|东西|事情|有什么|过什么", "", match[1]).strip()
                if value and value not in fact["value"]:
                    return False
        if intent in ["cause", "event"] or fact["predicate"] in ["decision", "recommendation", "relationship"]:
            focus = re.sub(r"(?:qq:member:|qq:|QQ)?[0-9]{5,16}", "", query)
            focus = re.sub(
                r"你还记得|还记得|请问|为什么|什么原因|原因|发生|事情|决定|约定|推荐|之前|上次|那次|当时|昨天|以前|什么|哪些|谁|的|了|吗|呢|我|他|她|你|是|有|和|与|关系|如何|怎样|怎么样|说过|做过|记得",
                "",
                focus,
            )
            terms = {t for t in query_terms(focus) if len(t) > 1 and not t.isdigit()}
            if terms and len(terms.intersection(query_terms(fact["value"]))) < max(1, (len(terms) + 1) // 2):
                return False
        return True

    def recall(
        self, query, limit=10, subject=None, mode="auto", intent="auto", targets=(), constraints=None, *, cancel=None
    ):
        if mode == "legacy":
            if targets or constraints or intent != "auto":
                from .storage import Refusal

                raise Refusal("WORKFLOW_REQUIRED_FOR_CONSTRAINTS")
            return self.base.recall(query, limit, subject, mode)
        c = constraints.model_dump(exclude_none=True) if hasattr(constraints, "model_dump") else (constraints or {})
        c = dated_constraints(query, c)
        if c.get("since") is not None and c.get("until") is not None and c["since"] > c["until"]:
            from .storage import Refusal

            raise Refusal("INVALID_TIME_RANGE")
        began = time.monotonic()
        deadline = began + self.deadline_ms / 1000
        intent, entities, predicates, ambiguous = self.plan(query, subject, intent, targets)

        def stopped():
            return time.monotonic() >= deadline or bool(cancel and cancel.is_set())

        key = json.dumps(
            [
                query,
                limit,
                subject.model_dump() if subject else None,
                mode,
                intent,
                entities,
                c,
                self.store.generation(),
                self.index.status()["generation"],
            ]
        )
        with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 10:
                # Source generation in key invalidates revoked/corrected evidence before reuse.
                result = json.loads(cached[1])
                result["diagnostics"].update(
                    cache_hit=True, elapsed_ms=round((time.monotonic() - began) * 1000, 2), stages=[], rounds=0
                )
                return result
        phrases = list(dict.fromkeys(quoted_phrases(query) + c.get("phrases", [])))
        stages = [{"stage": "planning", "ms": round((time.monotonic() - began) * 1000, 2)}]
        facts = []
        topics = []
        rounds = 0
        degraded = None
        semantic_count = 0
        state = "NOT_FOUND"
        reason = "no_supporting_evidence"

        def evidence_lookup():
            found = (
                self.index.lookup(
                    entities,
                    predicates,
                    conversation=c.get("conversation_id"),
                    since=c.get("since"),
                    until=c.get("until"),
                    candidates=True,
                )
                if predicates and (entities or intent != "identity")
                else []
            )
            for f in found:
                if stopped():
                    return
                if not entities:
                    meaningful = set(query_terms(query)) - set(
                        query_terms("我你他她谁什么之前昨天上次喜欢不喜欢偏好决定约定推荐为什么原因关系")
                    )
                    if meaningful and not meaningful.intersection(query_terms(f["value"])):
                        continue
                record = self.store.read(f["record_id"], length=1024)
                if record.get("revision") != f["revision"] or not self._allowed(record, c, phrases):
                    continue
                record["evidence"] = {
                    "entity": f["entity"],
                    "predicate": f["predicate"],
                    "value": f["value"],
                    "basis": f["basis"],
                    "confirmation": f["confirmation"],
                    "references": f["references"],
                    "scope": f["scope"],
                    "occurred_at": f["occurred"],
                    "conflict": False,
                    "eligible": bool(f["active"]),
                    "extractor_version": f["extractor"],
                }
                facts.append(record)
            if intent == "identity" and self.base.identities:
                for entity in entities:
                    platform, aid = entity.split(":", 1)
                    for record in self.base.identities.lookup(
                        RecallSubject(platform=platform, account_id=aid), self.store
                    ):
                        if not self._allowed(record, c, phrases):
                            continue
                        identity = record["identity"]
                        record["evidence"] = {
                            "entity": entity,
                            "predicate": "name",
                            "value": identity["name"],
                            "basis": "direct",
                            "confirmation": identity["confirmation"],
                            "references": [{"record_id": record["record_id"], "revision": record["revision"]}],
                            "scope": None,
                            "occurred_at": record["occurred_at"],
                            "conflict": identity["conflict"],
                            "eligible": True,
                            "extractor_version": "operator-identity-manifest",
                        }
                        facts.insert(0, record)

        t = time.monotonic()
        evidence_lookup()
        stages.append({"stage": "evidence_lookup", "ms": round((time.monotonic() - t) * 1000, 2), "count": len(facts)})
        rounds = 1

        def supported(rows):
            valid = [
                r
                for r in rows
                if r["evidence"].get("eligible", False) and self._answers_value(query, intent, r["evidence"])
            ]
            return (
                bool(valid)
                and not ambiguous
                and all(any(r["evidence"]["entity"] == e for r in valid) for e in entities)
            )

        sufficient = supported(facts)
        # Ordinary topical recall is still useful, but never substitutes for a missing relation.
        if not sufficient and not stopped():
            t = time.monotonic()
            raw = self.base.recall(query, min(50, max(limit, 20)), None, mode, deadline=deadline)
            degraded = raw.get("diagnostics", {}).get("degraded")
            semantic_count = raw.get("diagnostics", {}).get("semantic_candidates", 0)
            topics = [r for r in raw["items"] if self._allowed(r, c, phrases, entities)]
            stages.append({"stage": "hybrid", "ms": round((time.monotonic() - t) * 1000, 2), "count": len(topics)})
            if intent in ["event", "cause"] and topics and not stopped():
                rounds = 2
                for rid, edge in self.index.related([r["record_id"] for r in topics]):
                    if stopped():
                        break
                    record = self.store.read(rid, length=1024)
                    if self._allowed(record, c, phrases):
                        record["evidence"] = {
                            "entity": None,
                            "predicate": edge,
                            "value": "Context only; not a causal claim",
                            "basis": "context",
                            "confirmation": "source_link" if edge == "reply" else "temporal_neighbor",
                            "references": [{"record_id": rid, "revision": record["revision"]}],
                            "scope": record.get("source", {}).get("conversation_id"),
                            "occurred_at": record["occurred_at"],
                            "conflict": False,
                        }
                        topics.append(record)
            if not sufficient and (intent != "topic" or not topics) and not stopped():
                # Keep exact targets and quoted constraints; reducing question words cannot change the subject.
                reduced = " ".join([e.split(":", 1)[1] for e in entities] + ['"' + p + '"' for p in phrases])
                if not reduced:
                    terms = query_terms(
                        re.sub("请问|你还记得|还记得|有没有|相关记忆|告诉我|之前的|那件事|一下", "", query)
                    )
                    reduced = " ".join(terms[: max(1, len(terms) // 2)])
                if reduced and reduced != query:
                    rounds += 1
                    t = time.monotonic()
                    raw = self.base.recall(reduced, min(50, max(limit, 20)), None, "keyword", deadline=deadline)
                    topics.extend(r for r in raw["items"] if self._allowed(r, c, phrases, entities))
                    stages.append(
                        {
                            "stage": "relax_soft_terms",
                            "ms": round((time.monotonic() - t) * 1000, 2),
                            "count": len(raw["items"]),
                        }
                    )
        # Revalidate after all potentially slow channels, before compiling or caching evidence.
        validation_started = time.monotonic()
        facts = [
            r for r in facts if self._allowed(r, c, phrases) and self.index.valid_refs(r["evidence"]["references"])
        ]
        topics = [r for r in topics if self._allowed(r, c, phrases)]
        stages.append(
            {
                "stage": "source_validation",
                "ms": round((time.monotonic() - validation_started) * 1000, 2),
                "count": len(facts) + len(topics),
            }
        )
        sufficient = supported(facts)
        # Differences across name types/scopes/times are history, not automatically conflicts.
        conflicts = {}
        for r in facts:
            e = r["evidence"]
            if not e.get("eligible"):
                continue
            k = (e["entity"], e["predicate"], e["scope"], e["occurred_at"], e["confirmation"])
            conflicts.setdefault(k, set()).add(e["value"])
        conflict = any(len(v) > 1 for v in conflicts.values()) or any(r["evidence"]["conflict"] for r in facts)
        if ambiguous:
            state, reason, sufficient = "CONFLICT", "ambiguous_alias_requires_account", False
        elif conflict:
            state = "CONFLICT"
            reason = "differing_supported_claims"
            sufficient = False
        elif sufficient:
            state = (
                "INFERRED"
                if any(r["evidence"].get("eligible") and r["evidence"]["basis"] == "inferred" for r in facts)
                else "DIRECT"
            )
            reason = "requested_relation_supported"
        elif stopped() or degraded or self.index.status()["lag"] > 0:
            state = "INCOMPLETE"
            reason = (
                "cancelled"
                if cancel and cancel.is_set()
                else "deadline"
                if time.monotonic() >= deadline
                else "semantic_unavailable"
                if degraded
                else "index_lag"
            )
        elif facts or topics:
            state = "RELATED"
            reason = "related_content_does_not_establish_requested_relation"
        chosen = []
        seen = set()
        for r in facts + topics:
            evidence = r.get("evidence", {})
            evidence_key = (r["record_id"], evidence.get("entity"), evidence.get("predicate"), evidence.get("value"))
            if evidence_key in seen:
                continue
            seen.add(evidence_key)
            r = dict(r)
            r["content"] = r.get("content", "")[:1024]
            chosen.append(r)
            if len(chosen) >= limit:
                break
        if sufficient and not supported(
            [r for r in chosen if r.get("evidence", {}).get("basis") in ["direct", "inferred"]]
        ):
            state, reason, sufficient = "RELATED", "result_limit_omitted_required_evidence", False
        result = {
            "status": "OK",
            "scope": "GLOBAL_ONLINE",
            "generation": self.store.generation(),
            "items": chosen,
            "coverage": "global_online; current source revisions validated; offline sources excluded",
            "assessment": {"state": state, "sufficient": sufficient, "absence_confirmed": False, "reason": reason},
            "diagnostics": {
                "mode": mode,
                "semantic_requested": any(s["stage"] == "hybrid" for s in stages),
                "degraded": degraded,
                "identity_count": len([r for r in chosen if r.get("evidence", {}).get("predicate") == "name"]),
                "keyword_candidates": len(topics),
                "semantic_candidates": semantic_count,
                "elapsed_ms": round((time.monotonic() - began) * 1000, 2),
                "workflow_version": VERSION,
                "index_generation": self.index.status()["generation"],
                "stages": stages,
                "rounds": rounds,
                "stop_reason": reason,
            },
        }
        with self.lock:
            self.cache[key] = (time.monotonic(), json.dumps(result))
            while len(self.cache) > 64:
                self.cache.pop(next(iter(self.cache)))
        return result
