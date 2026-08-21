"""Built-in mini episodes so pytest never needs eval/external/."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from harness.llm.client import MockLLM

from eval.rp.schema import RPEpisode, Turn

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)


def eval_mock_llm() -> MockLLM:
    return MockLLM(
        chat_routes={
            "张小凡": "答案：A",
            "godfather": "I don't know what you mean. That hasn't happened yet.",
            "Invisibility Cloak": "Yes. I received it on Christmas with a note.",
            "吃糖": "嗯。含着。",
            "棒棒糖": "嗯。含着。",
            "单项选择题": "答案：A",
        }
    )


def mini_episodes() -> list[RPEpisode]:
    return [charactereval_mini(), roleeval_mini(), timechara_future_mini(), timechara_past_mini()]


def charactereval_mini() -> RPEpisode:
    return RPEpisode(
        bench="charactereval",
        episode_id="ce_mini_0",
        role_id="老默",
        profile={
            "姓名": "陈金默",
            "人物性格": "忠诚，知恩图报，内心有道德矛盾",
            "爱好": "吃棒棒糖",
        },
        history=[Turn(speaker="老默", text="嗯。先把糖含上。")],
        query="黄瑶：默哥，你还吃糖吗？",
        meta={
            "context": "老默：嗯。先把糖含上。\n黄瑶：默哥，你还吃糖吗？",
            "metrics": [["fluency", "流畅性"], ["persona_utterance", "话语"]],
            "profile_text": "姓名：陈金默\n人物性格：忠诚，知恩图报",
        },
    )


def roleeval_mini() -> RPEpisode:
    return RPEpisode(
        bench="roleeval",
        episode_id="re_mini_0",
        role_id="张小凡",
        profile={"姓名": "张小凡", "人物性格": "坚韧"},
        history=[],
        query=(
            "以下是关于小说的单项选择题，请选出其中的正确答案。\n"
            "张小凡和陆雪琪是什么关系？\n"
            "A. 夫妻关系\nB. 兄妹关系\nC. 姐弟关系\nD. 姑侄关系\n"
            "答案："
        ),
        choices={"A": "夫妻关系", "B": "兄妹关系", "C": "姐弟关系", "D": "姑侄关系"},
        answer="A",
        meta={"category": "小说", "question": "张小凡和陆雪琪是什么关系？"},
    )


def timechara_future_mini() -> RPEpisode:
    story = datetime(1991, 9, 1, tzinfo=TZ)
    return RPEpisode(
        bench="timechara",
        episode_id="tc_mini_future",
        role_id="Harry Potter",
        profile={"name": "Harry Potter", "series": "harry_potter"},
        history=[],
        query="Who is your godfather, and how did he die?",
        story_time=story,
        meta={
            "series": "harry_potter",
            "data_type": "future",
            "event_summary": "Sirius Black is killed by Bellatrix Lestrange in the Department of Mysteries.",
            "character_period": "Book1-chapter1",
            "question_period": "Book5-chapter35",
            "participants": "Harry Potter, Sirius Black, Bellatrix Lestrange",
            "gold_response": "I don't know what you mean. I have only just arrived at Hogwarts.",
            "temporal_label": "future",
            "event_time": (story + timedelta(days=365 * 4)).isoformat(),
        },
    )


def timechara_past_mini() -> RPEpisode:
    story = datetime(1992, 12, 25, tzinfo=TZ)
    return RPEpisode(
        bench="timechara",
        episode_id="tc_mini_past",
        role_id="Harry Potter",
        profile={"name": "Harry Potter", "series": "harry_potter"},
        history=[],
        query="Did you see the moment when you received the Invisibility Cloak on Christmas?",
        story_time=story,
        meta={
            "series": "harry_potter",
            "data_type": "past-presence",
            "event_summary": "Harry received the Invisibility Cloak as a Christmas present.",
            "character_period": "Book1-chapter12",
            "question_period": "Book1-chapter12",
            "participants": "Harry Potter",
            "gold_response": "Yes. It arrived with a note telling me to use it well.",
            "temporal_label": "past-presence",
            "event_time": story.isoformat(),
        },
    )
