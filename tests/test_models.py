"""models 单元测试。"""

from __future__ import annotations

from src.models import (
    BotState,
    CapabilityResult,
    GroupConfig,
    GroupProfile,
    Verdict,
    default_settings,
)


def test_bot_state_flags():
    state = BotState(group_openid="g1", member_role="admin", recv_msg_setting="all")
    assert state.is_admin is True
    assert state.full_msg is True
    member = BotState(group_openid="g1", member_role="member", recv_msg_setting="only_mention")
    assert member.is_admin is False
    assert member.full_msg is False


def test_group_profile_from_api():
    profile = GroupProfile.from_api(
        "g1",
        {
            "group_name": "读书分享会",
            "group_finger_memo": "每周一本",
            "group_class_text": "文化",
            "group_tags": ["阅读", "文学"],
            "group_member_num": "256",
        },
    )
    assert profile.name == "读书分享会"
    assert profile.member_num == 256
    assert profile.tags == ["阅读", "文学"]


def test_group_config_roundtrip_and_capability():
    config = GroupConfig.from_dict(
        {
            "group_id": "g1",
            "name": "测试群",
            "moderation_enabled": True,
            "trusted": ["u1", "", "u2"],
            "capabilities": {"mute": {"ok": True}},
        }
    )
    assert config.trusted == ["u1", "u2"]
    assert config.capability_ok("mute") is True
    assert config.capability_ok("recall") is False
    assert GroupConfig.from_dict(config.to_dict()).group_id == "g1"


def test_capability_result_probed_flag():
    result = CapabilityResult.from_dict("remove_member", {"ok": True, "probed": False})
    assert result.probed is False


def test_verdict_clamped():
    verdict = Verdict(
        verdict="maybe", category="", severity=9, confidence=3.0, reason="x" * 500
    ).clamped()
    assert verdict.verdict == "review"
    assert verdict.severity == 5
    assert verdict.confidence == 1.0
    assert len(verdict.reason) == 200
    assert verdict.is_violation is False


def test_default_settings_is_safe():
    settings = default_settings()
    assert settings["dry_run"] is True
    assert settings["mode"] == "lenient"
    assert settings["allow_without_full_msg"] is False
    assert settings["join_review_mode"] == "off"
    settings["mute_steps"]["3"] = 1
    assert default_settings()["mute_steps"]["3"] == 600
