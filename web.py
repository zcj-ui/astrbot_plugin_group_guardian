# -*- coding: utf-8 -*-
import asyncio
import csv
import io
import json
import time
from collections import deque
from datetime import datetime

from astrbot.api import logger

try:
    from quart import jsonify, request as quart_request
except ImportError:
    jsonify = None
    quart_request = None

from .constants import PLUGIN_NAME, PLUGIN_VERSION
from .patterns import AD_PATTERNS, SWEAR_PATTERNS


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

    def _register_web_apis(self):
        # 遍历路由表，每项含 path / handler / methods / desc，统一注册到 self.context.register_web_api。
        try:
            routes = [
                ("/stats", self._web_stats, ["GET"], "获取群管统计信息"),
                ("/config", self._web_get_config, ["GET"], "获取当前配置"),
                ("/config", self._web_update_config, ["POST"], "更新配置"),
                ("/providers", self._web_get_providers, ["GET"], "获取可用LLM Provider列表"),
                ("/lexicon", self._web_get_lexicon, ["GET"], "获取外置词库内容"),
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
                ("/admin/add", self._web_admin_add, ["POST"], "添加管理员"),
                ("/admin/remove", self._web_admin_remove, ["POST"], "移除管理员"),
                ("/today_stats", self._web_today_stats, ["GET"], "获取今日拦截统计"),
                ("/migration/status", self._web_migration_status, ["GET"], "获取SQLite迁移状态"),
                ("/migration/run", self._web_migration_run, ["POST"], "执行SQLite迁移"),
                ("/dashboard/trend", self._web_dashboard_trend, ["GET"], "获取每日拦截趋势数据"),
                ("/dashboard/distribution", self._web_dashboard_distribution, ["GET"], "获取违规类型分布"),
                ("/dashboard/hourly", self._web_dashboard_hourly, ["GET"], "获取时段分布"),
                ("/dashboard/group_ranking", self._web_dashboard_group_ranking, ["GET"], "获取历史群拦截排行"),
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
        # 返回插件全局概览：版本、黑白名单数、规则数、词库大小、今日拦截/放行/总计。
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
        stats = {
            "plugin_name": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "disclaimer_agreed": self.config.get("disclaimer_agreed", False),
            "auto_moderate_enabled": self.auto_moderate_enabled,
            "group_white_list_count": len(self.group_white_list),
            "group_black_list_count": len(self.group_black_list),
            "user_black_list_count": len(self.user_black_list),
            "admin_list_count": len(self.config.get("admin_list", [])),
            "swear_patterns_count": len(SWEAR_PATTERNS),
            "ad_patterns_count": len(AD_PATTERNS),
            "lexicon_categories_count": len(self._lexicon),
            "lexicon_total_keywords": sum(
                len(cat.get("keywords", [])) for cat in self._lexicon.values()
            ),
            "total_logs": self._storage.count_logs(),
            "today_total": today_total,
            "today_blocked": today_blocked,
            "today_passed": today_passed,
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
        # 返回插件当前所有配置项，同时附加黑白名单和管理员列表的运行时快照。
        safe_config = {}
        for k in self._config_schema:
            if k in self.config:
                safe_config[k] = self.config[k]
        safe_config["_white_list"] = self.group_white_list
        safe_config["_black_list"] = self.group_black_list
        safe_config["_user_black_list"] = self.user_black_list
        safe_config["_admin_list"] = self.config.get("admin_list", [])
        return jsonify(safe_config)

    async def _web_update_config(self):
        # 接收 POST JSON 批量更新配置项，根据 _conf_schema.json 的 type 字段做类型校验。
        # 特殊处理：int 类型有范围限制、list 类型自动同步 set 属性、lexicon_* 变更重编译词库正则。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            schema = self._config_schema
            int_ranges = {"moderation_ban_duration": (60, 2592000)}
            list_postprocess = {
                "group_white_list": ("group_white_list", "_group_white_set"),
                "group_black_list": ("group_black_list", "_group_black_set"),
                "user_black_list": ("user_black_list", "_user_black_set"),
            }
            updated = []
            for key, value in data.items():
                if key not in schema:
                    continue
                field_type = schema[key].get("type", "")
                if field_type == "bool":
                    self.config[key] = bool(value)
                    updated.append(key)
                elif field_type == "list":
                    val = value
                    if isinstance(val, str):
                        val = [x.strip() for x in val.replace("，", ",").split(",") if x.strip()]
                    if isinstance(val, list):
                        self.config[key] = [str(x).strip() for x in val if x]
                        updated.append(key)
                elif field_type == "int":
                    try:
                        val = int(value)
                        lo, hi = int_ranges.get(key, (None, None))
                        if lo is not None:
                            val = max(lo, val)
                        if hi is not None:
                            val = min(hi, val)
                        self.config[key] = val
                        updated.append(key)
                    except (ValueError, TypeError):
                        pass
                elif field_type in ("string", "text"):
                    self.config[key] = str(value)
                    updated.append(key)
            if "auto_moderate_enabled" in updated:
                self.auto_moderate_enabled = bool(self.config.get("auto_moderate_enabled", True))
            if any(k.startswith("lexicon_") for k in updated):
                self._compiled_lexicon = self._compile_lexicon()
            for cfg_key, (list_attr, set_attr) in list_postprocess.items():
                if cfg_key in updated:
                    raw = self.config.get(cfg_key, [])
                    cleaned = [str(g).strip() for g in (raw if isinstance(raw, list) else [raw]) if g]
                    setattr(self, list_attr, cleaned)
                    setattr(self, set_attr, set(cleaned))
            if "admin_list" in updated:
                al = self.config.get("admin_list", [])
                self.config["admin_list"] = [str(a).strip() for a in (al if isinstance(al, list) else [al]) if a]
                self._admin_role_cache.clear()
            if "enabled" in updated:
                self.config["enabled"] = bool(self.config.get("enabled", True))
            if updated:
                self._save_config_safe()
            return jsonify({"status": "success", "updated": updated})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_lexicon(self):
        # 返回外置词库完整内容（所有分类及关键词），供 WebUI 展示和编辑。
        return jsonify({"status": "success", "data": self._lexicon})

    async def _web_get_logs(self):
        # 分页查询审核日志（SQLite），limit 参数最大 200 条。
        try:
            limit = min(int(quart_request.args.get("limit", 50)), 200)
        except (ValueError, TypeError):
            limit = 50
        logs = self._storage.list_logs(limit=limit)
        return jsonify({"status": "success", "data": logs})

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
        # 以纯文本形式返回日志完整正文（text/plain），支持 CORS，用于复制或外部工具调用。
        _cors = {"Access-Control-Allow-Origin": "*", "Content-Type": "text/plain; charset=utf-8"}
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
        # 聚合被撤回消息的用户列表，按违规次数降序排列。
        # 支持 action 参数过滤（如只查"LLM撤回"），每人最多保留 50 条最近记录。
        logs = self._storage.list_logs(limit=5000)
        action_filter = quart_request.args.get("action", "").strip()
        user_map = {}
        for log in logs:
            if action_filter and action_filter not in log.get("action", ""):
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
                    "records": [],
                }
            u = user_map[uid]
            u["count"] += 1
            u["last_time"] = log.get("time", "")
            if len(u["records"]) < 50:
                u["records"].append({
                    "id": log.get("id"),
                    "time": log.get("time", ""),
                    "ts": log.get("ts", 0),
                    "group_id": log.get("group_id", ""),
                    "msg_preview": log.get("msg_preview", ""),
                    "msg_text": log.get("msg_text", ""),
                    "action": log.get("action", ""),
                    "reason": log.get("reason", ""),
                    "image_urls": log.get("image_urls", []),
                })
        users = sorted(user_map.values(), key=lambda x: x["count"], reverse=True)
        return jsonify({"status": "success", "data": users})

    async def _web_delete_logs(self):
        # 批量删除审核日志：支持按 id 列表删除或 delete_all=True 清空全部。
        # 删除后同步更新内存缓存和统计缓存。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            ids = data.get("ids", [])
            delete_all = data.get("delete_all", False)
            if delete_all:
                count = self._storage.delete_all_logs()
                self._moderation_logs.clear()
                self._invalidate_stats_cache()
                return jsonify({"status": "success", "deleted": count})
            if not ids:
                return jsonify({"status": "error", "message": "未指定要删除的日志ID"})
            id_set = set()
            for i in ids:
                try:
                    id_set.add(int(i))
                except (ValueError, TypeError):
                    continue
            deleted = self._storage.delete_logs(id_set)
            self._moderation_logs = deque((l for l in self._moderation_logs if l.get("id") not in id_set), maxlen=500)
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
                writer.writerow([
                    l.get("id", ""), l.get("time", ""), l.get("group_id", ""),
                    l.get("user_id", ""), l.get("user_name", ""),
                    l.get("msg_text", ""), l.get("action", ""), l.get("reason", ""),
                ])
            return output.getvalue(), 200, {"Content-Type": "text/csv; charset=utf-8", "Content-Disposition": "attachment; filename=moderation_logs.csv"}
        return jsonify({"status": "success", "data": logs})

    async def _web_get_groups(self):
        # 获取 Bot 加入的所有群列表，附带群头像、黑白名单状态、今日拦截数。
        # 需要 QQ 客户端已连接，否则返回错误提示。
        client = await self._get_client()
        if not client:
            return jsonify({"status": "error", "message": "无法获取QQ客户端，请确保已连接"})
        try:
            result = await client.call_action('get_group_list')
            groups = self._extract_list_result(result)
            today_start = self._today_start()
            white_set = self._group_white_set
            today_blocked_map = {}
            for l in list(self._moderation_logs):
                if l.get("ts", 0) >= today_start and "撤回" in l.get("action", ""):
                    gid = str(l.get("group_id", ""))
                    if gid in white_set:
                        today_blocked_map[gid] = today_blocked_map.get(gid, 0) + 1
            enriched = []
            for g in groups:
                gid = str(g.get("group_id", ""))
                member_count = g.get("member_count", 0)
                is_white = gid in white_set
                is_black = gid in self._group_black_set
                enriched.append({
                    "group_id": gid,
                    "group_name": g.get("group_name", ""),
                    "member_count": member_count,
                    "avatar": f"https://p.qlogo.cn/gh/{gid}/{gid}/",
                    "is_white": is_white,
                    "is_black": is_black,
                    "today_blocked": today_blocked_map.get(gid, 0),
                })
            return jsonify({"status": "success", "data": enriched})
        except Exception as e:
            return jsonify({"status": "error", "message": f"获取群列表失败: {e}"})

    async def _web_get_group_members(self):
        # 获取指定群的成员列表，附带头像、角色、头衔、是否为插件管理员等丰富信息。
        # 按角色排序（群主 → 管理员 → 成员），需要 group_id 查询参数。
        group_id = quart_request.args.get("group_id", "").strip()
        if not group_id:
            return jsonify({"status": "error", "message": "缺少 group_id 参数"})
        client = await self._get_client()
        if not client:
            return jsonify({"status": "error", "message": "无法获取QQ客户端"})
        try:
            gid = self._safe_int(group_id, 0)
            result = await client.call_action('get_group_member_list', group_id=gid, no_cache=True)
            members = self._extract_list_result(result)
            enriched = []
            admin_set = set(str(a).strip() for a in self.config.get("admin_list", []) if a)
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
            return jsonify({"status": "success", "data": enriched})
        except Exception as e:
            return jsonify({"status": "error", "message": f"获取群成员失败: {e}"})

    async def _web_whitelist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            if group_id in self._group_black_set:
                self._safe_list_remove(self.group_black_list, group_id)
                self._group_black_set.discard(group_id)
                self.config["group_black_list"] = self.group_black_list
            if group_id not in self._group_white_set:
                self.group_white_list.append(group_id)
                self._group_white_set.add(group_id)
                self.config["group_white_list"] = self.group_white_list
            self._save_config_safe()
            return jsonify({"status": "success", "group_id": group_id, "white_list": self.group_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_whitelist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            if group_id in self._group_white_set:
                self._safe_list_remove(self.group_white_list, group_id)
                self._group_white_set.discard(group_id)
                self.config["group_white_list"] = self.group_white_list
            self._save_config_safe()
            return jsonify({"status": "success", "group_id": group_id, "white_list": self.group_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_blacklist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            if group_id in self._group_white_set:
                self._safe_list_remove(self.group_white_list, group_id)
                self._group_white_set.discard(group_id)
                self.config["group_white_list"] = self.group_white_list
            if group_id not in self._group_black_set:
                self.group_black_list.append(group_id)
                self._group_black_set.add(group_id)
                self.config["group_black_list"] = self.group_black_list
            self._save_config_safe()
            return jsonify({"status": "success", "group_id": group_id, "black_list": self.group_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_blacklist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            if group_id in self._group_black_set:
                self._safe_list_remove(self.group_black_list, group_id)
                self._group_black_set.discard(group_id)
                self.config["group_black_list"] = self.group_black_list
            self._save_config_safe()
            return jsonify({"status": "success", "group_id": group_id, "black_list": self.group_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_blacklist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            if user_id not in self._user_black_set:
                self.user_black_list.append(user_id)
                self._user_black_set.add(user_id)
                self.config["user_black_list"] = self.user_black_list
            self._save_config_safe()
            return jsonify({"status": "success", "user_id": user_id, "user_black_list": self.user_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_blacklist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            if user_id in self._user_black_set:
                self._safe_list_remove(self.user_black_list, user_id)
                self._user_black_set.discard(user_id)
                self.config["user_black_list"] = self.user_black_list
            self._save_config_safe()
            return jsonify({"status": "success", "user_id": user_id, "user_black_list": self.user_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_admin_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            admin_list = self._get_admin_list()
            if user_id not in admin_list:
                admin_list.append(user_id)
                self.config["admin_list"] = admin_list
            self._admin_role_cache.clear()
            self._save_config_safe()
            return jsonify({"status": "success", "user_id": user_id, "admin_list": admin_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_admin_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            admin_list = self._get_admin_list()
            if user_id in admin_list:
                self._safe_list_remove(admin_list, user_id)
                self.config["admin_list"] = admin_list
            self._admin_role_cache.clear()
            self._save_config_safe()
            return jsonify({"status": "success", "user_id": user_id, "admin_list": admin_list})
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
        return jsonify({
            "status": "success",
            "data": {
                "total_today": total_today,
                "blocked_today": blocked_today,
                "passed_today": passed_today,
                "group_ranking": [{"group_id": g, "count": c} for g, c in group_ranking],
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


