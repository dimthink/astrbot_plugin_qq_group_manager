"""PluginStore 单元测试（KV 归一化、群配置、成员缓存）。"""

from __future__ import annotations

import asyncio

from src.store import KEY_GROUPS, KEY_SETTINGS, PluginStore, normalize_settings
from tests.fakes import FakeKV


def run(coro):
    return asyncio.run(coro)


def test_normalize_settings_clamps_and_fills():
    settings = normalize_settings(
        {
            "sample_rate": 5,
            "llm_timeout": 999,
            "mode": "unknown-mode",
            "send_conditions": ["bogus"],
            "mute_steps": "bad",
        }
    )
    assert settings["sample_rate"] == 1.0
    assert settings["llm_timeout"] == 120
    assert settings["mode"] == "lenient"
    assert settings["send_conditions"] == ["rule_hit"]
    assert settings["mute_steps"]["4"] == 3600


def test_first_run_writes_defaults():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    assert kv.data[KEY_SETTINGS]["dry_run"] is True
    assert kv.data[KEY_SETTINGS]["mode"] == "lenient"
    assert store.dry_run() is True


def test_update_settings_persists():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    updated = run(store.update_settings({"dry_run": False, "mode": "standard"}))
    assert updated["dry_run"] is False
    assert kv.data[KEY_SETTINGS]["mode"] == "standard"


def test_group_lifecycle():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.touch_group("g1", name="测试群"))
    config = store.group("g1")
    assert config is not None and config.name == "测试群"
    run(store.update_group("g1", {"moderation_enabled": True, "paused_reason": "x"}))
    assert store.group("g1").moderation_enabled is True
    assert store.group("g1").paused_reason == "x"
    run(store.flush())
    assert "g1" in kv.data[KEY_GROUPS]
    assert run(store.remove_group("g1")) is True
    assert store.group("g1") is None


def test_member_and_role_cache():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.remember_member("g1", "u1", name="张三", role="admin"))
    run(store.remember_member("g1", "u2", name="李四", role="member"))
    assert store.member_name("g1", "u1") == "张三"
    assert store.member_role("g1", "u1") == "admin"
    assert store.is_group_admin("g1", "u1") is True
    assert store.is_group_admin("g1", "u2") is False
    assert store.find_member_by_name("g1", "@张") == [("u1", "张三")]


def test_trusted_and_blacklist():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.update_trusted("g1", ["u1", " ", "u2"]))
    assert store.trusted("g1") == ["u1", "u2"]
    run(store.update_local_blacklist("g1", ["bad1"]))
    assert store.local_blacklist("g1") == ["bad1"]


def test_kv_failure_keeps_dirty_and_does_not_raise():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    kv.fail_keys.add(KEY_SETTINGS)
    run(store.update_settings({"dry_run": False}))
    assert store.dry_run() is False
