# -*- coding: utf-8 -*-
import asyncio
import csv
import io
import re
import sqlite3
import time
from collections import deque
from typing import Tuple

from astrbot.api import logger

try:
    from quart import jsonify, request as quart_request
except ImportError:
    jsonify = None
    quart_request = None

from .constants import PLUGIN_NAME, PLUGIN_VERSION


class WebMixin:
    # 本插件 WebUI 面板的所有 API 接口。
    # 注册通过 main.py 的 _register_web_apis() 调用 _register_routes() 完成。
    # 每个 API handler 通过 _wrap_web_handler 包装，自动检查 Quart 可用性并做统一异常捕获。
    @staticmethod
    def _check_quart_available():
        # 检查 Quart 框架是否已安装（AstrBot 4.x+ 内置 Quart），若未安装则抛出 RuntimeError。
        if quart_request is None or jsonify is None:
            raise RuntimeError("Web框架(Quart)不可用，请检查AstrBot版本")

    def _wrap_web_handler(self, handler):
        # 为每个 Web API handler 添加 Quart 可用性检查的装饰器层。
        # 这样每个 handler 在被调用前都会先验证 Quart 是否正常，避免奇怪的 ImportError。
        async def _wrapped(*args, **kwargs):
            self._check_quart_available()
            return await handler(*args, **kwargs)
        _wrapped.__name__ = handler.__name__
        return _wrapped

    def _apply_incremental_rule_rebuild(self, category: str) -> Tuple[bool, str]:
        self._rule_count_cache = None
        try:
            self._rebuild_rule_matcher(category)
            self._schedule_background_rebuild(f"规则分类 {category} 后台校验重建")
            return True, ""
        except Exception as e:
            logger.exception("[GroupMgr] 增量重建规则失败，将转后台全量重建")
            self._schedule_background_rebuild(f"规则分类 {category} 增量重建失败，转后台全量重建")
            return False, str(e)

    def _apply_incremental_lexicon_rebuild(self, category: str) -> Tuple[bool, str]:
        """尝试立即重建词库分类，失败时调度后台全量重建。"""
        try:
            self._rebuild_lexicon_category(category)
            self._schedule_background_rebuild(f"词库分类 {category} 后台校验重建")
            return True, ""
        except Exception as e:
            logger.exception("[GroupMgr] 增量重建词库分类失败，将转后台全量重建")
            self._schedule_background_rebuild(f"词库分类 {category} 增量重建失败，转后台全量重建")
            return False, str(e)

    @staticmethod
    def _parse_bool(value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            val = value.strip().lower()
            if val in ("1", "true", "yes", "on"):
                return True
            if val in ("0", "false", "no", "off", ""):
                return False
        return default

    @staticmethod
    def _csv_safe(value) -> str:
        """CSV 公式注入防护：单元格以 = + - @ 或制表/回车开头时前置单引号，
        防止 Excel/WPS 打开导出文件时把消息内容当公式执行（如 =HYPERLINK(...)）。"""
        s = "" if value is None else str(value)
        if s and s[0] in ('=', '+', '-', '@', '\t', '\r', '\n'):
            return "'" + s
        return s

    # 嵌套量词：一个带量词的分组整体又被量词修饰，如 (a+)+ (a*)* (a+)* (\d+)+
    _REDOS_PATTERN = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*]")

    @classmethod
    def _is_redos_prone(cls, pattern: str) -> bool:
        return bool(cls._REDOS_PATTERN.search(pattern or ""))

    @staticmethod
    def _config_int_ranges():
        return {
            "moderation_ban_duration": (60, 2592000),
            "anti_flood_rate_per_second": (0, 999),
            "anti_flood_rate_per_minute": (0, 99999),
            "anti_flood_rate_per_hour": (0, 999999),
            "anti_flood_night_start_hour": (0, 23),
            "anti_flood_night_end_hour": (0, 23),
            "anti_flood_night_rate_per_second": (0, 999),
            "anti_flood_night_rate_per_minute": (0, 99999),
            "anti_flood_night_rate_per_hour": (0, 999999),
            "anti_flood_mute_duration": (0, 2592000),
            "anti_flood_recall_threshold": (1, 999999),
            "repeat_detect_window_seconds": (0, 3600),
            "repeat_detect_count": (2, 9999),
            "long_text_threshold": (0, 1000000),
            "appeal_window_minutes": (1, 10080),
            "appeal_context_count": (1, 500),
            "auto_unban_scan_interval": (10, 3600),
            "auto_unban_permanent_hours": (1, 8760),
            "combine_detect_count": (2, 20),
            "combine_detect_window_seconds": (5, 600),
            "kick_recall_count": (1, 50),
        }

    def _normalize_int_config_value(self, key: str, value) -> int:
        meta = self._config_schema.get(key, {})
        try:
            val = int(value)
        except (ValueError, TypeError):
            try:
                val = int(meta.get("default", 0) or 0)
            except (ValueError, TypeError):
                val = 0
        lo, hi = self._config_int_ranges().get(key, (None, None))
        if lo is not None:
            val = max(lo, val)
        if hi is not None:
            val = min(hi, val)
        return val

    async def _read_required_json_value(self, key: str) -> Tuple[str, object]:
        data = await quart_request.get_json(force=True, silent=True) or {}
        value = str(data.get(key, "")).strip()
        if not value:
            return "", jsonify({"status": "error", "message": f"缺少 {key}"})
        return value, None

    def _managed_list_payload(self, id_key: str, value: str, response_key: str, values: list) -> dict:
        return {"status": "success", id_key: value, response_key: values}

    async def _call_onebot_web(self, client, action: str, timeout: float = 8.0, **kwargs):
        return await asyncio.wait_for(client.call_action(action, **kwargs), timeout=timeout)

    @staticmethod
    def _format_web_error(e: Exception) -> str:
        msg = str(e).strip()
        return msg or e.__class__.__name__

    def _fallback_web_groups(self) -> list:
        group_ids = set()
        group_ids.update(str(x) for x in getattr(self, "group_white_list", []) if x)
        group_ids.update(str(x) for x in getattr(self, "group_black_list", []) if x)
        try:
            group_ids.update(str(x) for x in self._storage.list_configured_groups() if x)
        except Exception:
            pass
        for item in list(getattr(self, "_moderation_logs", [])):
            gid = str(item.get("group_id", "")).strip()
            if gid:
                group_ids.add(gid)
        today_start = self._today_start()
        today_blocked_map = {}
        for item in list(getattr(self, "_moderation_logs", [])):
            if item.get("ts", 0) >= today_start and "撤回" in item.get("action", ""):
                gid = str(item.get("group_id", "")).strip()
                if gid:
                    today_blocked_map[gid] = today_blocked_map.get(gid, 0) + 1
        return [
            {
                "group_id": gid,
                "group_name": f"群 {gid}",
                "member_count": 0,
                "avatar": f"https://p.qlogo.cn/gh/{gid}/{gid}/",
                "is_white": gid in getattr(self, "_group_white_set", set()),
                "is_black": gid in getattr(self, "_group_black_set", set()),
                "today_blocked": today_blocked_map.get(gid, 0),
            }
            for gid in sorted(group_ids)
        ]

    def _fallback_web_group_members(self, group_id: str) -> list:
        users = {}
        admin_set = set(self._get_admin_list())
        for uid in admin_set:
            uid = str(uid).strip()
            if uid:
                users[uid] = {"user_id": uid, "display_name": uid, "role": "member", "is_plugin_admin": True}
        for uid in list(getattr(self, "user_black_list", [])) + list(getattr(self, "user_white_list", [])):
            uid = str(uid).strip()
            if uid:
                users.setdefault(uid, {"user_id": uid, "display_name": uid, "role": "member", "is_plugin_admin": uid in admin_set})
        for item in list(getattr(self, "_moderation_logs", [])):
            if str(item.get("group_id", "")) != str(group_id):
                continue
            uid = str(item.get("user_id", "")).strip()
            if not uid:
                continue
            name = str(item.get("user_name", "") or uid)
            users[uid] = {"user_id": uid, "display_name": name, "role": "member", "is_plugin_admin": uid in admin_set}
        enriched = []
        for uid, item in users.items():
            enriched.append({
                "user_id": uid,
                "nickname": item.get("display_name", uid),
                "card": "",
                "display_name": item.get("display_name", uid),
                "role": item.get("role", "member"),
                "title": "",
                "avatar": f"https://q.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640",
                "is_plugin_admin": bool(item.get("is_plugin_admin")),
            })
        enriched.sort(key=lambda x: (0 if x["is_plugin_admin"] else 1, x["display_name"]))
        return enriched

    def _register_web_apis(self):
        # 遍历路由表，每项含 path / handler / methods / desc，统一注册到 self.context.register_web_api。
        # 认证说明：register_web_api 注册的路由挂载在 AstrBot Dashboard 的 /api/plug/ 下，
        # 由 AstrBot 框架统一执行 Dashboard JWT 登录校验，未登录请求无法到达这些 handler。
        # 因此插件层不重复实现认证；请勿将 AstrBot Dashboard 端口直接暴露公网或关闭其登录验证。
        try:
            routes = [
                ("/stats", self._web_stats, ["GET"], "获取群管统计信息"),
                ("/config", self._web_get_config, ["GET"], "获取当前配置"),
                ("/config", self._web_update_config, ["POST"], "更新配置"),
                ("/providers", self._web_get_providers, ["GET"], "获取可用LLM Provider列表"),
                ("/lexicon", self._web_get_lexicon, ["GET"], "获取外置词库内容"),
                ("/lexicon/categories", self._web_get_lexicon_categories, ["GET"], "获取词库分类统计"),
                ("/lexicon/keywords", self._web_get_lexicon_keywords, ["GET"], "分页获取分类关键词"),
                ("/lexicon/keywords/add", self._web_add_lexicon_keyword, ["POST"], "添加词库关键词"),
                ("/lexicon/keywords/add_batch", self._web_add_lexicon_keywords_batch, ["POST"], "批量添加词库关键词"),
                ("/lexicon/keywords/update", self._web_update_lexicon_keyword, ["POST"], "编辑词库关键词"),
                ("/lexicon/keywords/delete", self._web_delete_lexicon_keyword, ["POST"], "删除词库关键词"),
                ("/lexicon/keywords/delete_batch", self._web_delete_lexicon_keywords_batch, ["POST"], "批量删除词库关键词"),
                ("/lexicon/keywords/export", self._web_export_lexicon_keywords, ["GET"], "导出词库关键词"),
                ("/rules", self._web_get_rules, ["GET"], "分页获取审核规则"),
                ("/rules/save", self._web_save_rule, ["POST"], "新增或更新审核规则"),
                ("/rules/delete", self._web_delete_rule, ["POST"], "删除审核规则"),
                ("/rules/delete_batch", self._web_delete_rules_batch, ["POST"], "批量删除审核规则"),
                ("/rules/toggle", self._web_toggle_rule, ["POST"], "启停审核规则"),
                ("/rules/toggle_batch", self._web_toggle_rules_batch, ["POST"], "批量启停审核规则"),
                ("/rules/rebuild_status", self._web_rebuild_status, ["GET"], "获取热更新重建状态"),
                ("/logs", self._web_get_logs, ["GET"], "获取最近审核日志"),
                ("/moderation_users", self._web_get_moderation_users, ["GET"], "获取被撤回用户聚合列表"),
                ("/logs/delete", self._web_delete_logs, ["POST"], "批量删除审核日志"),
                ("/logs/export", self._web_export_logs, ["GET"], "导出审核日志"),
                ("/log_detail", self._web_log_detail, ["GET"], "获取单条日志详情"),
                ("/log_chunk", self._web_log_chunk, ["GET"], "获取日志文本分片"),
                ("/log_raw_text", self._web_log_raw_text, ["GET"], "获取日志原始文本"),
                ("/groups", self._web_get_groups, ["GET"], "获取群列表"),
                ("/group_members", self._web_get_group_members, ["GET"], "获取群成员列表"),
                ("/whitelist/add", self._web_whitelist_add, ["POST"], "添加群白名单"),
                ("/whitelist/remove", self._web_whitelist_remove, ["POST"], "移除群白名单"),
                ("/blacklist/add", self._web_blacklist_add, ["POST"], "添加群黑名单"),
                ("/blacklist/remove", self._web_blacklist_remove, ["POST"], "移除群黑名单"),
                ("/user_blacklist/add", self._web_user_blacklist_add, ["POST"], "添加用户黑名单"),
                ("/user_blacklist/remove", self._web_user_blacklist_remove, ["POST"], "移除用户黑名单"),
                ("/user_whitelist/add", self._web_user_whitelist_add, ["POST"], "添加审核白名单用户"),
                ("/user_whitelist/remove", self._web_user_whitelist_remove, ["POST"], "移除审核白名单用户"),
                ("/admin/add", self._web_admin_add, ["POST"], "添加管理员"),
                ("/admin/remove", self._web_admin_remove, ["POST"], "移除管理员"),
                ("/today_stats", self._web_today_stats, ["GET"], "获取今日拦截统计"),
                ("/migration/status", self._web_migration_status, ["GET"], "获取SQLite迁移状态"),
                ("/migration/run", self._web_migration_run, ["POST"], "执行SQLite迁移"),
                ("/dashboard/trend", self._web_dashboard_trend, ["GET"], "获取每日拦截趋势数据"),
                ("/dashboard/distribution", self._web_dashboard_distribution, ["GET"], "获取违规类型分布"),
                ("/dashboard/hourly", self._web_dashboard_hourly, ["GET"], "获取时段分布"),
                ("/dashboard/group_ranking", self._web_dashboard_group_ranking, ["GET"], "获取历史群拦截排行"),
                ("/anti_flood/status", self._web_anti_flood_status, ["GET"], "获取防刷屏追踪状态"),
                ("/join_rules", self._web_get_join_rules, ["GET"], "获取入群审核规则列表"),
                ("/join_rules/save", self._web_save_join_rule, ["POST"], "保存入群审核规则"),
                ("/join_rules/delete", self._web_delete_join_rule, ["POST"], "删除入群审核规则"),
                ("/scheduled_unbans", self._web_get_scheduled_unbans, ["GET"], "获取定时解禁计划"),
                ("/scheduled_unbans/delete", self._web_delete_scheduled_unban, ["POST"], "取消定时解禁计划"),
                ("/appeals", self._web_get_appeals, ["GET"], "获取申诉记录"),
                ("/admin_grant", self._web_get_admin_grants, ["GET"], "获取群管理员授权配置"),
                ("/admin_grant/save", self._web_save_admin_grant, ["POST"], "保存群管理员授权配置"),
                ("/admin_grant/delete", self._web_delete_admin_grant, ["POST"], "删除群管理员授权配置"),
                ("/remote/actions", self._web_remote_actions, ["GET"], "获取可远程执行的操作列表"),
                ("/remote/execute", self._web_remote_execute, ["POST"], "远程执行群管操作（支持批量）"),
                ("/super_admin", self._web_get_super_admins, ["GET"], "获取群超管列表"),
                ("/super_admin/add", self._web_add_super_admin, ["POST"], "添加群超管"),
                ("/super_admin/remove", self._web_remove_super_admin, ["POST"], "移除群超管"),
                ("/admin_block", self._web_get_admin_blocks, ["GET"], "获取群权限黑名单"),
                ("/admin_block/add", self._web_add_admin_block, ["POST"], "移除某群管的bot权限"),
                ("/admin_block/remove", self._web_remove_admin_block, ["POST"], "恢复某群管的bot权限"),
                ("/group_config", self._web_get_group_config, ["GET"], "获取某群的独立配置"),
                ("/group_config/set", self._web_set_group_config, ["POST"], "设置某群某配置项"),
                ("/group_config/delete", self._web_delete_group_config, ["POST"], "删除某群某配置项（恢复继承）"),
                ("/group_config/clear", self._web_clear_group_config, ["POST"], "清空某群全部独立配置"),
                ("/configured_groups", self._web_configured_groups, ["GET"], "列出有独立配置的群"),
                ("/group_config/batch_set", self._web_batch_set_group_config, ["POST"], "批量设置某群配置"),
                ("/group_config/copy", self._web_copy_group_config, ["POST"], "从其他群复制配置"),
                ("/card_monitor/records", self._web_card_records, ["GET"], "获取名片变更/管理员任免记录"),
                ("/card_monitor/records/clear", self._web_card_records_clear, ["POST"], "清空名片记录"),
                ("/card_monitor/config", self._web_card_config_get, ["GET"], "获取名片监控开关"),
                ("/card_monitor/config/set", self._web_card_config_set, ["POST"], "设置名片监控开关"),
                ("/card_monitor/protected", self._web_card_protected_list, ["GET"], "获取名片保护名单"),
                ("/card_monitor/protected/add", self._web_card_protected_add, ["POST"], "添加名片保护成员"),
                ("/card_monitor/protected/remove", self._web_card_protected_remove, ["POST"], "移除名片保护成员"),
            ]
            for path, handler, methods, desc in routes:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}{path}",
                    self._wrap_web_handler(handler),
                    methods,
                    desc
                )
            logger.info("[GroupMgr] WebUI API 已注册")
        except Exception as e:
            logger.warning(f"[GroupMgr] 注册 WebUI API 失败: {e}")

    async def _web_stats(self):
        today_start = self._today_start()
        sc = self._stats_cache
        if sc["today_start"] == today_start:
            today_blocked = sc["blocked"]
            today_passed = sc["passed"]
            today_total = sc["total"]
        else:
            today_blocked = 0
            today_passed = 0
            today_total = 0
            for l in list(self._moderation_logs):
                if l.get("ts", 0) >= today_start:
                    today_total += 1
                    action = l.get("action", "")
                    if "撤回" in action:
                        today_blocked += 1
                    elif "放行" in action:
                        today_passed += 1
            sc.update(today_start=today_start, blocked=today_blocked, passed=today_passed, total=today_total)
        rc = getattr(self, "_rule_count_cache", None)
        now = time.time()
        if not rc or now - rc.get("ts", 0) > 30:
            rc = {
                "ts": now,
                "swear": self._storage.count_moderation_rules_filtered("swear", 1),
                "ad": self._storage.count_moderation_rules_filtered("ad", 1),
            }
            self._rule_count_cache = rc
        swear_count = rc["swear"]
        ad_count = rc["ad"]
        stats = {
            "plugin_name": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "disclaimer_agreed": self.config.get("disclaimer_agreed", False),
            "auto_moderate_enabled": self.auto_moderate_enabled,
            "group_white_list_count": len(self.group_white_list),
            "group_black_list_count": len(self.group_black_list),
            "user_black_list_count": len(self.user_black_list),
            "user_white_list_count": len(self.user_white_list),
            "admin_list_count": len(self._get_admin_list()),
            "swear_patterns_count": swear_count,
            "ad_patterns_count": ad_count,
            "lexicon_categories_count": len(self._lexicon),
            "lexicon_total_keywords": sum(
                len(cat.get("keywords", [])) for cat in self._lexicon.values()
            ),
            "total_logs": self._storage.count_logs(),
            "today_total": today_total,
            "today_blocked": today_blocked,
            "today_passed": today_passed,
            "configured_groups_count": len(self._storage.list_configured_groups()),
            "super_admin_count": len(self._storage.list_group_super_admins()),
        }
        return jsonify({"status": "success", "data": stats})

    async def _web_get_providers(self):
        # 获取 AstrBot 中所有已注册的 LLM Provider 列表，返回 id/name/model 供 WebUI 下拉选择。
        providers = []
        try:
            ps = (self.context.get_all_providers() if hasattr(self.context, 'get_all_providers') else []) or []
            for p in ps:
                try:
                    meta = p.meta() if hasattr(p, 'meta') else None
                    pid = getattr(meta, 'id', '') if meta else ''
                    pname = getattr(meta, 'model', '') or pid
                    providers.append({"id": pid, "name": pname, "model": getattr(meta, 'model', '')})
                except Exception:
                    continue
        except Exception as _e:
            logger.debug(f"[GroupMgr] 获取Provider列表失败: {_e}")
        return jsonify(providers)

    async def _web_get_config(self):
        safe_config = {}
        for k in self._config_schema:
            if k in self.config:
                safe_config[k] = self.config[k]
            else:
                safe_config[k] = self._config_schema[k].get("default")
        safe_config["_schema"] = self._config_schema
        safe_config["_white_list"] = self.group_white_list
        safe_config["_black_list"] = self.group_black_list
        safe_config["_user_black_list"] = self.user_black_list
        safe_config["_user_white_list"] = self.user_white_list
        safe_config["_admin_list"] = self._get_admin_list()
        return jsonify(safe_config)

    async def _web_update_config(self):
        # 接收 POST JSON 批量更新配置项，根据 _conf_schema.json 的 type 字段做类型校验。
        # 特殊处理：int 类型有范围限制、list 类型自动同步 set 属性、lexicon_* 变更重编译词库正则。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            schema = self._config_schema
            old_config = {k: self.config.get(k) for k in data if k.startswith("lexicon_")}
            old_enabled = self.config.get("anti_flood_enabled", True)
            # 单群管理类名单（群白/群黑/用户黑/用户白/管理员）v2.4.0 起改由专用 API + DB 管理，
            # 配置保存接口跳过它们，避免 config 与 DB 双写不一致。
            db_managed_keys = {"group_white_list", "group_black_list", "user_black_list", "user_white_list", "admin_list"}
            updated = []
            for key, value in data.items():
                if key not in schema:
                    continue
                if key in db_managed_keys:
                    continue
                field_type = schema[key].get("type", "")
                if field_type == "bool":
                    default_bool = bool(schema[key].get("default", False))
                    self.config[key] = self._parse_bool(value, default_bool)
                    updated.append(key)
                elif field_type == "list":
                    val = value
                    if isinstance(val, str):
                        val = [x.strip() for x in val.replace("，", ",").split(",") if x.strip()]
                    if isinstance(val, list):
                        self.config[key] = [str(x).strip() for x in val if x]
                        updated.append(key)
                elif field_type == "int":
                    self.config[key] = self._normalize_int_config_value(key, value)
                    updated.append(key)
                elif field_type in ("string", "text"):
                    str_value = str(value)
                    options = schema[key].get("options") or []
                    if options and str_value not in [str(x) for x in options]:
                        continue
                    self.config[key] = str_value
                    updated.append(key)
            if "auto_moderate_enabled" in updated:
                self.auto_moderate_enabled = self._parse_bool(self.config.get("auto_moderate_enabled", True), True)
            # 仅在 lexicon_* 开关值实际变更时按分类增量重建，并后台做一次全量校验重建
            lexicon_updated = [k for k in updated if k.startswith("lexicon_")]
            changed_lexicon = [k for k in lexicon_updated if str(old_config.get(k, "")) != str(self.config.get(k, ""))]
            if changed_lexicon:
                lexicon_key_map = {
                    "lexicon_political_enabled": "political",
                    "lexicon_porn_enabled": "porn",
                    "lexicon_violent_enabled": "violent_terror",
                    "lexicon_reactionary_enabled": "reactionary",
                    "lexicon_weapons_enabled": "weapons",
                    "lexicon_corruption_enabled": "corruption",
                    "lexicon_illegal_url_enabled": "illegal_url",
                    "lexicon_other_enabled": "other",
                }
                for key in changed_lexicon:
                    category = lexicon_key_map.get(key)
                    if category:
                        self._apply_incremental_lexicon_rebuild(category)
                # other 开关还影响 supplement/livelihood/tencent_ban
                if "lexicon_other_enabled" in changed_lexicon:
                    for extra_category in ("supplement", "livelihood", "tencent_ban"):
                        self._apply_incremental_lexicon_rebuild(extra_category)
            # 自定义违禁词变更时重建对应匹配器
            if "custom_swear_keywords" in updated:
                self._rebuild_rule_matcher("swear")
                self._rule_count_cache = None
            if "custom_ad_keywords" in updated:
                self._rebuild_rule_matcher("ad")
                self._rule_count_cache = None
            # 防刷屏总开关关闭时清空追踪缓冲区
            if "anti_flood_enabled" in updated and old_enabled and not self._cfg("anti_flood_enabled", True):
                self._anti_flood_data.clear()
            if "enabled" in updated:
                self.config["enabled"] = self._parse_bool(self.config.get("enabled", True), True)
            if updated:
                self._save_config_safe()
                # 全局值变化会影响未覆盖群的实际生效值，清空群配置缓存保证一致（扫描#38 S6）
                self._invalidate_group_cfg_cache()
            return jsonify({"status": "success", "updated": updated})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_lexicon(self):
        # 默认返回分类摘要，避免一次性传输 6 万级词库；full=1 时才返回完整词库。
        full = str(quart_request.args.get("full", "")).strip().lower() in ("1", "true", "yes")
        if full:
            return jsonify({"status": "success", "data": self._lexicon})
        return jsonify({"status": "success", "data": self._storage.list_lexicon_categories()})

    async def _web_get_lexicon_categories(self):
        try:
            return jsonify({"status": "success", "data": self._storage.list_lexicon_categories()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_lexicon_keywords(self):
        try:
            category = str(quart_request.args.get("category", "")).strip()
            if not category:
                return jsonify({"status": "error", "message": "缺少分类"})
            query = str(quart_request.args.get("q", "")).strip()
            page = max(1, self._safe_int(quart_request.args.get("page", 1), 1))
            page_size = min(200, max(1, self._safe_int(quart_request.args.get("page_size", 100), 100)))
            offset = (page - 1) * page_size
            items = self._storage.list_lexicon_keywords(category, query, page_size, offset)
            total = self._storage.count_lexicon_keywords_filtered(category, query)
            return jsonify({"status": "success", "data": {"items": items, "total": total, "page": page, "page_size": page_size}})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_add_lexicon_keyword(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            category = str(data.get("category", "")).strip()
            keyword = str(data.get("keyword", "")).strip()
            if not category or not keyword:
                return jsonify({"status": "error", "message": "缺少分类或关键词"})
            inserted = self._storage.add_lexicon_keyword(category, keyword)
            if not inserted:
                return jsonify({"status": "error", "message": "关键词已存在"})
            rebuilt, rebuild_err = self._apply_incremental_lexicon_rebuild(category)
            return jsonify({"status": "success", "data": {"category": category, "keyword": keyword, "rebuilt": rebuilt, "deferred": not rebuilt, "message": "已新增并生效" if rebuilt else f"已新增，后台重建中：{rebuild_err}"}})
        except ValueError as e:
            return jsonify({"status": "error", "message": str(e)})
        except Exception as e:
            logger.exception("[GroupMgr] 新增关键词失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_add_lexicon_keywords_batch(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            category = str(data.get("category", "")).strip()
            raw = str(data.get("keywords", "")).strip()
            if not category or not raw:
                return jsonify({"status": "error", "message": "缺少分类或关键词内容"})
            parsed = [x.strip() for x in re.split(r"[\r\n,，]+", raw) if x.strip()]
            if not parsed:
                return jsonify({"status": "error", "message": "未解析到有效关键词"})
            unique_keywords = []
            seen = set()
            duplicate_input_samples = []
            for kw in parsed:
                if kw in seen:
                    if len(duplicate_input_samples) < 10 and kw not in duplicate_input_samples:
                        duplicate_input_samples.append(kw)
                    continue
                seen.add(kw)
                unique_keywords.append(kw)
            duplicate_in_input = len(parsed) - len(unique_keywords)
            existing_keywords = self._storage.list_existing_lexicon_keywords(category, unique_keywords)
            existing_set = set(existing_keywords)
            to_add = [kw for kw in unique_keywords if kw not in existing_set]
            added = self._storage.add_lexicon_keywords(category, to_add) if to_add else 0
            duplicate_existing = len(unique_keywords) - len(to_add)
            duplicate_total = duplicate_in_input + duplicate_existing
            duplicate_samples = list(duplicate_input_samples)
            for kw in existing_keywords:
                if len(duplicate_samples) >= 10:
                    break
                if kw not in duplicate_samples:
                    duplicate_samples.append(kw)
            rebuilt = True
            rebuild_err = ""
            if added > 0:
                rebuilt, rebuild_err = self._apply_incremental_lexicon_rebuild(category)
            msg = "批量新增已生效" if added > 0 and rebuilt else "未新增新关键词，输入内容全部重复"
            if added > 0 and not rebuilt:
                msg = f"已批量新增，后台重建中：{rebuild_err}"
            return jsonify({
                "status": "success",
                "data": {
                    "category": category,
                    "input_total": len(parsed),
                    "parsed_unique": len(unique_keywords),
                    "added": added,
                    "duplicate_in_input": duplicate_in_input,
                    "duplicate_existing": duplicate_existing,
                    "duplicate_total": duplicate_total,
                    "duplicate_samples": duplicate_samples,
                    "rebuilt": rebuilt,
                    "deferred": not rebuilt,
                    "message": msg,
                },
            })
        except Exception as e:
            logger.exception("[GroupMgr] 批量新增关键词失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_update_lexicon_keyword(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            keyword_id = self._safe_int(data.get("id", 0), 0)
            category = str(data.get("category", "")).strip()
            keyword = str(data.get("keyword", "")).strip()
            if keyword_id <= 0 or not category or not keyword:
                return jsonify({"status": "error", "message": "缺少关键词ID、分类或关键词内容"})
            ok = self._storage.update_lexicon_keyword(keyword_id, category, keyword)
            if not ok:
                return jsonify({"status": "error", "message": "未找到关键词"})
            rebuilt, rebuild_err = self._apply_incremental_lexicon_rebuild(category)
            return jsonify({"status": "success", "data": {"id": keyword_id, "category": category, "keyword": keyword, "rebuilt": rebuilt, "deferred": not rebuilt, "message": "关键词已更新并生效" if rebuilt else f"关键词已更新，后台重建中：{rebuild_err}"}})
        except sqlite3.IntegrityError:
            return jsonify({"status": "error", "message": "关键词已存在"})
        except Exception as e:
            logger.exception("[GroupMgr] 编辑关键词失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_lexicon_keyword(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            keyword_id = self._safe_int(data.get("id", 0), 0)
            category = str(data.get("category", "")).strip()
            if keyword_id <= 0 or not category:
                return jsonify({"status": "error", "message": "缺少关键词ID或分类"})
            ok = self._storage.delete_lexicon_keyword(keyword_id)
            if not ok:
                return jsonify({"status": "error", "message": "未找到关键词"})
            rebuilt, rebuild_err = self._apply_incremental_lexicon_rebuild(category)
            return jsonify({"status": "success", "data": {"id": keyword_id, "category": category, "rebuilt": rebuilt, "deferred": not rebuilt, "message": "已删除并生效" if rebuilt else f"已删除，后台重建中：{rebuild_err}"}})
        except ValueError as e:
            return jsonify({"status": "error", "message": str(e)})
        except Exception as e:
            logger.exception("[GroupMgr] 删除关键词失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_lexicon_keywords_batch(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            ids = data.get("ids", []) or []
            category = str(data.get("category", "")).strip()
            if not category or not isinstance(ids, list):
                return jsonify({"status": "error", "message": "缺少分类或 IDs 无效"})
            deleted = self._storage.delete_lexicon_keywords(ids)
            if deleted <= 0:
                return jsonify({"status": "error", "message": "未删除任何关键词"})
            rebuilt, rebuild_err = self._apply_incremental_lexicon_rebuild(category)
            return jsonify({"status": "success", "data": {"deleted": deleted, "category": category, "rebuilt": rebuilt, "deferred": not rebuilt, "message": "批量删除已生效" if rebuilt else f"已批量删除，后台重建中：{rebuild_err}"}})
        except Exception as e:
            logger.exception("[GroupMgr] 批量删除关键词失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_export_lexicon_keywords(self):
        try:
            category = str(quart_request.args.get("category", "")).strip()
            query = str(quart_request.args.get("q", "")).strip()
            if not category:
                return jsonify({"status": "error", "message": "缺少分类"})
            items = self._storage.list_lexicon_keywords(category, query, 100000, 0)
            output = io.StringIO()
            # 写入 UTF-8 BOM，确保 Excel 打开 CSV 时正确识别中文编码
            output.write("\ufeff")
            writer = csv.writer(output)
            writer.writerow(["id", "category", "keyword"])
            for item in items:
                writer.writerow([self._csv_safe(item.get("id")), self._csv_safe(category), self._csv_safe(item.get("keyword"))])
            safe_cat = re.sub(r'[^\w\-]', '_', category)
            filename = f"lexicon_{safe_cat}.csv"
            # 返回 (body, status, headers) 元组让 Quart 直接触发下载，避免依赖未导入的 send_file
            return output.getvalue(), 200, {
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": f"attachment; filename={filename}",
            }
        except Exception as e:
            logger.exception("[GroupMgr] 导出关键词失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_rules(self):
        try:
            category = str(quart_request.args.get("category", "")).strip()
            query = str(quart_request.args.get("q", "")).strip()
            enabled_raw = str(quart_request.args.get("enabled", "")).strip().lower()
            enabled = None
            if enabled_raw in ("0", "1"):
                enabled = int(enabled_raw)
            page = max(1, self._safe_int(quart_request.args.get("page", 1), 1))
            page_size = min(200, max(1, self._safe_int(quart_request.args.get("page_size", 50), 50)))
            offset = (page - 1) * page_size
            items = self._storage.list_moderation_rules(category, enabled, query, page_size, offset)
            total = self._storage.count_moderation_rules_filtered(category, enabled, query)
            return jsonify({"status": "success", "data": {"items": items, "total": total, "page": page, "page_size": page_size}})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_save_rule(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            rule_id = self._safe_int(data.get("id", 0), 0)
            category = str(data.get("category", "")).strip().lower()
            pattern = str(data.get("pattern", "")).strip()
            description = str(data.get("description", "")).strip()
            enabled = self._parse_bool(data.get("enabled", True), True)
            if category not in ("swear", "ad"):
                return jsonify({"status": "error", "message": "规则分类仅支持 swear / ad"})
            if not pattern:
                return jsonify({"status": "error", "message": "规则内容不能为空"})
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                return jsonify({"status": "error", "message": f"正则无效: {e}"})
            # 拒绝嵌套量词等易导致灾难性回溯(ReDoS)的结构，防止匹配时阻塞事件循环
            if self._is_redos_prone(pattern):
                return jsonify({"status": "error", "message": "正则包含嵌套量词等高风险结构（可能导致卡死），已拒绝。如需纯文本匹配请直接填写字面量"})
            saved_id = self._storage.save_moderation_rule(category, pattern, description, enabled, rule_id)
            if rule_id > 0 and saved_id <= 0:
                return jsonify({"status": "error", "message": "未找到规则"})
            rebuilt, rebuild_err = self._apply_incremental_rule_rebuild(category)
            return jsonify({"status": "success", "data": {"id": saved_id, "category": category, "rebuilt": rebuilt, "deferred": not rebuilt, "message": "已保存并生效" if rebuilt else f"已保存，后台重建中：{rebuild_err}"}})
        except sqlite3.IntegrityError:
            return jsonify({"status": "error", "message": "规则已存在"})
        except ValueError as e:
            return jsonify({"status": "error", "message": str(e)})
        except Exception as e:
            logger.exception("[GroupMgr] 保存规则失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_rule(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            rule_id = self._safe_int(data.get("id", 0), 0)
            category = str(data.get("category", "")).strip().lower()
            if rule_id <= 0 or category not in ("swear", "ad"):
                return jsonify({"status": "error", "message": "缺少规则ID或分类无效"})
            ok = self._storage.delete_moderation_rule(rule_id)
            if not ok:
                return jsonify({"status": "error", "message": "未找到规则"})
            rebuilt, rebuild_err = self._apply_incremental_rule_rebuild(category)
            return jsonify({"status": "success", "data": {"id": rule_id, "category": category, "rebuilt": rebuilt, "deferred": not rebuilt, "message": "已删除并生效" if rebuilt else f"已删除，后台重建中：{rebuild_err}"}})
        except ValueError as e:
            return jsonify({"status": "error", "message": str(e)})
        except Exception as e:
            logger.exception("[GroupMgr] 删除规则失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_rules_batch(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            ids = data.get("ids", []) or []
            category = str(data.get("category", "")).strip().lower()
            if category not in ("swear", "ad") or not isinstance(ids, list):
                return jsonify({"status": "error", "message": "缺少分类或 IDs 无效"})
            deleted = self._storage.delete_moderation_rules(ids)
            if deleted <= 0:
                return jsonify({"status": "error", "message": "未删除任何规则"})
            rebuilt, rebuild_err = self._apply_incremental_rule_rebuild(category)
            return jsonify({"status": "success", "data": {"deleted": deleted, "category": category, "rebuilt": rebuilt, "deferred": not rebuilt, "message": "批量删除已生效" if rebuilt else f"已批量删除，后台重建中：{rebuild_err}"}})
        except Exception as e:
            logger.exception("[GroupMgr] 批量删除规则失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_toggle_rule(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            rule_id = self._safe_int(data.get("id", 0), 0)
            category = str(data.get("category", "")).strip().lower()
            enabled = self._parse_bool(data.get("enabled", True), True)
            if rule_id <= 0 or category not in ("swear", "ad"):
                return jsonify({"status": "error", "message": "缺少规则ID或分类无效"})
            ok = self._storage.toggle_moderation_rule(rule_id, enabled)
            if not ok:
                return jsonify({"status": "error", "message": "未找到规则"})
            rebuilt, rebuild_err = self._apply_incremental_rule_rebuild(category)
            return jsonify({"status": "success", "data": {"id": rule_id, "enabled": enabled, "category": category, "rebuilt": rebuilt, "deferred": not rebuilt, "message": "状态已更新并生效" if rebuilt else f"状态已更新，后台重建中：{rebuild_err}"}})
        except ValueError as e:
            return jsonify({"status": "error", "message": str(e)})
        except Exception as e:
            logger.exception("[GroupMgr] 切换规则状态失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_toggle_rules_batch(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            ids = data.get("ids", []) or []
            category = str(data.get("category", "")).strip().lower()
            enabled = self._parse_bool(data.get("enabled", True), True)
            if category not in ("swear", "ad") or not isinstance(ids, list):
                return jsonify({"status": "error", "message": "缺少分类或 IDs 无效"})
            changed = self._storage.toggle_moderation_rules(ids, enabled)
            if changed <= 0:
                return jsonify({"status": "error", "message": "未更新任何规则"})
            rebuilt, rebuild_err = self._apply_incremental_rule_rebuild(category)
            return jsonify({"status": "success", "data": {"updated": changed, "category": category, "enabled": enabled, "rebuilt": rebuilt, "deferred": not rebuilt, "message": "批量状态更新已生效" if rebuilt else f"已批量更新，后台重建中：{rebuild_err}"}})
        except Exception as e:
            logger.exception("[GroupMgr] 批量切换规则状态失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_rebuild_status(self):
        try:
            return jsonify({"status": "success", "data": self._rebuild_status})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_logs(self):
        try:
            limit = min(int(quart_request.args.get("limit", 50)), 200)
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = max(0, self._safe_int(quart_request.args.get("offset", 0), 0))
        except (ValueError, TypeError):
            offset = 0
        group_id = quart_request.args.get("group_id", "").strip()
        user_id = quart_request.args.get("user_id", "").strip()
        action = quart_request.args.get("action", "").strip()
        logs = self._storage.list_logs(limit=limit, offset=offset,
                                       group_id=group_id, user_id=user_id, action=action)
        total = self._storage.count_logs_filtered(group_id=group_id, user_id=user_id, action=action)
        return jsonify({"status": "success", "data": logs, "total": total, "limit": limit, "offset": offset})

    def _get_log_by_id(self, target_id: int):
        # 辅助方法：先查 SQLite，找不到再回退到内存缓存 _moderation_logs。
        log = self._storage.get_log(target_id)
        if log:
            return log
        for item in self._moderation_logs:
            if item.get("id") == target_id:
                return item
        return None

    async def _web_log_detail(self):
        # 获取单条日志的元信息（总长度、分片数、图片URL、原因、操作），不返回正文以节省带宽。
        # 正文通过 /log_chunk 和 /log_raw_text 按需分片加载。
        try:
            log_id = quart_request.args.get("id", "").strip()
            if not log_id:
                return jsonify({"status": "error", "message": "缺少日志ID"})
            try:
                target_id = int(log_id)
            except (ValueError, TypeError):
                return jsonify({"status": "error", "message": "无效的日志ID"})
            log = self._get_log_by_id(target_id)
            if log:
                msg = log.get("msg_text", "")
                chunk_size = 400
                chunk_count = (len(msg) + chunk_size - 1) // chunk_size if msg else 0
                logger.debug(f"[GroupMgr] log_detail id={target_id} msg_len={len(msg)} chunk_count={chunk_count}")
                return jsonify({
                    "status": "success",
                    "data": {
                        "total_len": len(msg),
                        "chunk_count": chunk_count,
                        "image_urls": log.get("image_urls", []),
                        "reason": log.get("reason", ""),
                        "action": log.get("action", ""),
                    }
                })
            return jsonify({"status": "error", "message": "未找到该日志"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_log_chunk(self):
        # 按分片索引（chunk 参数）返回日志正文的 400 字符片段，用于长消息的分页展示。
        try:
            log_id = quart_request.args.get("id", "").strip()
            chunk_idx = quart_request.args.get("chunk", "0").strip()
            if not log_id:
                return jsonify({"status": "error", "message": "缺少日志ID"})
            try:
                target_id = int(log_id)
            except (ValueError, TypeError):
                return jsonify({"status": "error", "message": "无效的日志ID"})
            try:
                idx = int(chunk_idx)
            except (ValueError, TypeError):
                idx = 0
            log = self._get_log_by_id(target_id)
            if log:
                msg = log.get("msg_text", "")
                chunk_size = 400
                start = idx * chunk_size
                piece = msg[start:start + chunk_size]
                return jsonify({"status": "success", "data": {"i": idx, "t": piece}})
            return jsonify({"status": "error", "message": "未找到该日志"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_log_raw_text(self):
        # 以纯文本形式返回日志完整正文（text/plain）。
        # 不再设置 Access-Control-Allow-Origin:*（含用户消息原文，避免恶意页面跨域读取），
        # 同源的 Dashboard 前端 fetch 不受影响。
        _cors = {"Content-Type": "text/plain; charset=utf-8"}
        try:
            log_id = quart_request.args.get("id", "").strip()
            if not log_id:
                return "缺少日志ID", 400, _cors
            try:
                target_id = int(log_id)
            except (ValueError, TypeError):
                return "无效的日志ID", 400, _cors
            log = self._get_log_by_id(target_id)
            if log:
                raw = log.get("msg_text", "")
                logger.debug(f"[GroupMgr] log_raw_text id={target_id} len={len(raw)}")
                return raw, 200, _cors
            return "未找到该日志", 404, _cors
        except Exception as e:
            return str(e), 500, _cors

    async def _web_get_moderation_users(self):
        logs = self._storage.list_logs(limit=5000)
        action_filter = quart_request.args.get("action", "").strip()
        group_filter = quart_request.args.get("group_id", "").strip()
        user_map = {}
        for log in logs:
            if action_filter and action_filter not in log.get("action", ""):
                continue
            if group_filter and log.get("group_id", "") != group_filter:
                continue
            uid = log.get("user_id", "")
            if not uid:
                continue
            if uid not in user_map:
                user_map[uid] = {
                    "user_id": uid,
                    "user_name": log.get("user_name", ""),
                    "group_id": log.get("group_id", ""),
                    "count": 0,
                    "first_time": log.get("time", ""),
                    "last_time": log.get("time", ""),
                    "groups": set(),
                    "records": [],
                }
            u = user_map[uid]
            u["count"] += 1
            u["last_time"] = log.get("time", "")
            gid = log.get("group_id", "")
            if gid:
                u["groups"].add(gid)
            if len(u["records"]) < 50:
                u["records"].append({
                    "id": log.get("id"),
                    "time": log.get("time", ""),
                    "ts": log.get("ts", 0),
                    "group_id": gid,
                    "msg_preview": log.get("msg_preview", ""),
                    "action": log.get("action", ""),
                    "reason": log.get("reason", ""),
                })
        for u in user_map.values():
            u["group_count"] = len(u["groups"])
            u["groups"] = sorted(u["groups"])
        users = sorted(user_map.values(), key=lambda x: x["count"], reverse=True)
        return jsonify({"status": "success", "data": users, "total": len(users)})

    async def _web_delete_logs(self):
        # 批量删除审核日志：支持按 id 列表删除或 delete_all=True 清空全部。
        # 删除后同步更新内存缓存和统计缓存。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            ids = data.get("ids", [])
            user_ids = data.get("user_ids", [])
            if isinstance(user_ids, str):
                user_ids = [x.strip() for x in re.split(r"[,，\s]+", user_ids) if x.strip()]
            delete_all = data.get("delete_all", False)
            if delete_all:
                count = self._storage.delete_all_logs()
                self._moderation_logs.clear()
                self._invalidate_stats_cache()
                return jsonify({"status": "success", "deleted": count})
            if user_ids:
                user_set = {str(uid).strip() for uid in user_ids if str(uid).strip()}
                if not user_set:
                    return jsonify({"status": "error", "message": "未指定要删除的用户ID"})
                deleted = self._storage.delete_logs_by_users(user_set)
                self._moderation_logs = deque((l for l in self._moderation_logs if str(l.get("user_id", "")) not in user_set), maxlen=500)
                self._invalidate_stats_cache()
                return jsonify({"status": "success", "deleted": deleted})
            if not ids:
                return jsonify({"status": "error", "message": "未指定要删除的日志ID"})
            id_set = set()
            for i in ids:
                try:
                    id_set.add(int(i))
                except (ValueError, TypeError):
                    continue
            deleted = self._storage.delete_logs(id_set)
            self._moderation_logs = deque((l for l in self._moderation_logs if self._safe_int(l.get("id"), 0) not in id_set), maxlen=500)
            self._invalidate_stats_cache()
            return jsonify({"status": "success", "deleted": deleted})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_export_logs(self):
        # 导出审核日志，支持 json（默认）和 csv 两种格式。
        # csv 格式返回带 Content-Disposition 的文本，浏览器会自动触发下载。
        fmt = quart_request.args.get("format", "json").strip().lower()
        logs = self._storage.list_logs(limit=100000)
        if fmt == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["ID", "时间", "群号", "用户ID", "用户名", "消息内容", "操作", "原因"])
            for l in logs:
                writer.writerow([self._csv_safe(x) for x in (
                    l.get("id", ""), l.get("time", ""), l.get("group_id", ""),
                    l.get("user_id", ""), l.get("user_name", ""),
                    l.get("msg_text", ""), l.get("action", ""), l.get("reason", ""),
                )])
            return output.getvalue(), 200, {"Content-Type": "text/csv; charset=utf-8", "Content-Disposition": "attachment; filename=moderation_logs.csv"}
        return jsonify({"status": "success", "data": logs})

    async def _web_get_groups(self):
        # 获取 Bot 加入的所有群列表，附带群头像、黑白名单状态、今日拦截数。
        # 需要 QQ 客户端已连接，否则返回错误提示。
        force = str(quart_request.args.get("force", "")).strip().lower() in ("1", "true", "yes")
        cache = getattr(self, "_web_group_cache", {"ts": 0.0, "data": []})
        now = time.time()
        if not force and cache.get("data") and now - float(cache.get("ts", 0) or 0) < 20:
            return jsonify({"status": "success", "data": cache.get("data", [])})
        client = await self._get_client()
        if not client:
            if cache.get("data"):
                return jsonify({"status": "success", "data": cache.get("data", []), "stale": True, "message": "无法获取QQ客户端，已显示缓存群列表"})
            fallback = self._fallback_web_groups()
            if fallback:
                self._web_group_cache = {"ts": now, "data": fallback}
                return jsonify({"status": "success", "data": fallback, "stale": True, "message": "无法获取QQ客户端，已显示本地配置群"})
            return jsonify({"status": "error", "message": "无法获取QQ客户端，请确保已连接"})
        try:
            result = await self._call_onebot_web(client, 'get_group_list', timeout=8.0)
            groups = self._extract_list_result(result)
            today_start = self._today_start()
            today_blocked_map = {}
            for l in list(self._moderation_logs):
                if l.get("ts", 0) >= today_start and "撤回" in l.get("action", ""):
                    gid = str(l.get("group_id", ""))
                    if gid:
                        today_blocked_map[gid] = today_blocked_map.get(gid, 0) + 1
            configured_set = set(self._storage.list_configured_groups())
            enriched = []
            for g in groups:
                gid = str(g.get("group_id", ""))
                enriched.append({
                    "group_id": gid,
                    "group_name": g.get("group_name", ""),
                    "member_count": g.get("member_count", 0),
                    "avatar": f"https://p.qlogo.cn/gh/{gid}/{gid}/",
                    "is_white": gid in self._group_white_set,
                    "is_black": gid in self._group_black_set,
                    "has_config": gid in configured_set,
                    "today_blocked": today_blocked_map.get(gid, 0),
                })
            self._web_group_cache = {"ts": now, "data": enriched}
            return jsonify({"status": "success", "data": enriched})
        except asyncio.TimeoutError:
            if cache.get("data"):
                return jsonify({"status": "success", "data": cache.get("data", []), "stale": True})
            fallback = self._fallback_web_groups()
            if fallback:
                self._web_group_cache = {"ts": now, "data": fallback}
                return jsonify({"status": "success", "data": fallback, "stale": True, "message": "获取群列表超时，已显示本地缓存/配置群"})
            return jsonify({"status": "error", "message": "获取群列表超时，请稍后重试"})
        except Exception as e:
            logger.warning(f"[GroupMgr] WebUI 获取群列表失败: {e!r}")
            if cache.get("data"):
                return jsonify({"status": "success", "data": cache.get("data", []), "stale": True, "message": f"获取群列表失败，已显示缓存: {self._format_web_error(e)}"})
            fallback = self._fallback_web_groups()
            if fallback:
                self._web_group_cache = {"ts": now, "data": fallback}
                return jsonify({"status": "success", "data": fallback, "stale": True, "message": f"获取群列表失败，已显示本地配置群: {self._format_web_error(e)}"})
            return jsonify({"status": "error", "message": f"获取群列表失败: {self._format_web_error(e)}"})

    async def _web_get_group_members(self):
        # 获取指定群的成员列表，附带头像、角色、头衔、是否为插件管理员等丰富信息。
        # 按角色排序（群主 → 管理员 → 成员），需要 group_id 查询参数。
        group_id = quart_request.args.get("group_id", "").strip()
        if not group_id:
            return jsonify({"status": "error", "message": "缺少 group_id 参数"})
        force = str(quart_request.args.get("force", "")).strip().lower() in ("1", "true", "yes")
        member_cache = getattr(self, "_web_member_cache", {})
        cached = member_cache.get(group_id)
        now = time.time()
        if not force and cached and now - float(cached.get("ts", 0) or 0) < 15:
            return jsonify({"status": "success", "data": cached.get("data", [])})
        client = await self._get_client()
        if not client:
            if cached:
                return jsonify({"status": "success", "data": cached.get("data", []), "stale": True, "message": "无法获取QQ客户端，已显示缓存成员"})
            fallback = self._fallback_web_group_members(group_id)
            if fallback:
                member_cache[group_id] = {"ts": now, "data": fallback}
                self._web_member_cache = member_cache
                return jsonify({"status": "success", "data": fallback, "stale": True, "message": "无法获取QQ客户端，已显示本地记录成员"})
            return jsonify({"status": "error", "message": "无法获取QQ客户端"})
        try:
            gid = self._safe_int(group_id, 0)
            result = await self._call_onebot_web(client, 'get_group_member_list', timeout=10.0, group_id=gid, no_cache=force)
            members = self._extract_list_result(result)
            enriched = []
            admin_set = set(self._get_admin_list())
            for m in members:
                uid = str(m.get("user_id", ""))
                card = m.get("card", "")
                nickname = m.get("nickname", "")
                role = m.get("role", "member")
                title = m.get("title", "") or m.get("special_title", "")
                is_plugin_admin = uid in admin_set
                enriched.append({
                    "user_id": uid,
                    "nickname": nickname,
                    "card": card,
                    "display_name": card or nickname,
                    "role": role,
                    "title": title,
                    "avatar": f"https://q.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640",
                    "is_plugin_admin": is_plugin_admin,
                })
            role_order = {"owner": 0, "admin": 1, "member": 2}
            enriched.sort(key=lambda x: (role_order.get(x["role"], 9), x["display_name"]))
            member_cache[group_id] = {"ts": now, "data": enriched}
            self._web_member_cache = member_cache
            return jsonify({"status": "success", "data": enriched})
        except asyncio.TimeoutError:
            if cached:
                return jsonify({"status": "success", "data": cached.get("data", []), "stale": True})
            fallback = self._fallback_web_group_members(group_id)
            if fallback:
                member_cache[group_id] = {"ts": now, "data": fallback}
                self._web_member_cache = member_cache
                return jsonify({"status": "success", "data": fallback, "stale": True, "message": "获取群成员超时，已显示本地缓存/记录成员"})
            return jsonify({"status": "error", "message": "获取群成员超时，请稍后重试"})
        except Exception as e:
            logger.warning(f"[GroupMgr] WebUI 获取群成员失败 group={group_id}: {e!r}")
            if cached:
                return jsonify({"status": "success", "data": cached.get("data", []), "stale": True, "message": f"获取群成员失败，已显示缓存: {self._format_web_error(e)}"})
            fallback = self._fallback_web_group_members(group_id)
            if fallback:
                member_cache[group_id] = {"ts": now, "data": fallback}
                self._web_member_cache = member_cache
                return jsonify({"status": "success", "data": fallback, "stale": True, "message": f"获取群成员失败，已显示本地记录成员: {self._format_web_error(e)}"})
            return jsonify({"status": "error", "message": f"获取群成员失败: {self._format_web_error(e)}。当前 OneBot/平台可能不支持 get_group_member_list，且本地暂无该群成员缓存。"})

    async def _web_whitelist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            # 加白名单：从黑名单移除（互斥），再加入白名单。均落 DB + 内存。
            self._managed_list_remove("group_black", group_id)
            self._managed_list_add("group_white", group_id)
            return jsonify({"status": "success", "group_id": group_id, "white_list": self.group_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_whitelist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            self._managed_list_remove("group_white", group_id)
            return jsonify({"status": "success", "group_id": group_id, "white_list": self.group_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_blacklist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            self._managed_list_remove("group_white", group_id)
            self._managed_list_add("group_black", group_id)
            return jsonify({"status": "success", "group_id": group_id, "black_list": self.group_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_blacklist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            self._managed_list_remove("group_black", group_id)
            return jsonify({"status": "success", "group_id": group_id, "black_list": self.group_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_blacklist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            self._managed_list_add("user_black", user_id)
            return jsonify({"status": "success", "user_id": user_id, "user_black_list": self.user_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_whitelist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            # 审核白名单与用户黑名单互斥
            self._managed_list_remove("user_black", user_id)
            self._managed_list_add("user_white", user_id)
            return jsonify({"status": "success", "user_id": user_id, "user_white_list": self.user_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_blacklist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            self._managed_list_remove("user_black", user_id)
            return jsonify({"status": "success", "user_id": user_id, "user_black_list": self.user_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_whitelist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            self._managed_list_remove("user_white", user_id)
            return jsonify({"status": "success", "user_id": user_id, "user_white_list": self.user_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_admin_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            self._managed_list_add("admin", user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "user_id": user_id, "admin_list": self._get_admin_list()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_admin_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            self._managed_list_remove("admin", user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "user_id": user_id, "admin_list": self._get_admin_list()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_today_stats(self):
        # 返回今日违规拦截的详细统计：总拦截数、放行数、群排行 Top20、用户排行 Top20。
        # 数据按天缓存（_stats_cache），跨天自动重新计算。
        today_start = self._today_start()
        sc = self._stats_cache
        if sc["today_start"] == today_start and sc.get("user_names"):
            blocked_today = sc["blocked"]
            passed_today = sc["passed"]
            total_today = sc["total"]
            group_stats = dict(sc["group_stats"])
            user_stats = dict(sc["user_stats"])
            user_names = dict(sc["user_names"])
        else:
            group_stats = {}
            user_stats = {}
            user_names = {}
            blocked_today = 0
            passed_today = 0
            total_today = 0
            for l in list(self._moderation_logs):
                uid = str(l.get("user_id", ""))
                if uid and uid not in user_names:
                    user_names[uid] = l.get("user_name", "")
                if l.get("ts", 0) >= today_start:
                    total_today += 1
                    gid = str(l.get("group_id", ""))
                    action = l.get("action", "")
                    if "撤回" in action:
                        blocked_today += 1
                        if gid:
                            group_stats[gid] = group_stats.get(gid, 0) + 1
                        if uid:
                            user_stats[uid] = user_stats.get(uid, 0) + 1
                    elif "放行" in action:
                        passed_today += 1
            sc.update(today_start=today_start, blocked=blocked_today, passed=passed_today,
                      total=total_today, group_stats=group_stats, user_stats=user_stats, user_names=user_names)
        group_ranking = sorted(group_stats.items(), key=lambda x: x[1], reverse=True)[:20]
        user_ranking = sorted(user_stats.items(), key=lambda x: x[1], reverse=True)[:20]
        group_name_map = {}
        cache_data = getattr(self, "_web_group_cache", {}).get("data", [])
        for g in cache_data:
            group_name_map[str(g.get("group_id", ""))] = g.get("group_name", "")
        return jsonify({
            "status": "success",
            "data": {
                "total_today": total_today,
                "blocked_today": blocked_today,
                "passed_today": passed_today,
                "block_rate": round(blocked_today / total_today * 100, 1) if total_today > 0 else 0,
                "group_ranking": [{"group_id": g, "group_name": group_name_map.get(g, ""), "count": c} for g, c in group_ranking],
                "user_ranking": [{"user_id": u, "user_name": user_names.get(u, ""), "count": c} for u, c in user_ranking],
            }
        })

    async def _web_migration_status(self):
        # 返回 SQLite 迁移状态：数据库路径、日志数、词库数、旧 JSON 文件是否存在等。
        try:
            return jsonify({"status": "success", "data": self._storage.migration_status()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_migration_run(self):
        # 将旧的 JSON 日志文件导入 SQLite，迁移完成后重新加载词库和日志缓存，刷新统计。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            confirm = str(data.get("confirm", "")).strip()
            if not confirm:
                return jsonify({"status": "error", "message": "请确认后再执行迁移"})
            result = self._storage.migrate_legacy(delete_logs=True)
            self._lexicon = self._storage.load_lexicon()
            self._compiled_lexicon = self._compile_lexicon()
            self._moderation_logs = deque(self._storage.list_logs_asc(limit=500), maxlen=500)
            self._next_log_id = max(self._init_next_log_id(), self._storage.max_log_id() + 1)
            self._invalidate_stats_cache()
            return jsonify({"status": "success", "data": result})
        except Exception as e:
            logger.exception("migration failed")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_dashboard_trend(self):
        # 返回最近 N 天的每日拦截/放行/审核趋势，days 参数默认 30 天。
        try:
            days = min(int(quart_request.args.get("days", "30")), 365)
        except (ValueError, TypeError):
            days = 30
        try:
            data = self._storage.get_daily_trend(days=days)
            return jsonify({"status": "success", "data": data})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_dashboard_distribution(self):
        # 返回违规类型分布：按 reason 分组统计，days 参数默认 30 天。
        try:
            days = min(int(quart_request.args.get("days", "30")), 365)
        except (ValueError, TypeError):
            days = 30
        try:
            data = self._storage.get_violation_distribution(days=days)
            return jsonify({"status": "success", "data": data})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_dashboard_hourly(self):
        # 返回时段的拦截量分布（0-23 小时），用于分析违规高发时段。
        try:
            days = min(int(quart_request.args.get("days", "7")), 90)
        except (ValueError, TypeError):
            days = 7
        try:
            data = self._storage.get_hourly_distribution(days=days)
            return jsonify({"status": "success", "data": data})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_dashboard_group_ranking(self):
        # 返回历史群拦截排行 Top N，支持 days 和 top 参数。
        try:
            days = min(int(quart_request.args.get("days", "30")), 365)
        except (ValueError, TypeError):
            days = 30
        try:
            top_n = min(int(quart_request.args.get("top", "10")), 50)
        except (ValueError, TypeError):
            top_n = 10
        try:
            data = self._storage.get_group_activity_ranking(days=days, top_n=top_n)
            return jsonify({"status": "success", "data": data})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_anti_flood_status(self):
        try:
            data = self._get_anti_flood_status()
            return jsonify({"status": "success", "data": data})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})



    # ============================================================
    # v2.4.0 新增 WebUI API
    # ============================================================
    async def _web_get_join_rules(self):
        # F1：返回所有入群审核规则（含 default 全局规则）。
        try:
            return jsonify({"status": "success", "data": self._storage.list_join_audit_rules()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_save_join_rule(self):
        # F1：保存某群（或 default）的入群审核规则。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip() or "default"
            accept = data.get("accept_keywords", [])
            reject = data.get("reject_keywords", [])
            if isinstance(accept, str):
                accept = [x.strip() for x in re.split(r"[\r\n,，]+", accept) if x.strip()]
            if isinstance(reject, str):
                reject = [x.strip() for x in re.split(r"[\r\n,，]+", reject) if x.strip()]
            default_action = str(data.get("default_action", "manual")).strip().lower()
            if default_action not in ("manual", "accept", "reject"):
                default_action = "manual"
            reject_reason = str(data.get("reject_reason", ""))
            enabled = self._parse_bool(data.get("enabled", True), True)
            self._storage.save_join_audit_rule(
                group_id,
                [str(x).strip() for x in accept if str(x).strip()],
                [str(x).strip() for x in reject if str(x).strip()],
                default_action, reject_reason, enabled,
            )
            return jsonify({"status": "success", "group_id": group_id})
        except Exception as e:
            logger.exception("[GroupMgr] 保存入群规则失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_join_rule(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            ok = self._storage.delete_join_audit_rule(group_id)
            return jsonify({"status": "success", "deleted": ok, "group_id": group_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_scheduled_unbans(self):
        # F3：返回所有定时解禁计划。
        try:
            return jsonify({"status": "success", "data": self._storage.list_all_scheduled_unbans()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_scheduled_unban(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            unban_id = self._safe_int(data.get("id", 0), 0)
            if unban_id <= 0:
                return jsonify({"status": "error", "message": "缺少有效的 id"})
            ok = self._storage.delete_scheduled_unban(unban_id)
            return jsonify({"status": "success", "deleted": ok})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_appeals(self):
        # F2：返回申诉记录，可选 status 过滤。
        try:
            status = str(quart_request.args.get("status", "")).strip()
            limit = min(self._safe_int(quart_request.args.get("limit", 200), 200), 1000)
            return jsonify({"status": "success", "data": self._storage.list_appeals(status, limit)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_admin_grants(self):
        # F5：返回所有群管理员授权配置。
        try:
            return jsonify({"status": "success", "data": self._storage.list_group_admin_grants()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_save_admin_grant(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            grant_owner = self._parse_bool(data.get("grant_owner", True), True)
            grant_admin = self._parse_bool(data.get("grant_admin", True), True)
            enabled = self._parse_bool(data.get("enabled", True), True)
            self._storage.save_group_admin_grant(group_id, grant_owner, grant_admin, enabled)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "group_id": group_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_admin_grant(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            ok = self._storage.delete_group_admin_grant(group_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "deleted": ok, "group_id": group_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    # ============================================================
    # v2.4.0 新增：WebUI 远程执行 + 群超管 + 群权限黑名单
    # ============================================================
    async def _web_remote_actions(self):
        # 返回可远程执行的操作列表（供 WebUI 渲染下拉菜单与表单）。
        try:
            return jsonify({"status": "success", "data": self._remote_actions_meta()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_remote_execute(self):
        # 远程执行群管操作，支持单个 user_id 或批量 user_ids。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            action = str(data.get("action", "")).strip()
            params = data.get("params", {}) or {}
            if not group_id or not action:
                return jsonify({"status": "error", "message": "缺少 group_id 或 action"})
            result = await self._remote_execute(group_id, action, params)
            return jsonify({"status": "success" if result.get("ok") else "error", "data": result, "message": result.get("message", "")})
        except Exception as e:
            logger.exception("[GroupMgr] 远程执行失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_super_admins(self):
        try:
            group_id = str(quart_request.args.get("group_id", "")).strip()
            return jsonify({"status": "success", "data": self._storage.list_group_super_admins(group_id)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_add_super_admin(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            if not group_id or not user_id:
                return jsonify({"status": "error", "message": "缺少 group_id 或 user_id"})
            self._storage.add_group_super_admin(group_id, user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "group_id": group_id, "user_id": user_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_remove_super_admin(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            if not group_id or not user_id:
                return jsonify({"status": "error", "message": "缺少 group_id 或 user_id"})
            self._storage.remove_group_super_admin(group_id, user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "group_id": group_id, "user_id": user_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_admin_blocks(self):
        try:
            group_id = str(quart_request.args.get("group_id", "")).strip()
            return jsonify({"status": "success", "data": self._storage.list_group_admin_blocks(group_id)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_add_admin_block(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            if not group_id or not user_id:
                return jsonify({"status": "error", "message": "缺少 group_id 或 user_id"})
            self._storage.add_group_admin_block(group_id, user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "group_id": group_id, "user_id": user_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_remove_admin_block(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            if not group_id or not user_id:
                return jsonify({"status": "error", "message": "缺少 group_id 或 user_id"})
            self._storage.remove_group_admin_block(group_id, user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "group_id": group_id, "user_id": user_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    # ============================================================
    # v2.3.0 新增：多群独立配置 WebUI API
    # ============================================================
    # 不可按群覆盖的全局项（名单类已迁 DB；provider/免责声明/暗色模式/提示词注入是全局语义）。
    _GROUP_CONFIG_EXCLUDE = {
        "disclaimer_agreed", "webui_dark_mode", "prompt_injection_enabled",
        "moderation_llm_provider_id", "ocr_provider_id",
        "join_accept_keywords", "join_reject_keywords",
        "auto_unban_scan_interval",
        "group_admin_grant_enabled", "legacy_role_admin_enabled",
        "group_white_list", "group_black_list", "user_black_list", "user_white_list", "admin_list",
    }

    def _group_overridable_keys(self):
        # 动态计算可按群覆盖的配置项：schema 中除全局项外的通用配置。
        # list 类型通常是全局名单或复杂集合，WebUI 由专门页面维护，不进入单群覆盖。
        supported_types = {"bool", "int", "string", "text"}
        return [
            k for k, meta in self._config_schema.items()
            if k not in self._GROUP_CONFIG_EXCLUDE
            and meta.get("type", "bool") in supported_types
        ]

    def _group_config_allowed(self, group_id: str) -> bool:
        # 多群配置仅对白名单群开放：白名单为空时（=全部群启用）允许任意群；非空时仅白名单群可配。
        if not self._group_white_set:
            return True
        return str(group_id) in self._group_white_set

    _CONFIG_CATEGORIES = {
        "enabled": "基础开关", "auto_moderate_enabled": "基础开关", "auto_moderate_notice": "基础开关",
        "scan_swear": "审核规则", "scan_ad": "审核规则", "llm_moderation_enabled": "审核规则",
        "llm_moderation_ban": "审核规则", "moderation_ban_duration": "审核规则", "ban_notice": "审核规则",
        "scan_forward_msg": "审核规则", "recall_qq_favorite_enabled": "审核规则",
        "ocr_enabled": "OCR", "ocr_prompt_template": "OCR",
        "ocr_custom_system_prompt": "OCR", "ocr_custom_user_prompt": "OCR", "scan_sticker_enabled": "OCR",
        "qrcode_decode_enabled": "OCR",
        "member_action_require_group_role": "基础开关",
        "set_admin_require_owner": "基础开关",
        "llm_moderation_custom_prompt": "审核规则",
        "kick_recall_enabled": "审核规则", "kick_recall_count": "审核规则",
        "combine_detect_enabled": "重复检测", "combine_detect_count": "重复检测",
        "combine_detect_window_seconds": "重复检测",
        "custom_swear_keywords": "审核规则", "custom_ad_keywords": "审核规则",
        "anti_flood_enabled": "防刷屏", "anti_flood_rate_per_second": "防刷屏",
        "anti_flood_rate_per_minute": "防刷屏", "anti_flood_rate_per_hour": "防刷屏",
        "anti_flood_mute_duration": "防刷屏", "anti_flood_recall_enabled": "防刷屏",
        "anti_flood_recall_threshold": "防刷屏",
        "anti_flood_night_enabled": "夜间限速", "anti_flood_night_start_hour": "夜间限速",
        "anti_flood_night_end_hour": "夜间限速", "anti_flood_night_rate_per_second": "夜间限速",
        "anti_flood_night_rate_per_minute": "夜间限速", "anti_flood_night_rate_per_hour": "夜间限速",
        "repeat_detect_enabled": "重复检测", "repeat_detect_window_seconds": "重复检测",
        "repeat_detect_count": "重复检测", "long_text_detect_enabled": "重复检测", "long_text_threshold": "重复检测",
        "appeal_enabled": "申诉", "appeal_window_minutes": "申诉", "appeal_context_count": "申诉",
        "appeal_at_template": "申诉",
        "auto_unban_enabled": "定时解禁", "auto_unban_permanent_hours": "定时解禁",
        "join_audit_enabled": "入群审核", "join_reject_use_lexicon": "入群审核",
        "join_default_action": "入群审核", "join_reject_reason": "入群审核",
    }

    async def _web_get_group_config(self):
        try:
            group_id = str(quart_request.args.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            if not self._group_config_allowed(group_id):
                return jsonify({"status": "error", "message": "仅白名单群可进行多群配置，请先将该群加入白名单"})
            overrides = self._storage.get_group_configs(group_id)
            schema = self._config_schema
            items = []
            for key in self._group_overridable_keys():
                meta = schema.get(key, {})
                global_val = self.config.get(key, meta.get("default"))
                items.append({
                    "key": key,
                    "description": meta.get("description", key),
                    "type": meta.get("type", "bool"),
                    "hint": meta.get("hint", ""),
                    "options": meta.get("options", []),
                    "global_value": global_val,
                    "override": overrides.get(key),
                    "category": self._CONFIG_CATEGORIES.get(key, "功能开关"),
                })
            override_count = len(overrides)
            return jsonify({"status": "success", "data": {"group_id": group_id, "items": items, "override_count": override_count}})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_set_group_config(self):
        # 设置某群某配置项的覆盖值。value 统一以字符串存储。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            key = str(data.get("key", "")).strip()
            if not group_id or not key:
                return jsonify({"status": "error", "message": "缺少 group_id 或 key"})
            if not self._group_config_allowed(group_id):
                return jsonify({"status": "error", "message": "仅白名单群可进行多群配置"})
            if key not in self._group_overridable_keys():
                return jsonify({"status": "error", "message": "该配置项不支持按群设置"})
            value = data.get("value")
            # 按 schema 类型规范化：bool→"true"/"false"，int→整数字符串，其余转字符串
            meta = self._config_schema.get(key, {})
            ftype = meta.get("type", "bool")
            if ftype == "bool":
                value = "true" if self._parse_bool(value, False) else "false"
            elif ftype == "int":
                value = str(self._normalize_int_config_value(key, value))
            else:
                value = "" if value is None else str(value)
                options = meta.get("options") or []
                if options and value not in [str(x) for x in options]:
                    return jsonify({"status": "error", "message": "配置值不在允许选项内"})
            self._storage.set_group_config(group_id, key, value)
            self._invalidate_group_cfg_cache(group_id)
            return jsonify({"status": "success", "group_id": group_id, "key": key, "value": value})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_group_config(self):
        # 删除某群某配置项覆盖，恢复继承全局。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            key = str(data.get("key", "")).strip()
            if not group_id or not key:
                return jsonify({"status": "error", "message": "缺少 group_id 或 key"})
            self._storage.delete_group_config(group_id, key)
            self._invalidate_group_cfg_cache(group_id)
            return jsonify({"status": "success", "group_id": group_id, "key": key})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_clear_group_config(self):
        # 清空某群全部独立配置。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            n = self._storage.clear_group_configs(group_id)
            self._invalidate_group_cfg_cache(group_id)
            return jsonify({"status": "success", "group_id": group_id, "cleared": n})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_configured_groups(self):
        try:
            groups = self._storage.list_configured_groups()
            details = []
            for gid in groups:
                overrides = self._storage.get_group_configs(gid)
                details.append({"group_id": gid, "override_count": len(overrides)})
            return jsonify({"status": "success", "data": details})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_batch_set_group_config(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            items = data.get("items", {})
            if not group_id or not isinstance(items, dict):
                return jsonify({"status": "error", "message": "缺少 group_id 或 items"})
            if not self._group_config_allowed(group_id):
                return jsonify({"status": "error", "message": "仅白名单群可进行多群配置"})
            overridable = set(self._group_overridable_keys())
            updated = []
            for key, value in items.items():
                if key not in overridable:
                    continue
                meta = self._config_schema.get(key, {})
                ftype = meta.get("type", "bool")
                if ftype == "bool":
                    value = "true" if self._parse_bool(value, False) else "false"
                elif ftype == "int":
                    value = str(self._normalize_int_config_value(key, value))
                else:
                    value = "" if value is None else str(value)
                    # 与单项设置路径一致：有 options 的字段校验值在白名单内，不合法跳过
                    options = meta.get("options") or []
                    if options and value not in [str(x) for x in options]:
                        continue
                self._storage.set_group_config(group_id, key, value)
                updated.append(key)
            self._invalidate_group_cfg_cache(group_id)
            return jsonify({"status": "success", "group_id": group_id, "updated": updated, "count": len(updated)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_copy_group_config(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            source_id = str(data.get("source_group_id", "")).strip()
            target_id = str(data.get("target_group_id", "")).strip()
            if not source_id or not target_id:
                return jsonify({"status": "error", "message": "缺少 source_group_id 或 target_group_id"})
            if source_id == target_id:
                return jsonify({"status": "error", "message": "源群和目标群不能相同"})
            if not self._group_config_allowed(target_id):
                return jsonify({"status": "error", "message": "目标群不在白名单中"})
            source_configs = self._storage.get_group_configs(source_id)
            if not source_configs:
                return jsonify({"status": "error", "message": "源群没有独立配置"})
            overridable = set(self._group_overridable_keys())
            copied = 0
            for key, value in source_configs.items():
                if key in overridable:
                    self._storage.set_group_config(target_id, key, value)
                    copied += 1
            self._invalidate_group_cfg_cache(target_id)
            return jsonify({"status": "success", "source": source_id, "target": target_id, "copied": copied})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    # ==================== 名片监控 WebUI API ====================
    _CARD_MONITOR_KEYS = [
        "card_monitor_enabled", "card_log_enabled", "card_monitor_notify",
        "card_protect_enabled", "card_audit_link_only", "card_audit_enabled",
        "card_audit_llm_always", "admin_change_notify_enabled",
    ]

    async def _web_card_records(self):
        try:
            limit = min(max(self._safe_int(quart_request.args.get("limit", 100), 100), 1), 500)
            offset = max(0, self._safe_int(quart_request.args.get("offset", 0), 0))
            group_id = str(quart_request.args.get("group_id", "")).strip()
            user_id = str(quart_request.args.get("user_id", "")).strip()
            kind = str(quart_request.args.get("kind", "")).strip()
            logs = self._storage.list_card_change_logs(limit=limit, offset=offset,
                                                       group_id=group_id, user_id=user_id, kind=kind)
            total = self._storage.count_card_change_logs(group_id=group_id, user_id=user_id, kind=kind)
            return jsonify({"status": "success", "data": logs, "total": total, "limit": limit, "offset": offset})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_card_records_clear(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            n = self._storage.clear_card_change_logs(group_id)
            return jsonify({"status": "success", "cleared": n})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_card_config_get(self):
        try:
            cfg = {}
            for k in self._CARD_MONITOR_KEYS:
                meta = self._config_schema.get(k, {})
                cfg[k] = {
                    "value": self._parse_bool(self.config.get(k, meta.get("default", False)), bool(meta.get("default", False))),
                    "description": meta.get("description", k),
                    "hint": meta.get("hint", ""),
                }
            return jsonify({"status": "success", "data": cfg})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_card_config_set(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            key = str(data.get("key", "")).strip()
            if key not in self._CARD_MONITOR_KEYS:
                return jsonify({"status": "error", "message": "非法配置项"})
            value = self._parse_bool(data.get("value"), False)
            self.config[key] = value
            self._save_config_safe()
            self._invalidate_group_cfg_cache()
            return jsonify({"status": "success", "key": key, "value": value})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_card_protected_list(self):
        try:
            group_id = str(quart_request.args.get("group_id", "")).strip()
            return jsonify({"status": "success", "data": self._storage.list_card_protected(group_id)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_card_protected_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            protected_card = str(data.get("protected_card", ""))
            if not group_id or not user_id:
                return jsonify({"status": "error", "message": "缺少 group_id 或 user_id"})
            import time as _t
            self._storage.add_card_protected(group_id, user_id, protected_card, int(_t.time()))
            return jsonify({"status": "success", "group_id": group_id, "user_id": user_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_card_protected_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            if not group_id or not user_id:
                return jsonify({"status": "error", "message": "缺少 group_id 或 user_id"})
            ok = self._storage.remove_card_protected(group_id, user_id)
            return jsonify({"status": "success", "removed": ok})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})
