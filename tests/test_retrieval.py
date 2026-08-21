from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from harness.core.models import CurrentInference, InteractionEvent, RetrievalQuery
from harness.memory.retrieval.planner import rule_plan

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)


def _msg(text: str, when: datetime, event_id: str) -> InteractionEvent:
    return InteractionEvent(
        event_id=event_id,
        timestamp=when,
        speaker="user",
        raw_text=text,
        session_id="sess_0881",
        evidence=[f"chat:sess_0881:{event_id}"],
    )


def test_rule_plan_running_example() -> None:
    query = RetrievalQuery(text="你昨天不是说不会再管我了吗？", now=NOW)
    plan = rule_plan(query)
    assert plan.time_scope == "yesterday"
    assert "user" in plan.entities
    assert plan.event_type == "commitment_or_statement"


def test_rule_plan_extended_time_scopes() -> None:
    assert rule_plan(RetrievalQuery(text="上个月收养的猫现在怎么样了", now=NOW)).time_scope == "last_month"
    assert rule_plan(RetrievalQuery(text="一个月前的那次争吵", now=NOW)).time_scope == "last_month"
    assert rule_plan(RetrievalQuery(text="前天你去哪里了", now=NOW)).time_scope == "day_before_yesterday"


def test_running_example_top1(service) -> None:
    service.apply_relationship_delta(
        "user",
        closeness=0.18,
        trust=0.24,
        recent_tension=0.31,
        last_major_event="evt_04010",
        now=NOW,
    )
    service.record_event(
        _msg("你以后不用管我了", datetime(2026, 8, 20, 23, 40, tzinfo=TZ), "evt_04010"),
        importance=0.86,
        emotional_valence=-0.55,
        event_type="commitment_or_statement",
        curate=False,
    )
    service.record_event(
        _msg("昨晚把碗洗了", datetime(2026, 8, 20, 19, 10, tzinfo=TZ), "evt_dishes"),
        importance=0.25,
        emotional_valence=0.1,
        curate=False,
    )
    service.record_event(
        _msg("那场雨真大，我们都淋湿了", datetime(2024, 6, 1, 19, tzinfo=TZ), "evt_rain_old"),
        importance=0.9,
        emotional_valence=-0.2,
        event_type="shared_history",
        curate=False,
    )
    service.record_event(
        _msg("今早下雨了", datetime(2026, 8, 21, 8, tzinfo=TZ), "evt_rain_today"),
        importance=0.3,
        emotional_valence=0.0,
        curate=False,
    )
    service.record_event(
        _msg("用户提到上周收养的猫生病了", datetime(2026, 8, 14, 21, 32, tzinfo=TZ), "evt_03812"),
        importance=0.72,
        emotional_valence=-0.3,
        curate=False,
    )

    hits = service.query(
        RetrievalQuery(
            text="你昨天不是说不会再管我了吗？",
            now=NOW,
            entities=["user", "character"],
        )
    )
    assert hits, "expected retrieval hits"
    top = hits[0]
    assert top.ref == "evt_04010"
    assert top.memory_type == "episodic"
    assert "entity:user" in top.why_retrieved
    assert "time:yesterday" in top.why_retrieved
    assert "commitment_conflict" in top.why_retrieved


def test_importance_and_recency_affect_rank(service) -> None:
    older = datetime(2026, 8, 20, 10, tzinfo=TZ)
    newer = datetime(2026, 8, 21, 12, tzinfo=TZ)
    service.record_event(
        _msg("我们讨论了工作上的安排", older, "evt_low"),
        importance=0.2,
        event_type="daily",
        curate=False,
    )
    service.record_event(
        _msg("我们讨论了工作上的重要安排，必须兑现", newer, "evt_high"),
        importance=0.95,
        event_type="commitment_or_statement",
        curate=False,
    )
    hits = service.query(RetrievalQuery(text="工作上的安排", now=NOW, entities=["user", "character"]))
    episodic = [item for item in hits if item.memory_type == "episodic"]
    assert episodic[0].ref == "evt_high"
    assert episodic[0].score_breakdown.importance > episodic[1].score_breakdown.importance


def test_time_scope_mismatch_falls_back(service) -> None:
    # memory lives last week, but the user says "yesterday" — a wrong date must not empty the pool
    service.record_event(
        _msg("用户提到上周收养的猫生病了", datetime(2026, 8, 14, 21, 32, tzinfo=TZ), "evt_03812"),
        importance=0.72,
        emotional_valence=-0.3,
        curate=False,
    )
    hits = service.query(
        RetrievalQuery(text="你昨天有没有问起猫的情况？", now=NOW, entities=["user", "character"])
    )
    episodic = [item for item in hits if item.memory_type == "episodic"]
    assert any(item.ref == "evt_03812" for item in episodic)
    assert any("time:fallback" in item.why_retrieved for item in episodic)


def test_last_month_window_covers_misdated_query(service) -> None:
    # user says "上个月"; the event is 20 days ago — the window must be wide enough
    service.record_event(
        _msg("用户提到收养的猫生病了", datetime(2026, 8, 1, 21, 32, tzinfo=TZ), "evt_early_aug"),
        importance=0.72,
        emotional_valence=-0.3,
        curate=False,
    )
    hits = service.query(
        RetrievalQuery(text="上个月收养的猫现在怎么样了", now=NOW, entities=["user", "character"])
    )
    assert any(item.ref == "evt_early_aug" for item in hits if item.memory_type == "episodic")


def test_conflict_retrieval_finds_counterevidence(service) -> None:
    service.record_event(
        _msg("你以后不用管我了", datetime(2026, 8, 20, 23, 40, tzinfo=TZ), "evt_04010"),
        importance=0.86,
        emotional_valence=-0.7,
        event_type="commitment_or_statement",
        curate=False,
    )
    service.record_event(
        _msg("今天一起吃饭很开心", datetime(2026, 8, 21, 12, tzinfo=TZ), "evt_lunch"),
        importance=0.4,
        emotional_valence=0.6,
        curate=False,
    )
    hits, conflicts = service.query_with_conflicts(
        RetrievalQuery(
            text="用户现在很信任我",
            now=NOW,
            current_inference=CurrentInference(
                about="user_trust",
                value="high",
                polarity="positive",
                entities=["user"],
            ),
        )
    )
    assert any(item.ref == "evt_04010" for item in conflicts)
    assert all("conflict_counterevidence" in item.why_retrieved for item in conflicts)
    # conflict retrieval must not silently overwrite semantic/episodic stores
    assert service.stores.episodic.get("evt_04010") is not None
