"""RuleEngine 单元测试。"""

from __future__ import annotations

from src.rules import RuleEngine


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def make_engine(keywords=None, clock=None) -> RuleEngine:
    engine = RuleEngine(keywords or {})
    if clock is not None:
        engine._clock = clock.monotonic
    return engine


def test_literal_hard_and_soft_rules():
    engine = make_engine(
        {
            "hard": [
                {
                    "id": "h1",
                    "type": "literal",
                    "pattern": "加群",
                    "action": ["recall", "mute"],
                    "scope": "all",
                    "enabled": True,
                    "note": "广告引流",
                }
            ],
            "soft": [{"id": "s1", "type": "regex", "pattern": "私\\s*聊", "scope": "all"}],
        }
    )
    evaluation = engine.evaluate("快来加群一起玩", group_id="g1")
    assert len(evaluation.hard_hits) == 1
    assert evaluation.hard_actions == ["recall", "mute"]
    assert evaluation.suspicious is True

    soft = engine.evaluate("可以私 聊 我", group_id="g1")
    assert soft.hard_hits == []
    assert len(soft.soft_hits) == 1


def test_scope_and_enabled_filters():
    engine = make_engine(
        {
            "hard": [
                {"id": "a", "type": "literal", "pattern": "禁止", "scope": "g2"},
                {"id": "b", "type": "literal", "pattern": "停用", "scope": "all", "enabled": False},
            ]
        }
    )
    assert engine.evaluate("禁止", group_id="g1").hits == []
    assert len(engine.evaluate("禁止", group_id="g2").hits) == 1
    assert engine.evaluate("停用", group_id="g1").hits == []


def test_invalid_regex_is_skipped():
    engine = make_engine({"hard": [{"id": "x", "type": "regex", "pattern": "([", "scope": "all"}]})
    assert engine.evaluate("任意文本", group_id="g1").hits == []


def test_builtin_detectors():
    engine = make_engine()
    link = engine.evaluate("看这个 https://example.com/a 或 t.me/abc", group_id="g1")
    assert link.has_link is True
    contact = engine.evaluate("微信：abc12345 联系我", group_id="g1")
    assert contact.has_contact is True
    repeated = engine.evaluate("啊啊啊啊啊啊啊啊", group_id="g1")
    assert repeated.repeated is True
    long_text = engine.evaluate("字" * 700, group_id="g1")
    assert long_text.long_text is True
    assert engine.evaluate("正常聊天", group_id="g1").suspicious is False


def test_flood_counting_and_window():
    clock = FakeClock()
    engine = make_engine(clock=clock)
    for _ in range(3):
        assert engine.note_message("g1", "u1") <= 3
    assert engine.note_message("g1", "u1") == 4
    assert engine.note_message("g1", "u2") == 1
    clock.now = 120.0  # 超过 60 秒窗口后重新计数
    assert engine.note_message("g1", "u1") == 1

    evaluation = engine.with_flood(engine.evaluate("hi", group_id="g1"), 9, 8)
    assert evaluation.flood is True
    assert "高频发言" in evaluation.summary()


def test_reload_replaces_rules():
    engine = make_engine({"hard": [{"pattern": "旧", "scope": "all"}]})
    assert len(engine.evaluate("旧", group_id="g1").hits) == 1
    engine.reload({"hard": [{"pattern": "新", "scope": "all"}]})
    assert engine.evaluate("旧", group_id="g1").hits == []
    assert len(engine.evaluate("新", group_id="g1").hits) == 1
