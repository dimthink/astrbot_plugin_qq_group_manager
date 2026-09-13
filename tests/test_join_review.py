"""JoinReviewer 单元测试：判定链、阈值、审批、去重。"""

from __future__ import annotations

import asyncio

from src.api_client import QQGroupAPI
from src.join_review import JoinReviewer
from src.store import PluginStore
from tests.fakes import FakeAudit, FakeKV, FakeTransport

LIST_PATH = "/v2/groups/{group_openid}/join_request_list"
APPROVE_PATH = "/v2/groups/{group_openid}/approval_join_request/{member_openid}"


def run(coro):
    return asyncio.run(coro)


def make_env(
    *,
    response_text='{"decision":"approve","confidence":0.9,"reason":"信息正常"}',
    transport=None,
    judge_error=None,
):
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.ensure_group("g1", name="测试群"))
    fake_transport = transport or FakeTransport(
        {
            ("GET", LIST_PATH): {"list": [], "next_cursor": ""},
            ("POST", APPROVE_PATH): {},
        }
    )
    api = QQGroupAPI(fake_transport)
    audit = FakeAudit()
    api.audit = audit

    async def judge_call(system_prompt, user_prompt):
        assert "申请人昵称" in user_prompt
        if judge_error:
            raise judge_error
        return response_text

    reviewer = JoinReviewer(api=api, store=store, audit=audit, judge_call=judge_call)
    return store, api, audit, reviewer, fake_transport


def request_payload(**overrides):
    payload = {
        "join_request_id": "j1",
        "member_openid": "u1",
        "username": "张三",
        "apply_source": "self_apply",
        "risk_tips": "",
        "bot": False,
        "verify_info": {"method": "verify_message", "verify_message": "你好"},
    }
    payload.update(overrides)
    return payload


def test_judge_hard_rules():
    store, _, _, reviewer, _ = make_env()
    top = run(reviewer.judge("g1", request_payload(risk_tips="top_tips"), mode="standard"))
    assert top.op == "decline" and top.auto and top.blacklist

    bot = run(reviewer.judge("g1", request_payload(bot=True), mode="standard"))
    assert bot.op == "decline" and bot.auto

    run(store.update_local_blacklist("g1", ["u1"]))
    blocked = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert blocked.op == "decline" and "黑名单" in blocked.reason

    run(store.update_local_blacklist("g1", []))
    run(store.update_settings({"join_trust_inviter": True}))
    invited = run(reviewer.judge("g1", request_payload(apply_source="invited"), mode="standard"))
    assert invited.op == "approve" and invited.auto


def test_judge_llm_decisions_and_threshold():
    _, _, _, reviewer, _ = make_env(
        response_text='{"decision":"approve","confidence":0.95,"reason":"正常"}'
    )
    decision = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert decision.op == "approve" and decision.auto and decision.source == "llm"

    _, _, _, strict, _ = make_env(
        response_text='{"decision":"decline","confidence":0.95,"reason":"广告"}'
    )
    declined = run(strict.judge("g1", request_payload(), mode="standard"))
    assert declined.op == "decline" and declined.blacklist is True

    _, _, _, low, _ = make_env(
        response_text='{"decision":"approve","confidence":0.3,"reason":"不确定"}'
    )
    manual = run(low.judge("g1", request_payload(), mode="standard"))
    assert manual.auto is False and manual.source == "manual"

    strict_mode = run(low.judge("g1", request_payload(), mode="strict"))
    assert strict_mode.op == "decline" and strict_mode.auto is True


def test_judge_human_mode_and_llm_failure():
    _, _, _, reviewer, _ = make_env()
    human = run(reviewer.judge("g1", request_payload(), mode="human"))
    assert human.auto is False and human.source == "manual"

    _, _, _, broken, _ = make_env(judge_error=RuntimeError("boom"))
    result = run(broken.judge("g1", request_payload(), mode="standard"))
    assert result.auto is False and "模型调用失败" in result.reason
    assert broken.stats.failed == 1


def test_parse_decision_invalid_json():
    _, _, _, reviewer, _ = make_env()
    decision = reviewer.parse_decision("不是 JSON", request_payload(), settings={}, mode="standard")
    assert decision.auto is False and decision.source == "manual"


def test_poll_group_auto_approves_and_dedupes():
    transport = FakeTransport(
        {
            ("GET", LIST_PATH): {
                "list": [request_payload()],
                "next_cursor": "next-1",
            },
            ("POST", APPROVE_PATH): {"trace_id": "t1"},
        }
    )
    store, _api, audit, reviewer, _ = make_env(transport=transport)
    run(store.update_group("g1", {"join_review_mode": "standard"}))
    created = run(reviewer.poll_group("g1"))
    assert created == []  # 自动审批，不进入待审
    assert reviewer.stats.approved == 1
    assert audit.joins["j1"]["decision"] == "approve"
    assert run(store.get_join_cursor("g1")) == "next-1"

    # 再次轮询不应重复审批（数据库已记录）
    before = len(transport.calls)
    run(reviewer.poll_group("g1"))
    approve_calls = [call for call in transport.calls[before:] if call["path"] == APPROVE_PATH]
    assert approve_calls == []


def test_poll_group_keeps_pending_for_human():
    transport = FakeTransport(
        {
            ("GET", LIST_PATH): {"list": [request_payload()], "next_cursor": ""},
            ("POST", APPROVE_PATH): {},
        }
    )
    store, _api, audit, reviewer, _ = make_env(
        transport=transport, response_text='{"decision":"approve","confidence":0.2}'
    )
    run(store.update_group("g1", {"join_review_mode": "standard"}))
    created = run(reviewer.poll_group("g1"))
    assert len(created) == 1
    pending = reviewer.list_pending("g1")
    assert len(pending) == 1
    assert audit.joins["j1"]["decision"] == "pending"

    result = run(
        reviewer.decide_manual("g1", "u1", op="approve", join_request_id="j1", by="human:tester")
    )
    assert result["ok"] is True
    assert reviewer.list_pending("g1") == []
    assert audit.joins["j1"]["decided_by"] == "human:tester"


def test_submit_failure_is_recorded():
    transport = FakeTransport(
        {
            ("GET", LIST_PATH): {"list": [request_payload()], "next_cursor": ""},
            ("POST", APPROVE_PATH): {"err_code": 11282, "message": "检查是否是管理员未通过"},
        }
    )
    store, _api, audit, reviewer, _ = make_env(transport=transport)
    run(store.update_group("g1", {"join_review_mode": "standard"}))
    run(reviewer.poll_group("g1"))
    assert reviewer.stats.failed == 1
    assert "11282" in (audit.joins["j1"]["reason"] or "") or "管理员" in (
        audit.joins["j1"]["reason"] or ""
    )
