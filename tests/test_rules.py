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


# --------------------------------------------------------------------------
# B3 竞赛域名白名单（只降权，不豁免）
# --------------------------------------------------------------------------
from src.links import allowlisted_domains, extract_domains  # noqa: E402

COMPETITION = "codeforces.com/contest/1"
ALLOWLIST = ["ac.nowcoder.com", "nowcoder.com", "codeforces.com", "atcoder.jp", "luogu.com.cn"]


def test_domain_allowlist_pure_competition_link_is_discounted():
    engine = make_engine()
    evaluation = engine.evaluate(
        "题解看 https://codeforces.com/contest/1", group_id="g1", allowlisted=True
    )
    assert evaluation.has_link is False
    assert evaluation.signals.get("link_allowlisted") is True
    assert "link" not in evaluation.signals
    assert evaluation.score == 0


def test_domain_allowlist_mixed_short_link_still_counts():
    text = "题解看 codeforces.com/contest/1 或 t.cn/abcdef"
    all_allowed, hits = allowlisted_domains(text, ALLOWLIST)
    assert all_allowed is False
    assert hits == {"codeforces.com"}
    engine = make_engine()
    evaluation = engine.evaluate(text, group_id="g1", allowlisted=all_allowed)
    assert evaluation.has_link is True
    assert evaluation.signals.get("link") == 25
    assert "link_allowlisted" not in evaluation.signals


def test_domain_allowlist_recognizes_bare_port_upper_and_cn_punctuation():
    text = "裸域名 codeforces.com/contest/1、带端口 CODEforces.com:8080、大写 ATCODER.JP、"
    text += "中文标点『luogu.com.cn』以及 nowcoder 点 com"
    domains = extract_domains(text)
    assert "codeforces.com" in domains
    assert "atcoder.jp" in domains
    assert "luogu.com.cn" in domains
    assert "nowcoder.com" in domains
    all_allowed, hits = allowlisted_domains(text, ALLOWLIST)
    assert all_allowed is True
    assert hits == domains


def test_domain_allowlist_subdomain_matches_but_lookalike_does_not():
    matched, hits = allowlisted_domains("m1.codeforces.com/contest/1", ["codeforces.com"])
    assert matched is True
    assert hits == {"m1.codeforces.com"}
    assert allowlisted_domains("fake-codeforces.com", ["codeforces.com"]) == (False, set())
    assert allowlisted_domains("codeforces.com.evil.com", ["codeforces.com"]) == (False, set())


def test_domain_allowlist_disabled_or_empty_matches_current_behaviour():
    engine = make_engine()
    baseline = engine.evaluate(COMPETITION, group_id="g1")
    explicit_off = engine.evaluate(COMPETITION, group_id="g1", allowlisted=False)
    assert baseline.score == explicit_off.score
    assert baseline.signals == explicit_off.signals
    assert baseline.has_link is True
    assert allowlisted_domains(COMPETITION, []) == (False, set())
    assert allowlisted_domains("没有链接的普通聊天", ALLOWLIST) == (False, set())


def test_domain_allowlist_keeps_rules_and_audit_trail():
    engine = make_engine(
        {
            "hard": [
                {
                    "id": "h1",
                    "type": "literal",
                    "pattern": "加群",
                    "action": ["mute"],
                    "scope": "all",
                    "enabled": True,
                    "note": "广告引流",
                }
            ]
        }
    )
    text = "加群 codeforces.com/contest/1"
    plain = engine.evaluate(text, group_id="g1")
    discounted = engine.evaluate(text, group_id="g1", allowlisted=True)
    assert plain.has_link is True and plain.signals.get("link") == 25
    assert discounted.has_link is False
    assert discounted.signals.get("link_allowlisted") is True
    # 规则仍然命中、动作仍然可执行：白名单不是后门
    assert [hit.rule_id for hit in discounted.hard_hits] == ["h1"]
    assert discounted.enforce_actions == ["mute"]
