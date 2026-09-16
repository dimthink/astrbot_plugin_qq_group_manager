"""指令文案单元测试。"""

from __future__ import annotations

from src.commands import (
    FULL_MSG_GUIDE,
    group_info_text,
    join_list_text,
    keyword_text,
    log_text,
    menu_text,
    moderation_status_text,
    selfcheck_text,
    stats_text,
)
from src.models import BotState, CapabilityResult, GroupProfile


def test_menu_text_has_sections():
    text = menu_text()
    assert "群信息" in text and "审核状态" in text and "群管理自检" in text
    assert "入群申请" in text and "禁言" in text


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
        join_mode="standard",
    )
    assert "未开启" in text
    assert "未开启（无法启用审核）" in text
    assert "dry-run" in text
    assert "标准" in text
    paused = moderation_status_text(
        group_id="g1",
        enabled=True,
        mode="standard",
        paused_reason="已失去「接收全部消息」能力",
        full_msg=False,
        is_admin=True,
        is_exempt=True,
        dry_run=False,
        stats={"events_total": 3, "verdicts": {"violation": 1, "review": 1}},
    )
    assert "已暂停" in paused and "审核 3 条" in paused


def test_selfcheck_text_lists_capabilities():
    results = {
        "group_info": CapabilityResult("group_info", True),
        "bot_state": CapabilityResult("bot_state", True),
        "is_admin": CapabilityResult("is_admin", False, note="机器人群内角色为 member"),
        "full_msg": CapabilityResult("full_msg", False, note="当前为 only_mention"),
        "remove_member": CapabilityResult("remove_member", False, probed=False),
    }
    text = selfcheck_text("g1", results, group_name="群A")
    assert "群基本信息：可用" in text
    assert "机器人是群管理员：不可用" in text
    assert "无只读探测接口" in text
    assert FULL_MSG_GUIDE.splitlines()[0] in text


def test_keyword_log_stats_join_texts():
    rules = {
        "hard": [{"pattern": "加群", "action": ["recall"], "scope": "all", "enabled": True}],
        "soft": [{"pattern": "私聊", "action": [], "scope": "g1", "enabled": True}],
    }
    text = keyword_text(rules, "g1")
    assert "加群" in text and "私聊" in text and "硬规则" in text

    logs = log_text(
        [
            {
                "ts": "2026-09-10T12:00:00+08:00",
                "verdict": "violation",
                "category": "广告引流",
                "severity": 3,
                "confidence": 0.88,
                "sender_name": "某人",
                "reason": "含链接",
            }
        ]
    )
    assert "violation" in logs and "0.88" in logs
    assert log_text([]) == "暂无审核记录。"

    stats = stats_text(
        {
            "events_total": 5,
            "verdicts": {"violation": 2},
            "actions": {"mute": {"ok": 1, "fail": 1}},
        },
        days=7,
    )
    assert "近 7 天" in stats and "mute" in stats

    pending = [
        {
            "request": {
                "username": "张三",
                "apply_source": "self_apply",
                "verify_info": {"verify_message": "你好"},
            },
            "decision": {"reason": "信息正常"},
        }
    ]
    assert "张三" in join_list_text(pending)
    assert "没有待人工审批" in join_list_text([])
