# -*- coding: utf-8 -*-
import json
import os
import re
import time
import asyncio
import csv
import io
import tempfile
from collections import deque
from datetime import datetime
from typing import Optional, Tuple, Dict, List

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

try:
    from quart import jsonify, request as quart_request
except ImportError:
    jsonify = None
    quart_request = None

from .patterns import _POLITICAL_WHITELIST, SWEAR_PATTERNS, AD_PATTERNS

_PLUGIN_NAME = "astrbot_plugin_group_guardian"
_PLUGIN_VERSION = "v1.9.8"


@register("astrbot_plugin_group_guardian", "zhaisir", "QQ群智能守护者 - AI审核+群管工具集", _PLUGIN_VERSION, "https://github.com/zcj-ui/astrbot_plugin_group_guardian")
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._config_schema = self._load_config_schema()
        self._sync_astrbot_admins()
        self._client = None
        _gwl = self.config.get("group_white_list", [])
        self.group_white_list = [str(g).strip() for g in (_gwl if isinstance(_gwl, list) else [_gwl]) if g]
        self._group_white_set = set(self.group_white_list)
        _gbl = self.config.get("group_black_list", [])
        self.group_black_list = [str(g).strip() for g in (_gbl if isinstance(_gbl, list) else [_gbl]) if g]
        self._group_black_set = set(self.group_black_list)
        _ubl = self.config.get("user_black_list", [])
        self.user_black_list = [str(u).strip() for u in (_ubl if isinstance(_ubl, list) else [_ubl]) if u]
        self._user_black_set = set(self.user_black_list)
        self.auto_moderate_enabled = self.config.get("auto_moderate_enabled", True)
        self._compiled_swear = self._build_combined_regex(SWEAR_PATTERNS)
        self._compiled_ad = self._build_combined_regex(AD_PATTERNS)
        self._lexicon = self._load_lexicon()
        self._compiled_lexicon = self._compile_lexicon()
        self._moderation_logs = deque(self._load_logs(), maxlen=500)
        self._last_log_save = 0.0
        self._admin_role_cache: Dict[str, Tuple[bool, float]] = {}
        self._admin_role_cache_ttl = 300.0
        self._stats_cache = {"today_start": 0, "blocked": 0, "passed": 0, "total": 0, "group_stats": {}, "user_stats": {}}
        self._llm_semaphore = asyncio.Semaphore(5)
        self._register_web_apis()

    async def terminate(self):
        try:
            data = list(self._moderation_logs)
            p = self._logs_path()
            await asyncio.to_thread(self._write_logs_sync, p, data)
            logger.info("[GroupMgr] 插件卸载，日志已保存")
        except Exception as e:
            logger.warning(f"[GroupMgr] 插件卸载保存日志失败: {e}")

    def _sync_astrbot_admins(self) -> None:
        try:
            ab_config = getattr(self.context, 'astrbot_config', None)
            if not ab_config:
                return
            astrbot_admin_ids = [str(x).strip() for x in (ab_config.get('admin_id', []) or []) if str(x).strip()]
            if not astrbot_admin_ids:
                return
            plugin_admins = self.config.get("admin_list", [])
            plugin_admins = [str(a).strip() for a in (plugin_admins if isinstance(plugin_admins, list) else [plugin_admins]) if a]
            new_admins = [a for a in astrbot_admin_ids if a not in plugin_admins]
            if new_admins:
                plugin_admins.extend(new_admins)
                self.config["admin_list"] = plugin_admins
                self._save_config_safe()
                logger.info(f"[GroupMgr] 自动同步AstrBot管理员到插件admin_list: {new_admins}")
        except Exception as _e:
            logger.debug(f"[GroupMgr] 同步AstrBot管理员失败: {_e}")

    def _save_config_safe(self) -> None:
        try:
            self.config.save_config()
        except Exception:
            logger.exception("save_config failed")

    @staticmethod
    def _load_config_schema() -> dict:
        try:
            schema_path = os.path.join(os.path.dirname(__file__), "_conf_schema.json")
            with open(schema_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _logs_path(self) -> str:
        data_dir = StarTools.get_data_dir()
        if not data_dir.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / "moderation_logs.json")

    def _load_logs(self) -> list:
        try:
            p = self._logs_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data[-500:]
        except Exception:
            logger.exception("load_logs failed")
        return []

    def _save_logs(self) -> None:
        try:
            p = self._logs_path()
            data = list(self._moderation_logs)
            try:
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, self._write_logs_sync, p, data)
                self._last_log_save = time.time()
            except RuntimeError:
                logger.warning("[GroupMgr] 无事件循环，跳过日志写入（将在下次可用时保存）")
        except Exception:
            logger.exception("save_logs failed")

    @staticmethod
    def _write_logs_sync(path: str, data: list) -> None:
        dir_name = os.path.dirname(path)
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix='.json', dir=dir_name)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    @staticmethod
    def _check_quart_available():
        if quart_request is None or jsonify is None:
            raise RuntimeError("Web框架(Quart)不可用，请检查AstrBot版本")

    def _wrap_web_handler(self, handler):
        async def _wrapped(*args, **kwargs):
            self._check_quart_available()
            return await handler(*args, **kwargs)
        _wrapped.__name__ = handler.__name__
        return _wrapped

    def _register_web_apis(self):
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
            ]
            for path, handler, methods, desc in routes:
                self.context.register_web_api(
                    f"/{_PLUGIN_NAME}{path}",
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
        stats = {
            "plugin_name": _PLUGIN_NAME,
            "version": _PLUGIN_VERSION,
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
            "total_logs": len(self._moderation_logs),
            "today_total": today_total,
            "today_blocked": today_blocked,
            "today_passed": today_passed,
        }
        return jsonify({"status": "success", "data": stats})

    async def _web_get_providers(self):
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
        safe_config["_white_list"] = self.group_white_list
        safe_config["_black_list"] = self.group_black_list
        safe_config["_user_black_list"] = self.user_black_list
        safe_config["_admin_list"] = self.config.get("admin_list", [])
        return jsonify(safe_config)

    async def _web_update_config(self):
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
        return jsonify({"status": "success", "data": self._lexicon})

    async def _web_get_logs(self):
        try:
            limit = min(int(quart_request.args.get("limit", 50)), 200)
        except (ValueError, TypeError):
            limit = 50
        logs = list(self._moderation_logs)[-limit:]
        return jsonify({"status": "success", "data": logs})

    async def _web_get_moderation_users(self):
        logs = list(self._moderation_logs)
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
                })
        users = sorted(user_map.values(), key=lambda x: x["count"], reverse=True)
        return jsonify({"status": "success", "data": users})

    async def _web_delete_logs(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            ids = data.get("ids", [])
            delete_all = data.get("delete_all", False)
            if delete_all:
                count = len(self._moderation_logs)
                self._moderation_logs.clear()
                self._invalidate_stats_cache()
                self._save_logs()
                return jsonify({"status": "success", "deleted": count})
            if not ids:
                return jsonify({"status": "error", "message": "未指定要删除的日志ID"})
            id_set = set()
            for i in ids:
                try:
                    id_set.add(int(i))
                except (ValueError, TypeError):
                    continue
            before = len(self._moderation_logs)
            new_logs = deque((l for l in self._moderation_logs if l.get("id") not in id_set), maxlen=500)
            for i, log in enumerate(new_logs):
                log["id"] = i
            self._moderation_logs = new_logs
            self._invalidate_stats_cache()
            self._save_logs()
            return jsonify({"status": "success", "deleted": before - len(self._moderation_logs)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_export_logs(self):
        fmt = quart_request.args.get("format", "json").strip().lower()
        logs = list(self._moderation_logs)
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
        client = await self._get_client()
        if not client:
            return jsonify({"status": "error", "message": "无法获取QQ客户端，请确保已连接"})
        try:
            result = await client.call_action('get_group_list')
            groups = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
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
        group_id = quart_request.args.get("group_id", "").strip()
        if not group_id:
            return jsonify({"status": "error", "message": "缺少 group_id 参数"})
        client = await self._get_client()
        if not client:
            return jsonify({"status": "error", "message": "无法获取QQ客户端"})
        try:
            gid = self._safe_int(group_id, 0)
            result = await client.call_action('get_group_member_list', group_id=gid, no_cache=True)
            members = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
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
            admin_list = self.config.get("admin_list", [])
            if not isinstance(admin_list, list):
                admin_list = []
            admin_list = [str(a).strip() for a in admin_list if a]
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
            admin_list = self.config.get("admin_list", [])
            if not isinstance(admin_list, list):
                admin_list = []
            admin_list = [str(a).strip() for a in admin_list if a]
            if user_id in admin_list:
                self._safe_list_remove(admin_list, user_id)
                self.config["admin_list"] = admin_list
            self._admin_role_cache.clear()
            self._save_config_safe()
            return jsonify({"status": "success", "user_id": user_id, "admin_list": admin_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_today_stats(self):
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

    def _safe_list_remove(self, lst: list, value) -> bool:
        try:
            lst.remove(value)
            return True
        except ValueError:
            return False

    def _cfg(self, key: str, default: bool = True) -> bool:
        return bool(self.config.get(key, default))

    def _today_start(self) -> int:
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(today.timestamp())

    def _safe_int(self, value, default: int = 0) -> int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _build_combined_regex(patterns: list, chunk_size: int = 500) -> list:
        if not patterns:
            return []
        compiled = []
        for i in range(0, len(patterns), chunk_size):
            chunk = patterns[i:i + chunk_size]
            combined = '|'.join(f'(?:{p})' for p in chunk)
            try:
                compiled.append(re.compile(combined, re.IGNORECASE))
            except re.error:
                for p in chunk:
                    try:
                        compiled.append(re.compile(p, re.IGNORECASE))
                    except re.error:
                        pass
        return compiled

    def _cfg_check(self, key: str, name: str) -> Tuple[bool, str]:
        if not self._cfg("enabled"):
            return False, "插件已禁用，所有功能不可用"
        if not self.config.get("disclaimer_agreed", False):
            return False, "您暂未阅读并同意免责声明，请在插件设置中阅读并同意免责声明后使用"
        if not self._cfg(key):
            return False, f"{name}功能已在配置中禁用"
        return True, ""

    def _check_api_result(self, result, action_name: str = "操作") -> Tuple[bool, str]:
        if result is None:
            return True, ""
        if isinstance(result, dict):
            status = result.get("status", "")
            retcode = result.get("retcode", 0)
            if status == "failed" or (retcode != 0 and retcode is not None):
                msg = result.get("msg", "") or result.get("message", "") or f"错误码: {retcode}"
                return False, msg
        return True, ""

    def _get_plugin_dir(self) -> str:
        return os.path.dirname(os.path.abspath(__file__))

    def _load_lexicon(self) -> Dict[str, Dict]:
        lexicon_path = os.path.join(self._get_plugin_dir(), "lexicon.json")
        data_dir = StarTools.get_data_dir()
        data_lexicon_path = str(data_dir / "lexicon.json")
        if os.path.exists(data_lexicon_path):
            lexicon_path = data_lexicon_path
        if not os.path.exists(lexicon_path):
            logger.warning("[GroupMgr] 外置词库文件不存在，跳过加载")
            return {}
        try:
            with open(lexicon_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            categories = data.get("categories", {})
            logger.info(f"[GroupMgr] 已加载外置词库: {len(categories)} 个分类")
            for cat_name, cat_data in categories.items():
                keywords = cat_data.get("keywords", [])
                logger.info(f"[GroupMgr]   - {cat_name}: {len(keywords)} 条关键词")
            return categories
        except Exception as e:
            logger.error(f"[GroupMgr] 加载外置词库失败: {e}")
            return {}

    def _compile_lexicon(self) -> Dict[str, List[re.Pattern]]:
        compiled = {}
        enable_political = self.config.get("lexicon_political_enabled", True)
        enable_porn = self.config.get("lexicon_porn_enabled", True)
        enable_violent = self.config.get("lexicon_violent_enabled", True)
        enable_reactionary = self.config.get("lexicon_reactionary_enabled", True)
        enable_weapons = self.config.get("lexicon_weapons_enabled", True)
        enable_corruption = self.config.get("lexicon_corruption_enabled", True)
        enable_illegal_url = self.config.get("lexicon_illegal_url_enabled", True)
        enable_other = self.config.get("lexicon_other_enabled", True)

        switch_map = {
            "political": enable_political,
            "porn": enable_porn,
            "violent_terror": enable_violent,
            "reactionary": enable_reactionary,
            "weapons": enable_weapons,
            "corruption": enable_corruption,
            "illegal_url": enable_illegal_url,
            "other": enable_other,
            "supplement": enable_other,
            "livelihood": enable_other,
            "tencent_ban": enable_other,
            "ad": True,
        }

        for cat_name, cat_data in self._lexicon.items():
            if not switch_map.get(cat_name, True):
                continue
            keywords = cat_data.get("keywords", [])
            escaped_parts = []
            min_len = 2 if cat_name == "illegal_url" else 3
            skip_keywords = _POLITICAL_WHITELIST if cat_name == "political" else set()
            for kw in keywords:
                kw = kw.strip()
                if not kw or kw.lower() in skip_keywords:
                    continue
                if '+' in kw and cat_name != "illegal_url":
                    parts = [p.strip() for p in kw.split('+') if p.strip()]
                    for part in parts:
                        if len(part) >= min_len and part.lower() not in skip_keywords:
                            escaped_parts.append(re.escape(part))
                else:
                    if len(kw) < min_len:
                        continue
                    escaped_parts.append(re.escape(kw))
            if escaped_parts:
                compiled[cat_name] = self._build_combined_regex_from_escaped(escaped_parts)
        return compiled

    @staticmethod
    def _build_combined_regex_from_escaped(escaped_parts: list, chunk_size: int = 3000) -> list:
        if not escaped_parts:
            return []
        compiled = []
        for i in range(0, len(escaped_parts), chunk_size):
            chunk = escaped_parts[i:i + chunk_size]
            combined = '|'.join(chunk)
            try:
                compiled.append(re.compile(combined, re.IGNORECASE))
            except re.error:
                for p in chunk:
                    try:
                        compiled.append(re.compile(p, re.IGNORECASE))
                    except re.error:
                        pass
        return compiled

    def _check_lexicon(self, text: str) -> Dict[str, bool]:
        result = {}
        text_lower = text.lower()
        for cat_name, patterns in self._compiled_lexicon.items():
            hit = False
            for p in patterns:
                m = p.search(text_lower)
                if m:
                    logger.debug(f"[GroupMgr] 词库命中 [{cat_name}]: 关键词='{m.group()}'")
                    hit = True
                    break
            result[cat_name] = hit
        return result

    async def _get_client(self, event: AstrMessageEvent = None):
        if event:
            client = getattr(event, 'bot', None)
            if client and hasattr(client, 'call_action'):
                self._client = client
                return client
        if self._client and hasattr(self._client, 'call_action'):
            return self._client
        try:
            pm = self.context.platform_manager
            if hasattr(pm, 'get_insts'):
                platforms = pm.get_insts() or []
            else:
                platforms = pm._platforms.values() if hasattr(pm, '_platforms') else []
            for platform in platforms:
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client and hasattr(client, 'call_action'):
                        self._client = client
                        return client
                elif hasattr(platform, 'client') and hasattr(platform.client, 'call_action'):
                    self._client = platform.client
                    return platform.client
        except Exception as e:
            logger.debug(f"[GroupMgr] 从 platform_manager 获取 client 失败: {e}")
        return None

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            if hasattr(event, 'group_id') and event.group_id:
                return str(event.group_id)
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'group_id'):
                return str(event.message_obj.group_id)
            if hasattr(event, 'raw_message') and hasattr(event.raw_message, 'group_id'):
                return str(event.raw_message.group_id)
            gid = event.get_group_id()
            if gid:
                return str(gid)
        except Exception as _e:
            logger.debug(f"[GroupMgr] _get_group_id fallback: {_e}")
        return ""

    def _try_get_sender_id(self, event: AstrMessageEvent) -> str:
        for getter in [
            lambda: str(event.get_sender_id()) if event.get_sender_id() else None,
            lambda: str(event.sender.user_id) if hasattr(event, 'sender') and hasattr(event.sender, 'user_id') else None,
            lambda: str(event.user_id) if hasattr(event, 'user_id') else None,
            lambda: str((getattr(event, 'raw_event', None) or {}).get('user_id') or (getattr(event, 'raw_event', None) or {}).get('sender', {}).get('user_id')) or None,
            lambda: str(event.message_obj.sender.user_id) if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'sender') and hasattr(event.message_obj.sender, 'user_id') else None,
        ]:
            try:
                result = getter()
                if result and result != 'None':
                    return result
            except Exception:
                pass
        return ""

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        user_id = self._try_get_sender_id(event)
        if not user_id:
            logger.warning(f"[GroupMgr] _is_admin 无法获取user_id from {type(event).__name__}")
            return False

        astrbot_admin_ids = []
        try:
            ab_config = getattr(self.context, 'astrbot_config', None)
            if ab_config:
                astrbot_admin_ids = [str(x).strip() for x in (ab_config.get('admin_id', []) or []) if str(x).strip()]
        except Exception as _e:
            logger.debug(f"[GroupMgr] 获取AstrBot管理员列表失败: {_e}")
        try:
            config_admins = self.config.get("admin_list", [])
            all_admins = set(astrbot_admin_ids) | set(str(a).strip() for a in config_admins if a)
            if user_id in all_admins:
                return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 读取config admin_list失败: {e}")

        group_id = self._get_group_id(event)
        if group_id:
            cache_key = f"{group_id}:{user_id}"
            if len(self._admin_role_cache) > 1000:
                now = time.time()
                self._admin_role_cache = {
                    k: v for k, v in self._admin_role_cache.items()
                    if now - v[1] < self._admin_role_cache_ttl
                }
            cached = self._admin_role_cache.get(cache_key)
            if cached:
                is_admin_val, ts = cached
                if time.time() - ts < self._admin_role_cache_ttl:
                    return is_admin_val

            try:
                group_id_int = int(group_id)
            except (ValueError, TypeError):
                return False
            try:
                user_id_int = int(user_id)
            except (ValueError, TypeError):
                return False
            try:
                client = await self._get_client(event)
                if client:
                    info = await client.call_action('get_group_member_info', group_id=group_id_int, user_id=user_id_int, no_cache=False)
                    if info:
                        role = info.get('role', '')
                        is_admin_val = role in ('admin', 'owner')
                        self._admin_role_cache[cache_key] = (is_admin_val, time.time())
                        return is_admin_val
            except Exception as e:
                logger.debug(f"[GroupMgr] 获取群成员信息失败: {e}")

        return False

    def _check_group_access(self, event: AstrMessageEvent) -> Tuple[bool, str]:
        group_id = self._get_group_id(event)
        if not group_id:
            return True, ""
        if self._group_black_set and group_id in self._group_black_set:
            return False, f"群 {group_id} 在黑名单中"
        if self._group_white_set:
            if group_id not in self._group_white_set:
                return False, f"群 {group_id} 不在白名单中"
        return True, ""

    async def _check_admin_cfg_access(self, event: AstrMessageEvent, cfg_key: str, feature_name: str, need_admin: bool = True) -> Tuple[bool, str]:
        if need_admin and not await self._is_admin(event):
            return False, "仅管理员可以使用此功能"
        ok, msg = self._cfg_check(cfg_key, feature_name)
        if not ok:
            return False, msg
        allowed, reason = self._check_group_access(event)
        if not allowed:
            return False, reason
        return True, ""

    async def _get_group_client(self, event: AstrMessageEvent, need_gid: bool = False) -> Tuple:
        group_id = self._get_group_id(event)
        if not group_id:
            return (None, None, None, "无法获取群号") if need_gid else (None, None, "无法获取群号")
        client = await self._get_client(event)
        if not client:
            return (None, None, None, "无法获取QQ客户端") if need_gid else (None, None, "无法获取QQ客户端")
        if need_gid:
            gid = self._safe_int(group_id, 0)
            if not gid:
                return None, None, None, "群号格式无效"
            return group_id, client, gid, ""
        return group_id, client, ""

    async def _call_group_api(self, client, action: str, result_name: str = "", **kwargs) -> Tuple[bool, str]:
        try:
            result = await client.call_action(action, **kwargs)
            ok, err = self._check_api_result(result, result_name or action)
            if not ok:
                return False, err
            return True, ""
        except Exception as e:
            return False, str(e)

    def _truncate(self, text: str, max_chars: int = 2000) -> str:
        if len(text) <= max_chars:
            return text
        suffix = f"\n...（已截断，原{len(text)}字符）"
        limit = max_chars - len(suffix)
        if limit <= 0:
            return text[:max_chars]
        return text[:limit] + suffix

    def _format_message_content(self, raw_message) -> str:
        if raw_message is None:
            return '[空消息]'
        if not isinstance(raw_message, list):
            return str(raw_message)
        parts = []
        for seg in raw_message:
            if not isinstance(seg, dict):
                parts.append(str(seg))
                continue
            t = seg.get('type', '')
            d = seg.get('data', {}) or {}
            if t == 'text':
                parts.append(d.get('text', ''))
            elif t == 'image':
                parts.append(d.get('summary', '[图片]') or '[图片]')
            elif t == 'at':
                parts.append(f"@{d.get('qq', '')}")
            elif t == 'reply':
                parts.append(f"[回复:{d.get('id', '')}]")
            elif t == 'face':
                parts.append("[表情]")
            elif t == 'market_face':
                parts.append("[商城表情]")
            elif t == 'forward':
                parts.append('[合并转发消息]')
            else:
                parts.append(f"[{t}]")
        return ''.join(parts) if parts else '[空消息]'

    def _invalidate_stats_cache(self):
        self._stats_cache["today_start"] = 0
        self._stats_cache["group_stats"] = {}
        self._stats_cache["user_stats"] = {}
        self._stats_cache.pop("user_names", None)

    def _log_moderation(self, group_id: str, user_id: str, user_name: str, msg_text: str, action: str, reason: str = "", image_urls: list = None):
        valid_urls = []
        if image_urls:
            for u in image_urls:
                if u:
                    valid_urls.append(u)
        log_entry = {
            "id": len(self._moderation_logs),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ts": int(time.time()),
            "group_id": group_id,
            "user_id": user_id,
            "user_name": user_name,
            "msg_text": msg_text,
            "msg_preview": msg_text[:100],
            "action": action,
            "reason": reason,
            "image_urls": valid_urls[:5],
        }
        self._moderation_logs.append(log_entry)
        today_start = self._today_start()
        sc = self._stats_cache
        if sc["today_start"] == today_start:
            sc["total"] += 1
            if user_id and user_name:
                un = sc.setdefault("user_names", {})
                un[user_id] = user_name
                if len(un) > 2000:
                    del_keys = list(un.keys())[:len(un) - 1500]
                    for k in del_keys:
                        del un[k]
            if "撤回" in action:
                sc["blocked"] += 1
                if group_id:
                    gs = sc["group_stats"]
                    gs[group_id] = gs.get(group_id, 0) + 1
                    if len(gs) > 500:
                        del_keys = sorted(gs, key=gs.get)[:len(gs) - 400]
                        for k in del_keys:
                            del gs[k]
                if user_id:
                    us = sc["user_stats"]
                    us[user_id] = us.get(user_id, 0) + 1
                    if len(us) > 2000:
                        del_keys = sorted(us, key=us.get)[:len(us) - 1500]
                        for k in del_keys:
                            del us[k]
            elif "放行" in action:
                sc["passed"] += 1
        now = time.time()
        if now - self._last_log_save >= 15.0:
            self._save_logs()
            self._last_log_save = now

    # ==================== LLM 群管工具 ====================
    @filter.llm_tool(name="ban_group_member")
    async def ban_group_member_tool(self, event: AstrMessageEvent, user_id: str, duration_minutes: int = 10):
        '''禁言群成员。当用户要求禁言某人时使用此工具。

        Args:
            user_id(string): 要禁言的用户QQ号
            duration_minutes(number): 禁言时长（分钟），默认10分钟
        '''
        ok, err = await self._check_admin_cfg_access(event, "ban_enabled", "禁言")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            duration_seconds = (min(max(duration_minutes, 1), 30 * 24 * 60) * 60)
            duration_seconds = (duration_seconds // 60) * 60
            ok, err = await self._call_group_api(client, 'set_group_ban', "禁言", group_id=gid, user_id=uid, duration=duration_seconds)
            if not ok:
                yield event.plain_result(f"禁言失败: {err}")
                return
            yield event.plain_result(f"已禁言 {user_id} {duration_minutes}分钟")
        except Exception as e:
            yield event.plain_result(f"禁言失败: {e}")

    @filter.llm_tool(name="unban_group_member")
    async def unban_group_member_tool(self, event: AstrMessageEvent, user_id: str):
        '''解除群成员禁言。当用户要求解除某人禁言时使用此工具。

        Args:
            user_id(string): 要解除禁言的用户QQ号
        '''
        ok, err = await self._check_admin_cfg_access(event, "unban_enabled", "解除禁言")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_ban', "解除禁言", group_id=gid, user_id=uid, duration=0)
            if not ok:
                yield event.plain_result(f"解除禁言失败: {err}")
                return
            yield event.plain_result(f"已解除 {user_id} 的禁言")
        except Exception as e:
            yield event.plain_result(f"解除禁言失败: {e}")

    @filter.llm_tool(name="kick_group_member")
    async def kick_group_member_tool(self, event: AstrMessageEvent, user_id: str):
        '''踢出群成员。当用户要求将某人踢出群时使用此工具。

        Args:
            user_id(string): 要踢出的用户QQ号
        '''
        ok, err = await self._check_admin_cfg_access(event, "kick_enabled", "踢人")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_kick', "踢人", group_id=gid, user_id=uid, reject_add_request=False)
            if not ok:
                yield event.plain_result(f"踢人失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 踢出群聊")
        except Exception as e:
            yield event.plain_result(f"踢人失败: {e}")

    @filter.llm_tool(name="set_whole_group_ban")
    async def set_whole_group_ban_tool(self, event: AstrMessageEvent, enable: bool = True):
        '''开启或关闭全体禁言。

        Args:
            enable(boolean): true开启全体禁言，false关闭全体禁言
        '''
        ok, err = await self._check_admin_cfg_access(event, "whole_ban_enabled", "全体禁言")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_whole_ban', "全体禁言", group_id=gid, enable=enable)
            if not ok:
                yield event.plain_result(f"设置全体禁言失败: {err}")
                return
            yield event.plain_result(f"已{'开启' if enable else '关闭'}全体禁言")
        except Exception as e:
            yield event.plain_result(f"设置全体禁言失败: {e}")

    @filter.llm_tool(name="set_member_card")
    async def set_member_card_tool(self, event: AstrMessageEvent, user_id: str, card: str):
        '''设置群成员群名片。

        Args:
            user_id(string): 目标用户QQ号
            card(string): 新的群名片
        '''
        ok, err = await self._check_admin_cfg_access(event, "set_card_enabled", "修改群名片")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_card', "设置群名片", group_id=gid, user_id=uid, card=card)
            if not ok:
                yield event.plain_result(f"设置群名片失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 的群名片设为: {card}")
        except Exception as e:
            yield event.plain_result(f"设置群名片失败: {e}")

    @filter.llm_tool(name="send_group_announcement")
    async def send_group_announcement_tool(self, event: AstrMessageEvent, content: str):
        '''发送群公告。

        Args:
            content(string): 公告内容
        '''
        ok, err = await self._check_admin_cfg_access(event, "send_announcement_enabled", "发送群公告")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, '_send_group_notice', "发送群公告", group_id=gid, content=content)
            if not ok:
                yield event.plain_result(f"发布公告失败: {err}")
                return
            yield event.plain_result("群公告已发布")
        except Exception as e:
            yield event.plain_result(f"发布公告失败: {e}")

    @filter.llm_tool(name="get_group_member_list")
    async def get_group_member_list_tool(self, event: AstrMessageEvent):
        '''获取群成员列表。'''
        ok, err = await self._check_admin_cfg_access(event, "member_list_enabled", "查看群成员列表")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
            if not members:
                yield event.plain_result("群成员列表为空")
                return
            member_texts = []
            for m in members[:30]:
                card = m.get("card", "")
                nickname = m.get("nickname", "")
                name = card if card else nickname
                role = m.get("role", "member")
                role_text = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role)
                member_texts.append(f"- {name}({m.get('user_id')}) [{role_text}]")
            yield event.plain_result(self._truncate(f"群成员（共{len(members)}人）：\n" + "\n".join(member_texts)))
        except Exception as e:
            yield event.plain_result(f"获取成员列表失败: {e}")

    @filter.llm_tool(name="set_group_admin")
    async def set_group_admin_tool(self, event: AstrMessageEvent, user_id: str, enable: bool = True):
        '''设置或取消群管理员。

        Args:
            user_id(string): 目标用户QQ号
            enable(boolean): true设为管理员，false取消管理员
        '''
        ok, err = await self._check_admin_cfg_access(event, "set_admin_enabled", "设置管理员")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_admin', "设置管理员", group_id=gid, user_id=uid, enable=enable)
            if not ok:
                yield event.plain_result(f"设置管理员失败: {err}")
                return
            yield event.plain_result(f"已{'设为' if enable else '取消'} {user_id} 的管理员")
        except Exception as e:
            yield event.plain_result(f"设置管理员失败: {e}")

    @filter.llm_tool(name="set_group_name")
    async def set_group_name_tool(self, event: AstrMessageEvent, group_name: str):
        '''修改群名称。

        Args:
            group_name(string): 新的群名称
        '''
        ok, err = await self._check_admin_cfg_access(event, "set_group_name_enabled", "修改群名称")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_name', "修改群名称", group_id=gid, group_name=group_name)
            if not ok:
                yield event.plain_result(f"改群名失败: {err}")
                return
            yield event.plain_result(f"群名已改为: {group_name}")
        except Exception as e:
            yield event.plain_result(f"改群名失败: {e}")

    @filter.llm_tool(name="set_member_title")
    async def set_member_title_tool(self, event: AstrMessageEvent, user_id: str, title: str):
        '''设置群成员专属头衔。

        Args:
            user_id(string): 目标用户QQ号
            title(string): 专属头衔
        '''
        ok, err = await self._check_admin_cfg_access(event, "set_title_enabled", "设置专属头衔")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_special_title', "设置头衔", group_id=gid, user_id=uid, special_title=title)
            if not ok:
                yield event.plain_result(f"设置头衔失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 的头衔设为: {title}")
        except Exception as e:
            yield event.plain_result(f"设置头衔失败: {e}")

    @filter.llm_tool(name="get_banned_members")
    async def get_banned_members_tool(self, event: AstrMessageEvent):
        '''获取群禁言列表。'''
        ok, err = await self._check_admin_cfg_access(event, "banned_list_enabled", "查看禁言列表")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_shut_list', group_id=gid)
            shut_list = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
            if not shut_list:
                yield event.plain_result("当前没有禁言成员")
                return
            member_texts = []
            for m in shut_list[:15]:
                uid = m.get("user_id", "")
                nickname = m.get("nickname", "")
                shut_time = self._safe_int(m.get("shut_up_timestamp", 0))
                if shut_time:
                    remain = max(0, shut_time - int(time.time()))
                    remain_str = f"{remain // 60}分{remain % 60}秒"
                else:
                    remain_str = "未知"
                member_texts.append(f"- {nickname}({uid}) 剩余: {remain_str}")
            yield event.plain_result(f"禁言列表（共{len(shut_list)}人）：\n" + "\n".join(member_texts))
        except Exception as e:
            yield event.plain_result(f"获取禁言列表失败: {e}")

    @filter.llm_tool(name="set_group_join_verify")
    async def set_group_join_verify_tool(self, event: AstrMessageEvent, verify_type: str = "allow"):
        '''设置群加群验证方式。

        Args:
            verify_type(string): 验证类型: allow(允许加入), deny(拒绝加入), need_verify(需要审核), not_allow(不允许)
        '''
        ok, err = await self._check_admin_cfg_access(event, "join_verify_enabled", "设置加群方式")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            group_id, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            type_map = {"allow": 2, "deny": 1, "need_verify": 3, "not_allow": 4}
            add_type = type_map.get(verify_type.lower(), 2)
            ok, err = await self._call_group_api(client, 'set_group_add_option', "设置加群方式", group_id=gid, add_type=add_type)
            if not ok:
                yield event.plain_result(f"设置加群方式失败: {err}")
                return
            type_text = {"allow": "允许加入", "deny": "拒绝加入", "need_verify": "需审核", "not_allow": "不允许"}.get(verify_type.lower(), verify_type)
            yield event.plain_result(f"加群方式已设为: {type_text}")
        except Exception as e:
            yield event.plain_result(f"设置加群方式失败: {e}")

    @filter.llm_tool(name="recall_message")
    async def recall_message_tool(self, event: AstrMessageEvent, message_id: str):
        '''撤回指定消息。

        Args:
            message_id(string): 要撤回的消息ID
        '''
        ok, err = await self._check_admin_cfg_access(event, "recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            group_id, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            mid = self._safe_int(message_id, 0)
            if not mid:
                yield event.plain_result("消息ID格式无效")
                return
            ok, err = await self._call_group_api(client, 'delete_msg', "撤回消息", message_id=mid)
            if not ok:
                yield event.plain_result(f"撤回失败: {err}")
                return
            yield event.plain_result(f"已撤回消息 {message_id}")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")

    @filter.llm_tool(name="set_essence_message")
    async def set_essence_message_tool(self, event: AstrMessageEvent, message_id: str):
        '''设置群精华消息。

        Args:
            message_id(string): 要设为精华的消息ID
        '''
        ok, err = await self._check_admin_cfg_access(event, "essence_enabled", "精华消息")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            mid = self._safe_int(message_id, 0)
            if not mid:
                yield event.plain_result("消息ID格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_essence_msg', "设精华", message_id=mid)
            if not ok:
                yield event.plain_result(f"设精华失败: {err}")
                return
            yield event.plain_result(f"已将 {message_id} 设为精华")
        except Exception as e:
            yield event.plain_result(f"设精华失败: {e}")

    @filter.llm_tool(name="delete_essence_message")
    async def delete_essence_message_tool(self, event: AstrMessageEvent, message_id: str):
        '''取消群精华消息。

        Args:
            message_id(string): 要取消精华的消息ID
        '''
        ok, err = await self._check_admin_cfg_access(event, "essence_enabled", "精华消息")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            mid = self._safe_int(message_id, 0)
            if not mid:
                yield event.plain_result("消息ID格式无效")
                return
            ok, err = await self._call_group_api(client, 'delete_essence_msg', "取消精华", message_id=mid)
            if not ok:
                yield event.plain_result(f"取消精华失败: {err}")
                return
            yield event.plain_result(f"已取消 {message_id} 的精华")
        except Exception as e:
            yield event.plain_result(f"取消精华失败: {e}")

    @filter.llm_tool(name="delete_group_notice")
    async def delete_group_notice_tool(self, event: AstrMessageEvent, notice_id: str):
        '''删除群公告。

        Args:
            notice_id(string): 公告ID
        '''
        ok, err = await self._check_admin_cfg_access(event, "delete_announcement_enabled", "删除群公告")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, '_del_group_notice', "删除公告", group_id=gid, notice_id=notice_id)
            if not ok:
                yield event.plain_result(f"删除公告失败: {err}")
                return
            yield event.plain_result(f"已删除公告 {notice_id}")
        except Exception as e:
            yield event.plain_result(f"删除公告失败: {e}")

    @filter.llm_tool(name="list_group_files")
    async def list_group_files_tool(self, event: AstrMessageEvent):
        '''查看群文件列表。'''
        ok, err = await self._check_admin_cfg_access(event, "group_files_enabled", "群文件")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            group_id, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_root_files', group_id=gid)
            files = (result.get('files') or []) if isinstance(result, dict) else []
            folders = (result.get('folders') or []) if isinstance(result, dict) else []
            if not files and not folders:
                yield event.plain_result("根目录下没有文件或文件夹")
                return
            lines = [f"群 {group_id} 根目录："]
            if folders:
                lines.append(f"  {len(folders)}个文件夹")
                for f in folders[:10]:
                    lines.append(f"    [{f.get('folder_id', '')}] {f.get('folder_name', '')}")
            if files:
                lines.append(f"  {len(files)}个文件")
                for f in files[:10]:
                    size_mb = self._safe_int(f.get('file_size', 0)) / (1024 * 1024)
                    lines.append(f"    [{f.get('file_id', '')}] {f.get('file_name', '')} ({size_mb:.1f}MB)")
            yield event.plain_result(self._truncate("\n".join(lines)))
        except Exception as e:
            yield event.plain_result(f"查文件失败: {e}")

    @filter.llm_tool(name="delete_group_file")
    async def delete_group_file_tool(self, event: AstrMessageEvent, file_id: str, busid: int = 102):
        '''删除群文件。

        Args:
            file_id(string): 文件ID
            busid(number): 文件类型ID，默认为102
        '''
        ok, err = await self._check_admin_cfg_access(event, "group_files_enabled", "群文件")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'delete_group_file', "删除文件", group_id=gid, file_id=file_id, busid=busid)
            if not ok:
                yield event.plain_result(f"删文件失败: {err}")
                return
            yield event.plain_result(f"已删除 {file_id}")
        except Exception as e:
            yield event.plain_result(f"删文件失败: {e}")

    @filter.llm_tool(name="get_group_notice_list")
    async def get_group_notice_list_tool(self, event: AstrMessageEvent):
        '''获取群公告列表。'''
        ok, err = await self._check_admin_cfg_access(event, "list_announcements_enabled", "查看公告列表")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('_get_group_notice', group_id=gid)
            notices = (result.get('data') or []) if isinstance(result, dict) else result
            if not notices:
                yield event.plain_result("暂无公告")
                return
            lines = [f"群公告（{len(notices)}条）"]
            for n in notices[:10]:
                notice_id = n.get('notice_id', '')
                sender_id = n.get('sender_id', '')
                _msg = n.get('msg')
                content = ((_msg.get('text', '') if isinstance(_msg, dict) else '') or n.get('content', ''))[:60]
                ts = n.get('publish_time', 0)
                t = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M') if ts else '未知'
                lines.append(f"  [{notice_id}] {content}... ({sender_id}, {t})")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取公告失败: {e}")

    @filter.llm_tool(name="upload_group_file")
    async def upload_group_file_tool(self, event: AstrMessageEvent, file_path: str, file_name: str = ""):
        '''上传文件到群文件。

        Args:
            file_path(string): 文件路径
            file_name(string): 上传后的文件名，可选
        '''
        ok, err = await self._check_admin_cfg_access(event, "group_files_enabled", "群文件")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            if not file_path or not os.path.exists(file_path):
                yield event.plain_result(f"文件不存在: {file_path}")
                return
            name = file_name or os.path.basename(file_path)
            result = await client.call_action('upload_group_file', group_id=gid, file=file_path, name=name)
            fid = result.get('file_id', '未知') if isinstance(result, dict) else '未知'
            yield event.plain_result(f"已上传，file_id: {fid}")
        except Exception as e:
            yield event.plain_result(f"上传失败: {e}")

    # ==================== LLM 审核 ====================
    async def _fetch_context_messages(self, group_id: str, current_msg_id: str, count: int = 30) -> list:
        if not self._client:
            return []
        client = self._client
        try:
            gid = int(group_id)
        except (ValueError, TypeError):
            return []
        try:
            result = await client.call_action('get_group_msg_history',
                group_id=gid, message_seq=0, count=min(count + 5, 100))
            messages = result.get('messages', []) if isinstance(result, dict) else []
            return [m for m in messages if str(m.get('message_id', '')) != str(current_msg_id)][-count:]
        except Exception:
            return []

    def _extract_llm_text(self, response) -> str:
        if hasattr(response, 'completion_text'):
            return response.completion_text
        return str(response)

    async def _call_llm_safe(self, system_prompt: str, prompt: str) -> str:
        configured_id = str(self.config.get("moderation_llm_provider_id", "")).strip()
        errors = []
        error_set = set()

        def _add_error(err: str):
            if err not in error_set:
                error_set.add(err)
                errors.append(err)

        async def _try_text_chat(prov, pid: str) -> str:
            if not hasattr(prov, 'text_chat'):
                return None
            signatures = [
                ((), {'system_prompt': system_prompt, 'prompt': prompt}),
                ((system_prompt + "\n\n" + prompt,), {}),
            ]
            for args, kwargs in signatures:
                try:
                    r = await prov.text_chat(*args, **kwargs)
                    if r:
                        return str(r)
                except (TypeError, ValueError):
                    continue
                except Exception as e:
                    _add_error(f"{pid}.text_chat: {str(e)[:120]}")
                    continue
            return None

        async def _try_provider(prov, pid: str) -> str:
            result = await _try_text_chat(prov, pid)
            if result:
                return result
            for meth in ('chat', 'invoke', 'complete'):
                fn = getattr(prov, meth, None)
                if not fn:
                    continue
                signatures = [
                    ((system_prompt + "\n\n" + prompt,), {}),
                    ((), {'prompt': system_prompt + "\n\n" + prompt}),
                ]
                for args, kwargs in signatures:
                    try:
                        r = await fn(*args, **kwargs)
                        if r:
                            return str(r)
                    except (TypeError, ValueError):
                        continue
                    except Exception as e:
                        _add_error(f"{pid}.{meth}: {str(e)[:120]}")
                        continue
            return None

        async def _try_by_id(pid: str) -> str:
            if hasattr(self.context, 'llm_generate'):
                try:
                    resp = await self.context.llm_generate(
                        chat_provider_id=pid, prompt=prompt, system_prompt=system_prompt)
                    if resp:
                        return self._extract_llm_text(resp)
                except Exception as e:
                    _add_error(f"llm_generate({pid}): {str(e)[:120]}")
            prov = self.context.get_provider_by_id(pid) if hasattr(self.context, 'get_provider_by_id') else None
            if prov:
                result = await _try_provider(prov, pid)
                if result:
                    return result
            raise RuntimeError(f"Provider {pid} 不可用")

        if configured_id:
            try:
                result = await _try_by_id(configured_id)
                logger.info(f"[GroupMgr] LLM审核使用指定provider: {configured_id}")
                return result
            except Exception as e:
                _add_error(f"指定{configured_id}: {str(e)[:120]}")

        try:
            ps = (self.context.get_all_providers() if hasattr(self.context, 'get_all_providers') else []) or []
        except Exception as e:
            ps = []
            _add_error(f"get_all_providers: {str(e)[:120]}")

        for p in ps:
            try:
                meta = p.meta()
                pid = meta.id
                result = await _try_by_id(pid)
                logger.info(f"[GroupMgr] LLM审核使用provider: {pid}")
                return result
            except Exception as e:
                _add_error(str(e)[:80])
                continue

        try:
            pm = getattr(self.context, 'provider_manager', None)
            if pm and hasattr(pm, 'get_using_provider'):
                up = pm.get_using_provider()
                if up:
                    result = await _try_provider(up, str(getattr(up, 'provider_name', up)))
                    if result:
                        logger.info("[GroupMgr] LLM审核使用provider_manager")
                        return result
        except Exception as e:
            _add_error(f"provider_manager: {str(e)[:120]}")

        detail = '; '.join(errors[:5]) if errors else '无任何可用Provider'
        raise RuntimeError(f"LLM调用失败({detail})。请检查AstrBot是否已配置LLM Provider")

    async def _call_llm_for_moderation(self, event: AiocqhttpMessageEvent,
                                        text: str, hit_types: Dict[str, bool],
                                        group_id: str = "") -> dict:
        if not group_id:
            group_id = self._get_group_id(event)
        msg_obj = getattr(event, 'message_obj', None)
        msg_id = str(getattr(msg_obj, 'message_id', '')) if msg_obj else ''
        user_name = event.get_sender_name()
        context_msgs = []
        if group_id and msg_id:
            context_msgs = await self._fetch_context_messages(group_id, msg_id, 30)
        context_text = ""
        if context_msgs:
            lines = []
            for m in context_msgs:
                sender_obj = m.get('sender')
                sender = sender_obj.get('nickname', '未知') if isinstance(sender_obj, dict) else '未知'
                content = self._format_message_content(m.get('message', ''))
                if len(content) > 200:
                    content = content[:200] + '...'
                lines.append(f"  {sender}: {content}")
            context_text = "\n".join(lines)
            if len(context_text) > 3000:
                context_text = context_text[-3000:]
        suspect_types = [k for k, v in hit_types.items() if v]
        suspect_tag = "+".join(suspect_types) if suspect_types else "无"
        type_desc = {
            "swear": "骂人/脏话",
            "ad": "广告/推广",
            "political": "政治敏感",
            "porn": "色情/淫秽",
            "violent_terror": "暴恐内容",
            "reactionary": "反动言论",
            "weapons": "涉枪涉爆",
            "corruption": "贪腐相关",
            "illegal_url": "违规网址",
            "other": "其他违规",
            "supplement": "补充违规",
            "livelihood": "民生敏感",
            "tencent_ban": "腾讯封禁",
        }
        suspect_desc = "+".join([type_desc.get(t, t) for t in suspect_types]) if suspect_types else "无"
        prompt = (
            f"你是一名群聊内容审核员。你的任务是判断被标记消息是否需要撤回，需要结合上下文语境合理判断。\n\n"
            f"【核心准则】\n"
            f"- 侮辱性脏话（傻逼、废物、脑残、操你妈等）对任何对象使用都应撤回，包括对机器人\n"
            f"- 广告内容零容忍，一律撤回\n"
            f"- 政治敏感词库误报率高，需结合上下文判断，技术/游戏讨论不违规\n"
            f"- 色情/暴恐等需结合上下文判断\n"
            f"- 涉及查询、泄露他人隐私信息（身份证、住址、电话等）→ 违规\n\n"
            f"【审核标准】\n"
            f"1. 骂人/脏话类（swear）—— 严格处理侮辱性词汇：\n"
            f"     * 使用侮辱性脏话（傻逼、废物、蠢货、脑残、智障等）\n"
            f"     * 涉及家人死亡的诅咒（\"你妈死了\"、\"死全家\"、\"nmsl\"等）\n"
            f"     * 极端恶意人身攻击，明显带有仇恨和恶意\n"
            f"     * 对任何对象使用\"傻逼\"、\"操你妈\"、\"废物\"等侮辱性词汇\n"
            f"     * 对机器人/AI使用侮辱性脏话（\"傻逼机器人\"、\"废物机器人\"等）\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 轻微口头禅（\"卧槽\"、\"我靠\"、\"牛逼\"等不含侮辱性的语气词）\n"
            f"     * 自嘲、自黑（\"我太菜了\"、\"我真是个憨憨\"等）\n"
            f"     * 游戏中的轻度调侃（\"垃圾队友\"、\"这打得真烂\"等游戏场景）\n\n"
            f"2. 广告类（ad）—— 零容忍，一律违规：\n"
            f"   - 任何推广引流行为 → 违规（加微信、扫码、兼职、赚钱、收徒、挂圈等）\n"
            f"   - 色情引流（\"18+进xxx\"、\"看片加Q\"、\"福利群\"等）→ 违规\n"
            f"   - 金融诈骗（开户、跑分、洗钱、赌博等）→ 违规\n"
            f"   - 商品推销、代购、微商 → 违规\n"
            f"   - 任何包含联系方式（QQ号、微信号、手机号）的推广内容 → 违规\n"
            f"   - 只有纯粹的资源分享（如\"推荐一部电影\"）且无任何引流意图 → 不违规\n\n"
            f"3. 色情类（porn）—— 识别真正的色情内容：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 明确的色情内容、招嫖信息\n"
            f"     * 发送色情图片/视频/链接\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 暧昧玩笑、两性话题讨论（只要不过于露骨）\n"
            f"     * 恋爱话题、情感倾诉\n\n"
            f"4. 暴恐/涉枪涉爆/贪腐类：\n"
            f"   - 明确的违法内容 → 违规\n"
            f"   - 游戏/影视/新闻讨论 → 不违规\n\n"
            f"5. 政治敏感类（political）—— 注意：该词库误报率很高，需严格区分：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 明确的颠覆国家政权言论（\"推翻政府\"、\"颠覆政权\"等）\n"
            f"     * 直接侮辱国家领导人（不是讨论政策，而是人身攻击）\n"
            f"     * 明确煽动分裂国家的言论\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 正常政治讨论、新闻评论\n"
            f"     * 游戏、影视中的政治元素讨论\n"
            f"     * 历史人物/事件的正常讨论\n\n"
            f"6. 违规网址类（illegal_url）—— 注意：误报率高：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 赌博、色情、诈骗网站\n"
            f"     * 恶意软件下载链接\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 正常游戏攻略、教程链接\n"
            f"     * 视频网站链接（B站、YouTube等）\n"
            f"     * 工具软件官网\n\n"
            f"7. 隐私泄露类：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 泄露他人身份证号、住址、电话\n"
            f"     * 人肉搜索、开盒行为\n"
            f"     * 公开他人私人信息\n\n"
            f"请严格按照以下JSON格式返回，不要返回其他内容：\n"
            f'{{"violation": true/false, "reason": "判断原因"}}\n\n'
            f"【被标记消息】\n"
            f"发送者: {user_name}\n"
            f"内容: {text}\n"
            f"可疑类型: {suspect_desc} ({suspect_tag})\n\n"
            f"【上下文消息】\n"
            f"{context_text}\n"
        )
        system_prompt = (
            "你是一名群聊内容审核员。你的任务是判断被标记消息是否需要撤回。"
            "请结合上下文语境合理判断。返回严格的JSON格式。"
        )
        try:
            async with self._llm_semaphore:
                llm_response = await self._call_llm_safe(system_prompt, prompt)
            json_match = re.search(r'\{[^{}]*"violation"[^{}]*\}', llm_response, re.DOTALL)
            if not json_match:
                json_match = re.search(r'\{.*\}', llm_response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                return result
            else:
                logger.warning(f"[GroupMgr] LLM返回非JSON格式: {llm_response[:200]}")
                return {"violation": False, "reason": "LLM返回格式异常"}
        except json.JSONDecodeError as e:
            logger.warning(f"[GroupMgr] LLM返回JSON解析失败: {e}")
            return {"violation": False, "reason": "JSON解析失败"}
        except Exception as e:
            logger.warning(f"[GroupMgr] LLM审核调用失败: {e}")
            return {"violation": False, "reason": f"LLM调用失败: {str(e)[:100]}"}

    def _is_ad_pattern(self, text: str) -> bool:
        if not text:
            return False
        return any(p.search(text) for p in self._compiled_ad)

    def _should_scan_message(self, event: AiocqhttpMessageEvent) -> bool:
        sub_type = ''
        raw = getattr(event, 'raw_event', None)
        if isinstance(raw, dict):
            sub_type = str(raw.get('sub_type', '')).lower()
        if sub_type in ('anonymous', 'notice'):
            return False
        chain = event.get_messages()
        for seg in (chain or []):
            if isinstance(seg, dict):
                seg_type = seg.get('type', '')
                if seg_type == 'text' and seg.get('data', {}).get('text', '').strip():
                    return True
                if seg_type == 'forward':
                    return True
                if seg_type == 'image':
                    return True
                if seg_type == 'market_face':
                    return True
            else:
                seg_cls = type(seg).__name__
                if seg_cls == 'Plain' and getattr(seg, 'text', '').strip():
                    return True
                if seg_cls == 'Forward' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'forward'):
                    return True
                if seg_cls == 'Image' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'image'):
                    return True
                if seg_cls == 'MarketFace' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'market_face'):
                    return True
                if seg_cls == 'Json' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'json'):
                    return True
                if seg_cls == 'App' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'app'):
                    return True
        return False

    async def _resolve_forward_messages(self, event: AiocqhttpMessageEvent) -> Tuple[str, bool]:
        client = await self._get_client(event)
        if not client:
            return "", False
        chain = event.get_messages() or []
        forward_ids = []
        for seg in chain:
            if isinstance(seg, dict) and seg.get('type') == 'forward':
                fid = seg.get('data', {}).get('id', '')
                if fid:
                    forward_ids.append(fid)
            else:
                seg_cls = type(seg).__name__
                if seg_cls == 'Forward' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'forward'):
                    fid = ''
                    if hasattr(seg, 'id'):
                        fid = getattr(seg, 'id', '')
                    elif hasattr(seg, 'data') and isinstance(getattr(seg, 'data', None), dict):
                        fid = getattr(seg, 'data', {}).get('id', '')
                    if fid:
                        forward_ids.append(fid)
        if not forward_ids:
            return "", False
        all_texts = []
        is_qq_favorite = False
        for fid in forward_ids:
            try:
                result = await client.call_action('get_forward_msg', message_id=fid)
                if not isinstance(result, dict):
                    continue
                messages = result.get('messages', []) or result.get('message', [])
                if isinstance(messages, dict):
                    messages = messages.get('message', [])
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    sender = msg.get('sender', {})
                    nickname = sender.get('nickname', '未知') if isinstance(sender, dict) else '未知'
                    card = sender.get('card', '') if isinstance(sender, dict) else ''
                    if 'QQ收藏' in nickname or 'QQ收藏' in card or 'qq收藏' in nickname.lower() or 'qq收藏' in card.lower():
                        is_qq_favorite = True
                    content = msg.get('message', '')
                    if isinstance(content, list):
                        parts = []
                        for c_seg in content:
                            if isinstance(c_seg, dict):
                                ct = c_seg.get('type', '')
                                cd = c_seg.get('data', {}) or {}
                                if ct == 'text':
                                    parts.append(cd.get('text', ''))
                                elif ct == 'image':
                                    parts.append('[图片]')
                                elif ct == 'forward':
                                    parts.append('[嵌套转发]')
                                elif ct == 'app':
                                    app_content = cd.get('content', '')
                                    if isinstance(app_content, str) and ('QQ收藏' in app_content or 'qq收藏' in app_content.lower()):
                                        is_qq_favorite = True
                                    parts.append(f'[{ct}]')
                                else:
                                    parts.append(f'[{ct}]')
                            else:
                                parts.append(str(c_seg))
                        content_text = ''.join(parts)
                    else:
                        content_text = str(content)
                        if isinstance(content, str) and ('QQ收藏' in content or 'qq收藏' in content.lower()):
                            is_qq_favorite = True
                    if content_text.strip():
                        all_texts.append(f"[转发]{nickname}: {content_text.strip()}")
            except Exception as e:
                logger.debug(f"[GroupMgr] 获取转发消息内容失败: {e}")
                all_texts.append("[转发消息获取失败]")
        return '\n'.join(all_texts), is_qq_favorite

    @staticmethod
    def _is_qq_favorite_text(text: str) -> bool:
        if not isinstance(text, str):
            return False
        return 'QQ收藏' in text or 'qq收藏' in text.lower() or 'sharechain.qq.com' in text

    @staticmethod
    def _check_dict_seg_qq_favorite(seg: dict) -> bool:
        if not isinstance(seg, dict):
            return False
        seg_type = seg.get('type', '')
        seg_data = seg.get('data', {}) or {}
        if seg_type == 'json':
            return Main._is_qq_favorite_text(seg_data.get('data', ''))
        if seg_type == 'app':
            return Main._is_qq_favorite_text(seg_data.get('content', ''))
        return False

    async def _check_qq_favorite_non_forward(self, event: AiocqhttpMessageEvent) -> bool:
        raw = getattr(event, 'raw_event', None)
        chain = event.get_messages() or []
        if isinstance(raw, dict):
            msg_list = raw.get('message', [])
            if isinstance(msg_list, list):
                for seg in msg_list:
                    if self._check_dict_seg_qq_favorite(seg):
                        return True
        for seg in chain:
            if isinstance(seg, dict):
                continue
            seg_cls = type(seg).__name__
            if seg_cls in ('Json',) or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'json'):
                json_data = getattr(seg, 'data', '') or ''
                if self._is_qq_favorite_text(json_data):
                    return True
                if isinstance(json_data, dict) and self._is_qq_favorite_text(str(json_data)):
                    return True
            elif seg_cls in ('App',) or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'app'):
                if self._is_qq_favorite_text(getattr(seg, 'content', '')):
                    return True
        return False

    _OCR_PROMPT_TEMPLATES = {
        "default": {
            "system": "你是一个图片内容识别助手。请仔细观察图片，用文字详细描述图片中的所有内容。如果图片中有文字，请完整转录所有文字内容。如果图片是广告、推广、违规内容，请特别说明。只输出图片内容描述，不要输出其他内容。",
            "prompt": "请识别并描述这张图片的内容，特别注意图片中的文字。"
        },
        "strict": {
            "system": "你是一个严格的内容审核图片识别助手。你的任务是识别图片中是否存在违规内容。请仔细检查：1.图片中是否有广告推广信息（联系方式、二维码、引流链接）2.是否有色情或低俗内容 3.是否有政治敏感内容 4.是否有暴恐或违法信息 5.是否有赌博或诈骗信息。如果图片中有文字，请完整转录。最后给出明确结论：该图片是否包含违规内容。",
            "prompt": "请严格审核这张图片，识别并描述所有可能违规的内容，完整转录图片中的文字。"
        },
        "text_only": {
            "system": "你是一个OCR文字识别助手。请将图片中的所有文字完整转录出来，保持原始格式和排版。如果图片中没有文字，请回复「图片中无文字」。只输出识别到的文字内容，不要添加任何分析或评论。",
            "prompt": "请将这张图片中的所有文字完整转录出来。"
        }
    }

    @staticmethod
    def _is_gif_url(url: str) -> bool:
        if not url:
            return False
        lower = url.lower()
        if lower.endswith('.gif'):
            return True
        if '.gif?' in lower or '.gif;' in lower:
            return True
        return False

    @staticmethod
    def _is_sticker_image(url: str) -> bool:
        if not url:
            return False
        lower = url.lower()
        sticker_markers = ['sticker', 'emoji', 'marketface', 'emoticon']
        if any(m in lower for m in sticker_markers):
            return True
        if '/face/' in lower or '/face?' in lower or '&face=' in lower or '?face=' in lower:
            return True
        return False

    async def _ocr_images(self, event: AiocqhttpMessageEvent, image_urls: list) -> str:
        if not image_urls:
            return ""
        all_ocr_texts = []
        for img_url in image_urls[:3]:
            try:
                is_gif = self._is_gif_url(img_url)
                is_sticker = self._is_sticker_image(img_url)
                ocr_text = await self._call_llm_ocr(img_url, is_gif=is_gif, is_sticker=is_sticker)
                if ocr_text and ocr_text.strip():
                    prefix = ""
                    if is_gif:
                        prefix = "[GIF动图] "
                    elif is_sticker:
                        prefix = "[表情包] "
                    all_ocr_texts.append(prefix + ocr_text.strip())
            except Exception as e:
                logger.debug(f"[GroupMgr] OCR识别失败: {e}")
        return '\n'.join(all_ocr_texts)

    async def _call_llm_ocr(self, image_url: str, is_gif: bool = False, is_sticker: bool = False) -> str:
        configured_id = str(self.config.get("ocr_provider_id", "")).strip()
        if not configured_id:
            return ""

        template_key = str(self.config.get("ocr_prompt_template", "default")).strip()
        custom_system = str(self.config.get("ocr_custom_system_prompt", "")).strip()
        custom_user = str(self.config.get("ocr_custom_user_prompt", "")).strip()

        if custom_system and custom_user:
            system_prompt = custom_system
            prompt = custom_user
        else:
            template = self._OCR_PROMPT_TEMPLATES.get(template_key, self._OCR_PROMPT_TEMPLATES["default"])
            system_prompt = template["system"]
            prompt = template["prompt"]

        if is_gif:
            prompt += "\n注意：这是一张GIF动图，可能包含多帧内容。请仔细观察每一帧，描述所有帧中出现的内容和文字，特别关注是否有违规内容在动画帧中出现。"
        elif is_sticker:
            prompt += "\n注意：这是一个表情包/贴纸图片。表情包中常包含文字，请完整转录表情包中的所有文字，并判断文字内容是否违规（如侮辱性脏话、广告推广等）。"

        try:
            if hasattr(self.context, 'llm_generate'):
                kwargs = {
                    'prompt': prompt,
                    'system_prompt': system_prompt,
                    'image_urls': [image_url],
                    'chat_provider_id': configured_id,
                }
                try:
                    resp = await self.context.llm_generate(**kwargs)
                    if resp:
                        return self._extract_llm_text(resp)
                except TypeError:
                    pass

            if hasattr(self.context, 'get_provider_by_id'):
                prov = self.context.get_provider_by_id(configured_id)
                if prov and hasattr(prov, 'text_chat'):
                    try:
                        r = await prov.text_chat(
                            system_prompt=system_prompt,
                            prompt=prompt,
                            image_urls=[image_url],
                        )
                        if r:
                            return str(r)
                    except TypeError:
                        pass
                    try:
                        r = await prov.text_chat(
                            system_prompt + "\n\n图片URL: " + image_url + "\n\n" + prompt,
                        )
                        if r:
                            return str(r)
                    except Exception as _e:
                        logger.debug(f"[GroupMgr] OCR LLM单次调用失败: {_e}")

            return ""
        except Exception as e:
            logger.debug(f"[GroupMgr] OCR LLM调用失败: {e}")
            return ""

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def _handle_message(self, event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            return
        if self._group_black_set and group_id in self._group_black_set:
            return
        if self._group_white_set and group_id not in self._group_white_set:
            return
        if not self._should_scan_message(event):
            return
        if not self._cfg("enabled"):
            return
        if not self.config.get("disclaimer_agreed", False):
            return
        if await self._is_admin(event):
            return
        if self._user_black_set:
            user_id = self._try_get_sender_id(event)
            if user_id and user_id in self._user_black_set:
                try:
                    await self._kick_member(event)
                    await self._mute_member(event, 60)
                    notice = self.config.get("ban_notice", "[群管] {name}({uid}) 已被踢出（黑名单）")
                    yield event.plain_result(notice.replace("{name}", event.get_sender_name()).replace("{uid}", user_id).replace("{group}", group_id))
                    event.stop_event()
                except Exception as e:
                    logger.warning(f"[GroupMgr] 黑名单执行出错: {e}")
                return
        chain = event.get_messages()
        raw_text_parts = []
        image_urls = []
        has_forward = False
        for seg in (chain or []):
            if isinstance(seg, dict):
                seg_type = seg.get('type', '')
                seg_data = seg.get('data', {}) or {}
                if seg_type == 'text':
                    raw_text_parts.append(seg_data.get('text', ''))
                elif seg_type == 'forward':
                    has_forward = True
                elif seg_type == 'image':
                    img_url = seg_data.get('url', '') or seg_data.get('file', '')
                    if img_url:
                        image_urls.append(img_url)
                elif seg_type == 'market_face':
                    mf_url = seg_data.get('url', '') or ''
                    if mf_url:
                        image_urls.append(mf_url)
            else:
                seg_cls = type(seg).__name__
                if seg_cls == 'Plain' or hasattr(seg, 'text'):
                    raw_text_parts.append(getattr(seg, 'text', '') or '')
                elif seg_cls == 'Forward' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'forward'):
                    has_forward = True
                elif seg_cls == 'Image' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'image'):
                    img_url = getattr(seg, 'url', '') or getattr(seg, 'file', '') or ''
                    if not img_url and hasattr(seg, 'data'):
                        seg_data = getattr(seg, 'data', {})
                        if isinstance(seg_data, dict):
                            img_url = seg_data.get('url', '') or seg_data.get('file', '')
                    if img_url:
                        image_urls.append(img_url)
                    else:
                        logger.debug(f"[GroupMgr] Image段无URL: {seg}")
                elif seg_cls == 'MarketFace' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'market_face'):
                    mf_url = getattr(seg, 'url', '') or ''
                    if not mf_url and hasattr(seg, 'data'):
                        seg_data = getattr(seg, 'data', {})
                        if isinstance(seg_data, dict):
                            mf_url = seg_data.get('url', '') or ''
                    if mf_url:
                        image_urls.append(mf_url)
        text = ''.join(raw_text_parts).strip()

        forward_text = ""
        forward_is_qq_favorite = False
        if has_forward:
            forward_text, forward_is_qq_favorite = await self._resolve_forward_messages(event)
            scan_forward = self._cfg("scan_forward_msg", True)
            if forward_text and scan_forward:
                if text:
                    text = text + '\n' + forward_text
                else:
                    text = forward_text

        if self._cfg("recall_qq_favorite_enabled", True):
            is_qq_fav = forward_is_qq_favorite or await self._check_qq_favorite_non_forward(event)
            if is_qq_fav:
                try:
                    msg_id = str(getattr(getattr(event, 'message_obj', None), 'message_id', ''))
                    if msg_id:
                        await self._recall_msg(event, msg_id)
                        user_name = event.get_sender_name()
                        user_id = self._try_get_sender_id(event)
                        yield event.plain_result(f"[群管] 检测到QQ收藏内容，已自动撤回")
                        self._log_moderation(group_id, user_id, user_name, "[QQ收藏消息]", "撤回", "QQ收藏内容自动撤回", image_urls)
                        event.stop_event()
                except Exception as e:
                    logger.warning(f"[GroupMgr] QQ收藏撤回失败: {e}")
                return
        if not self.auto_moderate_enabled:
            return

        if image_urls and self._cfg("ocr_enabled", False):
            ocr_urls = image_urls
            if not self._cfg("scan_sticker_enabled", True):
                ocr_urls = [u for u in image_urls if not self._is_sticker_image(u)]
            if ocr_urls:
                logger.info(f"[GroupMgr] OCR开始识别 {len(ocr_urls)} 张图片")
                ocr_text = await self._ocr_images(event, ocr_urls)
                if ocr_text:
                    if text:
                        text = text + '\n[OCR识图内容]\n' + ocr_text
                    else:
                        text = '[OCR识图内容]\n' + ocr_text
                    logger.info(f"[GroupMgr] OCR识别结果: {ocr_text[:100]}")
                else:
                    logger.debug(f"[GroupMgr] OCR识别返回空结果")

        if not text:
            if image_urls:
                logger.debug(f"[GroupMgr] 图片消息无文字且OCR未生效，跳过审核")
            return
        if len(text) > 5000:
            text = text[:5000]
        user_id = self._try_get_sender_id(event)
        user_name = event.get_sender_name()

        hit_types = {
            "swear": False,
            "ad": False,
            "political": False,
            "porn": False,
            "violent_terror": False,
            "reactionary": False,
            "weapons": False,
            "corruption": False,
            "illegal_url": False,
            "other": False,
        }

        swear_hit = False
        if self._cfg("scan_swear", True):
            for p in self._compiled_swear:
                m = p.search(text)
                if m:
                    logger.info(f"[GroupMgr] 正则脏话命中: {m.group()}")
                    swear_hit = True
                    break
        hit_types["swear"] = swear_hit

        ad_hit = False
        if self._cfg("scan_ad", True):
            ad_hit = self._is_ad_pattern(text)
        hit_types["ad"] = ad_hit

        lexicon_result = self._check_lexicon(text)
        for cat, hit in lexicon_result.items():
            if cat in hit_types:
                hit_types[cat] = hit

        should_check = any(hit_types.values())
        if not should_check:
            return

        if not self._cfg("llm_moderation_enabled", True):
            reason = "触发规则: " + ", ".join(k for k, v in hit_types.items() if v)
            logger.info(f"[GroupMgr] {user_name}({user_id}) in {group_id} -> {reason}")
            try:
                msg_id = str(getattr(getattr(event, 'message_obj', None), 'message_id', ''))
                await self._recall_msg(event, msg_id)
                await self._mute_member(event)
                notice = self.config.get("ban_notice", "[群管] {name}({uid}) 已被禁言（触发规则）")
                yield event.plain_result(notice.replace("{name}", user_name).replace("{uid}", user_id).replace("{group}", group_id))
                self._log_moderation(group_id, user_id, user_name, text, "撤回+禁言", reason, image_urls)
                event.stop_event()
            except Exception as e:
                logger.warning(f"[GroupMgr] 自动审核出错: {e}")
            return

        llm_result = await self._call_llm_for_moderation(event, text, hit_types, group_id=group_id)
        is_violation = llm_result.get("violation", False)
        reason = llm_result.get("reason", "无理由")

        if not is_violation:
            logger.info(f"[GroupMgr] LLM审核通过: {user_name}({user_id}) in {group_id} | 命中类型={{{', '.join(k for k, v in hit_types.items() if v)}}} | 原因={reason}")
            self._log_moderation(group_id, user_id, user_name, text, "LLM放行", reason, image_urls)
            return

        logger.info(f"[GroupMgr] LLM审核拦截: {user_name}({user_id}) in {group_id} | 命中类型={{{', '.join(k for k, v in hit_types.items() if v)}}} | 原因={reason}")

        try:
            msg_id = str(getattr(getattr(event, 'message_obj', None), 'message_id', ''))
            if msg_id:
                try:
                    await self._recall_msg(event, msg_id)
                except Exception as recall_err:
                    logger.warning(f"[GroupMgr] 撤回消息失败: {recall_err}")

            if self._cfg("llm_moderation_ban", True):
                try:
                    await self._mute_member(event)
                except Exception as ban_err:
                    logger.warning(f"[GroupMgr] 禁言失败: {ban_err}")

            if self._cfg("auto_moderate_notice", True):
                try:
                    notice = self.config.get("ban_notice", "[群管] {name}({uid}) 的消息已被撤回（违规内容）")
                    yield event.plain_result(notice.replace("{name}", user_name).replace("{uid}", user_id).replace("{group}", group_id))
                except Exception as notice_err:
                    logger.warning(f"[GroupMgr] 发送通知失败: {notice_err}")

            self._log_moderation(group_id, user_id, user_name, text, "LLM撤回", reason, image_urls)
            event.stop_event()
        except Exception as e:
            logger.warning(f"[GroupMgr] 自动审核出错: {e}")
            yield event.plain_result(f"[群管] 审核出错: {str(e)[:100]}")

    async def _recall_msg(self, event: AiocqhttpMessageEvent, msg_id: str):
        mid = self._safe_int(msg_id)
        if not mid:
            return
        client = await self._get_client(event)
        if not client:
            return
        try:
            await client.call_action('delete_msg', message_id=mid)
        except Exception as e:
            logger.warning(f"[GroupMgr] 撤回消息失败: {e}")
            self._client = None

    async def _kick_member(self, event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        user_id = self._try_get_sender_id(event)
        if not group_id or not user_id:
            return
        client = await self._get_client(event)
        if not client:
            return
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return
        try:
            await client.call_action('set_group_kick', group_id=gid, user_id=uid)
        except Exception as e:
            logger.warning(f"[GroupMgr] 踢人失败: {e}")
            self._client = None

    async def _mute_member(self, event: AiocqhttpMessageEvent, duration: int = None):
        group_id = self._get_group_id(event)
        user_id = self._try_get_sender_id(event)
        if not group_id or not user_id:
            return
        client = await self._get_client(event)
        if not client:
            return
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return
        ban_duration = duration if duration is not None else int(self.config.get("moderation_ban_duration", 1800))
        try:
            await client.call_action('set_group_ban', group_id=gid, user_id=uid, duration=ban_duration)
        except Exception as e:
            logger.warning(f"[GroupMgr] 禁言失败: {e}")
            self._client = None

    @filter.command("字数统计")
    async def word_count(self, event: AstrMessageEvent):
        '''统计群内关键词出现次数'''
        ok, err = await self._check_admin_cfg_access(event, "word_count_enabled", "字数统计", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /字数统计 <关键词> [天数] [类型]\n类型: 脏话/广告/敏感词/黑名单\n示例: /字数统计 傻逼 7 脏话")
            return
        keyword = args[1]
        days = 7
        search_type = "all"
        type_map = {"脏话": "swear", "广告": "ad", "敏感词": "sensitive", "黑名单": "black"}
        if len(args) >= 3:
            try:
                days = int(args[2])
            except ValueError:
                search_type = type_map.get(args[2], args[2].lower())
        if len(args) >= 4:
            search_type = type_map.get(args[3], args[3].lower())
        days = max(1, min(days, 90))
        try:
            group_id, client, _, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            count, sample_messages = await self._search_keyword_in_messages(event, group_id, keyword, days, search_type)
            if count == 0:
                yield event.plain_result(f"最近 {days} 天内未找到包含「{keyword}」的消息")
            else:
                result = f"最近 {days} 天内「{keyword}」出现次数: {count}\n"
                if sample_messages:
                    result += "\n最近消息:\n"
                    for msg in sample_messages[:5]:
                        result += f"  {msg}\n"
                yield event.plain_result(result)
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")

    async def _search_keyword_in_messages(self, event: AstrMessageEvent, group_id: str, keyword: str, days: int, search_type: str = "all") -> Tuple[int, list]:
        client = await self._get_client(event)
        if not client:
            return 0, []
        try:
            result = await client.call_action('get_group_msg_history', group_id=self._safe_int(group_id, 0), count=100)
            messages = result.get('messages', []) if isinstance(result, dict) else []
        except Exception as e:
            logger.warning(f"[GroupMgr] 获取历史消息失败: {e}")
            return 0, []
        now = int(time.time())
        cutoff = now - days * 24 * 3600
        count = 0
        sample_messages = []
        for msg in messages:
            try:
                msg_time = msg.get('time', 0)
                if msg_time < cutoff:
                    continue
                raw_message = msg.get('message', '')
                text = self._format_message_content(raw_message)
                if keyword.lower() in text.lower():
                    if search_type != "all":
                        is_match = False
                        if search_type == "swear":
                            is_match = any(p.search(text) for p in self._compiled_swear)
                        elif search_type == "ad":
                            is_match = self._is_ad_pattern(text)
                        elif search_type == "sensitive":
                            is_match = any(p.search(text) for p in self._compiled_lexicon.get("political", []))
                        elif search_type == "black":
                            sender = msg.get('sender', {})
                            uid = str(sender.get('user_id', ''))
                            is_match = uid in self._user_black_set
                        if not is_match:
                            continue
                    count += 1
                    sender = msg.get('sender', {})
                    nickname = sender.get('nickname', '未知')
                    sample_messages.append(f"{nickname}: {text[:50]}")
            except Exception:
                continue
        return count, sample_messages

    @filter.command("群统计")
    async def group_stats(self, event: AstrMessageEvent):
        '''显示群内今日消息统计和活跃排行'''
        ok, err = await self._check_admin_cfg_access(event, "group_stats_enabled", "群统计", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        try:
            group_id, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
            total = len(members)
            admins = sum(1 for m in members if m.get('role') in ('admin', 'owner'))
            owners = sum(1 for m in members if m.get('role') == 'owner')
            regular = total - admins
            stats = (
                f"群 {group_id} 统计:\n"
                f"  群主: {owners}人\n"
                f"  管理员: {admins - owners}人\n"
                f"  普通成员: {regular}人\n"
                f"  总计: {total}人"
            )
            yield event.plain_result(stats)
        except Exception as e:
            yield event.plain_result(f"获取统计失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("搜索成员")
    async def search_member(self, event: AstrMessageEvent):
        '''按昵称或QQ号搜索群成员'''
        ok, err = await self._check_admin_cfg_access(event, "member_list_enabled", "查看群成员", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /搜索成员 <关键词>")
            return
        keyword = args[1]
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
            matched = []
            for m in members:
                card = m.get("card", "")
                nickname = m.get("nickname", "")
                uid = str(m.get("user_id", ""))
                if keyword.lower() in card.lower() or keyword.lower() in nickname.lower() or keyword in uid:
                    matched.append(m)
            if not matched:
                yield event.plain_result(f"未找到匹配「{keyword}」的成员")
            else:
                result_text = f"找到 {len(matched)} 个匹配成员:\n"
                for m in matched[:20]:
                    card = m.get("card", "")
                    nickname = m.get("nickname", "")
                    name = card if card else nickname
                    role = m.get("role", "member")
                    role_text = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role)
                    result_text += f"  {name}({m.get('user_id')}) [{role_text}]\n"
                yield event.plain_result(result_text.strip())
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("撤回最新消息")
    async def recall_last(self, event: AstrMessageEvent):
        '''撤回群内最新一条或多条消息'''
        ok, msg = self._cfg_check("recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        args = event.message_str.split()
        count = 1
        if len(args) >= 2:
            try:
                count = int(args[1])
            except ValueError:
                pass
        count = max(1, min(count, 10))
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("无法获取群号")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        try:
            result = await client.call_action('get_group_msg_history', group_id=self._safe_int(group_id, 0), count=count + 1)
            messages = result.get('messages', []) if isinstance(result, dict) else []
            recalled = 0
            for msg in messages[-count:]:
                msg_id = msg.get('message_id')
                if msg_id:
                    try:
                        await client.call_action('delete_msg', message_id=msg_id)
                        recalled += 1
                    except Exception:
                        pass
            yield event.plain_result(f"已尝试撤回 {recalled} 条消息")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("禁言")
    async def cmd_ban(self, event: AstrMessageEvent):
        '''禁言指定群成员。用法: /禁言 <QQ号> <分钟>'''
        ok, err = await self._check_admin_cfg_access(event, "ban_enabled", "禁言", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /禁言 <QQ号> [时长(分钟)]\n示例: /禁言 123456 30")
            return
        try:
            user_id = str(args[1]).strip()
            duration = min(max(int(args[2]) if len(args) > 2 else 10, 1), 43200)
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_ban', "禁言", group_id=gid, user_id=int(user_id), duration=duration * 60)
            if not ok:
                yield event.plain_result(f"禁言失败: {err}")
                return
            yield event.plain_result(f"已禁言 {user_id}，时长 {duration} 分钟")
        except Exception as e:
            yield event.plain_result(f"禁言失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("解禁")
    async def cmd_unban(self, event: AstrMessageEvent):
        '''解除指定群成员禁言。用法: /解禁 <QQ号>'''
        ok, err = await self._check_admin_cfg_access(event, "unban_enabled", "解禁", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /解禁 <QQ号>\n示例: /解禁 123456")
            return
        try:
            user_id = str(args[1]).strip()
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_ban', "解禁", group_id=gid, user_id=int(user_id), duration=0)
            if not ok:
                yield event.plain_result(f"解禁失败: {err}")
                return
            yield event.plain_result(f"已解除 {user_id} 的禁言")
        except Exception as e:
            yield event.plain_result(f"解禁失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("踢人")
    async def cmd_kick(self, event: AstrMessageEvent):
        '''将成员移出群聊。用法: /踢人 <QQ号>'''
        ok, err = await self._check_admin_cfg_access(event, "kick_enabled", "踢人", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /踢人 <QQ号>\n示例: /踢人 123456")
            return
        try:
            user_id = str(args[1]).strip()
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_kick', "踢人", group_id=gid, user_id=int(user_id))
            if not ok:
                yield event.plain_result(f"踢人失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 踢出群聊")
        except Exception as e:
            yield event.plain_result(f"踢人失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("全体禁言")
    async def cmd_whole_ban(self, event: AstrMessageEvent):
        '''开启或关闭全员禁言。用法: /全体禁言 开启/关闭'''
        ok, err = await self._check_admin_cfg_access(event, "whole_ban_enabled", "全体禁言", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        enable = True
        if len(args) >= 2:
            action = args[1].strip()
            if action in ("关闭", "off", "0", "取消"):
                enable = False
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_whole_ban', "全体禁言", group_id=gid, enable=enable)
            if not ok:
                yield event.plain_result(f"操作失败: {err}")
                return
            yield event.plain_result(f"已{'开启' if enable else '关闭'}全体禁言")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置名片")
    async def cmd_set_card(self, event: AstrMessageEvent):
        '''修改成员群名片。用法: /设置名片 <QQ号> <新名称>'''
        ok, err = await self._check_admin_cfg_access(event, "set_card_enabled", "设置名片", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 3:
            yield event.plain_result("用法: /设置名片 <QQ号> <名片内容>\n示例: /设置名片 123456 管理员")
            return
        try:
            user_id = str(args[1]).strip()
            card = ' '.join(args[2:])
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_card', "设置名片", group_id=gid, user_id=int(user_id), card=card)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设置 {user_id} 的群名片为: {card}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发公告")
    async def cmd_send_notice(self, event: AstrMessageEvent):
        '''发布群公告。用法: /发公告 <内容>'''
        ok, err = await self._check_admin_cfg_access(event, "send_announcement_enabled", "发公告", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        content = event.message_str.replace("/发公告", "").strip()
        if not content:
            yield event.plain_result("用法: /发公告 <公告内容>")
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            r = await client.call_action('_send_group_notice', group_id=gid, content=content)
            api_ok, err = self._check_api_result(r, "发公告")
            if not api_ok:
                yield event.plain_result(f"发送失败: {err}")
                return
            notice_id = (r or {}).get("notice_id") or (r or {}).get("id") or ""
            yield event.plain_result(f"公告已发送{f'，ID: {notice_id}' if notice_id else ''}")
        except Exception as e:
            yield event.plain_result(f"发送失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删公告")
    async def cmd_delete_notice(self, event: AstrMessageEvent):
        '''删除群公告。用法: /删公告 <公告ID>'''
        ok, err = await self._check_admin_cfg_access(event, "delete_announcement_enabled", "删公告", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /删公告 <公告ID>")
            return
        try:
            notice_id = str(args[1]).strip()
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, '_del_group_notice', "删公告", group_id=gid, notice_id=notice_id)
            if not ok:
                yield event.plain_result(f"删除失败: {err}")
                return
            yield event.plain_result(f"已删除公告 {notice_id}")
        except Exception as e:
            yield event.plain_result(f"删除失败: {e}")

    @filter.command("公告列表")
    async def cmd_list_notices(self, event: AstrMessageEvent):
        '''查看群公告列表'''
        ok, err = await self._check_admin_cfg_access(event, "list_announcements_enabled", "公告列表", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('_get_group_notice', group_id=gid)
            notices = result.get("notices", []) if isinstance(result, dict) else []
            if not notices:
                yield event.plain_result("暂无群公告")
                return
            lines = [f"📋 群公告列表 ({len(notices)}条):"]
            for n in notices[:10]:
                nid = n.get("notice_id", n.get("id", ""))
                pub = n.get("publisher", {})
                name = pub.get("nickname", "未知")
                title = n.get("title", n.get("content", ""))[:40]
                lines.append(f"  ID:{nid} | {name}: {title}")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    @filter.command("文件列表")
    async def cmd_list_files(self, event: AstrMessageEvent):
        '''查看群文件列表'''
        ok, err = await self._check_admin_cfg_access(event, "group_files_enabled", "群文件管理", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_root_files', group_id=gid)
            files = result.get("files", []) if isinstance(result, dict) else []
            folders = result.get("folders", []) if isinstance(result, dict) else []
            lines = [f"📁 群文件列表:"]
            for f in folders[:15]:
                lines.append(f"  📁 {f.get('folder_name', '?')}")
            for f in files[:15]:
                size = f.get('size', 0)
                unit = "B"
                if size > 1024 * 1024:
                    size, unit = round(size / 1048576, 1), "MB"
                elif size > 1024:
                    size, unit = round(size / 1024, 1), "KB"
                lines.append(f"  📄 {f.get('file_name', '?')} ({size}{unit})")
            if not files and not folders:
                lines.append("  暂无文件")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删文件")
    async def cmd_delete_file(self, event: AstrMessageEvent):
        '''删除群文件。用法: /删文件 <文件ID>'''
        ok, err = await self._check_admin_cfg_access(event, "group_files_enabled", "群文件管理", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /删文件 <file_id>\n提示: 使用 /文件列表 查看 file_id")
            return
        try:
            file_id = str(args[1]).strip()
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'delete_group_file', "删文件", group_id=gid, file_id=file_id, busid=0)
            if not ok:
                yield event.plain_result(f"删除失败: {err}")
                return
            yield event.plain_result(f"已删除文件 {file_id}")
        except Exception as e:
            yield event.plain_result(f"删除失败: {e}")

    @filter.command("成员列表")
    async def cmd_member_list(self, event: AstrMessageEvent):
        '''查看群成员列表'''
        ok, err = await self._check_admin_cfg_access(event, "member_list_enabled", "成员列表", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = result if isinstance(result, list) else []
            role_count = {"owner": 0, "admin": 0, "member": 0}
            for m in members:
                role = m.get("role", "member")
                role_count[role] = role_count.get(role, 0) + 1
            total = len(members)
            lines = [
                f"👥 群成员列表 ({total}人):",
                f"  👑 群主: {role_count['owner']}人",
                f"  ⭐ 管理员: {role_count['admin']}人",
                f"  👤 成员: {role_count['member']}人",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    @filter.command("禁言列表")
    async def cmd_banned_list(self, event: AstrMessageEvent):
        '''查看当前被禁言的成员'''
        ok, err = await self._check_admin_cfg_access(event, "banned_list_enabled", "禁言列表", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_shut_list', group_id=gid)
            banned = result if isinstance(result, list) else []
            if not banned:
                yield event.plain_result("当前无人被禁言")
                return
            lines = [f"🚫 禁言列表 ({len(banned)}人):"]
            for b in banned[:20]:
                uid = b.get("user_id", "?")
                dur = b.get("duration", 0)
                lines.append(f"  QQ: {uid}, 剩余: {dur // 60}分钟")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("群名")
    async def cmd_set_name(self, event: AstrMessageEvent):
        '''修改群聊名称。用法: /群名 <新名称>'''
        ok, err = await self._check_admin_cfg_access(event, "set_group_name_enabled", "修改群名", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        name = event.message_str.replace("/群名", "").strip()
        if not name:
            yield event.plain_result("用法: /群名 <新群名>")
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_name', "修改群名", group_id=gid, group_name=name)
            if not ok:
                yield event.plain_result(f"修改失败: {err}")
                return
            yield event.plain_result(f"群名已修改为: {name}")
        except Exception as e:
            yield event.plain_result(f"修改失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("头衔")
    async def cmd_set_title(self, event: AstrMessageEvent):
        '''设置成员专属头衔。用法: /头衔 <QQ号> <头衔名>'''
        ok, err = await self._check_admin_cfg_access(event, "set_title_enabled", "设置头衔", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 3:
            yield event.plain_result("用法: /头衔 <QQ号> <头衔内容>\n示例: /头衔 123456 大佬")
            return
        try:
            user_id = str(args[1]).strip()
            title = ' '.join(args[2:])
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_special_title', "设置头衔", group_id=gid, user_id=int(user_id), special_title=title, duration=-1)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设置 {user_id} 的专属头衔: {title}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设精华")
    async def cmd_set_essence(self, event: AstrMessageEvent):
        '''设置精华消息。用法: /设精华 <消息ID>'''
        ok, err = await self._check_admin_cfg_access(event, "essence_enabled", "精华消息", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /设精华 <message_id>\n回复消息或提供 message_id")
            return
        try:
            msg_id = self._safe_int(args[1], 0)
            if not msg_id:
                yield event.plain_result("消息ID格式无效")
                return
            _, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_essence_msg', "设精华", message_id=msg_id)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设为精华消息 (ID: {msg_id})")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消精华")
    async def cmd_del_essence(self, event: AstrMessageEvent):
        '''取消精华消息。用法: /取消精华 <消息ID>'''
        ok, err = await self._check_admin_cfg_access(event, "essence_enabled", "精华消息", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /取消精华 <message_id>")
            return
        try:
            msg_id = self._safe_int(args[1], 0)
            if not msg_id:
                yield event.plain_result("消息ID格式无效")
                return
            _, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'delete_essence_msg', "取消精华", message_id=msg_id)
            if not ok:
                yield event.plain_result(f"取消失败: {err}")
                return
            yield event.plain_result(f"已取消精华 (ID: {msg_id})")
        except Exception as e:
            yield event.plain_result(f"取消失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置管理")
    async def cmd_set_admin(self, event: AstrMessageEvent):
        '''设置或取消群管理员。用法: /设置管理 <QQ号>'''
        ok, err = await self._check_admin_cfg_access(event, "set_admin_enabled", "设置管理员", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /设置管理 <QQ号>\n示例: /设置管理 123456")
            return
        try:
            user_id = str(args[1]).strip()
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_admin', "设置管理员", group_id=gid, user_id=int(user_id), enable=True)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 设为群管理员")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("加群方式")
    async def cmd_join_verify(self, event: AstrMessageEvent):
        '''修改入群验证方式。用法: /加群方式 <需要验证/允许/禁止>'''
        ok, err = await self._check_admin_cfg_access(event, "join_verify_enabled", "加群验证", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        method_map = {"需要验证": 1, "允许": 0, "禁止": 2, "免审核": 0}
        if len(args) < 2:
            yield event.plain_result("用法: /加群方式 <方法>\n方法: 需要验证/允许/禁止\n示例: /加群方式 需要验证")
            return
        try:
            method_str = args[1].strip()
            method = method_map.get(method_str, -1)
            if method == -1:
                yield event.plain_result("无效的方法，请选择: 需要验证/允许/禁止")
                return
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_add_option', "加群方式", group_id=gid, add_type=method)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"加群方式已设置为: {method_str}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("自动审核")
    async def cmd_auto_moderate(self, event: AstrMessageEvent):
        '''开关智能审核功能。用法: /自动审核 开启/关闭/状态'''
        args = event.message_str.split()
        if len(args) < 2:
            status = "开启" if self.auto_moderate_enabled else "关闭"
            yield event.plain_result(f"自动审核状态: {status}\n用法: /自动审核 开启|关闭")
            return
        action = args[1].strip()
        if action in ("开启", "on", "1"):
            self.auto_moderate_enabled = True
            self.config["auto_moderate_enabled"] = True
        elif action in ("关闭", "off", "0"):
            self.auto_moderate_enabled = False
            self.config["auto_moderate_enabled"] = False
        else:
            yield event.plain_result("参数错误，请使用: 开启 或 关闭")
            return
        self._save_config_safe()
        yield event.plain_result(f"自动审核已{action}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置管理插件")
    async def cmd_plugin_admin(self, event: AstrMessageEvent):
        '''管理插件管理员列表。用法: /设置管理插件 <QQ号> 添加/移除'''
        args = event.message_str.split()
        if len(args) < 2:
            admins = self.config.get("admin_list", [])
            yield event.plain_result(f"插件管理员 ({len(admins)}人): {', '.join(str(a) for a in admins) or '无'}\n用法: /设置管理插件 <QQ号> 添加/移除")
            return
        user_id = str(args[1]).strip()
        action = "添加" if len(args) < 3 else args[2].strip()
        admin_list = self.config.get("admin_list", [])
        if not isinstance(admin_list, list):
            admin_list = []
        admin_list = [str(a).strip() for a in admin_list if a]
        if action == "移除":
            if user_id in admin_list:
                self._safe_list_remove(admin_list, user_id)
                yield event.plain_result(f"已移除插件管理员: {user_id}")
            else:
                yield event.plain_result(f"{user_id} 不在管理员列表中")
        else:
            if user_id not in admin_list:
                admin_list.append(user_id)
                yield event.plain_result(f"已添加插件管理员: {user_id}")
            else:
                yield event.plain_result(f"{user_id} 已是插件管理员")
        self.config["admin_list"] = admin_list
        self._save_config_safe()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("批量撤回")
    async def recall_all(self, event: AstrMessageEvent):
        '''批量撤回最近消息。用法: /批量撤回 [条数] 或 /批量撤回 @用户 [条数]'''
        ok, msg = self._cfg_check("recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        args = event.message_str.split()
        target_user = None
        count = 20
        for arg in args[1:]:
            if arg.isdigit():
                count = max(1, min(int(arg), 100))
            elif arg.startswith('@'):
                target_user = arg[1:]
            else:
                target_user = arg
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("无法获取群号")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        try:
            result = await client.call_action('get_group_msg_history', group_id=self._safe_int(group_id, 0), count=100)
            messages = result.get('messages', []) if isinstance(result, dict) else []
            recalled = 0
            for msg in messages:
                if recalled >= count:
                    break
                sender = msg.get('sender', {})
                uid = str(sender.get('user_id', ''))
                if target_user and uid != target_user:
                    continue
                msg_id = msg.get('message_id')
                if msg_id:
                    try:
                        await client.call_action('delete_msg', message_id=msg_id)
                        recalled += 1
                    except Exception:
                        pass
            filter_desc = f"（用户{target_user}）" if target_user else ""
            yield event.plain_result(f"已尝试撤回 {recalled} 条消息{filter_desc}")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")