"""本地规则层：零成本的廉价过滤，先于 LLM 判定。

多视图匹配（raw / compact / skeleton / pinyin，见 src/normalize.py）+ 多种规则类型：

- literal     精确子串（与旧版行为一致，命中即按规则动作处置）
- normalized  归一化后子串匹配（覆盖"珈裙苓资料""加 群 领 资 料""加-群-领-资-料"）
- regex       正则（raw 与 compact 上各跑一遍）
- fuzzy       编辑距离 <= N（只兜少量错别字；长度 < 3 的模式不启用）
- pinyin      同音匹配（需环境安装 pypinyin，未安装自动跳过）
- 模板规则    "动作词 x 诱饵词"式组合，命中即产生风险分并送审

风险分（0~100）由命中与内置检测累加，送审条件可用 risk>=N 表达：
既拦住变体广告，又让纯闲聊保持"不送审"。

内置跨消息检测：同人 60 秒刷屏、同文案多号刷屏（同一骨架文本在窗口内被多个不同成员发送）。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .normalize import (
    fuzzy_contains,
    longest_digit_run,
    normalize,
    pinyin_available,
    to_pinyin_skeleton,
)
from .utils import digest_text

LINK_RE = re.compile(
    r"(https?://|www\.)[^\s]+"
    r"|(?:t\.me|t\.cn|qm\.qq\.com|dwz\.|url\.cn|sourl\.cn|jump\.|bit\.ly|suo\.im)"
    r"|[a-z0-9-]{2,}\s*(?:点|\.)\s*(?:com|cn|net|top|xyz|vip|cc)",
    re.IGNORECASE,
)

CONTACT_RE = re.compile(
    r"(?:扣扣|抠抠|qq|q群|企鹅|微信|weixin|wechat|vx|wx|v信|威信|telegram|tg|纸飞机)"
    r"\s*[:：号]?\s*[0-9a-zA-Z_-]{4,}"
    r"|(?:[vV]\s*[:：]\s*[0-9a-zA-Z_-]{4,})"
    r"|(?:群号|裙号)\s*[:：]?\s*\d{5,}"
    r"|(?:1[3-9]\d{9})"
    r"|(?:wxid_[0-9a-zA-Z_-]+)",
    re.IGNORECASE,
)

#: 引导动作词（"去哪里"）
INVITE_VERBS = (
    "加群",
    "进群",
    "入群",
    "拉群",
    "扫码进群",
    "扫码加",
    "扫码",
    "私聊",
    "私信",
    "私我",
    "加我",
    "加v",
    "加薇",
    "加微",
    "威信",
    "联系我",
    "扣我",
    "点击",
    "关注",
    "领取",
    "免费领",
    "群号",
    "二维码",
    "主页",
    "头像",
    "公告",
    "自取",
    "付 费",
    "付费",
    "问管理",
    "找管理",
    "私撩",
    "速进",
    "速上",
    "上车",
    "发车",
    "扣群",
    "进来",
    "加你",
    "扣扣",
    "更多",
)

#: 诱饵/资源词（"给什么"）
BAIT_NOUNS = (
    "资料",
    "资源",
    "福利",
    "教程",
    "答案",
    "兼职",
    "返利",
    "优惠",
    "红包",
    "课程",
    "网课",
    "真题",
    "题库",
    "源码",
    "外挂",
    "彩票",
    "博彩",
    "裸聊",
    "约炮",
    "看片",
    "片",
    "影视",
    "种子",
    "磁力",
    "合集",
    "目录",
    "破解",
    "会员",
    "惊喜",
    "车牌",
    "新片",
    "老片",
    "链接",
    "app",
    "地址",
    "在线",
    "资源站",
    "内部",
    "冷门",
)

#: 黑话/暗语标记（单独出现不算，但与他项组合即为强信号）
SPAM_MARKERS = (
    "懂的都懂",
    "懂的来",
    "你懂的",
    "先到先得",
    "手慢无",
    "非诚勿扰",
    "永久有效",
    "每日更新",
    "秒发",
    "诚信",
    "限时",
    "安全可靠",
    "拒绝白嫖",
    "白嫖",
    "寂寞",
    "睡不着",
    "深夜",
    "老司机",
    "速上",
    "速进",
    "别声张",
    "不迷路",
    "防失联",
    "备用群",
    "内部群",
    "拉你进群",
    "拉你进",
    "看更多",
    "朋友圈",
    "代找",
    "群主推荐",
    "一包烟钱",
    "全网资源",
    "想要的都",
    "上车",
    "车牌在",
    "稳赚",
    "稳赚不赔",
    "日赚",
    "无风险",
    "全网首发",
    "仅限今天",
    "限量",
    "加我扣扣",
    "加我微信",
)

#: 强标记词：单独出现在**短消息**里即可判定为暗语引流
STRONG_MARKERS = (
    "懂的都懂",
    "懂的来",
    "你懂的",
    "自取",
    "上车",
    "防失联",
    "备用群",
    "内部群",
    "加v",
    "加薇",
    "加微",
    "私聊我",
    "速上",
)

#: 内置广告模板（可被 KV keywords.templates 覆盖/追加）
BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "ad_invite",
        "name": "拉群引流",
        "all_of": [
            {"any_of": list(INVITE_VERBS)},
            {"any_of": [*BAIT_NOUNS, *SPAM_MARKERS]},
        ],
        "score": 50,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        "id": "ad_lure",
        "name": "黑话引流",
        "all_of": [
            {"any_of": list(SPAM_MARKERS)},
            {"any_of": list(BAIT_NOUNS)},
        ],
        "score": 55,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        "id": "ad_night",
        "name": "深夜福利引流",
        "all_of": [
            {
                "any_of": [
                    "深夜",
                    "老司机",
                    "发车",
                    "上车",
                    "车牌",
                    "开车",
                    "寂寞",
                    "睡不着",
                    "看片",
                ]
            },
            {"any_of": ["福利", "资源", "片", "群", "公告", "惊喜", "车牌", "影视"]},
        ],
        "score": 60,
        "category": "色情低俗",
        "severity": 4,
        "action": ["warn", "recall", "mute", "report"],
    },
    {
        "id": "ad_piracy",
        "name": "盗版资源贩卖",
        "all_of": [
            {
                "any_of": [
                    "资源",
                    "种子",
                    "磁力",
                    "破解",
                    "会员",
                    "影视",
                    "合集",
                    "打包",
                    "目录",
                    "看片",
                ]
            },
            {
                "any_of": [
                    "进群",
                    "私聊",
                    "管理",
                    "地址",
                    "自取",
                    "付费",
                    "群",
                    "低价",
                    "代找",
                    "秒发",
                ]
            },
        ],
        "score": 55,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        "id": "ad_call",
        "name": "召唤式引流",
        "all_of": [
            {
                "any_of": [
                    "加我",
                    "加你",
                    "私我",
                    "扣我",
                    "联系我",
                    "加v",
                    "加微",
                    "扣扣",
                    "威信",
                    "私聊",
                ]
            },
            {"any_of": ["领取", "免费", "资料", "资源", "福利", "群", "号", "看", "更多", "惊喜"]},
        ],
        "score": 50,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        "id": "ad_contact",
        "name": "私聊引流",
        "all_of": [
            {"any_of": ["私聊", "私信", "加我", "联系我", "加v", "加微", "加qq", "扣我", "私撩"]},
            {"any_of": [*BAIT_NOUNS, "号", "群"]},
        ],
        "score": 50,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        "id": "ad_parttime",
        "name": "兼职刷单",
        "all_of": [
            {"any_of": ["兼职", "刷单", "日结", "在家做", "轻松赚", "躺赚"]},
            {"any_of": ["日入", "月入", "元", "结算", "押金", "垫付", "返利"]},
        ],
        "score": 55,
        "category": "诈骗赌博",
        "severity": 4,
        "action": ["warn", "recall", "mute", "report"],
    },
    {
        "id": "ad_gamble",
        "name": "赌博引流",
        "all_of": [
            {"any_of": ["博彩", "彩票", "下注", "押注", "赌场", "棋牌", "六合", "时时彩"]},
            {"any_of": ["群", "平台", "app", "网址", "链接", "代理", "返水"]},
        ],
        "score": 60,
        "category": "诈骗赌博",
        "severity": 4,
        "action": ["warn", "recall", "mute", "report"],
    },
    {
        "id": "ad_porn",
        "name": "涉黄引流",
        "all_of": [
            {"any_of": ["裸聊", "约炮", "福利姬", "福利群", "色情", "成人", "av"]},
            {"any_of": ["群", "加", "进", "链接", "app", "资源"]},
        ],
        "score": 65,
        "category": "色情低俗",
        "severity": 5,
        "action": ["warn", "recall", "mute", "report"],
    },
]

SCORE_RULES: dict[str, int] = {
    "exact": 100,
    "normalized": 70,
    "pinyin": 60,
    "fuzzy": 55,
    "template": 45,
    "soft_rule": 20,
    "link": 25,
    "contact": 60,
    "digit_run": 20,
    "digit_run_long": 60,
    "invite_bait": 25,
    "spam_marker": 40,
    "spam_markers_many": 60,
    "bait_many": 60,
    "bait_pair": 40,
    "coded_hint": 25,
    "flood": 20,
    "duplicate_content": 35,
    "repeat_chars": 15,
    "long_text": 10,
    "image": 60,
}

SIGNAL_LABELS = {
    "link": "包含外链或短链",
    "contact": "疑似联系方式",
    "digit_run": "含长数字串且伴随引流词",
    "invite_bait": "含拉群动作词与诱饵词",
    "spam_marker": "含引流黑话",
    "spam_markers_many": "多处引流黑话",
    "bait_many": "多个资源/诱饵词",
    "bait_pair": "两个资源/诱饵词",
    "coded_hint": "短消息暗语",
    "flood": "短时间高频发言",
    "duplicate_content": "同一文案多号发送",
    "repeat_chars": "重复字符刷屏",
    "long_text": "超长文本",
    "image": "含图片",
}

MAX_CACHE_GROUPS = 500
FLOOD_WINDOW = 60.0
DUPLICATE_WINDOW = 300.0


@dataclass(slots=True)
class RuleHit:
    """一条命中的规则。"""

    bucket: str
    rule_id: str
    pattern: str
    rule_type: str = "literal"
    matched: str = ""
    actions: list[str] = field(default_factory=list)
    note: str = ""
    score: int = 0
    enforce: bool = False
    """True 表示精确命中硬规则，可直接按规则动作处置；否则只作为送审依据。"""
    category: str = ""
    severity: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "rule_id": self.rule_id,
            "pattern": self.pattern,
            "rule_type": self.rule_type,
            "matched": self.matched[:60],
            "actions": list(self.actions),
            "note": self.note,
            "score": self.score,
            "enforce": self.enforce,
        }


@dataclass(slots=True)
class RuleEvaluation:
    """一次规则评估的结果。"""

    hits: list[RuleHit] = field(default_factory=list)
    signals: dict[str, int] = field(default_factory=dict)
    score: int = 0
    views: dict[str, str] = field(default_factory=dict)
    has_link: bool = False
    has_contact: bool = False
    repeated: bool = False
    long_text: bool = False
    flood: bool = False
    duplicate_content: bool = False
    recent_messages: int = 0
    duplicate_senders: int = 0

    @property
    def hard_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.bucket == "hard"]

    @property
    def soft_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.bucket == "soft"]

    @property
    def template_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.bucket == "template"]

    @property
    def enforce_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.enforce]

    @property
    def enforce_actions(self) -> list[str]:
        actions: list[str] = []
        for hit in self.enforce_hits:
            for action in hit.actions:
                if action not in actions:
                    actions.append(action)
        return actions

    @property
    def hard_actions(self) -> list[str]:
        """兼容旧调用。"""
        return self.enforce_actions

    @property
    def suspicious(self) -> bool:
        return bool(self.hits or self.signals)

    def rule_summary(self) -> str:
        """只含"规则/模板命中"的摘要，用于 rule_hit 送审条件（不含内置启发式）。"""
        return "、".join(
            (hit.pattern if hit.rule_type == "literal" else hit.rule_type + ":" + hit.pattern)
            for hit in self.hits[:6]
        )

    def summary(self) -> str:
        """给提示词用的可疑点摘要。"""
        parts: list[str] = []
        for hit in self.hits[:6]:
            label = hit.pattern if hit.rule_type == "literal" else hit.rule_type + ":" + hit.pattern
            parts.append(label)
        for key in ("link", "contact", "digit_run", "invite_bait", "duplicate_content", "flood"):
            if key in self.signals:
                parts.append(SIGNAL_LABELS.get(key, key))
        if self.repeated:
            parts.append("重复字符刷屏")
        if self.long_text:
            parts.append("超长文本")
        if self.duplicate_senders >= 2:
            parts.append("同一文案被 " + str(self.duplicate_senders) + " 个成员发送")
        return "、".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "signals": dict(self.signals),
            "score": self.score,
            "views": dict(self.views),
            "summary": self.summary(),
        }


class RuleEngine:
    """规则求值 + 轻量刷屏/重复文案统计（纯内存，重启即清空）。"""

    def __init__(
        self,
        keywords: dict[str, list[dict[str, Any]]] | None = None,
        *,
        templates: list[dict[str, Any]] | None = None,
        homoglyph: dict[str, str] | None = None,
        auto_enforce_normalized: bool = False,
        fuzzy_max_distance: int = 1,
        pinyin_enabled: bool = False,
    ) -> None:
        self._compiled: dict[str, list[dict[str, Any]]] = {"hard": [], "soft": []}
        self._templates: list[dict[str, Any]] = []
        self._homoglyph: dict[str, str] = {}
        self._recent: dict[str, deque[float]] = {}
        self._content: dict[str, deque[tuple[float, str]]] = {}
        self._clock = time.monotonic
        self.auto_enforce_normalized = bool(auto_enforce_normalized)
        self.fuzzy_max_distance = max(0, int(fuzzy_max_distance))
        self.pinyin_enabled = bool(pinyin_enabled) and pinyin_available()
        self.reload(keywords or {}, templates=templates, homoglyph=homoglyph)

    def configure(
        self,
        *,
        auto_enforce_normalized: bool | None = None,
        fuzzy_max_distance: int | None = None,
        pinyin_enabled: bool | None = None,
    ) -> None:
        """热更新运行参数（来自插件配置）。"""
        if auto_enforce_normalized is not None:
            self.auto_enforce_normalized = bool(auto_enforce_normalized)
        if fuzzy_max_distance is not None:
            self.fuzzy_max_distance = max(0, int(fuzzy_max_distance))
        if pinyin_enabled is not None:
            self.pinyin_enabled = bool(pinyin_enabled) and pinyin_available()

    def reload(
        self,
        keywords: dict[str, list[dict[str, Any]]],
        *,
        templates: list[dict[str, Any]] | None = None,
        homoglyph: dict[str, str] | None = None,
    ) -> None:
        """重新编译规则、模板与形近字表（WebUI 保存后调用）。"""
        compiled: dict[str, list[dict[str, Any]]] = {"hard": [], "soft": []}
        for bucket in ("hard", "soft"):
            for item in keywords.get(bucket) or []:
                if not isinstance(item, dict) or not item.get("pattern"):
                    continue
                kind = str(item.get("type") or "literal").lower()
                pattern = str(item.get("pattern"))
                regex = None
                if kind == "regex":
                    try:
                        regex = re.compile(pattern, re.IGNORECASE)
                    except re.error:
                        continue
                compiled[bucket].append(
                    {
                        "id": str(item.get("id") or pattern[:24]),
                        "kind": kind,
                        "pattern": pattern,
                        "pattern_normalized": None,
                        "pattern_pinyin": None,
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
        if homoglyph is not None:
            self._homoglyph = {
                str(key): str(value)
                for key, value in (homoglyph or {}).items()
                if str(key) and str(value)
            }
        self._templates = self._compile_templates(templates)

    @staticmethod
    def _compile_templates(templates: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        # None：使用内置模板；[]（显式空列表）：完全关闭模板规则
        items = BUILTIN_TEMPLATES if templates is None else templates
        compiled: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            groups = [
                [str(word) for word in (group.get("any_of") or []) if str(word)]
                for group in (item.get("all_of") or [])
                if isinstance(group, dict)
            ]
            groups = [group for group in groups if group]
            if not groups:
                continue
            compiled.append(
                {
                    "id": str(item.get("id") or "template"),
                    "name": str(item.get("name") or item.get("id") or "广告模板"),
                    "groups": groups,
                    "score": int(item.get("score") or SCORE_RULES["template"]),
                    "category": str(item.get("category") or "广告引流"),
                    "severity": int(item.get("severity") or 3),
                    "actions": [
                        str(action) for action in (item.get("action") or []) if str(action)
                    ],
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        return compiled

    # ------------------------------------------------------------------
    def note_message(self, group_id: str, member_openid: str) -> int:
        """记录一条发言，返回该成员最近 60 秒内的发言数。"""
        if not member_openid:
            return 0
        key = group_id + ":" + member_openid
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

    def note_content(
        self,
        group_id: str,
        text: str,
        member_openid: str,
        *,
        window: float = DUPLICATE_WINDOW,
    ) -> int:
        """记录一条消息内容，返回窗口内发送同一文案的**不同成员数**。"""
        if not text or not member_openid:
            return 0
        key = group_id + ":" + digest_text(text, limit=256)
        now = self._clock()
        bucket = self._content.get(key)
        if bucket is None:
            if len(self._content) > MAX_CACHE_GROUPS * 100:
                self._content.clear()
            bucket = deque(maxlen=50)
            self._content[key] = bucket
        bucket.append((now, member_openid))
        cutoff = now - max(30.0, float(window))
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()
        return len({item[1] for item in bucket})

    # ------------------------------------------------------------------
    def _match_rule(self, rule: dict[str, Any], views: Any) -> tuple[str, bool]:
        """返回 (命中片段, 是否"原样精确命中")。空串表示未命中。"""
        kind = rule["kind"]
        pattern = rule["pattern"]
        if kind == "regex":
            for view in (views.raw, views.compact, views.skeleton):
                if view:
                    found = rule["regex"].search(view)
                    if found:
                        return found.group(0), False
            return "", False
        if kind == "pinyin":
            if not self.pinyin_enabled or not views.pinyin:
                return "", False
            needle = rule.get("pattern_pinyin")
            if needle is None:
                needle = to_pinyin_skeleton(pattern)
                rule["pattern_pinyin"] = needle
            return (pattern, False) if needle and needle in views.pinyin else ("", False)
        if kind == "fuzzy":
            if self.fuzzy_max_distance <= 0:
                return "", False
            for view in (views.compact, views.skeleton):
                if view and fuzzy_contains(view, pattern, self.fuzzy_max_distance):
                    return pattern, False
            return "", False
        if kind == "normalized":
            needle = self._normalized_pattern(rule)
            if not needle:
                return "", False
            for view in (views.compact, views.skeleton):
                if view and needle in view:
                    return needle, False
            return self._match_pinyin(rule, views)

        # literal：先按原样子串（精确命中），再退回归一化/骨架（变体命中）
        lowered = (views.raw or "").lower()
        if pattern.lower() in lowered:
            return pattern, True
        needle = self._normalized_pattern(rule)
        if needle:
            for view in (views.compact, views.skeleton):
                if view and needle in view:
                    return needle, False
        return self._match_pinyin(rule, views)

    def _match_pinyin(self, rule: dict[str, Any], views: Any) -> tuple[str, bool]:
        """同音兜底：把规则词转成拼音骨架后在消息的拼音视图里找（如 jiaqunlingziliao）。"""
        if not self.pinyin_enabled or not views.pinyin:
            return "", False
        needle = rule.get("pattern_pinyin")
        if needle is None:
            needle = to_pinyin_skeleton(rule["pattern"])
            rule["pattern_pinyin"] = needle
        if needle and needle in views.pinyin:
            return str(needle), False
        return "", False

    def _normalized_pattern(self, rule: dict[str, Any]) -> str:
        """规则的归一化形式（带缓存）。"""
        needle = rule.get("pattern_normalized")
        if needle is None:
            needle = normalize(rule["pattern"], homoglyph=self._homoglyph).skeleton
            rule["pattern_normalized"] = needle
        return str(needle or "")

    def evaluate(
        self,
        text: str,
        *,
        group_id: str = "",
        flood_threshold: int = 8,
        recent_messages: int = 0,
        duplicate_senders: int = 0,
        duplicate_members: int = 3,
    ) -> RuleEvaluation:
        """求值：多视图规则匹配 + 模板匹配 + 内置检测 + 风险分。"""
        views = normalize(text or "", homoglyph=self._homoglyph, with_pinyin=self.pinyin_enabled)
        result = RuleEvaluation(
            views={
                "raw": views.raw,
                "compact": views.compact,
                "skeleton": views.skeleton,
                "pinyin": views.pinyin,
            }
        )
        score = 0

        for bucket in ("hard", "soft"):
            for rule in self._compiled[bucket]:
                if not rule["enabled"] or rule["scope"] not in ("all", group_id):
                    continue
                matched, exact_raw = self._match_rule(rule, views)
                if not matched:
                    continue
                kind = rule["kind"]
                if kind == "literal":
                    # 变体命中（形近字/插符号/全角）只作为送审依据，不直接处置
                    contribution = SCORE_RULES["exact"] if exact_raw else SCORE_RULES["normalized"]
                elif bucket == "soft" and kind == "regex":
                    contribution = SCORE_RULES["soft_rule"]
                elif kind == "normalized":
                    contribution = SCORE_RULES["normalized"]
                elif kind == "pinyin":
                    contribution = SCORE_RULES["pinyin"]
                elif kind == "fuzzy":
                    contribution = SCORE_RULES["fuzzy"]
                else:
                    contribution = SCORE_RULES["soft_rule"]
                enforce = bucket == "hard" and (
                    exact_raw or (self.auto_enforce_normalized and kind != "regex")
                )
                score += contribution
                result.hits.append(
                    RuleHit(
                        bucket=bucket,
                        rule_id=rule["id"],
                        pattern=rule["pattern"],
                        rule_type=kind,
                        matched=matched,
                        actions=list(rule["actions"]),
                        note=rule["note"],
                        score=contribution,
                        enforce=enforce,
                    )
                )

        for template in self._templates:
            if not template["enabled"]:
                continue
            matched_words: list[str] = []
            ok = True
            for group in template["groups"]:
                found = ""
                for word in group:
                    needle = normalize(word, homoglyph=self._homoglyph).skeleton or word.lower()
                    if needle and (
                        needle in views.skeleton
                        or needle in views.compact
                        or word.lower() in (views.raw or "").lower()
                    ):
                        found = word
                        break
                if not found or found in matched_words:
                    ok = False
                    break
                matched_words.append(found)
            if ok:
                score += template["score"]
                result.hits.append(
                    RuleHit(
                        bucket="template",
                        rule_id=template["id"],
                        pattern=template["name"] + "：" + "+".join(matched_words),
                        rule_type="template",
                        matched="+".join(matched_words),
                        actions=list(template["actions"]),
                        note=template["category"],
                        score=template["score"],
                        enforce=False,
                        category=template["category"],
                        severity=template["severity"],
                    )
                )

        if LINK_RE.search(views.raw or ""):
            result.has_link = True
            result.signals["link"] = SCORE_RULES["link"]
        if CONTACT_RE.search(views.raw or "") or CONTACT_RE.search(views.compact):
            result.has_contact = True
            result.signals["contact"] = SCORE_RULES["contact"]
        digit_run = longest_digit_run(text or "")
        if digit_run >= 6 and any(
            word in views.skeleton for word in ("加", "群", "资料", "领", "联系")
        ):
            # 6~7 位只是弱信号；8 位以上（接近手机号/QQ号）单独就足以送审
            result.signals["digit_run"] = (
                SCORE_RULES["digit_run_long"] if digit_run >= 8 else SCORE_RULES["digit_run"]
            )
        skeleton = views.skeleton
        compact = views.compact
        verb_hits = [word for word in INVITE_VERBS if word in skeleton]
        bait_hits = [word for word in BAIT_NOUNS if word in skeleton]
        marker_hits = [word for word in SPAM_MARKERS if word in skeleton or word in compact]
        if verb_hits and bait_hits:
            result.signals["invite_bait"] = SCORE_RULES["invite_bait"]
        if marker_hits:
            result.signals["spam_marker"] = SCORE_RULES["spam_marker"]
        if len(marker_hits) >= 2:
            result.signals["spam_markers_many"] = SCORE_RULES["spam_markers_many"]
        if len(bait_hits) >= 3:
            result.signals["bait_many"] = SCORE_RULES["bait_many"]
        elif len(bait_hits) == 2:
            result.signals["bait_many"] = SCORE_RULES["bait_pair"]
        # 短消息 + 强暗语（你懂的 / 懂的都懂 / 上车 / 车牌…）才算暗语引流，
        # "深夜""睡不着"这类弱标记不算，避免把"深夜加班"误判。
        if (
            any(word in skeleton or word in compact for word in STRONG_MARKERS)
            and len(skeleton) <= 12
        ):
            result.signals["coded_hint"] = SCORE_RULES["coded_hint"]
        if recent_messages >= max(1, flood_threshold):
            result.flood = True
            result.recent_messages = recent_messages
            result.signals["flood"] = SCORE_RULES["flood"]
        if duplicate_senders >= max(2, duplicate_members):
            result.duplicate_content = True
            result.duplicate_senders = duplicate_senders
            result.signals["duplicate_content"] = SCORE_RULES["duplicate_content"]
        if re.search(r"(.)\1{6,}", text or ""):
            result.repeated = True
            result.signals["repeat_chars"] = SCORE_RULES["repeat_chars"]
        if len(text or "") >= 600:
            result.long_text = True
            result.signals["long_text"] = SCORE_RULES["long_text"]

        score += sum(result.signals.values())
        result.score = min(100, score)
        return result

    def with_flood(
        self, evaluation: RuleEvaluation, count: int, threshold: int = 8
    ) -> RuleEvaluation:
        """补充刷屏判定（兼容旧调用：先 note_message 再补分）。"""
        evaluation.recent_messages = count
        evaluation.flood = threshold > 0 and count >= threshold
        if evaluation.flood and "flood" not in evaluation.signals:
            evaluation.signals["flood"] = SCORE_RULES["flood"]
            evaluation.score = min(100, evaluation.score + SCORE_RULES["flood"])
        return evaluation
