"""招聘/家教语境豁免 + 数字连串回归。

线上误杀样本（2026-09-16）：群内学生接家教 / 学校招老师的正常信息，
因为正文里带"加微信 / 联系v:"被按广告引流处置。
"""
from __future__ import annotations

import pytest

from src import rules
from src.normalize import longest_digit_run

# 线上真实误杀样本
LEGIT = {
    "家教-六年级奥数": """六年级航天员起飞教学，绝密航空，魔王护航护航 220一次
详细加微信:sunxiaochuan666 六年级奥数网课，试课半价，急
辅导科目：六年级奥数
学员情况：男孩，杯赛难度
时间安排：周三晚上和周日二选一，一周一次2小时
教员要求：#985数学专业，有奥数杯赛带教经验优先，认真负责。
老师薪水：220/次
联系v:kfvvme50""",
    "家教-幼儿园NOI": """幼儿园小班NOI，试课半价，急
辅导科目：幼儿园小班NOI
学员情况：男孩，杯赛难度
时间安排：周三晚上和周日二选一，一周一次2小时
教员要求：#985计算机专业，有ICPC 金牌带教经验优先，认真负责。
老师薪水：114-514/次
联系v:kfvvme50""",
    "学校招聘": """高中物理教师
启明中学招聘高中物理教师1名，物理学或相关专业本科及以上学历，有高中教学经验者优先。提供五险一金、绩效奖金及寒暑假。""",
    "家教中介群发": """您好，北京各区线下家教代课老师愿意吗，主要负责小初高学生，工资高，工作时间灵活自由，北京各区就近安排合适的岗位，如果愿意的话，可以左上角相互交换一下微信，你加我微信即可""",
}

# 披着家教外衣的真实广告：不能豁免
DISGUISED = {
    "家教+资源": "招聘家教老师，加微信 abc12345 领取全套学习资源，日结300元",
    "家教+免费领取": "一对一辅导家教，加v:abc12345 免费领取资料包，限时",
    "家教+外链": "招聘奥数家教，详情见 https://spam.example.com/join 加群 123456789",
    "家教+色情诱饵": "家教兼职 加微信 abc12345 另有福利资源",
}


@pytest.fixture()
def engine() -> rules.RuleEngine:
    return rules.RuleEngine()


@pytest.mark.parametrize("label,text", list(LEGIT.items()))
def test_legit_recruitment_is_not_flagged(engine, label, text):
    evaluation = engine.evaluate(text=text, group_id="demo")
    assert evaluation.recruit_context is True, label
    assert "recruit_context" in evaluation.signals, label
    assert evaluation.score < 70, (label, evaluation.score)
    assert not evaluation.template_hits, (label, [h.pattern for h in evaluation.hits])


@pytest.mark.parametrize("label,text", list(DISGUISED.items()))
def test_disguised_ads_are_still_caught(engine, label, text):
    evaluation = engine.evaluate(text=text, group_id="demo")
    assert evaluation.recruit_context is False, label
    assert evaluation.score >= 70 or evaluation.template_hits, (
        label,
        evaluation.score,
        [h.pattern for h in evaluation.hits],
    )


def test_digit_run_counts_real_consecutive_run(engine):
    """回归：早期实现把全文数字抽出来拼接，等价于"数字总数"。

    实测家教信息里 220/985/666/50 被算成 24 位长号码 → digit_run_long +60。
    """
    assert longest_digit_run("联系v:kfvvme50 老师薪水：220/次 学历985") == 3
    assert longest_digit_run("加群 998877 领资料") == 6
    assert longest_digit_run("电话 13812345678") == 11
    assert longest_digit_run("2026年9月16日 3人 220元") == 4
    # 正常的家教信息不应再出现长号码信号
    evaluation = engine.evaluate(
        text=LEGIT["家教-六年级奥数"], group_id="demo"
    )
    assert "digit_run" not in evaluation.signals
    assert "digit_run_long" not in evaluation.signals


def test_real_ads_keep_firing(engine):
    """豁免规则不能放松既有能力。"""
    cases = [
        "兼职刷单日结，加我微信 abc12345 领取任务，日入500元，先垫付98元返还128元",
        "博彩平台新用户充值送彩金，进群 123456789 找代理拿返水，网址 xx-bet.com",
        "裸聊福利群，加v:abc12345 看片资源每日更新，懂的都懂",
    ]
    for text in cases:
        evaluation = engine.evaluate(text=text, group_id="demo")
        assert evaluation.recruit_context is False, text
        assert evaluation.score >= 70 or evaluation.template_hits, text
