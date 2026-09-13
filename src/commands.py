"""指令文案与路由（M1：菜单 / 群信息 / 审核状态 / 自检 / 配置提示）。

指令采用「全匹配 + 空格分词」，不依赖 AstrBot 的唤醒判定，因此在群开启
「接收全部消息」后依然可用（普通聊天不会误触发，因为必须完整等于指令名）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .api_client import QQApiError
from .models import CAP_FULL_MSG, CAP_IS_ADMIN, CAPABILITY_LABELS, BotState, GroupProfile
from .utils import human_duration, mask_openid, to_iso

MENU_COMMANDS = ("群管理菜单", "群管菜单", "qq群管理")
INFO_COMMANDS = ("群信息",)
STATUS_COMMANDS = ("审核状态",)
SELFCHECK_COMMANDS = ("群管理自检",)
CONFIG_COMMANDS = ("群管理配置",)

ALL_COMMANDS: tuple[str, ...] = (
    *MENU_COMMANDS,
    *INFO_COMMANDS,
    *STATUS_COMMANDS,
    *SELFCHECK_COMMANDS,
    *CONFIG_COMMANDS,
)

FULL_MSG_GUIDE = (
    "请用手机 QQ 打开本群 → 右上角「设置」→「机器人」→ 选中本机器人 → "
    "打开「接收全部消息」，然后回到这里点「重新检测」。\n"
    "（未开启时平台只会把 @机器人 的消息推送给机器人，审核会漏掉大量内容，"
    "因此插件拒绝在未开启时启用审核。）"
)

WEBUI_HINT = "完整配置与日志：AstrBot WebUI → 插件管理 → 「QQ群管理」→ 管理台页面。"


def menu_text() -> str:
    """菜单文本（供指令与 menu.md 复用）。"""
    return (
        "🛡️ QQ群管理\n"
        "──────────────\n"
        "👥 所有人\n"
        "• 群信息 ─ 本群档案与机器人在群状态\n"
        "• 审核状态 ─ 审核开关、模式与我的豁免状态\n"
        "• 群管理菜单 ─ 显示本菜单\n"
        "──────────────\n"
        "🔑 群主 / 群管理员\n"
        "• 审核开启 / 审核关闭\n"
        "• 审核模式 严格/标准/宽松/仅记录\n"
        "• 审核阈值 0.0-1.0\n"
        "• 关键词 添加/删除/列表 · 信任 @某人\n"
        "• 禁言 @某人 [时长] · 解禁 @某人 · 撤回\n"
        "• 审核日志 / 审核统计 / 入群申请\n"
        "──────────────\n"
        "⚙️ AstrBot 管理员\n"
        "• 群管理自检 ─ 平台能力探测\n"
        "• 群管理配置 ─ 打开管理台指引\n"
        "──────────────\n"
        "提示：指令为全匹配，可带 / 前缀。"
    )


def group_info_text(
    profile: GroupProfile | None,
    state: BotState | None,
    *,
    group_id: str,
    profile_error: QQApiError | None = None,
    state_error: QQApiError | None = None,
) -> str:
    """群档案 + 机器人在群状态。"""
    lines = [f"📋 群档案（{mask_openid(group_id)}）"]
    if profile is not None:
        lines.append(f"• 群名称：{profile.name or '（未返回）'}")
        if profile.memo:
            lines.append(f"• 群简介：{profile.memo}")
        if profile.category:
            lines.append(f"• 分类：{profile.category}")
        if profile.tags:
            lines.append(f"• 标签：{'、'.join(profile.tags[:8])}")
        if profile.member_num:
            lines.append(f"• 成员数：{profile.member_num}")
    else:
        lines.append("• 群基本信息：平台未返回")
        if profile_error is not None:
            lines.append(f"  └ {profile_error.hint or profile_error.message}")
    lines.append("")
    lines.append("🤖 机器人在群状态")
    if state is not None:
        role_label = {"member": "普通成员", "admin": "管理员", "owner": "群主"}.get(
            state.member_role, state.member_role or "未知"
        )
        lines.append(f"• 群内角色：{role_label}")
        lines.append(
            "• 消息接收："
            + {
                "all": "接收全部消息",
                "only_mention": "仅 @机器人 的消息",
                "mention_and_context": "@机器人及相关上下文",
            }.get(state.recv_msg_setting, state.recv_msg_setting or "未知")
        )
        lines.append(f"• 可主动推送：{'是' if state.allow_proactive_msg else '否'}")
        if state.joined_at:
            lines.append(f"• 入群时间：{to_iso(state.joined_at)}")
    else:
        lines.append("• 群内状态：平台未返回")
        if state_error is not None:
            lines.append(f"  └ {state_error.hint or state_error.message}")
    return "\n".join(lines)


def moderation_status_text(
    *,
    group_id: str,
    enabled: bool,
    mode: str,
    paused_reason: str,
    full_msg: bool | None,
    is_admin: bool | None,
    is_exempt: bool,
    dry_run: bool,
) -> str:
    """本群审核状态。"""
    lines = [f"🛡️ 审核状态（{mask_openid(group_id)}）"]
    if enabled and not paused_reason:
        lines.append("• 审核：✅ 已开启")
    elif paused_reason:
        lines.append(f"• 审核：⏸ 已暂停（{paused_reason}）")
    else:
        lines.append("• 审核：❌ 未开启")
    lines.append(f"• 模式：{mode or '默认'}")
    if dry_run:
        lines.append("• 运行模式：🧪 dry-run（只记录、不实际处置）")
    if full_msg is False:
        lines.append("• 全量消息：❌ 未开启（无法启用审核）")
    elif full_msg is True:
        lines.append("• 全量消息：✅ 已开启")
    if is_admin is False:
        lines.append("• 机器人权限：⚠️ 非群管理员（无法撤回/禁言）")
    elif is_admin is True:
        lines.append("• 机器人权限：✅ 群管理员")
    lines.append(f"• 我是否豁免审核：{'是' if is_exempt else '否'}")
    return "\n".join(lines)


def _capability_line(name: str, result: Any) -> str:
    label = CAPABILITY_LABELS.get(name, name)
    if not getattr(result, "probed", True):
        return f"• {label}：➖ 无只读探测接口（按需尝试）"
    if result.ok:
        return f"• {label}：✅ 可用" + (f"（{result.note}）" if result.note else "")
    detail = result.note or ""
    code = f"err_code={result.err_code}" if result.err_code else ""
    suffix = "，".join(part for part in (code, detail) if part)
    return f"• {label}：❌ 不可用" + (f"（{suffix}）" if suffix else "")


def selfcheck_text(group_id: str, results: dict[str, Any], *, group_name: str = "") -> str:
    """能力自检报告。"""
    title = f"🔍 能力自检（{group_name or mask_openid(group_id)}）"
    lines = [title, "──────────────"]
    for name in (
        "group_info",
        "bot_state",
        "is_admin",
        "full_msg",
        "recall",
        "mute",
        "join_review",
        "member_list",
        "blacklist",
        "remove_member",
    ):
        result = results.get(name)
        if result is None:
            continue
        lines.append(_capability_line(name, result))
    lines.append("──────────────")
    full_msg = results.get(CAP_FULL_MSG)
    is_admin = results.get(CAP_IS_ADMIN)
    if full_msg is not None and not full_msg.ok:
        lines.append("👉 " + FULL_MSG_GUIDE.splitlines()[0])
    if is_admin is not None and not is_admin.ok:
        lines.append("👉 需要撤回/禁言/入群审批时，请把机器人设为群管理员。")
    return "\n".join(lines)


def suggestions_for(group_id: str, results: dict[str, Any]) -> list[str]:
    """根据探测结果生成处置建议（WebUI 使用）。"""
    tips: list[str] = []
    for name, result in results.items():
        if getattr(result, "ok", False) or not getattr(result, "probed", True):
            continue
        hint = result.note or ""
        if result.err_code == 11253:
            hint = "该接口仅白名单机器人可用，请向 QQ 开放平台申请权限"
        tips.append(f"{CAPABILITY_LABELS.get(name, name)}：{hint or '不可用'}")
    return tips


def format_duration(seconds: int | None) -> str:
    return human_duration(seconds)


def join_lines(items: Iterable[str]) -> str:
    return "\n".join(str(item) for item in items)
