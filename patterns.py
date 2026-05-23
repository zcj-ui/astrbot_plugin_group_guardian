# -*- coding: utf-8 -*-
"""政治敏感词白名单（运行时使用）。正则规则已迁移到 lexicon.db 的 moderation_rules 表。"""

_POLITICAL_WHITELIST = {
    "服务器", "管理", "管理员", "管里", "官方", "维护", "系统", "系统公告",
    "客服", "运营", "测试", "服务管理", "客户服务",
    "admin", "administrator", "system", "master",
    "client", "server", "test",
    "乱伦", "爱滋", "爱滋病", "艾滋",
    "草",
}
