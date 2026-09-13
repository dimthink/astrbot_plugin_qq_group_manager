"""本地规则层：零成本的廉价过滤，先于 LLM 判定。

规则来自 KV keywords（WebUI 可在「关键词」视图维护）：
    {"id": "...", "type": "literal"|"regex", "pattern": "加群", "action": ["recall","mute"],
     "scope": "all" | "<group_openid>", "enabled": true, "note": ""}

- hard 规则：命中即按 action 直接处置（action 为空则用处置矩阵）；
- soft 规则：命中只提升关注度（提高严重度基线），仍走 LLM 判定。

另外内置若干启发式检测：外链、联系方式、刷屏、超长文本、新成员，用于
「送审条件」判定与提示词补充。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

LINK_RE = re.compile(
    r"(https?://|www\.)[^\s]+"
    r"|(?:t\.me|t\.cn|qm\.qq\.com|dwz\.|url\.cn|sourl\.cn|jump\.)",
    re.IGNORECASE,
)
CONTACT_RE = re.compile(
    r"(?:扣扣|QQ|qq|微信|wx|vx|V信|威信)\s*[:：]?\s*[0-9a-zA-Z_-]{5,}"
    r"|(?:1[3-9]\d{9})"
    r"|(?:[0-9]{5,12}\s*(?:群|加群))",
)
REPEAT_RE = re.compile(r"(.)\1{6,}")

MAX_CACHE_GROUPS = 500
FLOOD_WINDOW = 60.0


@dataclass(slots=True)
class RuleHit:
    """一条命中的规则。"""

    bucket: str  # hard / soft / builtin
    rule_id: str
    pattern: str
    matched: str = ""
    actions: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "rule_id": self.rule_id,
            "pattern": self.pattern,
            "matched": self.matched[:60],
            "actions": list(self.actions),
            "note": self.note,
        }


@dataclass(slots=True)
class RuleEvaluation:
    """一次规则评估的结果。"""

    hits: list[RuleHit] = field(default_factory=list)
    has_link: bool = False
    has_contact: bool = False
    repeated: bool = False
    long_text: bool = False
    flood: bool = False
    recent_messages: int = 0

    @property
    def hard_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.bucket == "hard"]

    @property
    def soft_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.bucket == "soft"]

    @property
    def hard_actions(self) -> list[str]:
        """硬规则要求执行的动作（去重、保序）。"""
        actions: list[str] = []
        for hit in self.hard_hits:
            for action in hit.actions:
                if action not in actions:
                    actions.append(action)
        return actions

    @property
    def suspicious(self) -> bool:
        if self.hits:
            return True
        return bool(
            self.has_link or self.has_contact or self.repeated or self.flood or self.long_text
        )

    def summary(self) -> str:
        """给提示词用的可疑点摘要。"""
        parts: list[str] = []
        for hit in self.hits[:5]:
            parts.append(f"{hit.bucket}:{hit.pattern}")
        if self.has_link:
            parts.append("包含外链")
        if self.has_contact:
            parts.append("疑似联系方式")
        if self.flood:
            parts.append(f"60 秒内发言 {self.recent_messages} 条（刷屏）")
        if self.repeated:
            parts.append("包含重复字符刷屏")
        if self.long_text:
            parts.append("超长文本")
        return "、".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "has_link": self.has_link,
            "has_contact": self.has_contact,
            "flood": self.flood,
            "recent_messages": self.recent_messages,
            "summary": self.summary(),
        }


class RuleEngine:
    """规则求值 + 轻量刷屏统计（纯内存，重启即清空）。"""

    def __init__(self, keywords: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._compiled: dict[str, list[dict[str, Any]]] = {"hard": [], "soft": []}
        self._recent: dict[str, deque[float]] = {}
        self._clock = time.monotonic
        self.reload(keywords or {})

    # ------------------------------------------------------------------
    def reload(self, keywords: dict[str, list[dict[str, Any]]]) -> None:
        """重新编译规则（WebUI 保存后调用）。"""
        compiled: dict[str, list[dict[str, Any]]] = {"hard": [], "soft": []}
        for bucket in ("hard", "soft"):
            items = keywords.get(bucket) or []
            for item in items:
                if not isinstance(item, dict) or not item.get("pattern"):
                    continue
                kind = str(item.get("type") or "literal").lower()
                pattern = str(item.get("pattern"))
                regex = None
                if kind == "regex":
                    try:
                        regex = re.compile(pattern, re.IGNORECASE)
                    except re.error:
                        continue  # 非法正则直接跳过，避免影响审核链路
                compiled[bucket].append(
                    {
                        "id": str(item.get("id") or pattern[:24]),
                        "kind": kind,
                        "pattern": pattern,
                        "regex": regex,
                        "actions": [
                            str(action)
                            for action in (item.get("action") or [])
                            if isinstance(action, (str, int))
                        ],
                        "scope": str(item.get("scope") or "all"),
                        "enabled": bool(item.get("enabled", True)),
                        "note": str(item.get("note") or ""),
                    }
                )
        self._compiled = compiled

    # ------------------------------------------------------------------
    def note_message(self, group_id: str, member_openid: str) -> int:
        """记录一条发言，返回该成员最近 60 秒内的发言数。"""
        if not member_openid:
            return 0
        key = f"{group_id}:{member_openid}"
        now = self._clock()
        bucket = self._recent.get(key)
        if bucket is None:
            if len(self._recent) > MAX_CACHE_GROUPS * 100:
                self._recent.clear()
            bucket = deque(maxlen=200)
            self._recent[key] = bucket
        bucket.append(now)
        cutoff = now - FLOOD_WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket)

    def evaluate(
        self, text: str, *, group_id: str = "", flood_threshold: int = 8
    ) -> RuleEvaluation:
        """求值：返回命中的规则与启发式信号。"""
        value = text or ""
        result = RuleEvaluation()
        for bucket in ("hard", "soft"):
            for rule in self._compiled[bucket]:
                if not rule["enabled"]:
                    continue
                if rule["scope"] not in ("all", group_id):
                    continue
                matched = ""
                if rule["kind"] == "regex":
                    found = rule["regex"].search(value)
                    matched = found.group(0) if found else ""
                elif rule["pattern"] in value:
                    matched = rule["pattern"]
                if matched:
                    result.hits.append(
                        RuleHit(
                            bucket=bucket,
                            rule_id=rule["id"],
                            pattern=rule["pattern"],
                            matched=matched,
                            actions=list(rule["actions"]),
                            note=rule["note"],
                        )
                    )
        result.has_link = bool(LINK_RE.search(value))
        result.has_contact = bool(CONTACT_RE.search(value))
        result.repeated = bool(REPEAT_RE.search(value))
        result.long_text = len(value) >= 600
        return result

    def with_flood(
        self, evaluation: RuleEvaluation, count: int, threshold: int = 8
    ) -> RuleEvaluation:
        """补充刷屏判定（调用方先 note_message 拿到 count）。"""
        evaluation.recent_messages = count
        evaluation.flood = threshold > 0 and count >= threshold
        return evaluation
