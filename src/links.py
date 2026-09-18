"""链接与域名工具：抽取文本里的域名并做「后缀白名单」判定。

用于 B3 竞赛域名降权：白名单内的链接不计 link 风险分，但**只降权不豁免**——
规则、审计与 LLM 送审判断仍完整执行（见 docs/申诉闭环与白名单实现文档.md §B3）。

本模块只依赖标准库，便于在没有 AstrBot 运行时的情况下做单元测试。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: 与 rules.LINK_RE 保持一致的识别面：带/不带协议、带端口、裸域名均可抽取。
URL_HOST_RE = re.compile(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?::\d+)?", re.I)

#: 中文点/句号包裹的域名（LINK_RE 的第三种写法），先归一成英文点再抽取。
_CN_DOT_RE = re.compile(r"(?<=[a-z0-9-])\s*[点。]\s*(?=[a-z0-9-])", re.I)

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def normalize_domain(value: str) -> str:
    """小写、去协议/路径/端口、去 www. 前缀，返回可比较的域名。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = _SCHEME_RE.sub("", text)
    for separator in ("/", "?", "#"):
        text = text.split(separator, 1)[0]
    text = text.rsplit("@", 1)[-1]  # 去掉 userinfo
    if text.startswith("["):  # IPv6 字面量：不参与域名白名单
        return ""
    if ":" in text:
        text = text.split(":", 1)[0]
    text = text.strip().strip(".")
    if text.startswith("www."):
        text = text[4:]
    return text.strip(".")


def extract_domains(text: str) -> set[str]:
    """从文本抽域名（含裸域名/短链形态），返回归一化域名集合。"""
    raw = str(text or "")
    if not raw:
        return set()
    scan = _CN_DOT_RE.sub(".", raw)
    domains: set[str] = set()
    for match in URL_HOST_RE.finditer(scan):
        domain = normalize_domain(match.group(1))
        if domain and "." in domain:
            domains.add(domain)
    return domains


#: 二维码承载文本里的引流特征（群号 / 短链 / 加群口令）
QR_GROUP_RE = re.compile(r"(?:群号|加群|进群|群聊|qq群)\D{0,4}(\d{5,12})", re.I)
QR_SHORT_LINK_RE = re.compile(
    r"(?:t\.me|t\.cn|qm\.qq\.com|dwz\.|url\.cn|sourl\.cn|jump\.|bit\.ly|suo\.im)",
    re.I,
)


def qr_risk_text(value: object) -> str:
    """二维码承载文本是否含引流特征；命中返回原因，否则返回空串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if QR_GROUP_RE.search(text):
        return "二维码含群号"
    if QR_SHORT_LINK_RE.search(text):
        return "二维码含短链"
    domains = extract_domains(text)
    if domains:
        return "二维码含链接"
    return ""


def allowlisted_domains(text: str, allowlist: Iterable[str]) -> tuple[bool, set[str]]:
    """返回 (是否「全部链接都在白名单内」, 命中的白名单域名)。

    - 无链接 → (False, set())；
    - 只要有一个非白名单域名 → (False, ...)；
    - 白名单为空 → (False, set())，调用方行为与未启用白名单完全一致。
    """
    domains = extract_domains(text)
    if not domains:
        return False, set()
    allowed = {
        domain for domain in (normalize_domain(item) for item in (allowlist or ())) if domain
    }
    if not allowed:
        return False, set()
    hits = {
        domain
        for domain in domains
        if any(domain == item or domain.endswith("." + item) for item in allowed)
    }
    return len(hits) == len(domains), hits
