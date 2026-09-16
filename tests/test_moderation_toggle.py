"""审核启用流程测试（set_moderation 的前置校验与状态写入）。"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path

from src.models import CAP_BOT_STATE, CAP_FULL_MSG, CAP_IS_ADMIN, CapabilityResult
from src.store import PluginStore
from tests.fakes import FakeKV

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _load_main():
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    return importlib.import_module(f"{PLUGIN_ROOT.name}.main")


def make_store() -> PluginStore:
    store = PluginStore(FakeKV())
    asyncio.run(store.load())
    asyncio.run(store.ensure_group("g1", name="测试群"))
    return store


def make_service(results):
    """构造只带 set_moderation 所需依赖的假服务对象。"""

    class FakeApi:
        def dry_run(self) -> bool:
            return True

    class Service:
        pass

    service = Service()
    service.store = make_store()
    service.logger = logging.getLogger("qqgm-test")
    service.api = FakeApi()

    async def probe_group(group_id, caller="probe"):
        del caller
        return results

    service.probe_group = probe_group
    return service


def capabilities(*, bot_state=True, full_msg=True, is_admin=True):
    return {
        CAP_BOT_STATE: CapabilityResult(CAP_BOT_STATE, bot_state),
        CAP_FULL_MSG: CapabilityResult(CAP_FULL_MSG, full_msg),
        CAP_IS_ADMIN: CapabilityResult(CAP_IS_ADMIN, is_admin, note="机器人群内角色为 member"),
    }


def test_enable_moderation_succeeds_when_full_msg_available():
    main = _load_main()
    service = make_service(capabilities())
    result = asyncio.run(main.QQGroupManager.set_moderation(service, "g1", True, caller="test"))
    assert result["ok"] is True
    assert service.store.group("g1").moderation_enabled is True
    assert service.store.group("g1").paused_reason == ""


def test_enable_moderation_warns_when_not_group_admin():
    main = _load_main()
    service = make_service(capabilities(is_admin=False))
    result = asyncio.run(main.QQGroupManager.set_moderation(service, "g1", True, caller="test"))
    assert result["ok"] is True
    assert result["warnings"], "非群管理员时必须给出降级提示"
    assert service.store.group("g1").moderation_enabled is True


def test_enable_moderation_rejected_without_full_msg():
    main = _load_main()
    service = make_service(capabilities(full_msg=False))
    result = asyncio.run(main.QQGroupManager.set_moderation(service, "g1", True, caller="test"))
    assert result["ok"] is False
    assert result["reason_code"] == "need_full_msg"
    assert "接收全部消息" in result["message"]
    assert service.store.group("g1").moderation_enabled is False


def test_enable_moderation_rejected_without_bot_state():
    main = _load_main()
    service = make_service(capabilities(bot_state=False))
    result = asyncio.run(main.QQGroupManager.set_moderation(service, "g1", True, caller="test"))
    assert result["ok"] is False
    assert result["reason_code"] == "bot_state_unavailable"
    assert service.store.group("g1").moderation_enabled is False


def test_allow_without_full_msg_escape_hatch():
    main = _load_main()
    service = make_service(capabilities(full_msg=False))
    asyncio.run(service.store.update_settings({"allow_without_full_msg": True}))
    result = asyncio.run(main.QQGroupManager.set_moderation(service, "g1", True, caller="test"))
    assert result["ok"] is True
    assert any("覆盖率不完整" in item for item in result["warnings"])


def test_disable_moderation_clears_pause_reason():
    main = _load_main()
    service = make_service(capabilities())
    asyncio.run(
        service.store.update_group("g1", {"moderation_enabled": True, "paused_reason": "x"})
    )
    result = asyncio.run(main.QQGroupManager.set_moderation(service, "g1", False, caller="test"))
    assert result["ok"] is True
    assert service.store.group("g1").moderation_enabled is False
    assert service.store.group("g1").paused_reason == ""


def test_private_message_handler_registered():
    main = _load_main()
    assert hasattr(main.QQGroupManager, "on_private_message")
    assert hasattr(main.QQGroupManager, "on_group_message")
