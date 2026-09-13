"""文本归一化：让规则层"看得见"形近字、插符号、全角等规避写法。

三个视图（规则会同时在这些视图上求值）：
- raw：原样，保持与旧版行为一致；
- compact：NFKC（全角转半角/兼容字符归一）→ 小写 → 去零宽与变体选择符 → 去空白与干扰符；
- skeleton：在 compact 基础上做形近字/同音字映射，再去掉非中日韩/字母/数字字符。

可选第四个视图：
- pinyin：环境已安装 pypinyin 时给出拼音骨架（jiaqunlingziliao），未安装则为空串，
  因此插件保持"零第三方依赖"（见 docs/判定规则优化方案.md §5.1）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

#: 内置形近字/同音字基线（可在 WebUI「形近字表」中增删覆盖）
BUILTIN_HOMOGLYPH: dict[str, str] = {
    # 加群
    "珈": "加",
    "伽": "加",
    "茄": "加",
    "枷": "加",
    "佳": "加",
    "家": "加",
    "架": "加",
    # 群
    "裙": "群",
    "羣": "群",
    "麇": "群",
    # 领 / 进
    "苓": "领",
    "伶": "领",
    "玲": "领",
    "岭": "领",
    "呤": "领",
    "領": "领",
    "進": "进",
    "逬": "进",
    # 资 / 料
    "咨": "资",
    "姿": "资",
    "滋": "资",
    "兹": "资",
    "資": "资",
    "廖": "料",
    "疗": "料",
    "聊": "料",
    # 微 / 信 / 扣
    "薇": "微",
    "巍": "微",
    "溦": "微",
    "芯": "信",
    "薪": "信",
    "馨": "信",
    "抠": "扣",
    "蔻": "扣",
    "釦": "扣",
    # 其他广告常用字形
    "費": "费",
    "優": "优",
    "惠": "惠",
    "賺": "赚",
    "錢": "钱",
    "兼职": "兼职",
    "刷單": "刷单",
    "號": "号",
    "碼": "码",
    "顔": "颜",
    "圖": "图",
    "視頻": "视频",
    "網": "网",
    "址": "址",
    "鏈": "链",
    "接": "接",
    "免費": "免费",
    "領取": "领取",
}

#: 干扰符号（compact 视图会去掉）：空白、常见标点、装饰符号
_INTERFERENCE = set(
    " \t\r\n\v\f.,-_~^*|/\\+=!?;:\"'()[]{}<>@#$%&",
)

#: 全角标点（NFKC 之后基本已转半角，这里兜底）
_INTERFERENCE.update("！？，。：；、～…—－＿　·•●○◆◇■□★☆→←↑↓")

_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x00A0, *range(0xFE00, 0xFE10)]
)

_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f0ff"
    "\U00002190-\U000021ff\U00002b00-\U00002bff\U0000fe0f\U000020e3]"
)

_WORD_RE = re.compile(r"[0-9a-z\u4e00-\u9fff]+")

#: 中文数字（用于识别用汉字写的号码）
CN_DIGITS = "零一二三四五六七八九〇两"

_pinyin_available: bool | None = None


def pinyin_available() -> bool:
    """环境是否安装了 pypinyin（可选依赖，未安装不影响功能）。"""
    global _pinyin_available
    if _pinyin_available is None:
        try:
            import pypinyin  # noqa: F401

            _pinyin_available = True
        except Exception:
            _pinyin_available = False
    return _pinyin_available


def to_pinyin_skeleton(text: str) -> str:
    """把文本转成"无分隔拼音骨架"（jiaqunlingziliao），失败返回空串。"""
    if not text or not pinyin_available():
        return ""
    try:
        from pypinyin import Style, lazy_pinyin

        parts = lazy_pinyin(text, style=Style.NORMAL, errors="ignore")
    except Exception:  # pragma: no cover - pypinyin 异常时降级
        return ""
    return "".join(part for part in parts if part.isalnum())


@dataclass(slots=True)
class NormalizedText:
    """一条消息的多个归一化视图。"""

    raw: str = ""
    compact: str = ""
    skeleton: str = ""
    pinyin: str = ""
    homoglyph_hits: list[str] = field(default_factory=list)

    def views(self) -> list[str]:
        """去重后的全部视图（用于多视图匹配）。"""
        seen: list[str] = []
        for value in (self.raw, self.compact, self.skeleton, self.pinyin):
            if value and value not in seen:
                seen.append(value)
        return seen


def _strip_noise(text: str) -> str:
    value = text.translate(_ZERO_WIDTH)
    return _EMOJI_RE.sub("", value)


def compact_text(text: str, *, keep_case: bool = False) -> str:
    """compact 视图：NFKC + 去噪 + 去干扰符（默认转小写）。"""
    value = unicodedata.normalize("NFKC", _strip_noise(text or ""))
    if not keep_case:
        value = value.lower()
    return "".join(char for char in value if char not in _INTERFERENCE)


def skeleton_text(text: str, homoglyph: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """skeleton 视图：compact 之上做形近字映射，再去掉非中日韩/字母/数字字符。

    返回 (骨架文本, 命中的形近字列表)。
    """
    table = dict(BUILTIN_HOMOGLYPH)
    if homoglyph:
        table.update({str(k): str(v) for k, v in homoglyph.items() if str(k) and str(v)})
    compact = compact_text(text)
    hits: list[str] = []
    mapped: list[str] = []
    for char in compact:
        replacement = table.get(char)
        if replacement and replacement != char:
            hits.append(f"{char}->{replacement}")
            mapped.append(replacement)
        else:
            mapped.append(char)
    skeleton = "".join(mapped)
    skeleton = "".join(char for char in skeleton if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    # 去重但保持顺序
    uniq: list[str] = []
    for item in hits:
        if item not in uniq:
            uniq.append(item)
    return skeleton, uniq


def normalize(
    text: str, *, homoglyph: dict[str, str] | None = None, with_pinyin: bool = False
) -> NormalizedText:
    """生成全部视图。"""
    raw = text or ""
    compact = compact_text(raw)
    skeleton, hits = skeleton_text(raw, homoglyph)
    pinyin = to_pinyin_skeleton(compact) if with_pinyin else ""
    return NormalizedText(
        raw=raw, compact=compact, skeleton=skeleton, pinyin=pinyin, homoglyph_hits=hits
    )


def digits_of(text: str) -> str:
    """取文本中的数字串（含中文数字转换），用于"长号码"类判定。"""
    value = compact_text(text)
    out: list[str] = []
    for char in value:
        if char.isdigit():
            out.append(char)
        elif char in CN_DIGITS:
            out.append(str(CN_DIGITS.index(char) % 10))
    return "".join(out)


def longest_digit_run(text: str) -> int:
    """最长连续数字串长度（中文数字也计入）。"""
    best = 0
    current = 0
    for char in digits_of(text):
        current += 1
        best = max(best, current)
        if not char.isdigit():
            current = 0
    return best


def edit_distance_within(a: str, b: str, limit: int) -> bool:
    """判断两串编辑距离是否 <= limit（带上界剪枝，避免长文本开销）。"""
    if limit <= 0:
        return a == b
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        best = current[0]
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > limit:
            return False
        previous = current
    return previous[-1] <= limit


def fuzzy_contains(text: str, pattern: str, limit: int = 1) -> bool:
    """在 text 中滑动窗口判断是否存在与 pattern 编辑距离 <= limit 的子串。"""
    if not pattern or not text:
        return False
    size = len(pattern)
    if size < 3:
        return False
    for start in range(0, max(1, len(text) - size + 1)):
        window = text[start : start + size + limit]
        if edit_distance_within(pattern, window[:size], limit):
            return True
    return False
