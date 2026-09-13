"""界面与后端配置契约：防止"点了保存却被后端过滤掉"（表现为保存后恢复默认）。

历史 bug：WebUI 送审条件新增的三个选项（has_contact / ad_template / has_image）
没有同步加进 models.SEND_CONDITIONS，normalize_settings 会把它们过滤掉，
用户勾选保存后界面又变回未勾选状态。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.models import SEND_CONDITIONS, default_settings

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
APP_JS = PLUGIN_ROOT / "pages" / "manage" / "app.js"


def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_condition_labels_all_supported_by_backend():
    text = app_js()
    block = re.search(r"const CONDITION_LABELS = \{(.*?)\};", text, re.S)
    assert block, "未找到 CONDITION_LABELS 定义"
    keys = re.findall(r"^\s*([a-z_]+):", block.group(1), re.M)
    assert keys, "CONDITION_LABELS 解析为空"
    missing = [key for key in keys if key not in SEND_CONDITIONS]
    assert not missing, f"这些送审条件在界面上可选，但后端不支持（会被静默丢弃）：{missing}"


def test_policy_payload_keys_are_known_settings():
    text = app_js()
    start = text.index("async function viewPolicy")
    block = re.search(r"const payload = \{(.*?)\n    \};", text[start:], re.S)
    assert block, "未找到策略页 payload 定义"
    keys = re.findall(r"^\s{6}([a-z_]+):", block.group(1), re.M)
    assert keys, "payload 解析为空"
    known = default_settings()
    missing = [key for key in keys if key not in known]
    assert not missing, (
        f"策略页会提交这些配置，但 default_settings 未声明（保存后会被丢弃）：{missing}"
    )


def test_send_conditions_roundtrip_keeps_all_ui_options():
    from src.store import normalize_settings

    ui_options = [
        "rule_hit",
        "has_link",
        "has_contact",
        "ad_template",
        "has_image",
        "long_text",
        "new_member",
        "flood",
        "all",
    ]
    normalized = normalize_settings({"send_conditions": ui_options})["send_conditions"]
    assert normalized == ui_options
