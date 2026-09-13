"""QQ群管理插件入口（AstrBot Star）。

面向 QQ 官方机器人（qq_official）：
- 群档案与平台能力探测（白名单 / 群管理员 / 内邀），受限一律留痕；
- 基于 AstrBot 已配置 LLM 的群消息合法性识别与处置（M2）；
- 入群申请智能审批与成员管理（M3）；
- SQLite 审计库 + WebUI 管理台（本文件负责装配与生命周期）。

M1 交付范围：骨架、QQ API 客户端、SQLite 审计、能力探测、基础指令、WebUI 骨架。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if _PLUGIN_ROOT not in sys.path:  # 允许 from .src... 导入与直接运行测试
    sys.path.insert(0, _PLUGIN_ROOT)

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .src.api_client import BotpyTransport, QQApiError, QQGroupAPI
from .src.audit import AuditStore
from .src.commands import (
    CONFIG_COMMANDS,
    FULL_MSG_GUIDE,
    INFO_COMMANDS,
    MENU_COMMANDS,
    SELFCHECK_COMMANDS,
    STATUS_COMMANDS,
    WEBUI_HINT,
    group_info_text,
    menu_text,
    moderation_status_text,
    selfcheck_text,
    suggestions_for,
)
from .src.models import (
    CAP_BOT_STATE,
    CAP_FULL_MSG,
    CAP_IS_ADMIN,
    CapabilityResult,
)
from .src.scheduler import TaskScheduler, TaskSpec
from .src.store import AstrBotKVBackend, PluginStore
from .src.utils import mask_openid, normalize_command, now_ts, to_iso
from .src.web_api import EventBus, WebApi

PLUGIN_NAME = "astrbot_plugin_qq_group_manager"
VERSION = "0.1.0"

STATE_FLUSH_INTERVAL = 30.0
MAINTENANCE_INTERVAL = 3600.0


class QQGroupManager(Star):
    """QQ 群管理插件主体。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.store = PluginStore(AstrBotKVBackend(self), logger=self.logger)
        self.bus = EventBus()
        self.scheduler = TaskScheduler(logger=self.logger)
        self.api = QQGroupAPI(None, dry_run_getter=self.store.dry_run)
        self.audit: AuditStore | None = None
        self.initialized = False
        self.data_dir: Path = self._resolve_data_dir()
        self._platform_id: str = ""
        self._last_prune_day: str = ""
        WebApi(self).register()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """加载配置、打开审计库、启动后台任务。"""
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
        self._resolve_transport()
        self._register_tasks()
        await self.scheduler.start()
        self.initialized = True
        self.logger.info(
            "QQ群管理 %s 已启动（db=%s，transport=%s，dry_run=%s）",
            VERSION,
            path,
            "可用" if self.api.available else "不可用",
            settings.get("dry_run"),
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
        self.scheduler.add(
            TaskSpec(
                "probe_capabilities",
                self._task_probe_capabilities,
                float(self.store.get_setting("probe_full_msg_interval", 1800) or 1800),
            )
        )
        self.scheduler.add(TaskSpec("maintenance", self._task_maintenance, MAINTENANCE_INTERVAL))

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
    # 供 WebUI / 指令使用的服务方法
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
            "db_queue": dict(self.audit.stats) if self.audit else {},
            "sse_subscribers": self.bus.subscriber_count(),
            "now": to_iso(),
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
        results = await self.probe_group(group_id, caller=caller)
        state = results.get(CAP_BOT_STATE)
        if state is None or not state.ok:
            note = (state.note if state else "") or "平台未授权（可能需要在开放平台申请白名单）"
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
            return {
                "ok": False,
                "reason_code": "need_full_msg",
                "message": FULL_MSG_GUIDE,
            }
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
    # 消息处理
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群消息：登记活跃群/成员缓存，并处理管理指令。

        M1 只做登记与指令；内容审核在 M2 接入（同一入口扩展）。
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
            name, args = self._match_command(text)
            if not name:
                return
            reply = await self._handle_command(
                event,
                group_id=group_id,
                name=name,
                args=args,
                admin=bool(event.is_admin()),
                group_admin=self.store.is_group_admin(group_id, sender_openid),
                sender_openid=sender_openid,
            )
            for chunk in reply:
                yield event.plain_result(chunk)
        except Exception as exc:  # pragma: no cover - 不让插件异常影响群聊
            self.logger.error("处理群消息失败：%s", exc, exc_info=True)

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
    def _match_command(text: str) -> tuple[str, list[str]]:
        """全匹配指令（支持带参数）。"""
        if not text:
            return "", []
        parts = text.split(" ")
        name = parts[0]
        known = (
            set(MENU_COMMANDS)
            | set(INFO_COMMANDS)
            | set(STATUS_COMMANDS)
            | set(SELFCHECK_COMMANDS)
            | set(CONFIG_COMMANDS)
        )
        if name in known:
            return name, parts[1:]
        return "", []

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
    ) -> list[str]:
        """构造指令回复（返回若干文本分段）。"""
        del args, event
        if name in MENU_COMMANDS:
            return [menu_text()]

        if name in CONFIG_COMMANDS:
            if not admin:
                return ["仅 AstrBot 管理员可查看管理台配置。"]
            return [WEBUI_HINT]

        if name in SELFCHECK_COMMANDS:
            if not admin:
                return ["仅 AstrBot 管理员可执行能力自检。"]
            results = await self.probe_group(group_id, caller="command")
            config = self.store.group_or_default(group_id)
            return [selfcheck_text(group_id, results, group_name=config.name)]

        if name in INFO_COMMANDS:
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
            return [
                group_info_text(
                    profile,
                    state,
                    group_id=group_id,
                    profile_error=profile_error,
                    state_error=state_error,
                )
            ]

        if name in STATUS_COMMANDS:
            config = self.store.group_or_default(group_id)
            caps = config.capabilities
            trusted = self.store.trusted(group_id)
            return [
                moderation_status_text(
                    group_id=group_id,
                    enabled=config.moderation_enabled,
                    mode=config.mode or str(self.store.get_setting("mode") or ""),
                    paused_reason=config.paused_reason,
                    full_msg=(caps.get(CAP_FULL_MSG) or {}).get("ok"),
                    is_admin=(caps.get(CAP_IS_ADMIN) or {}).get("ok"),
                    is_exempt=sender_openid in trusted or admin or group_admin,
                    dry_run=self.store.dry_run(),
                )
            ]
        return []
