# -*- coding: utf-8 -*-
"""多协议平台适配层（v2.12.0）。

在 AIOCQHTTP（OneBot / QQ）平台，插件运行「全量模式」：完整审核链路
（文本/图片/视频/转发/OCR/LLM）与全部群管指令。其余平台（Telegram、
Discord、QQ 官方协议等）在开启 ``multi_protocol_enabled`` 后以「受限
模式」运行：文本关键词审核 + 撤回 + 可选禁言 + 违规记录。图片/视频/
OCR/LLM 与任免管理员等能力依赖 OneBot 特有数据结构与 API，跨平台不可用。

受限模式下 Telegram/Discord 的群管操作（撤回/禁言/解禁/踢人/查询群角色）
由 ``platform_ops.PlatformOpsMixin`` 以 duck typing 实现平台路由；查询群
角色使「按角色分权限」（群主/群管理员豁免与 role_*_require 分级）在受限
平台同样生效。仅 AIOCQHTTP 提供完整能力。

本模块只做三件事：平台名归一、平台能力查询、启动日志。业务分发见
``moderation._handle_message_limited`` 与 ``main._multi_protocol_active``。
"""
from astrbot.api import logger

# 平台名归一：不同协议端上报的适配器名映射到内部统一名。
# 只登记已知平台；未知平台保持原名并走受限模式（仅文本审核）。
_PLATFORM_ALIASES = {
    "aiocqhttp": "aiocqhttp",
    "napcat": "aiocqhttp",
    "llonebot": "aiocqhttp",
    "onebot": "aiocqhttp",
    "qq": "qq_official",
    "qqofficial": "qq_official",
    "telegram": "telegram",
    "tg": "telegram",
    "discord": "discord",
}

# 平台能力表：full=True 表示全量（完整审核 + 全部群管操作）。
# 受限平台仅启用文本审核与尽力撤回；其余能力（图片/视频审核、禁言/踢人/
# 任免管理员等）标记为 False，对应代码路径不会在受限模式下执行。
PLATFORM_CAPABILITIES = {
    "aiocqhttp": {
        "full": True,
        "text_audit": True, "image_audit": True, "video_audit": True,
        "recall": True, "ban": True, "kick": True, "group_admin": True,
    },
    "qq_official": {
        "full": False,
        "text_audit": True, "image_audit": False, "video_audit": False,
        "recall": False, "ban": False, "kick": False, "group_admin": False,
    },
    "telegram": {
        "full": False,
        "text_audit": True, "image_audit": False, "video_audit": False,
        "recall": True, "ban": True, "kick": True, "group_admin": False,
    },
    "discord": {
        "full": False,
        "text_audit": True, "image_audit": False, "video_audit": False,
        "recall": True, "ban": True, "kick": True, "group_admin": False,
    },
}

# 受限模式默认支持平台（multi_protocol_platforms 配置缺省值）
DEFAULT_LIMITED_PLATFORMS = ("telegram", "discord", "qq_official")


def get_platform_name(event) -> str:
    """从 AstrBot 事件安全获取归一化平台名，未知返回 'unknown'。"""
    try:
        raw = str(getattr(event, "get_platform_name", lambda: "")() or "")
        raw = raw.strip().lower()
    except Exception:
        raw = ""
    return _PLATFORM_ALIASES.get(raw, raw or "unknown")


def is_aiocqhttp(platform: str) -> bool:
    return platform == "aiocqhttp"


# 有平台路由实现（platform_ops.PlatformOpsMixin）的受限平台白名单。
# 注意：判断必须是白名单式——unknown（如单测 fake event）或其它未接入
# 平台路由的平台一律走原有 OneBot call_action 逻辑，保证向后兼容。
PLATFORM_ROUTED = ("telegram", "discord")


def is_platform_routed(platform: str) -> bool:
    """该平台是否进入 PlatformOpsMixin 平台路由（Telegram / Discord）。"""
    return platform in PLATFORM_ROUTED


def platform_capabilities(platform: str) -> dict:
    """查询平台能力表。未登记平台退回受限模式（仅文本审核）。"""
    caps = PLATFORM_CAPABILITIES.get(platform)
    if caps:
        return dict(caps)
    return {
        "full": False,
        "text_audit": True, "image_audit": False, "video_audit": False,
        "recall": False, "ban": False, "kick": False, "group_admin": False,
    }


def log_startup_support() -> None:
    """启动日志：展示各平台支持情况，便于用户排查多协议问题。"""
    try:
        lines = [
            f"{name}={'全量' if caps['full'] else '受限'}"
            for name, caps in PLATFORM_CAPABILITIES.items()
        ]
        logger.info(f"[GroupMgr] 多协议支持一览: {'; '.join(lines)}")
    except Exception:
        pass
