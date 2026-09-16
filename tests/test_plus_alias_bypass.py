"""规避写法回归：`➕v shhdjdkl` 这类加号变体必须命中，同时不能引入误报。

实测漏检来源：➕(U+2795) 落在 _EMOJI_RE 区间被当表情删除、`+` 被当干扰符删除，
规则层因此完全看不到"加v"。
"""
from __future__ import annotations

import pytest

from src import rules

# 同一句话的各种加号/形近写法，都应命中
PLUS_VARIANTS = [
    "资料 ➕v shhdjdkl",
    "资料+v shhdjdkl",
    "资料＋v shhdjdkl",
    "资料﹢v shhdjdkl",
    "资料✚v shhdjdkl",
    "资料➕vshhdjdkl",
    "＋v shhdjdkl 领资料",
    "有资料 ➕薇 shhdjdkl",
    "资料 ➕微 shhdjdkl",
]

# 正常消息不应因为"+"被映射成"加"而误报
NEGATIVE_SAMPLES = [
    "C++ 和 Python 哪个好写",
    "3+2=5，这题太简单了",
    "我算了一下 1+1=2 没错",
    "今天打了 2+2 小时的比赛",
    "版本 v1.2.3 更新了",
    "这个函数是 f(x)=x+1",
]


@pytest.fixture()
def engine() -> rules.RuleEngine:
    return rules.RuleEngine()


@pytest.mark.parametrize("text", PLUS_VARIANTS)
def test_plus_variants_are_detected(engine, text):
    evaluation = engine.evaluate(text=text, group_id="demo")
    assert evaluation.score >= 60, (text, evaluation.score)
    # 至少要有模板命中或联系方式信号
    assert evaluation.template_hits or evaluation.has_contact, text


@pytest.mark.parametrize("text", NEGATIVE_SAMPLES)
def test_plus_mapping_does_not_create_false_positives(engine, text):
    evaluation = engine.evaluate(text=text, group_id="demo")
    assert evaluation.score < 70, (text, evaluation.score)
    assert not evaluation.template_hits, (text, [h.pattern for h in evaluation.hits])


def test_plus_symbol_is_not_stripped_before_mapping():
    """骨架视图里必须能看到"加"，而不是把 ➕ 丢掉。"""
    from src.normalize import normalize

    views = normalize("资料 ➕v shhdjdkl", homoglyph=None, with_pinyin=False)
    assert "加v" in views.skeleton, views.skeleton
    assert "资料加v" in views.skeleton, views.skeleton
