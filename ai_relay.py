# -*- coding: utf-8 -*-
"""AI 特殊数字检测（AI Relay）共享模块。

默认关闭。仅当「全局证明 + 群内双重开关」全部满足时才生效：
全局 ``ai_relay_mode_enabled`` 为真（主开关，群覆盖无法绕过）且
:func:`ai_relay_risk_acknowledged` 为真，同时群内 ``ai_relay_mode_enabled``
与 ``ai_relay_zero_guard_enabled`` 均为真。
"""

import re

# 逐字确认短语；两个确认开关 + 该短语齐备才算「已确认」。
AI_RELAY_ACK_TEXT = "我确认启用AI特殊检测并承担误撤回风险"
AI_RELAY_CONFIRM_KEYS = (
    "ai_relay_ack_recall",
    "ai_relay_ack_false_positive",
    "ai_relay_risk_ack",
)
# ``0.`` 任意位置（10.5 / 0.0 等版本号/小数会被有意命中）或两侧非数字的 0XX / 00XX。
AI_RELAY_ZERO_GUARD_PATTERN = re.compile(
    r"(?:0\.|(?<!\d)(?:0\d{2}|00\d{2})(?!\d))"
)
_TRUE_LITERALS = frozenset(("1", "true", "yes", "on"))


def _coerce_bool(value) -> bool:
    """只接受 bool 与明确的真值字面量；其它取值（含 2、1.0、nan、空串）按关闭处理。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_LITERALS


def ai_relay_risk_acknowledged(config) -> bool:
    """两个确认开关为真且确认短语逐字一致（不裁剪）才算已确认。"""
    return (
        _coerce_bool(config.get("ai_relay_ack_recall", False))
        and _coerce_bool(config.get("ai_relay_ack_false_positive", False))
        and str(config.get("ai_relay_risk_ack", "") or "") == AI_RELAY_ACK_TEXT
    )


def ai_relay_zero_guard_hit(text) -> bool:
    """文本是否命中特殊数字模式（空/非文本返回 False）。"""
    return bool(text) and AI_RELAY_ZERO_GUARD_PATTERN.search(str(text)) is not None


def ai_relay_zero_guard_active(cfg, config, group_id: str = "") -> bool:
    """运行时门控：全局证明 + 群内双重开关全部满足才返回 True。

    ``cfg`` 为 ``self._cfg``（签名 ``(key, default, group_id=None)``，返回 bool）。
    """
    return (
        cfg("ai_relay_mode_enabled", False)
        and ai_relay_risk_acknowledged(config)
        and cfg("ai_relay_mode_enabled", False, group_id=group_id)
        and cfg("ai_relay_zero_guard_enabled", False, group_id=group_id)
    )
