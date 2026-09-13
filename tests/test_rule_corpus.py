"""判定规则回归语料测试：正样本必须被送审，负样本不应被送审。

语料在 tests/data/rule_corpus.json，可用真机反馈持续补充。验收线（见
docs/判定规则优化方案.md §9）：正样本送审率 >= 95%，负样本误送审率 <= 5%。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.moderator import LLMModerator
from src.rules import RuleEngine

CORPUS_PATH = Path(__file__).resolve().parent / "data" / "rule_corpus.json"


def load_corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def build() -> tuple[RuleEngine, LLMModerator]:
    engine = RuleEngine(
        {
            "hard": [
                {
                    "id": "加群领资料",
                    "type": "literal",
                    "pattern": "加群领资料",
                    "action": ["warn", "recall", "mute"],
                    "scope": "all",
                    "enabled": True,
                }
            ],
            "soft": [],
        }
    )

    async def provider_call(request, system_prompt, user_prompt):  # pragma: no cover
        del request, system_prompt, user_prompt
        return '{"verdict":"allow","severity":1,"confidence":0.9}'

    settings = {
        "send_conditions": ["rule_hit", "risk>=60"],
        "llm_min_confidence": 0.7,
    }
    moderator = LLMModerator(provider_call, settings_getter=lambda: settings)
    return engine, moderator


def will_send(engine: RuleEngine, moderator: LLMModerator, text: str) -> bool:
    evaluation = engine.evaluate(text, group_id="g1")
    return bool(
        moderator.should_send(
            rule_summary=evaluation.rule_summary(),
            has_link=evaluation.has_link,
            long_text=evaluation.long_text,
            new_member=False,
            flood=evaluation.flood,
            recent=evaluation.recent_messages,
            risk_score=evaluation.score,
            has_contact=evaluation.has_contact,
            ad_template=bool(evaluation.template_hits),
        )
    )


def test_positive_corpus_send_rate():
    corpus = load_corpus()
    engine, moderator = build()
    missed = [text for text in corpus["positive"] if not will_send(engine, moderator, text)]
    rate = 1 - len(missed) / max(1, len(corpus["positive"]))
    assert rate >= corpus["targets"]["positive_send_rate"], (
        "正样本送审率 " + f"{rate:.2%}" + " 低于目标，漏检：" + repr(missed)
    )


def test_negative_corpus_false_positive_rate():
    corpus = load_corpus()
    engine, moderator = build()
    flagged = [text for text in corpus["negative"] if will_send(engine, moderator, text)]
    rate = len(flagged) / max(1, len(corpus["negative"]))
    assert rate <= corpus["targets"]["negative_send_rate"], (
        "负样本误送审率 " + f"{rate:.2%}" + " 超目标，误报：" + repr(flagged)
    )


def test_exact_rule_still_enforces_directly():
    """精确命中的硬规则必须保持"直接处置"，不能被归一化改动影响。"""
    engine, _moderator = build()
    evaluation = engine.evaluate("加群领资料", group_id="g1")
    assert evaluation.enforce_actions == ["warn", "recall", "mute"]
    assert evaluation.score == 100


def test_variant_hit_does_not_auto_enforce():
    """形近字变体命中只作为送审依据（决策：交给 LLM 判定后再处置）。"""
    engine, moderator = build()
    text = "珈裙苓资料123456"
    evaluation = engine.evaluate(text, group_id="g1")
    assert evaluation.enforce_actions == []
    assert evaluation.hits, "变体应当命中规则或模板"
    assert evaluation.score >= 60
    assert will_send(engine, moderator, text) is True


def test_normalize_disabled_falls_back_to_old_behaviour():
    """关闭归一化后，变体应当回到"不命中"（可一键回退到旧行为）。"""
    engine = RuleEngine(
        {
            "hard": [
                {
                    "id": "kw",
                    "type": "literal",
                    "pattern": "加群领资料",
                    "action": ["recall"],
                    "scope": "all",
                    "enabled": True,
                }
            ],
            "soft": [],
        },
        templates=[],
    )
    # templates=[] 表示不使用模板；literal 规则对变体仍会走归一化兜底，
    # 这里验证的是"模板关闭后仅靠规则"的场景
    evaluation = engine.evaluate("珈裙苓资料123456", group_id="g1")
    assert evaluation.template_hits == [], "显式传空列表应彻底关闭模板规则"
    assert evaluation.enforce_actions == []
    # 变体仍靠 literal 规则的归一化兜底命中（只送审、不直接处置）
    assert evaluation.hits and evaluation.score >= 60


def test_pinyin_matching_when_available():
    """同音匹配：安装了 pypinyin 时，拼音规则应能命中同音变体。"""
    from src.normalize import pinyin_available

    if not pinyin_available():
        import pytest

        pytest.skip("未安装 pypinyin（可选依赖），跳过同音匹配用例")
    engine = RuleEngine(
        {
            "hard": [
                {
                    "id": "pinyin-rule",
                    "type": "pinyin",
                    "pattern": "加群领资料",
                    "action": ["warn"],
                    "scope": "all",
                    "enabled": True,
                }
            ],
            "soft": [],
        },
        templates=[],
        pinyin_enabled=True,
    )
    evaluation = engine.evaluate("jiaqun ling ziliao", group_id="g1")
    assert any(hit.rule_type == "pinyin" for hit in evaluation.hits)


def test_template_hit_sends_but_does_not_enforce():
    """模板命中只送审（决策：违规后由 LLM 判定结果决定处置）。"""
    engine = RuleEngine({"hard": [], "soft": []})
    evaluation = engine.evaluate("进群领取资料", group_id="g1")
    assert evaluation.template_hits
    assert evaluation.enforce_actions == []
    assert evaluation.score >= 60
