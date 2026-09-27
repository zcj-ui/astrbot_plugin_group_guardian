# -*- coding: utf-8 -*-
"""链接白名单（Issue #89）。

管理员配置允许的域名后，指向这些域名的普通链接不再触发广告/违规网址等本地规则。
只把白名单链接本身从「规则初筛用的文本」里去掉，消息其余部分照常审核；
送给 LLM 的原文不做改动。

安全要点——只按「主机名」精确匹配，防止以下绕过：
- ``github.com.evil.com``：主机名是 evil 的子域，不匹配 ``github.com``；
- ``evilgithub.com``：必须等于白名单域名或以 ``.白名单域名`` 结尾；
- ``https://github.com@evil.com``：``@`` 之后才是真实主机，``evil.com`` 仍会被审核；
- ``https://evil.com/?r=github.com``：整条链接的主机是 evil.com，不会因查询参数放行。

本模块只依赖标准库，便于独立单测。
"""
import re
from typing import Iterable, Tuple

# 协议可选；主机名由若干 label + 字母顶级域组成；可带端口与路径/查询/片段。
# 路径在空白、引号、尖括号与常见中文标点处截止，避免吞掉后续正文。
_URL_RE = re.compile(
    r"(?<![\w.@-])"
    r"(?:(?:https?|ftp)://)?"
    r"(?P<host>(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})"
    r"(?::\d{1,5})?"
    r"(?:[/?#][^\s<>\"'，。！？、；：）)\]】」』]*)?",
    re.IGNORECASE,
)

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def normalize_whitelist(entries: Iterable) -> Tuple[str, ...]:
    """把配置项规整为小写主机名元组。

    接受 ``github.com`` / ``https://github.com/xx`` / ``*.github.com`` /
    ``.github.com`` / ``GitHub.com:443`` 等写法；丢弃无效项（无点号、含空白等）。
    """
    hosts = []
    for raw in entries or []:
        value = str(raw or "").strip().lower()
        if not value:
            continue
        value = _SCHEME_RE.sub("", value)
        value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        value = value.rsplit("@", 1)[-1]  # 去掉 userinfo
        value = value.split(":", 1)[0]    # 去掉端口
        value = value.lstrip("*").strip(".")
        if not value or "." not in value or any(ch.isspace() for ch in value):
            continue
        if value not in hosts:
            hosts.append(value)
    return tuple(hosts)


def host_is_whitelisted(host: str, whitelist: Tuple[str, ...]) -> bool:
    """主机名是否等于白名单域名，或是其子域名。"""
    host = str(host or "").lower().rstrip(".")
    if not host:
        return False
    for domain in whitelist:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def strip_whitelisted_links(text: str, whitelist: Tuple[str, ...]) -> str:
    """把白名单主机的链接替换为空格，其余内容原样保留。"""
    if not text or not whitelist:
        return text

    def replace(match: "re.Match") -> str:
        if host_is_whitelisted(match.group("host"), whitelist):
            return " "
        return match.group(0)

    return _URL_RE.sub(replace, text)
