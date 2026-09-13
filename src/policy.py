"""入群自动审批策略（官方服务端能力）与白名单管理。

官方策略是「服务端白名单放行」：命中的申请由平台自动通过，插件看不到该申请。
因此插件默认不创建策略，只做透明化展示、启停与白名单维护，并检测与插件自身
自动审批的冲突（见 docs/设计方案.md §4.4）。
"""

from __future__ import annotations

import time
from typing import Any

STRATEGY_LIST_TTL = 120.0


class ApprovalPolicyService:
    """策略查询/维护 + 冲突检测。"""

    def __init__(self, *, api: Any, store: Any = None, logger: Any = None) -> None:
        self.api = api
        self.store = store
        self.logger = logger
        self._cache: dict[str, Any] = {"at": 0.0, "items": []}

    async def list_strategies(
        self, *, force: bool = False, caller: str = "policy"
    ) -> list[dict[str, Any]]:
        """查询策略列表（带 2 分钟缓存）。"""
        now = time.monotonic()
        cached = self._cache.get("items") or []
        if not force and cached and now - float(self._cache.get("at") or 0) < STRATEGY_LIST_TTL:
            return list(cached)
        response = await self.api.list_approval_strategies(caller=caller)
        items = [item for item in (response.get("strategies") or []) if isinstance(item, dict)]
        self._cache = {"at": now, "items": items}
        return list(items)

    async def create(self, payload: dict[str, Any], *, caller: str = "policy") -> dict[str, Any]:
        """创建策略。"""
        result = await self.api.create_approval_strategy(payload, caller=caller)
        self._cache["items"] = []
        return result

    async def set_enabled(
        self, strategy_id: str, enabled: bool, *, caller: str = "policy"
    ) -> dict[str, Any]:
        """启用/停用策略。"""
        result = await self.api.update_approval_strategy(
            strategy_id, {"is_enable": "on" if enabled else "off"}, caller=caller
        )
        self._cache["items"] = []
        return result

    async def update(
        self, strategy_id: str, payload: dict[str, Any], *, caller: str = "policy"
    ) -> dict[str, Any]:
        """修改策略（备注 / 过期时间 / 关联群增删）。"""
        result = await self.api.update_approval_strategy(strategy_id, payload, caller=caller)
        self._cache["items"] = []
        return result

    async def delete(self, strategy_id: str, *, caller: str = "policy") -> dict[str, Any]:
        """删除策略。"""
        result = await self.api.delete_approval_strategy(strategy_id, caller=caller)
        self._cache["items"] = []
        return result

    async def execute(self, strategy_id: str, *, caller: str = "policy") -> dict[str, Any]:
        """触发全量扫描（异步，官方说明约 10 分钟）。"""
        return await self.api.execute_approval_strategy(strategy_id, caller=caller)

    async def update_whitelist(
        self, strategy_id: str, *, op: str, users: list[str], caller: str = "policy"
    ) -> dict[str, Any]:
        """批量增删白名单号码。"""
        result = await self.api.update_strategy_whitelist(
            strategy_id, op=op, users=users, caller=caller
        )
        self._cache["items"] = []
        return result

    def _effective_join_mode(self, group_id: str, default: str) -> str:
        config = self.store.group(group_id) if self.store is not None else None
        return str((config.join_review_mode if config else "") or default or "off")

    async def conflicts(self, group_ids: list[str], *, caller: str = "policy") -> dict[str, Any]:
        """检测「官方策略」与「插件自动审批」同时生效的群。"""
        try:
            strategies = await self.list_strategies(caller=caller)
        except Exception as exc:  # pragma: no cover - 策略接口可能未开放
            return {
                "checked": False,
                "conflicts": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        enabled_groups: set[str] = set()
        for item in strategies:
            if str(item.get("is_enable") or "").lower() != "on":
                continue
            enabled_groups.update(str(gid) for gid in (item.get("group_openids") or []))
        default_mode = ""
        if self.store is not None:
            default_mode = str(self.store.get_setting("join_review_mode") or "off")
        conflicts = [
            group_id
            for group_id in group_ids
            if group_id in enabled_groups
            and self._effective_join_mode(group_id, default_mode) != "off"
        ]
        return {"checked": True, "conflicts": conflicts, "error": ""}

    def status(self) -> dict[str, Any]:
        """给 WebUI 的简要状态。"""
        return {
            "cached": len(self._cache.get("items") or []),
            "cached_at": self._cache.get("at"),
        }
