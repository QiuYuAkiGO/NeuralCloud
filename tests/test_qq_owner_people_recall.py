"""Broad owner retrospectives leave room for other people's actual impressions."""
import json

import pytest

from harness.cognitive.qq_memory import select_recall
from tests.test_qq_private_recall import (
    NOW, PRIVATE, add_episode, add_impression, memory, qq_scope,
)


QUERY = '今天感觉怎么样，遇见的人给你的感受如何'


def _recall(mem, *, query=QUERY, **kwargs):
    return select_recall(mem, group_id=PRIVATE, query=query, now=NOW,
                         allowed_conversation_ids=('100', '101'), include_owner_local=True, **kwargs)


def test_people_retrospective_reserves_at_most_two_other_people_before_echoes():
    mem = memory()
    echo = add_episode(mem, 'question-echo', scope=qq_scope(PRIVATE), text=QUERY)
    add_episode(mem, 'old-answer', scope=qq_scope(PRIVATE), member='999', text=QUERY + '，一时想不到。')
    add_impression(mem, PRIVATE, statement=QUERY)
    people = [add_impression(mem, '100', member=str(301 + index), topic=f'交往感受{index}',
        statement='这位成员愿意分享自己的感受，暂时觉得他乐于交流。') for index in range(3)]
    result = _recall(mem, max_items=3)
    selected = result['entries']
    assert all(row['record_id'] in {person.impression_id for person in people} for row in selected[:2])
    assert selected[0]['subject_refs'] != selected[1]['subject_refs']
    assert selected[2]['record_id'] == echo.event_id or selected[2]['source_conversation_id'] == 'qq:' + PRIVATE
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) <= 4000


@pytest.mark.parametrize('query', ['谁和你讨论过 Docker 或 WSL？', '群友对街霸有什么看法？'])
def test_specific_factual_topics_keep_strong_exact_match_first(query):
    mem = memory()
    wanted = add_episode(mem, 'specific-fact', scope=qq_scope('100'), text=query)
    add_impression(mem, '101', member='301', topic='感受印象', statement='这位群友愿意谈自己的感受和印象。')
    assert _recall(mem, query=query, max_items=1)['entries'][0]['record_id'] == wanted.event_id


def test_people_priority_requires_owner_expansion():
    mem = memory()
    echo = add_episode(mem, 'question-echo', scope=qq_scope(PRIVATE), text=QUERY)
    other = add_impression(mem, '100', topic='吃饭', statement='这位成员说想吃米饭。')
    assert _recall(mem, max_items=1)['entries'][0]['record_id'] == other.impression_id
    ordinary = select_recall(mem, group_id=PRIVATE, query=QUERY, now=NOW,
        allowed_conversation_ids=('100',), include_owner_local=False, max_items=1)
    assert ordinary['entries'][0]['record_id'] == echo.event_id


def test_broad_group_impression_question_does_not_require_literal_term_overlap():
    mem = memory()
    query = '你对群友有什么印象'
    add_episode(mem, 'question-echo', scope=qq_scope(PRIVATE), text=query)
    people = [add_impression(mem, '100', member=str(301 + index), topic='解释习惯',
        statement='愿意耐心解释细节，认真回应求助。') for index in range(2)]
    selected = _recall(mem, query=query, max_items=2)['entries']
    assert {row['record_id'] for row in selected} == {person.impression_id for person in people}
    assert all(row['kind'] == 'person' for row in selected)


def test_prioritized_impression_keeps_its_whole_counterevidence_and_budget():
    mem = memory()
    item = add_impression(mem, '100', member='301', topic='感受', counter=True)
    add_episode(mem, 'question-echo', scope=qq_scope(PRIVATE), text=QUERY)
    result = _recall(mem, max_items=1)
    row = result['entries'][0]
    assert row['record_id'] == item.impression_id and row['status'] == 'disputed'
    assert row['counterevidence'] and '原因尚不能确定' in row['counterevidence'][0]['statement']
    required = len(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    smaller = _recall(mem, max_items=1, max_characters=required - 1)
    assert all(entry['record_id'] != item.impression_id for entry in smaller['entries'])
    assert len(json.dumps(smaller, ensure_ascii=False, separators=(',', ':'))) <= required - 1
