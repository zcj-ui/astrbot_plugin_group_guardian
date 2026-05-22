# -*- coding: utf-8 -*-
import asyncio
import json
import os
import re
import tempfile
import time
from datetime import datetime
from typing import Dict, List, Tuple

from astrbot.api import logger
from astrbot.api.star import StarTools

from .patterns import _POLITICAL_WHITELIST


class UtilitiesMixin:
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
        except Exception as e:
            logger.warning(f"[GroupMgr] 加载配置schema失败: {e}")
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

    def _init_next_log_id(self) -> int:
        max_id = -1
        for item in self._moderation_logs:
            try:
                max_id = max(max_id, int(item.get("id", -1)))
            except (ValueError, TypeError, AttributeError):
                continue
        return max_id + 1

    def _save_logs(self) -> None:
        try:
            p = self._logs_path()
            data = list(self._moderation_logs)
            try:
                loop = asyncio.get_running_loop()
                task = loop.run_in_executor(None, self._write_logs_sync, p, data)
                self._log_save_task = task
                task.add_done_callback(self._on_log_save_done)
                self._last_log_save = time.time()
            except RuntimeError:
                logger.warning("[GroupMgr] 无事件循环，跳过日志写入（将在下次可用时保存）")
        except Exception:
            logger.exception("save_logs failed")

    @staticmethod
    def _on_log_save_done(task) -> None:
        try:
            task.result()
        except Exception:
            logger.exception("save_logs async failed")

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

    def _safe_list_remove(self, lst: list, value) -> bool:
        try:
            lst.remove(value)
            return True
        except ValueError:
            return False

    def _cfg(self, key: str, default: bool = True) -> bool:
        if key in self._config_schema:
            default = self._config_schema[key].get("default", default)
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

    def _get_admin_list(self) -> list:
        admin_list = self.config.get("admin_list", [])
        if not isinstance(admin_list, list):
            admin_list = []
        return [str(a).strip() for a in admin_list if a]

    @staticmethod
    def _extract_data_result(result):
        if isinstance(result, dict) and "data" in result:
            return result.get("data")
        return result

    @staticmethod
    def _extract_list_result(result) -> list:
        result = UtilitiesMixin._extract_data_result(result)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("messages") or result.get("files") or result.get("notices") or []
        return []

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
            formatter = self._SEG_FORMATTERS.get(t)
            parts.append(formatter(d) if formatter else f"[{t}]")
        return ''.join(parts) if parts else '[空消息]'

    def _invalidate_stats_cache(self):
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
