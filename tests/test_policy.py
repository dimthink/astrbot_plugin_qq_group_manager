"""ApprovalPolicyService 单元测试。"""

from __future__ import annotations

import asyncio

from src.api_client import QQGroupAPI
from src.policy import ApprovalPolicyService
from src.store import PluginStore
from tests.fakes import FakeKV, FakeTransport

STRATEGY_PATH = "/v2/groups/join_approval_strategy"


def run(coro):
    return asyncio.run(coro)


def test_conflicts_detection():
    transport = FakeTransport(
        {
            ("GET", STRATEGY_PATH): {
                "strategies": [
                    {
                        "strategy_id": "st_1",
                        "group_openids": ["g1"],
                        "is_enable": "on",
                    },
                    {
                        "strategy_id": "st_2",
                        "group_openids": ["g2"],
                        "is_enable": "off",
                    },
                ],
                "next_cursor": "",
            }
        }
    )
    store = PluginStore(FakeKV())
    run(store.load())
    run(store.ensure_group("g1"))
    run(store.ensure_group("g2"))
    run(store.update_settings({"join_review_mode": "off"}))
    run(store.update_group("g1", {"join_review_mode": "standard"}))
    run(store.update_group("g2", {"join_review_mode": "standard"}))
    service = ApprovalPolicyService(api=QQGroupAPI(transport), store=store)
    result = run(service.conflicts(["g1", "g2"]))
    assert result["checked"] is True
    assert result["conflicts"] == ["g1"]  # g2 的策略未启用


def test_conflicts_reports_unavailable_interface():
    transport = FakeTransport(
        {("GET", STRATEGY_PATH): {"err_code": 11253, "message": "应用无接口访问权限"}}
    )
    service = ApprovalPolicyService(api=QQGroupAPI(transport))
    result = run(service.conflicts(["g1"]))
    assert result["checked"] is False
    assert "11253" in result["error"] or "权限" in result["error"]


def test_strategy_cache_and_invalidation():
    calls = {"n": 0}

    def handler(path_params, query, json_body):
        calls["n"] += 1
        return {"strategies": [{"strategy_id": f"st_{calls['n']}"}], "next_cursor": ""}

    transport = FakeTransport({("GET", STRATEGY_PATH): handler})
    service = ApprovalPolicyService(api=QQGroupAPI(transport))
    first = run(service.list_strategies())
    second = run(service.list_strategies())
    assert first == second and calls["n"] == 1  # 命中缓存
    third = run(service.list_strategies(force=True))
    assert calls["n"] == 2 and third != first


def test_whitelist_and_enable_paths():
    transport = FakeTransport(
        {
            ("GET", STRATEGY_PATH): {"strategies": []},
            ("POST", STRATEGY_PATH + "/{strategy_id}/whitelist_users"): {"whitelist_user_count": 2},
            ("PATCH", STRATEGY_PATH + "/{strategy_id}"): {"is_enable": "off"},
            ("POST", STRATEGY_PATH + "/{strategy_id}/execute"): {},
        }
    )
    service = ApprovalPolicyService(api=QQGroupAPI(transport))
    whitelist = run(service.update_whitelist("st_1", op="add", users=["1234567", "1234568"]))
    assert whitelist["whitelist_user_count"] == 2
    body = [call for call in transport.calls if call["method"] == "POST"][-1]["json"]
    assert body["op"] == "add" and body["whitelist_users"] == ["1234567", "1234568"]
    assert run(service.set_enabled("st_1", False))["is_enable"] == "off"
    assert run(service.execute("st_1")) == {}
