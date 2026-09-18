"""入群申请轮询与审批引擎。

为什么是轮询：QQ 的 GROUP_JOIN_REQUEST 属于 intent 1<<24，AstrBot 的 qq_official
适配器既未订阅也未实现回调，事件收不到（见 docs/平台能力调研.md §1）。因此本模块
按固定间隔调用 /v2/groups/{gid}/join_request_list，并用数据库去重保证幂等。

决策链（docs/设计方案.md §4.2）：
    risk_tips / bot / 黑名单等硬规则 → LLM 审核申请人 → 自动审批或转人工
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .moderator import extract_json_object
from .utils import clamp_float, now_ts, truncate

JOIN_SYSTEM_PROMPT = """你是 QQ 群入群申请审核引擎。根据群规与申请信息，判断是否放行该申请人。
只输出一个 JSON 对象，不要输出解释性文字，不要使用 Markdown 代码块。

字段：
- decision: "approve" | "decline"
- confidence: 0-1 的小数
- risk: "无" | "广告" | "骚扰" | "诈骗" | "违规内容" | "其他"
- reason: 不超过 40 字的中文理由（拒绝时会给申请人看，请中性客观）

准则：
- 信息正常、无风险信号 → approve
- 昵称/验证消息含广告、引流、联系方式、诈骗话术 → decline
- 验证问题答案与问题明显无关或答非所问 → decline
- 信息不足、无法判断 → decline 并把 confidence 给低值（交由人工处理）
- 不臆测：不要因为昵称特殊字符、地区等无关特征拒绝正常用户"""

JOIN_USER_TEMPLATE = """【群规摘要】{rules_brief}
【申请来源】{apply_source}
【申请人昵称】{username}
【验证方式】{verify_method}
【验证消息】
<<<MESSAGE
{verify_message}
MESSAGE>>>
【问答】
{review_qa}
【平台风险提示】{risk_tips}
【是否机器人账号】{is_bot}
【邀请人 OpenID】{invited_by}"""


@dataclass(slots=True)
class JoinDecision:
    """一次入群申请的判定结果。"""

    op: str = "decline"
    auto: bool = False
    confidence: float = 0.0
    reason: str = ""
    risk: str = "无"
    blacklist: bool = False
    source: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "auto": self.auto,
            "confidence": self.confidence,
            "reason": self.reason,
            "risk": self.risk,
            "blacklist": self.blacklist,
            "source": self.source,
        }


@dataclass
class JoinStats:
    """运行统计。"""

    polls: int = 0
    fetched: int = 0
    approved: int = 0
    declined: int = 0
    manual: int = 0
    failed: int = 0
    last_error: str = ""
    last_poll: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "polls": self.polls,
            "fetched": self.fetched,
            "approved": self.approved,
            "declined": self.declined,
            "manual": self.manual,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_poll": self.last_poll,
        }


class JoinReviewer:
    """入群申请轮询 + 判定 + 审批。"""

    def __init__(
        self,
        *,
        api: Any,
        store: Any,
        audit: Any = None,
        judge_call: Callable[[str, str], Awaitable[str]] | None = None,
        notifier: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        logger: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.store = store
        self.audit = audit
        self.judge_call = judge_call
        self.notifier = notifier
        self.logger = logger
        self._clock = clock
        self.stats = JoinStats()
        self._seen: set[str] = set()
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    async def poll_all(self) -> dict[str, int]:
        """按群轮询入群申请；返回 {group_id: 新增待审数量}。"""
        result: dict[str, int] = {}
        for group_id in list(self.store.groups()):
            try:
                result[group_id] = len(await self.poll_group(group_id))
            except Exception as exc:  # pragma: no cover - 单群失败不影响其它群
                self.stats.failed += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                if self.logger is not None:
                    self.logger.warning("轮询群 %s 的入群申请失败：%s", group_id, exc)
        self.stats.polls += 1
        self.stats.last_poll = now_ts()
        return result

    async def poll_group(self, group_id: str) -> list[dict[str, Any]]:
        """拉取一页入群申请并处理，返回本次新登记（待审）的申请。"""
        cursor = await self.store.get_join_cursor(group_id)
        response = await self.api.join_request_list(
            group_id, cursor=cursor, limit=20, caller="join_review"
        )
        next_cursor = str(response.get("next_cursor") or "")
        await self.store.set_join_cursor(group_id, next_cursor)
        requests = [item for item in (response.get("list") or []) if isinstance(item, dict)]
        self.stats.fetched += len(requests)

        created: list[dict[str, Any]] = []
        for request in requests:
            request_id = str(request.get("join_request_id") or "")
            if not request_id or await self._already_handled(request_id):
                continue
            mode = self._mode_for(group_id)
            decision = await self.judge(group_id, request, mode=mode)
            if decision.auto and mode != "human":
                await self.submit(group_id, request, decision, by=decision.source)
            else:
                self._pending[request_id] = {
                    "group_id": group_id,
                    "request": request,
                    "decision": decision.to_dict(),
                    "created_at": now_ts(),
                }
                self.stats.manual += 1
                await self._persist(group_id, request, decision, decided_by="pending")
                created.append(self._pending[request_id])
                await self._notify_pending(group_id, request, decision)
        return created

    # ------------------------------------------------------------------
    def _mode_for(self, group_id: str) -> str:
        config = self.store.group(group_id)
        mode = str((config.join_review_mode if config else "") or "")
        return mode or str(self.store.get_setting("join_review_mode") or "off")

    async def _already_handled(self, request_id: str) -> bool:
        if request_id in self._seen:
            return True
        if self.audit is not None:
            existing = await self.audit.get_join(request_id)
            if existing and existing.get("decision") not in (None, "", "pending"):
                self._seen.add(request_id)
                return True
        return False

    async def judge(self, group_id: str, request: dict[str, Any], *, mode: str) -> JoinDecision:
        """规则 + LLM 判定一条入群申请。"""
        member_openid = str(request.get("member_openid") or "")
        risk_tips = str(request.get("risk_tips") or "")
        username = str(request.get("username") or "")
        verify = request.get("verify_info") or {}
        verify_message = str(verify.get("verify_message") or "")
        apply_source = str(request.get("apply_source") or "self_apply")

        # 1) 硬规则
        if member_openid and member_openid in self.store.local_blacklist(group_id):
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=1.0,
                reason="命中本地黑名单",
                risk="违规内容",
                source="rule",
            )
        # 1.5) 跨群黑名单（B5）：在 A 群被拉黑的人，B 群默认也拒绝入群
        global_check = getattr(self.store, "is_globally_blacklisted", None)
        if member_openid and callable(global_check) and global_check(member_openid):
            reason = ""
            reason_getter = getattr(self.store, "global_blacklist_reason", None)
            if callable(reason_getter):
                reason = str(reason_getter(member_openid) or "")
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=1.0,
                reason="命中跨群黑名单" + (f"（{reason}）" if reason else ""),
                risk="违规内容",
                source="rule",
            )
        if str(request.get("bot", "")).lower() == "true" or request.get("bot") is True:
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=1.0,
                reason="机器人账号不予放行",
                risk="其他",
                source="rule",
            )
        if risk_tips == "top_tips":
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=1.0,
                reason="平台风险提示（top_tips），自动拒绝",
                risk="违规内容",
                blacklist=True,
                source="rule",
            )
        settings = self.store.settings()
        if apply_source == "invited" and settings.get("join_trust_inviter", False):
            return JoinDecision(
                op="approve",
                auto=True,
                confidence=1.0,
                reason="邀请入群且已信任邀请人",
                source="rule",
            )

        # 2) 模式与 LLM
        if mode == "human" or self.judge_call is None:
            return JoinDecision(
                op="approve",
                auto=False,
                reason="等待人工审批" if mode == "human" else "未配置可用的对话模型，转人工",
                source="manual",
            )
        prompt_request = {
            "rules_brief": self._rules_brief(group_id, settings),
            "apply_source": {"self_apply": "主动申请", "invited": "被邀请"}.get(
                apply_source, apply_source
            ),
            "username": truncate(username, 40) or "未知",
            "verify_method": str(verify.get("method") or "未知"),
            "verify_message": truncate(verify_message, 300) or "（无）",
            "review_qa": self._render_qa(verify.get("review_qa_list")),
            "risk_tips": risk_tips or "无",
            "is_bot": "否",
            "invited_by": str(request.get("invited_by") or "无") or "无",
        }
        user_prompt = JOIN_USER_TEMPLATE
        for key, value in prompt_request.items():
            user_prompt = user_prompt.replace("{" + key + "}", str(value))
        try:
            raw = await self.judge_call(JOIN_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:
            self.stats.failed += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            return JoinDecision(
                op="approve", auto=False, reason="模型调用失败，转人工", source="manual"
            )
        return self.parse_decision(raw, request, settings=settings, mode=mode)

    def parse_decision(
        self,
        raw: str,
        request: dict[str, Any],
        *,
        settings: dict[str, Any],
        mode: str,
    ) -> JoinDecision:
        """解析模型输出并按阈值决定是否自动执行。"""
        payload = extract_json_object(raw or "")
        if payload is None:
            return JoinDecision(
                op="approve",
                auto=False,
                confidence=0.0,
                reason="模型返回无法解析，转人工",
                source="manual",
            )
        decision = "approve" if str(payload.get("decision", "")).lower() == "approve" else "decline"
        confidence = clamp_float(payload.get("confidence"), 0.0, 0.0, 1.0)
        reason = truncate(payload.get("reason") or "", 60)
        risk = str(payload.get("risk") or "无")
        threshold = float(settings.get("join_min_confidence", 0.8) or 0.8)
        if str(request.get("risk_tips") or "") == "warning_tips":
            threshold = min(0.98, threshold + 0.1)
        if mode == "strict" and decision == "approve" and confidence < threshold:
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=confidence,
                reason=reason or "严格模式下置信度不足，自动拒绝",
                risk=risk,
                source="llm",
            )
        auto = confidence >= threshold
        if not auto:
            return JoinDecision(
                op=decision,
                auto=False,
                confidence=confidence,
                reason=reason or "置信度不足，转人工",
                risk=risk,
                source="manual",
            )
        blacklist = decision == "decline" and bool(settings.get("join_decline_blacklist", True))
        return JoinDecision(
            op=decision,
            auto=True,
            confidence=confidence,
            reason=reason,
            risk=risk,
            blacklist=blacklist,
            source="llm",
        )

    def _rules_brief(self, group_id: str, settings: dict[str, Any]) -> str:
        config = self.store.group(group_id)
        if config is not None and config.rules_brief:
            return config.rules_brief
        return str(settings.get("group_rules_brief") or "（未配置，按通用社区规范判断）")

    @staticmethod
    def _render_qa(items: Any) -> str:
        if not isinstance(items, list) or not items:
            return "（无）"
        lines = []
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            question = truncate(item.get("question") or "", 60)
            answer = truncate(item.get("answer") or "", 60)
            lines.append(f"问：{question} / 答：{answer}")
        return "\n".join(lines) or "（无）"

    # ------------------------------------------------------------------
    async def submit(
        self,
        group_id: str,
        request: dict[str, Any],
        decision: JoinDecision,
        *,
        by: str = "llm",
    ) -> dict[str, Any]:
        """执行审批（approve / decline）并落库。"""
        member_openid = str(request.get("member_openid") or "")
        request_id = str(request.get("join_request_id") or "")
        try:
            response = await self.api.approve_join_request(
                group_id,
                member_openid,
                op=decision.op,
                join_request_id=request_id,
                reject_reason=decision.reason if decision.op == "decline" else "",
                add_to_blacklist=bool(decision.blacklist),
                caller="join_review",
            )
        except Exception as exc:
            self.stats.failed += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            await self._persist(group_id, request, decision, decided_by=by, error=str(exc))
            return {"ok": False, "message": str(exc), "join_request_id": request_id}
        self._seen.add(request_id)
        self._pending.pop(request_id, None)
        if decision.op == "approve":
            self.stats.approved += 1
        else:
            self.stats.declined += 1
        await self._persist(group_id, request, decision, decided_by=by)
        return {
            "ok": True,
            "op": decision.op,
            "join_request_id": request_id,
            "dry_run": bool(isinstance(response, dict) and response.get("_dry_run")),
            "reason": decision.reason,
        }

    async def decide_manual(
        self,
        group_id: str,
        member_openid: str,
        *,
        op: str,
        join_request_id: str = "",
        reason: str = "",
        blacklist: bool = False,
        by: str = "human",
    ) -> dict[str, Any]:
        """人工审批（WebUI 或群指令）。"""
        request = {
            "member_openid": member_openid,
            "join_request_id": join_request_id,
        }
        if join_request_id and join_request_id in self._pending:
            request = self._pending[join_request_id]["request"]
        decision = JoinDecision(
            op="approve" if op == "approve" else "decline",
            auto=True,
            confidence=1.0,
            reason=reason or ("人工通过" if op == "approve" else "人工拒绝"),
            blacklist=blacklist,
            source="human",
        )
        result = await self.submit(group_id, request, decision, by=by)
        result["by"] = by
        del member_openid
        return result

    # ------------------------------------------------------------------
    async def _persist(
        self,
        group_id: str,
        request: dict[str, Any],
        decision: JoinDecision,
        *,
        decided_by: str,
        error: str = "",
    ) -> None:
        if self.audit is None:
            return
        verify = request.get("verify_info") or {}
        await self.audit.record_join(
            join_request_id=str(request.get("join_request_id") or ""),
            group_id=group_id,
            member_openid=str(request.get("member_openid") or ""),
            union_openid=str(request.get("union_openid") or ""),
            username=str(request.get("username") or ""),
            apply_source=str(request.get("apply_source") or ""),
            invited_by=str(request.get("invited_by") or ""),
            is_bot=1 if request.get("bot") else 0,
            risk_tips=str(request.get("risk_tips") or ""),
            verify_method=str(verify.get("method") or ""),
            verify_message=truncate(verify.get("verify_message") or "", 300),
            review_qa=str(verify.get("review_qa_list") or ""),
            decision="pending" if decided_by == "pending" else decision.op,
            decided_by=decided_by,
            confidence=decision.confidence,
            reason=(decision.reason + (f" | 失败：{error}" if error else ""))[:200],
            blacklisted=1 if decision.blacklist else 0,
        )

    async def _notify_pending(
        self, group_id: str, request: dict[str, Any], decision: JoinDecision
    ) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier(
                "join_pending",
                {
                    "group_id": group_id,
                    "group_name": (
                        self.store.group(group_id).name if self.store.group(group_id) else ""
                    ),
                    "member_openid": str(request.get("member_openid") or ""),
                    "username": str(request.get("username") or ""),
                    "join_request_id": str(request.get("join_request_id") or ""),
                    "risk_tips": str(request.get("risk_tips") or ""),
                    "verify_message": truncate(
                        (request.get("verify_info") or {}).get("verify_message") or "", 80
                    ),
                    "suggestion": decision.reason,
                },
            )
        except Exception as exc:  # pragma: no cover
            if self.logger is not None:
                self.logger.warning("入群申请通知失败：%s", exc)

    # ------------------------------------------------------------------
    def list_pending(self, group_id: str | None = None) -> list[dict[str, Any]]:
        """列出待人工审批的申请。"""
        items = list(self._pending.values())
        if group_id:
            items = [item for item in items if item.get("group_id") == group_id]
        return [dict(item) for item in items]

    def status(self) -> dict[str, Any]:
        """运行状态（供 WebUI）。"""
        return {
            "pending": len(self._pending),
            "seen": len(self._seen),
            **self.stats.to_dict(),
        }
