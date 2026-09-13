"""指令文案单元测试。"""

from __future__ import annotations

from src.commands import (
    FULL_MSG_GUIDE,
    group_info_text,
    menu_text,
    moderation_status_text,
    selfcheck_text,
)
from src.models import BotState, CapabilityResult, GroupProfile


def test_menu_text_has_sections():
    text = menu_text()
    assert "群信息" in text and "审核状态" in text and "群管理自检" in text


def test_group_info_text_with_data():
    profile = GroupProfile.from_api("g1", {"group_name": "群A", "group_member_num": 8})
    state = BotState(
        group_openid="g1",
        member_role="admin",
        recv_msg_setting="all",
        allow_proactive_msg=True,
    )
    text = group_info_text(profile, state, group_id="g1")
    assert "群A" in text and "成员数：8" in text
    assert "管理员" in text and "接收全部消息" in text


def test_group_info_text_degrades_without_platform():
    text = group_info_text(None, None, group_id="g1")
    assert "平台未返回" in text


def test_moderation_status_variants():
    text = moderation_status_text(
        group_id="g1",
        enabled=False,
        mode="lenient",
        paused_reason="",
        full_msg=False,
        is_admin=False,
        is_exempt=False,
        dry_run=True,
    )
    assert "未开启" in text
    assert "未开启（无法启用审核）" in text
    assert "dry-run" in text
    paused = moderation_status_text(
        group_id="g1",
        enabled=True,
        mode="standard",
        paused_reason="已失去「接收全部消息」能力",
        full_msg=False,
        is_admin=True,
        is_exempt=True,
        dry_run=False,
    )
    assert "已暂停" in paused


def test_selfcheck_text_lists_capabilities():
    results = {
        "group_info": CapabilityResult("group_info", True),
        "bot_state": CapabilityResult("bot_state", True),
        "is_admin": CapabilityResult("is_admin", False, note="机器人群内角色为 member"),
        "full_msg": CapabilityResult("full_msg", False, note="当前为 only_mention"),
        "remove_member": CapabilityResult("remove_member", False, probed=False),
    }
    text = selfcheck_text("g1", results, group_name="群A")
    assert "群基本信息：✅" in text
    assert "机器人是群管理员：❌" in text
    assert "无只读探测接口" in text
    assert FULL_MSG_GUIDE.splitlines()[0] in text
