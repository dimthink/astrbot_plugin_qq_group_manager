"""LLMModerator 单元测试：解析、阈值、缓存、熔断、预算、采样。"""

from __future__ import annotations

import asyncio

from src.moderator import (
    LLMModerator,
    ModerationRequest,
    extract_json_object,
    parse_verdict,
)

VALID = (
    '{"verdict":"violation","category":"广告引流","severity":4,'
    '"confidence":0.92,"reason":"含引流链接","suggested_action":"mute_and_recall"}'
)


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def make_settings(**overrides):
    settings = {
        "llm_min_confidence": 0.7,
        "cache_ttl": 600,
        "circuit_break_threshold": 3,
        "sample_rate": 1.0,
        "llm_daily_budget": 0,
        "llm_timeout": 5,
    }
    settings.update(overrides)
    return lambda: dict(settings)


def make_moderator(response=VALID, clock=None, **settings):
    calls = {"n": 0}

    async def provider_call(request, system_prompt, user_prompt):
        calls["n"] += 1
        assert "<<<MESSAGE" in user_prompt
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(calls["n"])
        return response

    moderator = LLMModerator(
        provider_call,
        settings_getter=make_settings(**settings),
        clock=(clock or FakeClock()).monotonic,
    )
    return moderator, calls


def test_extract_json_object_handles_noise_and_nesting():
    assert extract_json_object('前言 {"a": {"b": 1}} 后记') == {"a": {"b": 1}}
    assert extract_json_object('{"text": "包含 } 的字符串", "verdict": "allow"}') == {
        "text": "包含 } 的字符串",
        "verdict": "allow",
    }
    assert extract_json_object("没有 JSON") is None
    assert extract_json_object("") is None


def test_parse_verdict_clamps_and_flags_errors():
    verdict = parse_verdict(VALID, latency_ms=12)
    assert verdict.verdict == "violation"
    assert verdict.category == "广告引流"
    assert verdict.severity == 4
    assert verdict.confidence == 0.92
    assert verdict.latency_ms == 12

    weird = parse_verdict(
        '{"verdict":"nonsense","category":"不存在","severity":99,"confidence":5,'
        '"suggested_action":"explode"}'
    )
    assert weird.verdict == "review"
    assert weird.category == "其他"
    assert weird.severity == 5
    assert weird.confidence == 1.0
    assert weird.suggested_action == "none"

    broken = parse_verdict("模型抽风了")
    assert broken.verdict == "review"
    assert broken.parse_error is True


def test_judge_returns_verdict_and_caches():
    clock = FakeClock()
    moderator, calls = make_moderator(clock=clock)
    request = ModerationRequest(group_id="g1", text="加群送资料", sender_role="member")
    first = run(moderator.judge(request))
    assert first.verdict == "violation"
    assert calls["n"] == 1
    second = run(moderator.judge(request))
    assert second.verdict == "violation"
    assert calls["n"] == 1  # 命中缓存
    assert moderator.stats.cache_hits == 1
    clock.now = 1000.0  # 缓存过期后重新调用
    run(moderator.judge(request))
    assert calls["n"] == 2


def test_low_confidence_downgrades_to_review():
    response = (
        '{"verdict":"violation","category":"辱骂攻击","severity":3,'
        '"confidence":0.4,"reason":"疑似","suggested_action":"mute"}'
    )
    moderator, _ = make_moderator(response=response)
    verdict = run(moderator.judge(ModerationRequest(group_id="g1", text="你个笨蛋")))
    assert verdict.verdict == "review"
    assert "置信度不足" in verdict.reason
    assert verdict.suggested_action == "none"


def test_failures_trigger_circuit_breaker():
    moderator, calls = make_moderator(response=RuntimeError("boom"))
    request = ModerationRequest(group_id="g1", text="第一条")
    verdict = run(moderator.judge(request))
    assert verdict.verdict == "review"
    assert moderator.stats.failures == 1
    run(moderator.judge(ModerationRequest(group_id="g1", text="第二条")))
    run(moderator.judge(ModerationRequest(group_id="g1", text="第三条")))
    assert moderator.circuit_open() is True
    before = calls["n"]
    run(moderator.judge(ModerationRequest(group_id="g1", text="第四条")))
    assert calls["n"] == before  # 熔断期间不再调用模型


def test_budget_and_sampling_skip_calls():
    moderator, calls = make_moderator(llm_daily_budget=1)
    run(moderator.judge(ModerationRequest(group_id="g1", text="一")))
    assert calls["n"] == 1
    verdict = run(moderator.judge(ModerationRequest(group_id="g1", text="二")))
    assert calls["n"] == 1
    assert "预算" in verdict.reason

    silent, silent_calls = make_moderator(sample_rate=0.0)
    verdict = run(silent.judge(ModerationRequest(group_id="g1", text="三")))
    assert silent_calls["n"] == 0
    assert "采样" in verdict.reason


def test_should_send_conditions():
    moderator, _ = make_moderator(send_conditions=["rule_hit", "has_link"])
    assert moderator.should_send(
        rule_summary="", has_link=True, long_text=False, new_member=False, flood=False, recent=1
    )
    assert not moderator.should_send(
        rule_summary="", has_link=False, long_text=False, new_member=False, flood=False, recent=1
    )
    only_all, _ = make_moderator(send_conditions=["all"])
    assert only_all.should_send(
        rule_summary="", has_link=False, long_text=False, new_member=False, flood=False, recent=0
    )


def test_unavailable_provider_is_safe():
    moderator = LLMModerator(None, settings_getter=make_settings())
    verdict = run(moderator.judge(ModerationRequest(group_id="g1", text="hi")))
    assert verdict.verdict == "review"
    assert moderator.available() is False


def test_choose_provider_id_prefers_configured():
    from src.moderator import choose_provider_id

    # 未配置 → 用会话默认
    assert choose_provider_id("", "session-provider") == "session-provider"
    # 配置了且存在 → 用配置的
    assert (
        choose_provider_id("mod-provider", "session-provider", ["mod-provider", "x"])
        == "mod-provider"
    )
    # 配置的已不存在 → 回退会话默认
    assert (
        choose_provider_id("gone", "session-provider", ["session-provider"]) == "session-provider"
    )
    # 无法枚举模型（老版本）时信任配置值
    assert choose_provider_id("mod-provider", "", []) == "mod-provider"
    # 两者都为空 → 空串（由调用方走 get_using_provider_async 兜底）
    assert choose_provider_id("", "", []) == ""


def test_llm_provider_id_is_editable_and_persisted():
    import asyncio

    from src.store import PluginStore, normalize_settings
    from tests.fakes import FakeKV

    assert normalize_settings({"llm_provider_id": "abc"})["llm_provider_id"] == "abc"
    assert normalize_settings({})["llm_provider_id"] == ""

    async def scenario():
        kv = FakeKV()
        store = PluginStore(kv)
        await store.load()
        updated = await store.update_settings({"llm_provider_id": "mod-provider"})
        assert updated["llm_provider_id"] == "mod-provider"
        assert kv.data["settings"]["llm_provider_id"] == "mod-provider"

    asyncio.run(scenario())
