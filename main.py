"""QQ群管理插件入口（AstrBot Star）。

面向 QQ 官方机器人（qq_official）：
- 群档案与平台能力探测（白名单 / 群管理员 / 内邀），受限一律留痕；
- 基于 AstrBot 已配置 LLM 的群消息合法性识别与处置（警告 / 撤回 / 禁言 / 上报）；
- 入群申请轮询智能审批与群成员管理（禁言台账 / 黑名单）；
- SQLite 审计库 + WebUI 管理台。

本文件负责服务装配、生命周期与消息/指令处理；具体能力分别在 src/ 下各模块。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if _PLUGIN_ROOT not in sys.path:  # 允许 from .src... 导入与直接运行测试
    sys.path.insert(0, _PLUGIN_ROOT)

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star

from .src.actions import ActionExecutor
from .src.api_client import BotpyTransport, QQApiError, QQGroupAPI
from .src.audit import AuditStore
from .src.commands import (
    APPEAL_COMMANDS,
    BLACKLIST_COMMANDS,
    CONFIG_COMMANDS,
    DRYRUN_COMMANDS,
    FULL_MSG_GUIDE,
    GROUP_ADMIN_COMMANDS,
    INFO_COMMANDS,
    JOIN_APPROVE_COMMANDS,
    JOIN_DECLINE_COMMANDS,
    JOIN_LIST_COMMANDS,
    JOIN_MODE_COMMANDS,
    JOIN_MODE_LABELS,
    KEYWORD_COMMANDS,
    LOG_COMMANDS,
    MENU_COMMANDS,
    MODE_COMMANDS,
    MODE_FOLLOW,
    MODE_LABELS,
    MUTE_COMMANDS,
    RECALL_COMMANDS,
    SELFCHECK_COMMANDS,
    STATS_COMMANDS,
    STATUS_COMMANDS,
    THRESHOLD_COMMANDS,
    TOGGLE_COMMANDS,
    TRUST_COMMANDS,
    UNMUTE_COMMANDS,
    WEBUI_HINT,
    blacklist_text,
    format_duration,
    group_info_text,
    join_list_text,
    keyword_text,
    log_text,
    match_command,
    menu_text,
    mode_label_with_source,
    moderation_status_text,
    selfcheck_text,
    stats_text,
    suggestions_for,
)
from .src.join_review import JoinReviewer
from .src.models import (
    CAP_BOT_STATE,
    CAP_FULL_MSG,
    CAP_IS_ADMIN,
    JOIN_REVIEW_MODES,
    MODERATION_MODES,
    CapabilityResult,
    Verdict,
)
from .src.moderator import LLMModerator, ModerationRequest, choose_provider_id
from .src.normalize import skeleton_text
from .src.policy import ApprovalPolicyService
from .src.rules import SCORE_RULES, RuleEngine
from .src.scheduler import TaskScheduler, TaskSpec
from .src.store import AstrBotKVBackend, PluginStore
from .src.utils import (
    digest_text,
    mask_openid,
    normalize_command,
    now_ts,
    parse_duration,
    parse_iso,
    to_iso,
)
from .src.web_api import EventBus, WebApi

PLUGIN_NAME = "astrbot_plugin_qq_group_manager"
VERSION = "0.4.2"

STATE_FLUSH_INTERVAL = 30.0
MAINTENANCE_INTERVAL = 3600.0
MUTE_SYNC_INTERVAL = 300.0
SEEN_MESSAGE_TTL = 600.0
DEFAULT_MUTE_SECONDS = 600


class QQGroupManager(Star):
    """QQ 群管理插件主体。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.store = PluginStore(AstrBotKVBackend(self), logger=self.logger)
        self.bus = EventBus()
        self.scheduler = TaskScheduler(logger=self.logger)
        self.api = QQGroupAPI(None, dry_run_getter=self.store.dry_run)
        self.audit: AuditStore | None = None
        settings = self.store.settings()
        self.rules = RuleEngine(
            self.store.keywords(),
            templates=(self.store.templates() or None)
            if settings.get("template_enabled", True)
            else [],
            homoglyph=(self.store.homoglyph() or None)
            if settings.get("homoglyph_enabled", True)
            else {},
            auto_enforce_normalized=bool(settings.get("auto_enforce_normalized")),
            fuzzy_max_distance=int(settings.get("fuzzy_max_distance", 1) or 0),
            pinyin_enabled=bool(settings.get("pinyin_enabled")),
        )
        self.moderator = LLMModerator(settings_getter=self.store.settings, logger=self.logger)
        self.actions = ActionExecutor(api=self.api, store=self.store, logger=self.logger)
        self.joins = JoinReviewer(api=self.api, store=self.store, logger=self.logger)
        self.policy = ApprovalPolicyService(api=self.api, store=self.store, logger=self.logger)
        self.initialized = False
        self.data_dir: Path = self._resolve_data_dir()
        self._platform_id: str = ""
        self._last_prune_day: str = ""
        self._seen_messages: dict[str, float] = {}
        self._last_provider_id: str = ""
        WebApi(self).register()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """加载配置、打开审计库、绑定 LLM、启动后台任务。"""
        await self.store.load()
        settings = self.store.settings()
        db_path = str(settings.get("db_path") or "").strip()
        path = Path(db_path) if db_path else self.data_dir / "moderation.db"
        self.audit = AuditStore(
            path,
            queue_maxsize=int(settings.get("audit_queue_maxsize", 5000)),
            logger=self.logger,
            on_record=self._on_audit_record,
        )
        await self.audit.initialize()
        self.api.audit = self.audit
        self.actions.audit = self.audit
        self.joins.audit = self.audit
        self.moderator.provider_call = self._llm_call
        self.joins.judge_call = self._join_judge_call
        self.actions.notifier = self._notify
        self.joins.notifier = self._notify
        self.rules.reload(self.store.keywords())
        self._resolve_transport()
        self._register_tasks()
        await self.scheduler.start()
        self.initialized = True
        self.logger.info(
            "QQ群管理 %s 已启动（db=%s，transport=%s，dry_run=%s，群=%d）",
            VERSION,
            path,
            "可用" if self.api.available else "不可用",
            settings.get("dry_run"),
            len(self.store.groups()),
        )

    async def terminate(self) -> None:
        """停止任务、落盘配置、关闭审计库。"""
        self.initialized = False
        await self.scheduler.stop()
        try:
            await self.store.flush(force=True)
        except Exception as exc:  # pragma: no cover
            self.logger.error("退出前保存配置失败：%s", exc)
        if self.audit is not None:
            try:
                await self.audit.close()
            except Exception as exc:  # pragma: no cover
                self.logger.error("关闭审计库失败：%s", exc)
            self.audit = None
        self.logger.info("QQ群管理已停止")

    def _resolve_data_dir(self) -> Path:
        """解析 data/plugin_data/<插件名>/ 目录。"""
        try:
            from astrbot.core.star.star_tools import StarTools

            return Path(StarTools.get_data_dir())
        except Exception:  # pragma: no cover - 兼容旧版本
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
            except Exception:
                return Path(_PLUGIN_ROOT) / "data"

    def _on_audit_record(self, kind: str, payload: dict[str, Any]) -> None:
        """审计写入时同步推给 WebUI（SSE）。"""
        topic = "audit" if kind in {"events", "actions"} else kind
        enriched = dict(payload)
        enriched.setdefault("kind", kind)
        enriched.setdefault("ts", to_iso(enriched.get("ts_unix") or now_ts()))
        self.bus.publish(topic, enriched)

    # ------------------------------------------------------------------
    # 后台任务
    # ------------------------------------------------------------------
    def _register_tasks(self) -> None:
        self.scheduler.add(TaskSpec("flush_state", self._task_flush_state, STATE_FLUSH_INTERVAL))
        interval = float(self.store.get_setting("probe_full_msg_interval", 1800) or 1800)
        self.scheduler.add(TaskSpec("probe_capabilities", self._task_probe_capabilities, interval))
        self.scheduler.add(TaskSpec("maintenance", self._task_maintenance, MAINTENANCE_INTERVAL))
        self.scheduler.add(TaskSpec("join_poll", self._task_join_poll, self._join_interval()))
        self.scheduler.add(TaskSpec("mute_sync", self._task_mute_sync, MUTE_SYNC_INTERVAL))

    def _join_interval(self) -> float:
        try:
            return float(self.store.get_setting("join_poll_interval", 60) or 60)
        except (TypeError, ValueError):
            return 60.0

    async def _task_flush_state(self) -> None:
        """把内存里的群/成员缓存写回 KV。"""
        await self.store.flush()

    async def _task_probe_capabilities(self) -> None:
        """周期重探能力；全量消息能力丢失时自动暂停审核。"""
        if not self._resolve_transport():
            return
        for group_id, config in list(self.store.groups().items()):
            results = await self.probe_group(group_id, caller="scheduler")
            full_msg = results.get(CAP_FULL_MSG)
            if (
                config.moderation_enabled
                and full_msg is not None
                and not full_msg.ok
                and not self.store.get_setting("allow_without_full_msg", False)
            ):
                await self.store.update_group(
                    group_id,
                    {
                        "moderation_enabled": False,
                        "paused_reason": "已失去「接收全部消息」能力",
                    },
                )
                self.logger.warning(
                    "群 %s 的「接收全部消息」已关闭，审核已自动暂停", mask_openid(group_id)
                )
                self.bus.publish(
                    "capability",
                    {
                        "kind": "alert",
                        "group_id": group_id,
                        "capability": CAP_FULL_MSG,
                        "ok": False,
                        "note": "审核已自动暂停：需要重新开启「接收全部消息」",
                        "ts": to_iso(),
                    },
                )
        await self.store.flush()

    async def _task_join_poll(self) -> None:
        """轮询入群申请（仅对启用入群审批的群）。"""
        if not self._resolve_transport():
            return
        default_mode = str(self.store.get_setting("join_review_mode") or "off")
        pending = 0
        for group_id, config in list(self.store.groups().items()):
            mode = str(config.join_review_mode or default_mode)
            if mode in ("", "off"):
                continue
            try:
                pending += len(await self.joins.poll_group(group_id))
            except Exception as exc:
                self.logger.warning("轮询群 %s 入群申请失败：%s", mask_openid(group_id), exc)
        if pending:
            self.logger.info("新增 %d 条待审入群申请", pending)
            self.bus.publish("audit", {"kind": "join_pending", "count": pending, "ts": to_iso()})

    async def _task_mute_sync(self) -> None:
        """与平台对账禁言台账（能力可用时）。"""
        if self.audit is None or not self._resolve_transport():
            return
        for group_id, config in list(self.store.groups().items()):
            if not config.capability_ok("mute"):
                continue
            try:
                response = await self.api.get_restrict_setting(group_id, caller="mute_sync")
            except QQApiError:
                continue
            members = response.get("members") or []
            platform_map = {
                str(item.get("member_openid")): item
                for item in members
                if isinstance(item, dict) and item.get("member_openid")
            }
            for row in await self.audit.list_mutes(group_id):
                openid = str(row.get("member_openid") or "")
                item = platform_map.get(openid)
                if item is None:
                    await self.audit.set_mute_active(group_id, openid, False)
                    continue
                until = parse_iso(item.get("mute_expire_at"))
                if until:
                    await self.audit.upsert_mute(
                        group_id=group_id,
                        member_openid=openid,
                        username=str(item.get("username") or row.get("username") or ""),
                        until_unix=until,
                        reason=str(row.get("reason") or ""),
                        source=str(row.get("source") or "sync"),
                        active=True,
                    )

    async def _task_maintenance(self) -> None:
        """每日裁剪与统计归档。"""
        if self.audit is None:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._last_prune_day:
            return
        settings = self.store.settings()
        deleted = await self.audit.prune(
            {
                "events": settings["retention_events_days"],
                "actions": settings["retention_events_days"],
                "api": settings["retention_api_days"],
                "capability": settings["retention_capability_days"],
                "join": settings["retention_join_days"],
            }
        )
        stats = await self.audit.summary(1)
        await self.audit.save_daily_stats(today, stats)
        self._last_prune_day = today
        self.logger.info("审计库维护完成：%s", deleted)

    # ------------------------------------------------------------------
    # 平台通道与状态
    # ------------------------------------------------------------------
    def _resolve_transport(self) -> bool:
        """确保 API 客户端拿到 botpy 传输层（优先平台实例）。"""
        if self.api.available:
            return True
        try:
            get_insts = getattr(self.context.platform_manager, "get_insts", None)
            instances = get_insts() if callable(get_insts) else []
        except Exception:  # pragma: no cover
            instances = []
        for instance in instances or []:
            try:
                meta = instance.meta()
            except Exception:
                continue
            if getattr(meta, "name", "") != "qq_official":
                continue
            transport = BotpyTransport.from_platform(instance)
            if transport is not None and transport.available:
                self.api.transport = transport
                self._platform_id = getattr(meta, "id", "") or self._platform_id
                return True
        return False

    def transport_status(self) -> dict[str, Any]:
        return {
            "available": self.api.available,
            "platform_id": self._platform_id,
            "dry_run": self.store.dry_run(),
        }

    def runtime_status(self) -> dict[str, Any]:
        """运行态快照（WebUI 顶栏与总览使用）。"""
        groups = self.store.groups()
        enabled = [gid for gid, cfg in groups.items() if cfg.moderation_enabled]
        settings = self.store.settings()
        return {
            "version": VERSION,
            "initialized": self.initialized,
            "dry_run": bool(settings.get("dry_run")),
            "enabled": bool(settings.get("enabled")),
            "mode": settings.get("mode"),
            "transport": self.transport_status(),
            "groups_total": len(groups),
            "groups_moderating": len(enabled),
            "join_review_mode": settings.get("join_review_mode"),
            "moderation_provider": {
                "configured": str(settings.get("llm_provider_id") or ""),
                "last_used": self._last_provider_id,
            },
            "db_queue": dict(self.audit.stats) if self.audit else {},
            "sse_subscribers": self.bus.subscriber_count(),
            "moderator": self.moderator.status(),
            "actions": self.actions.status(),
            "joins": self.joins.status(),
            "now": to_iso(),
        }

    def available_provider_ids(self) -> list[str]:
        """当前可用的对话模型 ID 列表（供审核模型选择与校验）。"""
        ids: list[str] = []
        try:
            providers = self.context.get_all_providers() or []
        except Exception:  # pragma: no cover - 运行环境异常时退化为空列表
            providers = []
        for provider in providers:
            try:
                meta = provider.meta()
            except Exception:
                continue
            provider_id = str(getattr(meta, "id", "") or "")
            if provider_id:
                ids.append(provider_id)
        return ids

    def list_providers(self) -> dict[str, Any]:
        """列出可用于内容审核的对话模型（WebUI 选择用）。"""
        items: list[dict[str, str]] = []
        try:
            providers = self.context.get_all_providers() or []
        except Exception:  # pragma: no cover
            providers = []
        for provider in providers:
            try:
                meta = provider.meta()
            except Exception:
                continue
            items.append(
                {
                    "id": str(getattr(meta, "id", "") or ""),
                    "model": str(getattr(meta, "model", "") or ""),
                    "type": str(getattr(meta, "type", "") or ""),
                }
            )
        configured = str(self.store.get_setting("llm_provider_id") or "")
        return {
            "items": items,
            "configured": configured,
            "last_used": self._last_provider_id,
        }

    def groups_snapshot(self) -> list[dict[str, Any]]:
        """群列表（含能力矩阵与状态），供 WebUI 表格。"""
        snapshot: list[dict[str, Any]] = []
        settings = self.store.settings()
        for config in sorted(
            self.store.groups().values(), key=lambda item: item.last_seen, reverse=True
        ):
            data = config.to_dict()
            caps = data.get("capabilities") or {}
            full_msg = caps.get(CAP_FULL_MSG) or {}
            is_admin = caps.get(CAP_IS_ADMIN) or {}
            data.update(
                {
                    "cap_full_msg": bool(full_msg.get("ok")),
                    "cap_is_admin": bool(is_admin.get("ok")),
                    "effective_mode": config.mode or settings.get("mode"),
                    "effective_join_mode": config.join_review_mode
                    or settings.get("join_review_mode"),
                    "last_seen_iso": to_iso(config.last_seen) if config.last_seen else "",
                }
            )
            snapshot.append(data)
        return snapshot

    async def probe_group(
        self, group_id: str, *, caller: str = "probe"
    ) -> dict[str, CapabilityResult]:
        """探测某群的能力并落库（含受限留痕）。"""
        if not self._resolve_transport():
            note = "未找到 qq_official 平台实例，无法调用 QQ 接口"
            return {
                name: CapabilityResult(capability=name, ok=False, note=note, checked_at=now_ts())
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
                )
            }
        results = await self.api.probe(group_id, caller=caller)
        await self.store.set_capabilities(group_id, results)
        return results

    def suggestions(self, group_id: str, results: dict[str, CapabilityResult]) -> list[str]:
        return suggestions_for(group_id, results)

    async def set_moderation(
        self, group_id: str, enable: bool, *, caller: str = "webui"
    ) -> dict[str, Any]:
        """启用/停用审核（启用前强制校验「接收全部消息」，见设计方案 §5.2）。"""
        if not enable:
            await self.store.update_group(
                group_id, {"moderation_enabled": False, "paused_reason": ""}
            )
            await self.store.flush()
            self.logger.info("审核已关闭：group=%s by=%s", mask_openid(group_id), caller)
            return {"ok": True, "group": self.store.group_or_default(group_id).to_dict()}

        settings = self.store.settings()
        self.logger.info(
            "开始启用审核：group=%s by=%s（先做一次能力探测）", mask_openid(group_id), caller
        )
        results = await self.probe_group(group_id, caller=caller)
        state = results.get(CAP_BOT_STATE)
        if state is None or not state.ok:
            note = (state.note if state else "") or "平台未授权（可能需要在开放平台申请白名单）"
            self.logger.warning(
                "启用审核失败（无法读取群内状态）：group=%s %s", mask_openid(group_id), note
            )
            return {
                "ok": False,
                "reason_code": "bot_state_unavailable",
                "message": "无法获取机器人群内状态：" + note,
            }
        full_msg = results.get(CAP_FULL_MSG)
        if (
            full_msg is not None
            and not full_msg.ok
            and not settings.get("allow_without_full_msg", False)
        ):
            self.logger.warning(
                "启用审核被拒绝（未开启接收全部消息）：group=%s", mask_openid(group_id)
            )
            return {"ok": False, "reason_code": "need_full_msg", "message": FULL_MSG_GUIDE}
        warnings: list[str] = []
        is_admin = results.get(CAP_IS_ADMIN)
        if is_admin is not None and not is_admin.ok:
            warnings.append("机器人不是群管理员：违规消息将只能警告，无法撤回或禁言")
        if full_msg is not None and not full_msg.ok and settings.get("allow_without_full_msg"):
            warnings.append("审核覆盖率不完整：当前未开启「接收全部消息」")
        await self.store.update_group(group_id, {"moderation_enabled": True, "paused_reason": ""})
        await self.store.flush()
        self.logger.info("审核已启用：group=%s by=%s", mask_openid(group_id), caller)
        return {
            "ok": True,
            "warnings": warnings,
            "capabilities": {name: result.to_dict() for name, result in results.items()},
            "group": self.store.group_or_default(group_id).to_dict(),
        }

    # ------------------------------------------------------------------
    # LLM 绑定与通知
    # ------------------------------------------------------------------
    async def _llm_call(
        self, request: ModerationRequest, system_prompt: str, user_prompt: str
    ) -> str:
        """调用 AstrBot 已配置的 LLM（复用官方 SDK）。"""
        umo = request.umo or str(self.store.get_setting("notify_session") or "")
        session_default = ""
        try:
            session_default = await self.context.get_current_chat_provider_id(umo=umo or None)
        except Exception:
            session_default = ""
        available = self.available_provider_ids()
        configured = str(self.store.get_setting("llm_provider_id") or "")
        provider_id = choose_provider_id(configured, session_default, available)
        if configured and provider_id != configured:
            self.logger.warning(
                "配置的审核模型 %s 当前不可用，已回退到 %s",
                configured,
                provider_id or "会话默认模型",
            )
        self._last_provider_id = provider_id
        if provider_id:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=user_prompt,
                system_prompt=system_prompt,
                contexts=[],
                image_urls=list(request.image_urls) or None,
            )
            return str(getattr(response, "completion_text", "") or "")
        provider = await self.context.get_using_provider_async(umo=umo or None)
        if provider is None:
            raise RuntimeError("未配置可用的对话模型")
        response = await provider.text_chat(
            system_prompt=system_prompt,
            prompt=user_prompt,
            image_urls=list(request.image_urls) or None,
        )
        return str(getattr(response, "completion_text", "") or "")

    async def _join_judge_call(self, system_prompt: str, user_prompt: str) -> str:
        """入群申请审核的 LLM 调用（无事件上下文）。"""
        request = ModerationRequest(
            group_id="",
            text=user_prompt,
            umo=str(self.store.get_setting("notify_session") or ""),
        )
        return await self._llm_call(request, system_prompt, user_prompt)

    async def _notify(self, kind: str, payload: dict[str, Any]) -> None:
        """通知管理员（优先使用配置的会话，其次写日志）。"""
        session = str(payload.get("umo") or self.store.get_setting("notify_session") or "")
        text = self._render_notification(kind, payload)
        if session:
            try:
                sent = await self.context.send_message(session, MessageChain([Plain(text)]))
                if sent:
                    return
            except Exception as exc:  # pragma: no cover - 主动消息可能受限
                self.logger.warning("通知发送失败：%s", exc)
        self.logger.info("[通知:%s] %s", kind, text.replace("\n", " | "))

    def _render_notification(self, kind: str, payload: dict[str, Any]) -> str:
        """构造通知文本。"""
        if kind == "moderation":
            return (
                "内容审核告警\n"
                "群：{group}\n"
                "判定：{verdict} / {category}（severity={severity}，置信度 {confidence}）\n"
                "成员：{name}（{openid}）\n"
                "理由：{reason}\n"
                "消息 ID：{msg_id}".format(
                    group=payload.get("group_name") or mask_openid(payload.get("group_id")),
                    verdict=payload.get("verdict"),
                    category=payload.get("category"),
                    severity=payload.get("severity"),
                    confidence=round(float(payload.get("confidence") or 0.0), 2),
                    name=payload.get("sender_name") or "未知",
                    openid=mask_openid(payload.get("sender_openid")),
                    reason=payload.get("reason") or "-",
                    msg_id=payload.get("msg_id") or "-",
                )
            )
        if kind == "join_pending":
            return (
                "待审入群申请\n"
                "群：{group}\n"
                "申请人：{name}（{openid}）\n"
                "验证消息：{verify}\n"
                "风险提示：{risk}\n"
                "机器建议：{suggestion}\n"
                "请在管理台「入群审批」或群内使用「入群申请」处理。".format(
                    group=payload.get("group_name") or mask_openid(payload.get("group_id")),
                    name=payload.get("username") or "未知",
                    openid=mask_openid(payload.get("member_openid")),
                    verify=payload.get("verify_message") or "（无）",
                    risk=payload.get("risk_tips") or "无",
                    suggestion=payload.get("suggestion") or "-",
                )
            )
        if kind == "appeal":
            return (
                "审核申诉\n"
                "群：{group}\n"
                "成员：{name}（{openid}）\n"
                "理由：{text}\n"
                "事件 ID：{event_id}".format(
                    group=payload.get("group_name") or mask_openid(payload.get("group_id")),
                    name=payload.get("sender_name") or "未知",
                    openid=mask_openid(payload.get("sender_openid")),
                    text=payload.get("text") or "",
                    event_id=payload.get("event_id") or "-",
                )
            )
        return f"{kind}: {payload}"

    # ------------------------------------------------------------------
    # 消息处理：登记 → 指令 → 内容审核
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群消息入口：登记活跃群/成员，处理管理指令，并执行内容审核。

        该 handler 命中会让 AstrBot 把事件标记为 is_wake，但不会触发聊天 LLM
        （只有 is_at_or_wake_command 为真才调用 LLM），因此不影响正常对话。
        """
        try:
            self._bind_event(event)
            group_id = event.get_group_id() or ""
            if not group_id:
                return
            sender_openid, sender_name, sender_role = self._sender_meta(event)
            await self.store.touch_group(group_id, name=self._group_name(event))
            await self.store.remember_member(
                group_id, sender_openid, name=sender_name, role=sender_role
            )

            text = normalize_command(event.message_str or "")
            name, args = match_command(text)
            if name:
                self.logger.info(
                    "收到指令：%s（群=%s 发送者=%s）",
                    name,
                    mask_openid(group_id),
                    mask_openid(sender_openid),
                )
                try:
                    replies = await self._handle_command(
                        event,
                        group_id=group_id,
                        name=name,
                        args=args,
                        admin=bool(event.is_admin()),
                        group_admin=self.store.is_group_admin(group_id, sender_openid),
                        sender_openid=sender_openid,
                        sender_name=sender_name,
                    )
                except Exception as exc:  # 指令异常必须回话，否则用户看到的是"没反应"
                    self.logger.error("指令 %s 执行失败：%s", name, exc, exc_info=True)
                    replies = [f"指令执行失败：{type(exc).__name__}: {exc}"]
                for chunk in replies:
                    if chunk:
                        yield event.plain_result(chunk)
                return

            if not self.store.get_setting("enabled", True):
                return
            config = self.store.group_or_default(group_id)
            if config.moderation_enabled and not config.paused_reason:
                await self._moderate(
                    event,
                    group_id=group_id,
                    config=config,
                    sender_openid=sender_openid,
                    sender_name=sender_name,
                    sender_role=sender_role,
                )
        except Exception as exc:  # pragma: no cover - 不让插件异常影响群聊
            self.logger.error("处理群消息失败：%s", exc, exc_info=True)

    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """私聊入口：群管理指令只作用于群聊，这里给出明确回复（而不是静默无响应）。"""
        try:
            self._bind_event(event)
            text = normalize_command(event.message_str or "")
            name, _args = match_command(text)
            if not name:
                return
            self.logger.info(
                "收到私聊指令：%s（发送者=%s）", name, mask_openid(event.get_sender_id())
            )
            yield event.plain_result(
                "QQ群管理指令只作用于**群聊**：请在需要管理的群里发送（若机器人只接收 @消息，"
                "请先 @机器人再发送指令，或在群内开启「接收全部消息」）。\n"
                "私聊可用的指令：群管理菜单、群管理配置。"
            )
        except Exception as exc:  # pragma: no cover
            self.logger.error("处理私聊消息失败：%s", exc, exc_info=True)

    def _bind_event(self, event: AstrMessageEvent) -> None:
        """从事件里绑定平台实例 ID 与 botpy 传输层。"""
        try:
            self._platform_id = event.get_platform_id() or self._platform_id
        except Exception:
            pass
        if not self.api.available:
            transport = BotpyTransport.from_event(event)
            if transport is not None and transport.available:
                self.api.transport = transport

    @staticmethod
    def _group_name(event: AstrMessageEvent) -> str:
        group = getattr(event.message_obj, "group", None)
        return str(getattr(group, "group_name", "") or "")

    @staticmethod
    def _sender_meta(event: AstrMessageEvent) -> tuple[str, str, str]:
        """从原始消息里取发送者 openid / 昵称 / 群内角色。"""
        sender_openid = str(event.get_sender_id() or "")
        sender_name = str(event.get_sender_name() or "")
        role = ""
        raw = getattr(event.message_obj, "raw_message", None)
        author = getattr(raw, "author", None)
        if author is not None:
            sender_openid = str(getattr(author, "member_openid", "") or sender_openid)
            sender_name = str(getattr(author, "username", "") or sender_name)
            role = str(getattr(author, "member_role", "") or "")
        return sender_openid, sender_name, role

    @staticmethod
    def _message_id(event: AstrMessageEvent) -> str:
        raw = getattr(event.message_obj, "raw_message", None)
        return str(getattr(raw, "id", "") or getattr(event.message_obj, "message_id", "") or "")

    @staticmethod
    def _extract_text(event: AstrMessageEvent) -> tuple[str, str, list[str]]:
        """提取待审核内容：正文 + 语音转写 + 图片 URL。

        返回 (文本, 消息类型标签, 图片 URL 列表)。图片是否真的送审由
        settings.image_review 决定（off / with_text / always）。
        """
        parts: list[str] = []
        images: list[str] = []
        base = str(event.message_str or "").strip()
        if base:
            parts.append(base)
        kind = "文本"

        # AstrBot 消息组件里的图片（QQ 官方适配器会把 image 附件转成 Image.fromURL）
        getter = getattr(event, "get_messages", None)
        components = []
        if callable(getter):
            try:
                components = list(getter() or [])
            except Exception:  # pragma: no cover - 兼容异常事件对象
                components = []
        for component in components:
            url = str(getattr(component, "url", "") or getattr(component, "file", "") or "").strip()
            if url.startswith(("http://", "https://", "base64://", "file://")):
                images.append(url)

        # 原始 payload 里的附件：语音转写文本 + 图片 URL（组件缺失时兜底）
        raw = getattr(event.message_obj, "raw_message", None)
        attachments = getattr(raw, "attachments", None) or []
        for item in attachments:
            if isinstance(item, dict):
                asr = item.get("asr_refer_text")
                content_type = str(item.get("content_type") or "")
                url = str(item.get("url") or "")
            else:
                asr = getattr(item, "asr_refer_text", None)
                content_type = str(getattr(item, "content_type", "") or "")
                url = str(getattr(item, "url", "") or "")
            if asr:
                parts.append("[语音转写] " + str(asr))
                kind = "语音转写"
            elif content_type.startswith("image"):
                if url and url not in images:
                    images.append(url)
                kind = "图片"
            elif content_type.startswith("video"):
                kind = "视频"

        if images and parts:
            kind = "图文"
        elif images:
            kind = "图片"
        return "\n".join(part for part in parts if part).strip(), kind, images

    def _seen_message(self, group_id: str, msg_id: str) -> bool:
        """平台可能重复推送同一条消息，这里做短 TTL 去重。"""
        if not msg_id:
            return False
        key = f"{group_id}:{msg_id}"
        now = now_ts()
        if len(self._seen_messages) > 5000:
            self._seen_messages = {
                item: stamp
                for item, stamp in self._seen_messages.items()
                if now - stamp < SEEN_MESSAGE_TTL
            }
        if key in self._seen_messages:
            return True
        self._seen_messages[key] = now
        return False

    def _is_exempt(
        self,
        event: AstrMessageEvent,
        group_id: str,
        sender_openid: str,
        sender_role: str,
    ) -> bool:
        """判断是否豁免审核：AstrBot 管理员 / 群主或管理员 / 信任名单。"""
        if bool(event.is_admin()):
            return True
        if sender_role in ("admin", "owner"):
            return True
        return sender_openid in self.store.trusted(group_id)

    def _days_in_group(self, group_id: str, sender_openid: str) -> int | None:
        first_seen = self.store.member_first_seen(group_id, sender_openid)
        if not first_seen:
            return None
        return max(0, int((now_ts() - first_seen) / 86400))

    async def _moderate(
        self,
        event: AstrMessageEvent,
        *,
        group_id: str,
        config: Any,
        sender_openid: str,
        sender_name: str,
        sender_role: str,
    ) -> None:
        """内容审核主链路：规则 → LLM → 处置。"""
        import time

        settings = self.store.settings()
        msg_id = self._message_id(event)
        if self._seen_message(group_id, msg_id):
            return
        text, kind, images = self._extract_text(event)
        image_mode = str(settings.get("image_review") or "off")
        send_images: list[str] = []
        if (
            images
            and image_mode in ("with_text", "always")
            and (image_mode == "always" or text.strip())
        ):
            limit = int(settings.get("image_review_max", 1) or 1)
            send_images = images[: max(1, limit)]
        if not text.strip() and not send_images:
            return
        if self._is_exempt(event, group_id, sender_openid, sender_role):
            return

        recent = self.rules.note_message(group_id, sender_openid)
        threshold = int(settings.get("flood_threshold", 8) or 8)
        duplicate_senders = 0
        if text.strip():
            # 用骨架文本做去重键：同一文案的变体写法也能聚到一起
            skeleton, _hits = skeleton_text(text, self.store.homoglyph() or None)
            duplicate_senders = self.rules.note_content(
                group_id,
                skeleton or text,
                sender_openid,
                window=float(settings.get("duplicate_flood_window", 300) or 300),
            )
        evaluation = self.rules.evaluate(
            text,
            group_id=group_id,
            flood_threshold=threshold,
            recent_messages=recent,
            duplicate_senders=duplicate_senders,
            duplicate_members=int(settings.get("duplicate_flood_members", 3) or 3),
        )
        if send_images and "image" not in evaluation.signals:
            evaluation.signals["image"] = SCORE_RULES["image"]
            evaluation.score = min(100, evaluation.score + SCORE_RULES["image"])
        hard_actions = evaluation.enforce_actions
        verdict = None
        latency_ms = 0
        sampled = False

        if hard_actions:
            hit = evaluation.hard_hits[0]
            verdict = Verdict(
                verdict="violation",
                category=hit.note or "广告引流",
                severity=4,
                confidence=1.0,
                reason=f"命中本地硬规则：{hit.pattern}",
                suggested_action="mute_and_recall",
                source="rule",
            )
            hard_actions = list(hard_actions)
        else:
            days = self._days_in_group(group_id, sender_openid)
            should_send = self.moderator.should_send(
                # rule_hit 只认规则/模板命中；外链、联系方式、刷屏等内置信号通过
                # risk>=N 或各自的条件（has_link / has_contact / flood）触发，
                # 这样"纯闲聊 + 偶尔发个链接"不会消耗 token。
                rule_summary=evaluation.rule_summary(),
                has_link=evaluation.has_link,
                long_text=evaluation.long_text,
                new_member=days is not None and days <= 1,
                flood=evaluation.flood,
                recent=recent,
                risk_score=evaluation.score,
                has_contact=evaluation.has_contact,
                ad_template=bool(evaluation.template_hits),
                has_image=bool(send_images),
            )
            if not should_send:
                return
            request = ModerationRequest(
                group_id=group_id,
                text=text,
                image_urls=send_images,
                risk_score=evaluation.score,
                risk_signals=dict(evaluation.signals),
                matched=[hit.pattern for hit in evaluation.hits],
                normalized_text=str((evaluation.views or {}).get("skeleton") or ""),
                sender_openid=sender_openid,
                sender_name=sender_name,
                sender_role=sender_role,
                group_name=config.name,
                rules_brief=config.rules_brief or str(settings.get("group_rules_brief") or ""),
                rule_summary=evaluation.summary(),
                message_kind=kind,
                recent_messages=recent,
                days_in_group=days,
                umo=event.unified_msg_origin,
                message_id=msg_id,
            )
            templates = {
                "system": str(settings.get("prompt_system") or ""),
                "user": str(settings.get("prompt_user") or ""),
            }
            started = time.monotonic()
            verdict = await self.moderator.judge(request, templates=templates)
            latency_ms = int((time.monotonic() - started) * 1000)
            sampled = verdict.source == "llm"

        if verdict is None:
            return

        async def _send(text_out: str) -> None:
            await event.send(MessageChain([Plain(text_out)]))

        summary = await self.actions.handle(
            group_id=group_id,
            config=config,
            verdict=verdict,
            settings=settings,
            msg_id=msg_id,
            sender_openid=sender_openid,
            sender_name=sender_name,
            sender_role=sender_role,
            message_excerpt=text,
            text_digest=digest_text(text),
            rule_hits=[hit.to_dict() for hit in evaluation.hits],
            hard_actions=hard_actions,
            source=verdict.source,
            umo=event.unified_msg_origin,
            provider_id=self._last_provider_id,
            latency_ms=latency_ms,
            sampled=sampled,
            send=_send,
        )
        if verdict.is_violation:
            self.logger.info(
                "规则评估：score=%s 命中=%s 信号=%s 骨架=%s",
                evaluation.score,
                [hit.pattern for hit in evaluation.hits][:4],
                list(evaluation.signals),
                str((evaluation.views or {}).get("skeleton") or "")[:60],
            )
            self.logger.info(
                "审核判定 %s/%s sev=%s conf=%.2f 实际动作=%s 计划动作=%s%s",
                verdict.verdict,
                verdict.category,
                verdict.severity,
                verdict.confidence,
                [item.get("action") for item in summary.get("actions") or []],
                summary.get("planned") or [],
                (
                    "（被拦下：" + str(summary.get("skipped")) + "）"
                    if summary.get("skipped")
                    else ""
                ),
            )
            if settings.get("block_llm_on_violation", False):
                event.should_call_llm(False)

    # ------------------------------------------------------------------
    # 指令处理
    # ------------------------------------------------------------------
    @staticmethod
    def _at_targets(event: AstrMessageEvent) -> list[tuple[str, str]]:
        """取出消息中 @ 的人（openid, 昵称）。"""
        targets: list[tuple[str, str]] = []
        for component in event.get_messages():
            if isinstance(component, At):
                openid = str(getattr(component, "qq", "") or "")
                name = str(getattr(component, "name", "") or "")
                if openid and openid != "all":
                    targets.append((openid, name))
        return targets

    @staticmethod
    def _reply_message_id(event: AstrMessageEvent) -> str:
        """取出被引用消息的 ID（用于「撤回」指令）。"""
        for component in event.get_messages():
            if isinstance(component, Reply):
                return str(getattr(component, "id", "") or "")
        return ""

    def _resolve_target(
        self, event: AstrMessageEvent, group_id: str, args: list[str]
    ) -> tuple[str, str]:
        """解析指令目标：@某人 → openid；否则按昵称缓存或原始 openid。"""
        targets = self._at_targets(event)
        if targets:
            return targets[0]
        for token in args:
            cleaned = token.strip().lstrip("@")
            if not cleaned or cleaned.startswith("-"):
                continue
            if cleaned.startswith("u_") or len(cleaned) >= 24:
                return cleaned, self.store.member_name(group_id, cleaned)
            matches = self.store.find_member_by_name(group_id, cleaned)
            if matches:
                return matches[0]
        return "", ""

    async def _handle_command(
        self,
        event: AstrMessageEvent,
        *,
        group_id: str,
        name: str,
        args: list[str],
        admin: bool,
        group_admin: bool,
        sender_openid: str,
        sender_name: str,
    ) -> list[str]:
        """指令分发。"""
        if name in MENU_COMMANDS:
            return [menu_text()]
        if name in CONFIG_COMMANDS:
            return [WEBUI_HINT] if admin else ["仅 AstrBot 管理员可查看管理台配置。"]
        if name in INFO_COMMANDS:
            return [await self._cmd_group_info(group_id)]
        if name in STATUS_COMMANDS:
            return [await self._cmd_status(group_id, sender_openid, admin, group_admin)]
        if name in SELFCHECK_COMMANDS:
            if not admin:
                return ["仅 AstrBot 管理员可执行能力自检。"]
            results = await self.probe_group(group_id, caller="command")
            config = self.store.group_or_default(group_id)
            return [selfcheck_text(group_id, results, group_name=config.name)]

        if name in GROUP_ADMIN_COMMANDS and not (admin or group_admin):
            return ["该指令仅群主 / 群管理员或 AstrBot 管理员可用。"]

        if name in TOGGLE_COMMANDS:
            return await self._cmd_toggle(group_id, name, sender_openid)
        if name in MODE_COMMANDS:
            return await self._cmd_mode(group_id, args)
        if name in THRESHOLD_COMMANDS:
            return await self._cmd_threshold(args)
        if name in KEYWORD_COMMANDS:
            return await self._cmd_keyword(group_id, args)
        if name in TRUST_COMMANDS:
            return await self._cmd_trust(event, group_id, name, args)
        if name in MUTE_COMMANDS:
            return await self._cmd_mute(event, group_id, args, sender_name)
        if name in UNMUTE_COMMANDS:
            return await self._cmd_unmute(event, group_id, args)
        if name in RECALL_COMMANDS:
            return await self._cmd_recall(event, group_id)
        if name in LOG_COMMANDS:
            return await self._cmd_log(group_id, args)
        if name in STATS_COMMANDS:
            return await self._cmd_stats(args)
        if name in APPEAL_COMMANDS:
            return await self._cmd_appeal(event, group_id, args, sender_openid, sender_name)
        if name in JOIN_MODE_COMMANDS:
            return await self._cmd_join_mode(group_id, args)
        if name in JOIN_LIST_COMMANDS:
            return await self._cmd_join_list(group_id)
        if name in JOIN_APPROVE_COMMANDS or name in JOIN_DECLINE_COMMANDS:
            return await self._cmd_join_decide(
                group_id, name, args, sender_openid, approve=name in JOIN_APPROVE_COMMANDS
            )
        if name in BLACKLIST_COMMANDS:
            return await self._cmd_blacklist(event, group_id, args)
        if name in DRYRUN_COMMANDS:
            return await self._cmd_dry_run(args)
        return []

    async def _cmd_group_info(self, group_id: str) -> str:
        profile = None
        state = None
        profile_error: QQApiError | None = None
        state_error: QQApiError | None = None
        if self._resolve_transport():
            try:
                profile = await self.api.get_group_info(group_id, caller="command")
            except QQApiError as exc:
                profile_error = exc
            try:
                state = await self.api.get_bot_state(group_id, caller="command")
            except QQApiError as exc:
                state_error = exc
        else:
            state_error = QQApiError("未找到 qq_official 平台实例", semantic="transport_error")
        return group_info_text(
            profile,
            state,
            group_id=group_id,
            profile_error=profile_error,
            state_error=state_error,
        )

    async def _cmd_status(
        self, group_id: str, sender_openid: str, admin: bool, group_admin: bool
    ) -> str:
        config = self.store.group_or_default(group_id)
        caps = config.capabilities
        summary = await self.audit.summary(1) if self.audit is not None else {}
        return moderation_status_text(
            group_id=group_id,
            enabled=config.moderation_enabled,
            mode=mode_label_with_source(
                "",
                str(config.mode or ""),
                str(self.store.get_setting("mode") or ""),
            ),
            paused_reason=config.paused_reason,
            full_msg=(caps.get(CAP_FULL_MSG) or {}).get("ok"),
            is_admin=(caps.get(CAP_IS_ADMIN) or {}).get("ok"),
            is_exempt=sender_openid in self.store.trusted(group_id) or admin or group_admin,
            dry_run=self.store.dry_run(),
            join_mode=config.join_review_mode
            or str(self.store.get_setting("join_review_mode") or "off"),
            stats=summary,
        )

    async def _cmd_toggle(self, group_id: str, name: str, sender_openid: str) -> list[str]:
        enable = name.endswith("开启")
        result = await self.set_moderation(group_id, enable, caller=f"command:{sender_openid}")
        if not result.get("ok"):
            return [str(result.get("message") or "操作失败")]
        lines = ["审核已" + ("启用" if enable else "停用") + "。"]
        for warning in result.get("warnings") or []:
            lines.append("提示：" + warning)
        return ["\n".join(lines)]

    async def _cmd_mode(self, group_id: str, args: list[str]) -> list[str]:
        if not args:
            return [
                "当前生效模式："
                + mode_label_with_source(
                    "",
                    str(self.store.group_or_default(group_id).mode or ""),
                    str(self.store.get_setting("mode") or ""),
                )
                + "\n用法：审核模式 严格/标准/宽松/仅记录/跟随（跟随全局）"
            ]
        label = args[0]
        if label in MODE_FOLLOW:
            await self.store.update_group(group_id, {"mode": ""})
            current = str(self.store.get_setting("mode") or "")
            return [
                "本群已改为跟随全局模式："
                + MODE_LABELS.get(current, current)
                + "（管理台「策略」页可改全局默认）"
            ]
        reverse = {value: key for key, value in MODE_LABELS.items()}
        mode = reverse.get(label, label if label in MODERATION_MODES else "")
        if not mode:
            return ["未知模式，可选：严格 / 标准 / 宽松 / 仅记录 / 跟随（跟随全局）"]
        await self.store.update_group(group_id, {"mode": mode})
        return [f"本群审核模式已切换为：{MODE_LABELS.get(mode, mode)}（群级覆盖）"]

    async def _cmd_threshold(self, args: list[str]) -> list[str]:
        if not args:
            return [f"当前阈值：{self.store.get_setting('llm_min_confidence')}"]
        try:
            value = float(args[0])
        except ValueError:
            return ["用法：审核阈值 0.0-1.0"]
        settings = await self.store.update_settings({"llm_min_confidence": value})
        return [f"置信度门槛已设为：{settings['llm_min_confidence']}"]

    async def _cmd_keyword(self, group_id: str, args: list[str]) -> list[str]:
        if not args or args[0] in ("列表", "list"):
            return [keyword_text(self.store.keywords(), group_id)]
        action = args[0]
        bucket = "hard"
        rest = args[1:]
        if rest and rest[0] in ("硬", "软", "hard", "soft"):
            bucket = "hard" if rest[0] in ("硬", "hard") else "soft"
            rest = rest[1:]
        pattern = " ".join(rest).strip()
        if not pattern:
            return ["用法：关键词 添加 [硬/软] <内容> / 关键词 删除 <内容> / 关键词 列表"]
        keywords = self.store.keywords()
        if action in ("添加", "add"):
            keywords.setdefault(bucket, []).append(
                {
                    "id": pattern[:24],
                    "type": "literal",
                    "pattern": pattern,
                    "action": ["recall", "mute"] if bucket == "hard" else [],
                    "scope": "all",
                    "enabled": True,
                }
            )
        elif action in ("删除", "del", "remove"):
            removed = False
            for key in ("hard", "soft"):
                items = keywords.get(key) or []
                kept = [item for item in items if str(item.get("pattern")) != pattern]
                if len(kept) != len(items):
                    removed = True
                keywords[key] = kept
            if not removed:
                return [f"未找到规则：{pattern}"]
        else:
            return ["用法：关键词 添加 [硬/软] <内容> / 关键词 删除 <内容> / 关键词 列表"]
        await self.store.update_keywords(keywords)
        self.rules.reload(self.store.keywords())
        return [f"已更新规则库（{bucket}）：{pattern}"]

    async def _cmd_trust(
        self, event: AstrMessageEvent, group_id: str, name: str, args: list[str]
    ) -> list[str]:
        openid, member_name = self._resolve_target(event, group_id, args)
        if not openid:
            return ["请 @ 目标成员，或使用其 openid。"]
        trusted = self.store.trusted(group_id)
        if name == "信任":
            if openid not in trusted:
                trusted.append(openid)
        else:
            trusted = [item for item in trusted if item != openid]
        await self.store.update_trusted(group_id, trusted)
        verb = "已加入" if name == "信任" else "已移出"
        return [f"{verb}审核豁免名单：{member_name or mask_openid(openid)}"]

    async def _cmd_mute(
        self,
        event: AstrMessageEvent,
        group_id: str,
        args: list[str],
        sender_name: str,
    ) -> list[str]:
        del sender_name
        openid, member_name = self._resolve_target(event, group_id, args)
        if not openid:
            return ["请 @ 要禁言的成员，或使用其 openid。"]
        seconds = DEFAULT_MUTE_SECONDS
        for token in args:
            parsed = parse_duration(token)
            if parsed:
                seconds = parsed
                break
        max_seconds = int(self.store.get_setting("max_mute_days", 30) or 30) * 86400
        seconds = max(60, min(seconds, max_seconds))
        try:
            await self.api.mute_member(group_id, openid, seconds=seconds, caller="command")
        except QQApiError as exc:
            return [f"禁言失败：{exc.hint or exc.message}"]
        if self.audit is not None:
            await self.audit.upsert_mute(
                group_id=group_id,
                member_openid=openid,
                username=member_name,
                until_unix=now_ts() + seconds,
                reason="管理员指令",
                source="manual",
                active=True,
            )
        return [f"已禁言 {member_name or mask_openid(openid)}，时长 {format_duration(seconds)}。"]

    async def _cmd_unmute(
        self, event: AstrMessageEvent, group_id: str, args: list[str]
    ) -> list[str]:
        openid, member_name = self._resolve_target(event, group_id, args)
        if not openid:
            return ["请 @ 要解禁的成员，或使用其 openid。"]
        try:
            await self.api.unmute_member(group_id, openid, caller="command")
        except QQApiError as exc:
            return [f"解禁失败：{exc.hint or exc.message}"]
        if self.audit is not None:
            await self.audit.set_mute_active(group_id, openid, False)
        return [f"已解除 {member_name or mask_openid(openid)} 的禁言。"]

    async def _cmd_recall(self, event: AstrMessageEvent, group_id: str) -> list[str]:
        message_id = self._reply_message_id(event) or self._message_id(event)
        if not message_id:
            return ["请引用要撤回的消息后发送「撤回」。"]
        try:
            await self.api.recall_message(group_id, message_id, caller="command")
        except QQApiError as exc:
            return [f"撤回失败：{exc.hint or exc.message}"]
        return ["已撤回该消息。"]

    async def _cmd_log(self, group_id: str, args: list[str]) -> list[str]:
        if self.audit is None:
            return ["审计库尚未就绪。"]
        limit = 5
        if args:
            try:
                limit = max(1, min(20, int(args[0])))
            except ValueError:
                limit = 5
        result = await self.audit.query_logs(
            "events", filters={"group_id": group_id}, page=1, page_size=limit
        )
        return [log_text(result.get("items") or [])]

    async def _cmd_stats(self, args: list[str]) -> list[str]:
        if self.audit is None:
            return ["审计库尚未就绪。"]
        days = 1
        if args and args[0] in ("7天", "7", "周"):
            days = 7
        summary = await self.audit.summary(days)
        return [stats_text(summary, days=days)]

    async def _cmd_appeal(
        self,
        event: AstrMessageEvent,
        group_id: str,
        args: list[str],
        sender_openid: str,
        sender_name: str,
    ) -> list[str]:
        if self.audit is None:
            return ["审计库尚未就绪。"]
        reason = " ".join(args).strip()
        if not reason:
            return ["用法：申诉 <理由>（可回复被处置的消息）"]
        row = await self.audit.find_last_event(group_id, sender_openid)
        if row is None:
            return ["没有找到你近期被处置的记录。"]
        await self.audit.mark_appeal(int(row.get("id") or 0), reason)
        await self._notify(
            "appeal",
            {
                "group_id": group_id,
                "group_name": self.store.group_or_default(group_id).name,
                "sender_name": sender_name,
                "sender_openid": sender_openid,
                "text": reason,
                "event_id": row.get("id"),
            },
        )
        del event
        return ["申诉已提交，管理员会复核这条记录。"]

    async def _cmd_join_mode(self, group_id: str, args: list[str]) -> list[str]:
        if not args:
            current = self.store.group_or_default(group_id).join_review_mode or str(
                self.store.get_setting("join_review_mode")
            )
            return [
                f"当前入群审批模式：{JOIN_MODE_LABELS.get(current, current)}"
                "\n用法：入群审核 开启 / 关闭 / 模式 严格|标准|人工"
            ]
        token = args[0]
        mode = {
            "开启": "standard",
            "打开": "standard",
            "关闭": "off",
            "严格": "strict",
            "标准": "standard",
            "人工": "human",
        }.get(token, "")
        if not mode and len(args) > 1:
            mode = {
                "严格": "strict",
                "标准": "standard",
                "人工": "human",
                "关闭": "off",
            }.get(args[1], "")
        if mode not in JOIN_REVIEW_MODES:
            return ["用法：入群审核 开启 / 关闭 / 模式 严格|标准|人工"]
        await self.store.update_group(group_id, {"join_review_mode": mode})
        return [f"本群入群审批已设为：{JOIN_MODE_LABELS.get(mode, mode)}"]

    async def _cmd_join_list(self, group_id: str) -> list[str]:
        pending = self.joins.list_pending(group_id)
        if not pending and self._resolve_transport():
            try:
                await self.joins.poll_group(group_id)
            except Exception as exc:
                return [f"拉取入群申请失败：{exc}"]
            pending = self.joins.list_pending(group_id)
        return [join_list_text(pending)]

    async def _cmd_join_decide(
        self,
        group_id: str,
        name: str,
        args: list[str],
        sender_openid: str,
        *,
        approve: bool,
    ) -> list[str]:
        del name
        pending = self.joins.list_pending(group_id)
        if not pending:
            return ["当前没有待人工审批的入群申请。"]
        index = 1
        reason_parts = args
        if args:
            try:
                index = int(args[0])
                reason_parts = args[1:]
            except ValueError:
                index = 1
        if not 1 <= index <= len(pending):
            return [f"序号超出范围（1-{len(pending)}）。"]
        item = pending[index - 1]
        request = item.get("request") or {}
        result = await self.joins.decide_manual(
            group_id,
            str(request.get("member_openid") or ""),
            op="approve" if approve else "decline",
            join_request_id=str(request.get("join_request_id") or ""),
            reason=" ".join(reason_parts).strip(),
            by=f"human:{sender_openid}",
        )
        if not result.get("ok"):
            return [f"审批失败：{result.get('message')}"]
        return [
            "已{verb} {name} 的入群申请。".format(
                verb="通过" if approve else "拒绝",
                name=str(request.get("username") or "该申请人"),
            )
        ]

    async def _cmd_dry_run(self, args: list[str]) -> list[str]:
        """查看/切换 dry-run（实际处置开关）。"""
        if not args:
            current = self.store.dry_run()
            return [
                "当前运行模式："
                + (
                    "dry-run（只记录 + 警告，不撤回/禁言）"
                    if current
                    else "实际处置（撤回/禁言会真实执行）"
                )
                + "\n用法：dry-run 关闭 / dry-run 开启 / dry-run"
            ]
        token = args[0].strip().lower()
        if token in ("关闭", "off", "disable", "0", "false", "实际", "真实"):
            settings = await self.store.update_settings({"dry_run": False})
            self.logger.warning("dry-run 已关闭，实际处置生效（by=command）")
            return [
                "已关闭 dry-run：命中规则的撤回/禁言会**真实执行**（当前模式："
                + str(settings.get("mode"))
                + "）。如需回退请发送「dry-run 开启」。"
            ]
        if token in ("开启", "on", "enable", "1", "true", "演练"):
            settings = await self.store.update_settings({"dry_run": True})
            self.logger.warning("dry-run 已开启，实际处置停止（by=command）")
            return [
                "已开启 dry-run：只记录与警告，不会撤回或禁言。当前模式："
                + str(settings.get("mode"))
            ]
        return ["用法：dry-run 关闭 / dry-run 开启 / dry-run"]

    async def _cmd_blacklist(
        self, event: AstrMessageEvent, group_id: str, args: list[str]
    ) -> list[str]:
        if not args or args[0] in ("列表", "list"):
            platform: list[dict[str, Any]] = []
            error = ""
            if self._resolve_transport():
                try:
                    response = await self.api.get_blacklist(group_id, limit=20, caller="command")
                    platform = [
                        item for item in (response.get("users") or []) if isinstance(item, dict)
                    ]
                except QQApiError as exc:
                    error = exc.hint or exc.message
            local = self.store.local_blacklist(group_id)
            return [blacklist_text(platform, local, error=error)]
        action = args[0]
        openid, member_name = self._resolve_target(event, group_id, args[1:])
        if not openid:
            return ["请 @ 目标成员，或使用其 openid。"]
        local = self.store.local_blacklist(group_id)
        if action in ("添加", "add"):
            if openid not in local:
                local.append(openid)
            await self.store.update_local_blacklist(group_id, local)
            message = f"已加入本地黑名单：{member_name or mask_openid(openid)}"
            if self._resolve_transport():
                try:
                    await self.api.update_blacklist(
                        group_id, op="add", member_openids=[openid], caller="command"
                    )
                    message += "（平台黑名单已同步）"
                except QQApiError as exc:
                    message += f"（平台黑名单同步失败：{exc.hint or exc.message}）"
            return [message]
        if action in ("移除", "删除", "del"):
            await self.store.update_local_blacklist(
                group_id, [item for item in local if item != openid]
            )
            message = f"已移出本地黑名单：{member_name or mask_openid(openid)}"
            if self._resolve_transport():
                try:
                    await self.api.update_blacklist(
                        group_id, op="del", member_openids=[openid], caller="command"
                    )
                    message += "（平台黑名单已同步）"
                except QQApiError as exc:
                    message += f"（平台黑名单同步失败：{exc.hint or exc.message}）"
            return [message]
        return ["用法：黑名单 添加 @某人 / 黑名单 移除 @某人 / 黑名单 列表"]

    # ------------------------------------------------------------------
    # 供 WebUI 调用的服务方法
    # ------------------------------------------------------------------
    def keywords_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """规则库快照。"""
        return self.store.keywords()

    async def update_keywords(
        self, payload: dict[str, list[dict[str, Any]]]
    ) -> dict[str, list[dict[str, Any]]]:
        """保存规则库并热更新规则引擎。"""
        await self.store.update_keywords(payload or {})
        self.reload_rules()
        return self.store.keywords()

    def reload_rules(self) -> None:
        """按当前配置重建规则引擎（关键词 / 模板 / 形近字表 / 运行参数）。"""
        settings = self.store.settings()
        self.rules.reload(
            self.store.keywords(),
            templates=(self.store.templates() or None)
            if settings.get("template_enabled", True)
            else [],
            homoglyph=(self.store.homoglyph() or None)
            if settings.get("homoglyph_enabled", True)
            else {},
        )
        self.rules.configure(
            auto_enforce_normalized=bool(settings.get("auto_enforce_normalized")),
            fuzzy_max_distance=int(settings.get("fuzzy_max_distance", 1) or 0),
            pinyin_enabled=bool(settings.get("pinyin_enabled")),
        )

    async def update_templates(self, payload: Any) -> list[dict[str, Any]]:
        items = await self.store.update_templates(payload)
        self.reload_rules()
        return items

    async def update_homoglyph(self, payload: Any) -> dict[str, str]:
        items = await self.store.update_homoglyph(payload)
        self.reload_rules()
        return items

    async def test_rules(self, text: str, group_id: str = "") -> dict[str, Any]:
        """规则命中测试（WebUI 关键词视图用）。"""
        settings = self.store.settings()
        evaluation = self.rules.evaluate(
            text or "",
            group_id=group_id,
            flood_threshold=int(settings.get("flood_threshold", 8) or 8),
            duplicate_members=int(settings.get("duplicate_flood_members", 3) or 3),
        )
        return {
            "hits": [hit.to_dict() for hit in evaluation.hits],
            "hard_actions": evaluation.hard_actions,
            "enforce_actions": evaluation.enforce_actions,
            "summary": evaluation.summary(),
            "has_link": evaluation.has_link,
            "has_contact": evaluation.has_contact,
            "long_text": evaluation.long_text,
            "score": evaluation.score,
            "signals": evaluation.signals,
            "views": evaluation.views,
            "should_send": self.moderator.should_send(
                rule_summary=evaluation.rule_summary(),
                has_link=evaluation.has_link,
                long_text=evaluation.long_text,
                new_member=False,
                flood=evaluation.flood,
                recent=evaluation.recent_messages,
                risk_score=evaluation.score,
                has_contact=evaluation.has_contact,
                ad_template=bool(evaluation.template_hits),
            ),
        }

    async def list_mutes(self, group_id: str | None = None) -> list[dict[str, Any]]:
        """本地禁言台账。"""
        if self.audit is None:
            return []
        return await self.audit.list_mutes(group_id)

    async def mute_members(
        self, group_id: str, openids: list[str], *, seconds: int, reason: str = "管理员操作"
    ) -> dict[str, Any]:
        """批量禁言（供 WebUI/指令复用）。"""
        results: list[dict[str, Any]] = []
        for openid in openids:
            try:
                await self.api.mute_member(group_id, openid, seconds=seconds, caller="webui")
                if self.audit is not None:
                    await self.audit.upsert_mute(
                        group_id=group_id,
                        member_openid=openid,
                        username=self.store.member_name(group_id, openid),
                        until_unix=now_ts() + seconds,
                        reason=reason,
                        source="manual",
                        active=True,
                    )
                results.append({"member_openid": openid, "ok": True})
            except QQApiError as exc:
                results.append(
                    {
                        "member_openid": openid,
                        "ok": False,
                        "message": exc.hint or exc.message,
                        "err_code": exc.err_code,
                    }
                )
        return {"results": results}

    async def unmute_members(self, group_id: str, openids: list[str]) -> dict[str, Any]:
        """批量解禁。"""
        results: list[dict[str, Any]] = []
        for openid in openids:
            try:
                await self.api.unmute_member(group_id, openid, caller="webui")
                if self.audit is not None:
                    await self.audit.set_mute_active(group_id, openid, False)
                results.append({"member_openid": openid, "ok": True})
            except QQApiError as exc:
                results.append(
                    {
                        "member_openid": openid,
                        "ok": False,
                        "message": exc.hint or exc.message,
                        "err_code": exc.err_code,
                    }
                )
        return {"results": results}

    async def sync_mutes(self, group_id: str) -> dict[str, Any]:
        """与平台对账某群禁言状态。"""
        try:
            response = await self.api.get_restrict_setting(group_id, caller="webui")
        except QQApiError as exc:
            return {"ok": False, "message": exc.hint or exc.message, "err_code": exc.err_code}
        members = [item for item in (response.get("members") or []) if isinstance(item, dict)]
        if self.audit is not None:
            for item in members:
                openid = str(item.get("member_openid") or "")
                until = parse_iso(item.get("mute_expire_at"))
                if not openid or not until:
                    continue
                await self.audit.upsert_mute(
                    group_id=group_id,
                    member_openid=openid,
                    username=str(item.get("username") or ""),
                    until_unix=until,
                    reason="平台对账",
                    source="sync",
                    active=True,
                )
        return {"ok": True, "platform_members": members, "count": len(members)}

    async def member_search(self, group_id: str, query: str) -> dict[str, Any]:
        """成员查询：本地缓存 + 平台接口（内邀能力）。"""
        local = [
            {"member_openid": openid, "username": name, "source": "cache"}
            for openid, name in self.store.find_member_by_name(group_id, query)
        ]
        platform: list[dict[str, Any]] = []
        error = ""
        if self._resolve_transport() and (query or "").startswith(("u_", "A", "F", "7", "E")):
            try:
                detail = await self.api.get_member(group_id, query, caller="webui")
                if detail:
                    platform.append({**detail, "source": "platform"})
            except QQApiError as exc:
                error = exc.hint or exc.message
        elif self._resolve_transport():
            try:
                response = await self.api.list_members(group_id, caller="webui")
                for item in response.get("members") or []:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("username") or "")
                    if query and query not in name:
                        continue
                    platform.append({**item, "source": "platform"})
            except QQApiError as exc:
                error = exc.hint or exc.message
        return {"local": local, "platform": platform, "error": error}

    async def blacklist_snapshot(self, group_id: str) -> dict[str, Any]:
        """平台 + 本地黑名单。"""
        platform: list[dict[str, Any]] = []
        error = ""
        if self._resolve_transport():
            try:
                response = await self.api.get_blacklist(group_id, limit=50, caller="webui")
                platform = [
                    item for item in (response.get("users") or []) if isinstance(item, dict)
                ]
            except QQApiError as exc:
                error = exc.hint or exc.message
        return {
            "platform": platform,
            "local": self.store.local_blacklist(group_id),
            "error": error,
        }

    async def blacklist_update(
        self,
        group_id: str,
        *,
        op: str,
        openids: list[str],
        local_only: bool = False,
    ) -> dict[str, Any]:
        """黑名单增删（平台优先，失败或指定 local_only 时写本地）。"""
        local = self.store.local_blacklist(group_id)
        if op == "add":
            for openid in openids:
                if openid not in local:
                    local.append(openid)
        else:
            local = [item for item in local if item not in openids]
        await self.store.update_local_blacklist(group_id, local)
        result: dict[str, Any] = {"local": local}
        if local_only or not self._resolve_transport():
            result["platform"] = None
            return result
        try:
            response = await self.api.update_blacklist(
                group_id, op=op, member_openids=openids, caller="webui"
            )
            result["platform"] = response
        except QQApiError as exc:
            result["platform"] = {
                "ok": False,
                "message": exc.hint or exc.message,
                "err_code": exc.err_code,
            }
        return result

    async def remove_members(
        self,
        group_id: str,
        openids: list[str],
        *,
        add_to_blacklist: bool = False,
    ) -> dict[str, Any]:
        """批量移除成员（内邀能力，失败会留痕）。"""
        try:
            response = await self.api.batch_remove_members(
                group_id,
                openids,
                add_to_blacklist=add_to_blacklist,
                caller="webui",
            )
            return {"ok": True, "response": response}
        except QQApiError as exc:
            return {"ok": False, "message": exc.hint or exc.message, "err_code": exc.err_code}

    async def joins_snapshot(self, group_id: str | None = None) -> dict[str, Any]:
        """待审申请 + 历史记录 + 官方策略冲突。"""
        pending = self.joins.list_pending(group_id)
        history: list[dict[str, Any]] = []
        if self.audit is not None:
            history = await self.audit.list_joins(group_id, limit=50)
        group_ids = [group_id] if group_id else list(self.store.groups())
        conflicts = await self.policy.conflicts([gid for gid in group_ids if gid])
        return {
            "pending": pending,
            "history": history,
            "conflicts": conflicts,
            "status": self.joins.status(),
        }

    async def joins_fetch(self, group_id: str) -> dict[str, Any]:
        """立即拉取一次入群申请。"""
        if not self._resolve_transport():
            return {"ok": False, "message": "QQ 平台通道不可用"}
        try:
            created = await self.joins.poll_group(group_id)
        except QQApiError as exc:
            return {"ok": False, "message": exc.hint or exc.message, "err_code": exc.err_code}
        return {"ok": True, "created": created, "pending": self.joins.list_pending(group_id)}

    async def joins_decide(
        self,
        group_id: str,
        member_openid: str,
        *,
        op: str,
        join_request_id: str = "",
        reason: str = "",
        blacklist: bool = False,
        by: str = "webui",
    ) -> dict[str, Any]:
        """人工审批。"""
        return await self.joins.decide_manual(
            group_id,
            member_openid,
            op=op,
            join_request_id=join_request_id,
            reason=reason,
            blacklist=blacklist,
            by=by,
        )

    async def policy_snapshot(self, *, force: bool = False) -> dict[str, Any]:
        """官方入群自动审批策略列表 + 冲突信息。"""
        error = ""
        items: list[dict[str, Any]] = []
        try:
            items = await self.policy.list_strategies(force=force, caller="webui")
        except QQApiError as exc:
            error = exc.hint or exc.message
        conflicts = await self.policy.conflicts(list(self.store.groups()))
        return {"strategies": items, "error": error, "conflicts": conflicts}

    async def policy_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """策略维护：enable/disable/execute/whitelist_add/whitelist_del/create/update/delete。"""
        op = str(payload.get("op") or "")
        strategy_id = str(payload.get("strategy_id") or "")
        users = [str(item) for item in (payload.get("users") or []) if str(item).strip()]
        try:
            if op == "enable" or op == "disable":
                return await self.policy.set_enabled(strategy_id, op == "enable", caller="webui")
            if op == "execute":
                return await self.policy.execute(strategy_id, caller="webui")
            if op == "whitelist_add" or op == "whitelist_del":
                return await self.policy.update_whitelist(
                    strategy_id,
                    op="add" if op == "whitelist_add" else "del",
                    users=users,
                    caller="webui",
                )
            if op == "create":
                return await self.policy.create(dict(payload.get("payload") or {}), caller="webui")
            if op == "update":
                return await self.policy.update(
                    strategy_id, dict(payload.get("payload") or {}), caller="webui"
                )
            if op == "delete":
                return await self.policy.delete(strategy_id, caller="webui")
        except QQApiError as exc:
            return {"ok": False, "message": exc.hint or exc.message, "err_code": exc.err_code}
        return {"ok": False, "message": f"未知操作：{op}"}

    async def dryrun(self, payload: dict[str, Any]) -> dict[str, Any]:
        """审核链路试跑：完整判定但不执行任何动作。"""
        import time

        kind = str(payload.get("kind") or "message")
        settings = self.store.settings()
        if kind == "join_request":
            request = dict(payload.get("request") or {})
            group_id = str(payload.get("group_id") or "")
            if not request:
                return {"ok": False, "message": "缺少 request 内容"}
            mode = str(payload.get("mode") or "standard")
            decision = await self.joins.judge(group_id, request, mode=mode)
            return {"ok": True, "kind": kind, "decision": decision.to_dict()}

        text = str(payload.get("text") or "")
        group_id = str(payload.get("group_id") or "")
        if not text.strip():
            return {"ok": False, "message": "缺少待测文本"}
        group_config = self.store.group_or_default(group_id)
        evaluation = self.rules.evaluate(text, group_id=group_id)
        hard_actions = evaluation.hard_actions
        steps: list[dict[str, Any]] = [
            {
                "step": "rules",
                "hits": [hit.to_dict() for hit in evaluation.hits],
                "summary": evaluation.summary(),
                "hard_actions": hard_actions,
            }
        ]
        verdict = None
        if hard_actions:
            hit = evaluation.hard_hits[0]
            verdict = Verdict(
                verdict="violation",
                category=hit.note or "广告引流",
                severity=4,
                confidence=1.0,
                reason=f"命中本地硬规则：{hit.pattern}",
                suggested_action="mute_and_recall",
                source="rule",
            )
        else:
            request = ModerationRequest(
                group_id=group_id,
                text=text,
                sender_role=str(payload.get("sender_role") or "member"),
                sender_name=str(payload.get("sender_name") or "试跑用户"),
                rules_brief=group_config.rules_brief
                or str(settings.get("group_rules_brief") or ""),
                rule_summary=evaluation.summary(),
                message_kind="文本",
                recent_messages=0,
                umo=str(settings.get("notify_session") or ""),
            )
            started = time.monotonic()
            verdict = await self.moderator.judge(
                request,
                templates={
                    "system": str(settings.get("prompt_system") or ""),
                    "user": str(settings.get("prompt_user") or ""),
                },
            )
            steps.append(
                {
                    "step": "llm",
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "raw": verdict.raw,
                    "parse_error": verdict.parse_error,
                    "circuit_open": self.moderator.circuit_open(),
                }
            )
        mode = str(group_config.mode or settings.get("mode") or "standard")
        planned = self.actions.plan_actions(
            verdict=verdict, settings=settings, hard_actions=hard_actions
        )
        if mode == "log_only":
            planned = []
        elif mode == "lenient":
            planned = [action for action in planned if action in ("warn", "report")]
        if verdict.verdict == "allow":
            planned = []
        return {
            "ok": True,
            "kind": kind,
            "verdict": verdict.to_dict()
            if hasattr(verdict, "to_dict")
            else {
                "verdict": verdict.verdict,
                "category": verdict.category,
                "severity": verdict.severity,
                "confidence": verdict.confidence,
                "reason": verdict.reason,
                "suggested_action": verdict.suggested_action,
                "source": verdict.source,
            },
            "mode": mode,
            "planned_actions": planned,
            "dry_run": True,
            "steps": steps,
        }

    async def stats(self, days: int = 1) -> dict[str, Any]:
        """统计汇总（WebUI 总览）。"""
        if self.audit is None:
            return {}
        return await self.audit.summary(days)
