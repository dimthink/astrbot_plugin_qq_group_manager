"""重度混淆广告识别（繁体 + 异体字 + 全角数学字母 + 空格插字）。

线上实测样本（2026-09-16）：一条保健药广告与一条非法荐股广告，
通篇用"侽 亾 哋 / 進 媺 國 原 裝 偉 哥 / 純 兲 嘫 無 副 莋 鼡"这类写法，
修复前规则层评分 **0**，完全漏检。
"""
from __future__ import annotations

import pytest

from src import rules
from src.normalize import normalize
from src.simplify import to_simplified

MEDICINE = """💊【侽 亾 哋 𝕤𝕖𝕔𝕣𝕖𝕥！】💊
進 🚪 媺 國 原 裝 偉 哥 💪
讓 沵 𝕣𝕚ɡ𝕙𝕥 𝕦𝕡 變 佷 侽 亾！
🔥 1 粒 見 效，持 玖 4 曉 ⏰
🔥 純 兲 嘫 無 副 莋 鼡
🔥 bǎo 密 髮 貨 📦
dàn 吢 試，芣 滿 噫 tuì 💰！
佲 📞：1 3 8 x x x x （ 苝 炷 「 侽 ㄕ 」）
嫃 潙 沵 女 盆 伖 zhe 想！👊"""

STOCK_SCAM = """📈【nei 募 xiāo 息！】📈
苝 ㊥ 頭 蔀 帶 沵 進 圈 孖！
ㄧ 対 ㄧ 溮 溮 帶 盤 📊
仴 収 益 稳 萣 𝟛𝟘%➕
✅ 實 盤 驗 証 珂 查
✅ suí ⏰ 岀 jīn 芣 鎖 倉
✅ 巳 幫 5 0 0 ➕ 亾 實 哯 財 務 zì 甴
僅 限 1 0 亾！諴 杺 萪 V：
[ t e s t _ i n v e s t ]
備 炷：「悝 財」
錯 濄 僦 湜 ㄧ 輩 孖 🚀"""


@pytest.fixture()
def engine() -> rules.RuleEngine:
    return rules.RuleEngine()


def test_simplified_conversion_table():
    """OpenCC 离线生成的繁→简表可用（运行时零依赖）。"""
    assert to_simplified("進媺國原裝偉哥讓沵變佷侽亾見效") == "进媺国原装伟哥让沵变佷侽亾见效"
    assert to_simplified("財務自由、實盤驗證、咨詢") == "财务自由、实盘验证、咨询"
    # 简体文本不受影响
    assert to_simplified("这个题目我写完了") == "这个题目我写完了"


def test_variant_characters_reach_skeleton():
    """异体/仿造字在骨架视图里应还原成常用字。"""
    views = normalize("侽 亾 哋 沵 佷 媺 兲 嘫 莋 鼡 芣 噫", homoglyph=None, with_pinyin=False)
    assert "男人的你" in views.skeleton
    assert "很" in views.skeleton and "美" in views.skeleton
    assert "天然" in views.skeleton and "作用" in views.skeleton
    assert "不" in views.skeleton and "意" in views.skeleton


def test_obfuscated_medicine_ad_is_caught(engine):
    evaluation = engine.evaluate(text=MEDICINE, group_id="demo")
    assert evaluation.template_hits, [h.pattern for h in evaluation.hits]
    assert any("药品保健品" in h.pattern for h in evaluation.hits)


def test_obfuscated_stock_scam_is_caught(engine):
    evaluation = engine.evaluate(text=STOCK_SCAM, group_id="demo")
    assert evaluation.template_hits, [h.pattern for h in evaluation.hits]
    assert any("荐股理财" in h.pattern for h in evaluation.hits)
    assert evaluation.score >= 70


def test_channel_view_survives_bracket_obfuscation(engine):
    """`V：\\n[ t e s t _ i n v e s t ]` 曾被 compact 删掉冒号而识别不到联系方式。"""
    evaluation = engine.evaluate(text=STOCK_SCAM, group_id="demo")
    assert evaluation.has_contact is True


def test_traditional_normal_chat_is_not_flagged(engine):
    """繁→简不能让正常繁体聊天产生误报。"""
    for text in (
        "這個題目我寫完了，謝謝大家",
        "請問這道題的複雜度是多少？",
        "我們下週再約時間討論",
    ):
        evaluation = engine.evaluate(text=text, group_id="demo")
        assert evaluation.score < 70, (text, evaluation.score)
        assert not evaluation.template_hits, (text, [h.pattern for h in evaluation.hits])


def test_previous_capabilities_unchanged(engine):
    """既有能力回归：家教放行、刷单/赌博/色情仍处置。"""
    tutor = """辅导科目：六年级奥数
学员情况：男孩，杯赛难度
时间安排：周三晚上和周日二选一，一周一次2小时
教员要求：#985数学专业，有奥数杯赛带教经验优先，认真负责。
老师薪水：220/次
联系v:kfvvme50"""
    assert engine.evaluate(text=tutor, group_id="demo").score < 70
    for text in (
        "兼职刷单日结，加我微信 abc12345 领取任务，日入500元，先垫付98元返还128元",
        "博彩平台新用户充值送彩金，进群 123456789 找代理拿返水，网址 xx-bet.com",
    ):
        evaluation = engine.evaluate(text=text, group_id="demo")
        assert evaluation.score >= 70 or evaluation.template_hits, text
