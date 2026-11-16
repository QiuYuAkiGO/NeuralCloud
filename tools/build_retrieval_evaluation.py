"""Freeze a private historical corpus and 200 source-grounded, session-disjoint queries.

Paraphrases are authored from historical evidence, not claimed to be 200 real user turns.
Only the identity incident is a direct replay. No test labels enter threshold tuning.
"""

import hashlib
import json
from pathlib import Path
import sqlite3
import argparse

root = Path("/var/lib/misty-retrieval-eval/20261027")
parser = argparse.ArgumentParser()
parser.add_argument("--queries", type=Path, default=root / "retrieval_queries.private.txt")
args = parser.parse_args()
cutoff = 1793079742.0  # 2026-10-27 13:42:22 +08:00, before the incident response.
target = root / "evaluation.sqlite3"
if target.exists():
    raise SystemExit("FROZEN_CORPUS_ALREADY_EXISTS")
with sqlite3.connect(root / "memory.snapshot.sqlite3") as src, sqlite3.connect(target) as dst:
    src.backup(dst)
    # Conservatively omit records changed after the cutoff instead of exposing later revisions.
    excluded = dst.execute(
        "SELECT r.id FROM records r LEFT JOIN (SELECT record_id,max(received_at) latest FROM changes GROUP BY record_id) c ON c.record_id=r.id WHERE r.occurred>? OR c.latest>?",
        (cutoff, cutoff),
    ).fetchall()
    for (rid,) in excluded:
        dst.execute("UPDATE records SET status='INACTIVE' WHERE id=?", (rid,))
        dst.execute("DELETE FROM search WHERE record_id=?", (rid,))
    dst.commit()
    rows = {
        r[0]: json.loads(r[1])
        for r in dst.execute(
            "SELECT r.id,v.payload FROM records r JOIN revisions v ON r.id=v.record_id AND r.revision=v.revision WHERE r.status='ONLINE'"
        )
    }

cases = []
for line in args.queries.read_text().splitlines():
    if not line or line.startswith("#"):
        continue
    prefix, q1, q2 = line.split("|")
    ids = [rid for rid in rows if rid.startswith("claim_v2_" + prefix)]
    assert len(ids) == 1, (prefix, ids)
    rid = ids[0]
    p = rows[rid]
    split = "dev" if p["session_id"] in ["qq:group:712342021", "qq:group:1060979317"] else "test"
    assert split == "dev" or p["session_id"] in ["qq:group:1044946257", "qq:private:2015188508:2285027817"]
    gold = [rid] + [s for s in p.get("source_ids", []) if s in rows]
    # Exact duplicate evidence is also relevant, regardless of storage channel.
    gold += [other for other, value in rows.items() if value["content"] == p["content"] and other not in gold]
    for j, q in enumerate([q1, q2]):
        cases.append(
            dict(
                id=f"{prefix}-{j}",
                split=split,
                session=p["session_id"],
                query=q,
                category="paraphrase",
                relevant=gold,
                cutoff=cutoff,
            )
        )

no_answers = [
    "账号99123456789001的姓名是什么",
    "99123456789002是哪位创造者",
    "99123456789003上个月去过哪里",
    "99123456789004最喜欢哪首歌",
    "99123456789005说过什么职业",
    "南极中微子站的校准误差是多少",
    "木卫二潜航计划由谁批准",
    "银杏号深海钻探船何时下水",
    "月背氦三采矿项目有多少预算",
    "天琴引力波观测站选在哪里",
    "99123456789011有没有申请过假期",
    "99123456789012是谁的同事",
    "99123456789013拥有哪款电脑",
    "99123456789014平时如何通勤",
    "99123456789015的朋友叫什么",
    "火卫一轨道电梯使用什么材料",
    "北极量子雷达站几点检修",
    "土卫六气象飞艇传回多少数据",
    "晨曦号海底实验室有哪些人员",
    "金星浮空温室种植了什么",
]
for i, q in enumerate(no_answers):
    marker = q[q.find("99") : q.find("99") + 14] if "991234" in q else q[:6]
    assert not any(marker in p["content"] or marker in p.get("participants", []) for p in rows.values())
    cases.append(
        dict(
            id=f"no-answer-{i}",
            split="dev" if i < 10 else "test",
            session=f"negative-{i}",
            query=q,
            category="no_answer",
            relevant=[],
            cutoff=cutoff,
        )
    )

creator = next(
    rid for rid, p in rows.items() if p.get("pinned") and "唯一创造者" in p["content"] and "2285027817" in p["content"]
)
identity_queries = [
    "对了，你还记得我是谁吗",
    "我应该怎么称呼",
    "你认得这个账号吗",
    "还记得我们的关系吗",
    "我的全名叫什么",
    "2285027817自称自己是什么",
    "你知道是谁创造了你吗",
    "之前让你怎样称呼我",
    "这个QQ号对应哪位熟人",
    "2285027817 群成员 身份 昵称 秋日私语 1967 VRChat",
]
for i, q in enumerate(identity_queries):
    cases.append(
        dict(
            id=f"identity-{i}",
            split="test",
            session="identity-incident-group104",
            query=q,
            category="identity",
            subject=dict(platform="qq", account_id="2285027817"),
            relevant=[creator],
            cutoff=cutoff,
        )
    )
assert len(cases) == 200, len(cases)
assert sum(c["split"] == "dev" for c in cases) == 100
assert not (
    {c["session"] for c in cases if c["split"] == "dev"} & {c["session"] for c in cases if c["split"] == "test"}
)
dataset = root / "queries.frozen.json"
dataset.write_text(json.dumps(cases, ensure_ascii=False, indent=2))
(root / "identity.manifest.json").write_text(
    json.dumps(
        [
            dict(
                platform="qq",
                account_id="2285027817",
                record_id=creator,
                revision=1,
                confirmation="operator_confirmed",
                name="秋语",
                relation="唯一创造者；全名秋语随风",
            )
        ],
        ensure_ascii=False,
        indent=2,
    )
)
(root / "evaluation.manifest.json").write_text(
    json.dumps(
        dict(
            cutoff=cutoff,
            excluded_after_cutoff=len(excluded),
            queries=200,
            dev=100,
            test=100,
            dataset_sha256=hashlib.sha256(dataset.read_bytes()).hexdigest(),
            limitations=[
                "Paraphrases authored from evidence; only identity incident is a real query replay.",
                "Negative set covers unknown accounts, not all out-of-domain or ambiguous questions.",
                "Relevance labels include anchor/source/identical duplicates; paraphrased alternate evidence requires pooled review before production quality acceptance.",
            ],
        ),
        indent=2,
    )
)
print("FROZEN", len(rows), "records", len(cases), "queries")
