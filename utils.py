# -*- coding: utf-8 -*-
import json
import os
import re
import time
from datetime import datetime
from typing import Dict, List, Tuple

from astrbot.api import logger


class UtilitiesMixin:
    # 跨模块共享的无副作用工具函数。
    # _format_message_content 负责把 OneBot 消息序列化为审核系统能用的纯文本字符串。
    # 日志和统计缓存不依赖第三方数据库，直接操作 Python 数据结构。
    _SEG_FORMATTERS = {
        'text':        lambda d: d.get('text', ''),
        'image':       lambda d: d.get('summary', '[图片]') or '[图片]',
        'at':          lambda d: f"@{d.get('qq', '')}",
        'reply':       lambda d: f"[回复:{d.get('id', '')}]",
        'face':        lambda d: "[表情]",
        'market_face': lambda d: "[商城表情]",
        'forward':     lambda d: '[合并转发消息]',
    }

    def _sync_astrbot_admins(self) -> None:
        # 从 AstrBot 主配置读取 admin_id，补充到插件 admin_list 中，使所有管理员来源统一。
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
        # 安全保存配置：调用 AstrBotConfig.save_config()，失败时记录异常但不抛错。
        try:
            self.config.save_config()
        except Exception:
            logger.exception("save_config failed")

    @staticmethod
    def _load_config_schema() -> dict:
        # 从插件目录读取 _conf_schema.json，返回 dict 供 WebUI 渲染配置面板。
        try:
            schema_path = os.path.join(os.path.dirname(__file__), "_conf_schema.json")
            with open(schema_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[GroupMgr] 加载配置schema失败: {e}")
            return {}

    def _get_data_dir(self):
        # 获取 AstrBot 分配的持久化数据目录（不会随插件更新覆盖）。
        data_dir = self._data_dir
        if not data_dir.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    def _load_logs(self) -> list:
        # 从 SQLite 加载最近 500 条审核日志到内存缓存（_moderation_logs deque）。
        try:
            return self._storage.list_logs_asc(limit=500)
        except Exception:
            logger.exception("load_logs from sqlite failed")
        return []

    def _init_next_log_id(self) -> int:
        # 扫描内存缓存中的最大日志 ID，返回其值+1 作为下一个新增日志的 ID。
        max_id = -1
        for item in self._moderation_logs:
            try:
                max_id = max(max_id, int(item.get("id", -1)))
            except (ValueError, TypeError, AttributeError):
                continue
        return max_id + 1

    def _safe_list_remove(self, lst: list, value) -> bool:
        # 安全移除列表元素：不存在时不抛 ValueError，返回是否实际移除。
        try:
            lst.remove(value)
            return True
        except ValueError:
            return False

    def _cfg(self, key: str, default: bool = True) -> bool:
        # 读取配置项并转为 bool：优先取 config 值（运行时），其次取 schema 默认值。
        if key in self._config_schema:
            default = self._config_schema[key].get("default", default)
        return bool(self.config.get(key, default))

    def _today_start(self) -> int:
        # 返回今日零点的 Unix 时间戳，用于日统计缓存判断是否跨天。
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(today.timestamp())

    def _safe_int(self, value, default: int = 0) -> int:
        # 安全转为 int：转换失败返回 default，避免 ValueError 中断主流程。
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def _get_admin_list(self) -> list:
        # 管理员列表存储在 config["admin_list"]，由 AstrBot 配置同步和 WebUI 管理。
        admin_list = self.config.get("admin_list", [])
        if not isinstance(admin_list, list):
            admin_list = []
        return [str(a).strip() for a in admin_list if a]

    @staticmethod
    def _extract_data_result(result):
        # 从 OneBot API 返回值中提取 data 字段：若响应是 {"data": {...}} 则取 data，否则原样返回。
        if isinstance(result, dict) and "data" in result:
            return result.get("data")
        return result

    @staticmethod
    def _extract_list_result(result) -> list:
        # 从 OneBot API 返回值中提取列表：优先取 data 字段，再尝试 messages/files/notices 等嵌套 key。
        result = UtilitiesMixin._extract_data_result(result)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("messages") or result.get("files") or result.get("notices") or []
        return []

    @staticmethod
    def _build_combined_regex(patterns: list, chunk_size: int = 500) -> list:
        # 将多个正则模式用 | 合并为一个，减少匹配循环；超出 chunk_size 则分批，防止单条表达式过长导致回溯爆炸。
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
        # 三级权限/功能检查：插件总开关 → 免责声明未同意 → 具体功能配置关闭，逐层短路返回错误。
        if not self._cfg("enabled"):
            return False, "插件已禁用，所有功能不可用"
        if not self.config.get("disclaimer_agreed", False):
            return False, "您暂未阅读并同意免责声明，请在插件设置中阅读并同意免责声明后使用"
        if not self._cfg(key):
            return False, f"{name}功能已在配置中禁用"
        return True, ""

    def _check_api_result(self, result, action_name: str = "操作") -> Tuple[bool, str]:
        # 检查 OneBot API 返回值：status=="failed" 或 retcode!=0 视为失败。
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
        # 返回插件源代码目录的绝对路径，用于定位 lexicon.db 等内置资源文件。
        return os.path.dirname(os.path.abspath(__file__))

    def _load_lexicon(self) -> Dict[str, Dict]:
        try:
            categories = self._storage.load_lexicon()
            logger.info(f"[GroupMgr] 已从 SQLite 加载外置词库: {len(categories)} 个分类")
            for cat_name, cat_data in categories.items():
                keywords = cat_data.get("keywords", [])
                logger.info(f"[GroupMgr]   - {cat_name}: {len(keywords)} 条关键词")
            return categories
        except Exception as e:
            logger.error(f"[GroupMgr] 加载 SQLite 外置词库失败: {e}")
            return {}

    def _compile_lexicon(self) -> Dict[str, List[re.Pattern]]:
        compiled = {}
        cfg = self.config

        def _enabled(key: str) -> bool:
            return cfg.get(f"lexicon_{key}_enabled", True)

        enable_other = _enabled("other")
        switch_map = {
            "political": _enabled("political"),
            "porn": _enabled("porn"),
            "violent_terror": _enabled("violent"),
            "reactionary": _enabled("reactionary"),
            "weapons": _enabled("weapons"),
            "corruption": _enabled("corruption"),
            "illegal_url": _enabled("illegal_url"),
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
            for kw in keywords:
                kw = kw.strip()
                if not kw:
                    continue
                if '+' in kw and cat_name != "illegal_url":
                    parts = [p.strip() for p in kw.split('+') if p.strip()]
                    for part in parts:
                        if len(part) >= min_len:
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
        # 将已 re.escape 处理过的关键词字符串用 | 拼接编译为正则列表。
        # chunk_size=3000 控制每组拼接数量，超出则分批，避免单条正则过长导致性能下降。
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
        # 用预编译的词库正则逐类扫描文本，返回各分类是否命中的 dict。
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

    def _truncate(self, text: str, max_chars: int = 2000) -> str:
        # 截断超长文本：超过 max_chars 时添加 "已截断" 提示。
        if len(text) <= max_chars:
            return text
        suffix = f"\n...（已截断，原{len(text)}字符）"
        limit = max_chars - len(suffix)
        if limit <= 0:
            return text[:max_chars]
        return text[:limit] + suffix

    def _format_message_content(self, raw_message) -> str:
        # 将 OneBot 消息链（segment 列表）按 type 分派到 _SEG_FORMATTERS，拼接为纯文本供审核规则匹配。
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
            formatter = self._SEG_FORMATTERS.get(t)
            parts.append(formatter(d) if formatter else f"[{t}]")
        return ''.join(parts) if parts else '[空消息]'

    def _invalidate_stats_cache(self):
        # 清除当日统计缓存：today_start 归零 + 清空 group_stats/user_stats/user_names。
        # 下次访问时会自动重新从日志计算当日数据。
        self._stats_cache["today_start"] = 0
        self._stats_cache["group_stats"] = {}
        self._stats_cache["user_stats"] = {}
        self._stats_cache.pop("user_names", None)

    def _log_moderation(self, group_id: str, user_id: str, user_name: str, msg_text: str, action: str, reason: str = "", image_urls: list = None):
        valid_urls = [u for u in (image_urls or []) if u][:5]
        log_id = self._next_log_id
        self._next_log_id += 1
        log_entry = {
            "id": log_id,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ts": int(time.time()),
            "group_id": group_id,
            "user_id": user_id,
            "user_name": user_name,
            "msg_text": msg_text,
            "msg_preview": msg_text[:100],
            "action": action,
            "reason": reason,
            "image_urls": valid_urls,
        }
        self._moderation_logs.append(log_entry)
        try:
            self._storage.add_log(log_entry)
        except Exception:
            logger.exception("save moderation log to sqlite failed")
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
        self._last_log_save = time.time()
