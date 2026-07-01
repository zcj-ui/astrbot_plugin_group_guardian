# -*- coding: utf-8 -*-
import json
import os
import time
from datetime import datetime
from typing import Dict, List, Tuple

from astrbot.api import logger
from .automaton import KeywordAutomaton


class UtilitiesMixin:

    def _extract_at_targets(self, event) -> list:
        targets = []
        seen = set()
        try:
            chain = event.get_messages() or []
        except Exception:
            chain = []
        for seg in chain:
            qq = None
            if isinstance(seg, dict):
                if seg.get("type") == "at":
                    qq = (seg.get("data", {}) or {}).get("qq", "")
            else:
                seg_cls = type(seg).__name__
                if seg_cls == "At" or (hasattr(seg, "type") and getattr(seg, "type", "") == "at"):
                    qq = getattr(seg, "qq", "") or ""
            qq = str(qq).strip()
            if not qq or qq.lower() in ("all", "0"):
                continue
            if qq not in seen and qq.isdigit():
                seen.add(qq)
                targets.append(qq)
        return targets

    _SEG_FORMATTERS = {
        'text':        lambda d: d.get('text', ''),
        'image':       lambda d: d.get('summary', '[图片]') or '[图片]',
        'at':          lambda d: f"@{d.get('qq', '')}",
        'reply':       lambda d: f"[回复:{d.get('id', '')}]",
        'face':        lambda d: "[表情]",
        'market_face': lambda d: "[商城表情]",
        'forward':     lambda d: '[合并转发消息]',
    }

    def _get_reply_message_id(self, event) -> str:
        """从消息事件中提取被回复消息的 message_id。

        遍历消息链，找到 reply 类型段后返回其 id 字段。
        用于检测管理员是否引用了待审通知消息进行回复。

        Args:
            event: 消息事件对象

        Returns:
            str: 被回复消息的 ID，无回复时返回空字符串
        """
        try:
            chain = event.get_messages() or []
        except Exception:
            chain = []
        for seg in chain:
            if isinstance(seg, dict):
                if seg.get("type") == "reply":
                    return str((seg.get("data", {}) or {}).get("id", ""))
            else:
                seg_cls = type(seg).__name__
                if seg_cls == "Reply" or (hasattr(seg, "type") and getattr(seg, "type", "") == "reply"):
                    return str(getattr(seg, "id", "") or "")
        return ""

    def _extract_plain_text(self, event) -> str:
        """提取消息事件中的纯文本内容（仅拼接 text 段，忽略 @/回复/图片等段）。

        用于入群审核回复判定：当管理员「@机器人 通过」时，event.message_str 可能
        含 @ 前缀导致 startswith("通过") 失败，这里只取 text 段保证关键字命中。

        Args:
            event: 消息事件对象

        Returns:
            str: 去除首尾空白后的纯文本
        """
        try:
            chain = event.get_messages() or []
        except Exception:
            chain = []
        parts = []
        for seg in chain:
            if isinstance(seg, dict):
                if seg.get("type") == "text":
                    parts.append((seg.get("data", {}) or {}).get("text", ""))
            else:
                seg_type = getattr(seg, "type", "") or type(seg).__name__
                if seg_type == "text" or seg_type == "Text":
                    data = getattr(seg, "data", None)
                    if isinstance(data, dict):
                        parts.append(data.get("text", ""))
                    else:
                        parts.append(str(getattr(seg, "text", "") or ""))
        return "".join(parts).strip()

    def _sync_astrbot_admins(self) -> None:
        # 从 AstrBot 主配置读取 admin_id，补充到插件管理员名单（DB managed_lists + 内存），统一管理员来源。
        try:
            ab_config = getattr(self.context, 'astrbot_config', None)
            if not ab_config:
                return
            astrbot_admin_ids = [str(x).strip() for x in (ab_config.get('admin_id', []) or []) if str(x).strip()]
            if not astrbot_admin_ids:
                return
            current = set(getattr(self, "admin_list", []) or [])
            new_admins = [a for a in astrbot_admin_ids if a not in current]
            if new_admins:
                for a in new_admins:
                    self._managed_list_add("admin", a)
                logger.info(f"[GroupMgr] 自动同步AstrBot管理员到插件admin名单: {new_admins}")
        except Exception as _e:
            logger.debug(f"[GroupMgr] 同步AstrBot管理员失败: {_e}")

    def _save_config_safe(self) -> None:
        # 安全保存配置：调用 AstrBotConfig.save_config()，失败时记录异常但不抛错。
        if not hasattr(self.config, "save_config"):
            logger.warning("[GroupMgr] 当前配置对象不支持 save_config()，本次修改仅保留在内存中")
            return
        try:
            self.config.save_config()
        except Exception:
            logger.exception("save_config failed")

    # 单群管理类名单的统一映射：DB list_type -> (config key, 内存 list 属性, 内存 set 属性)
    # v2.4.0 起这些名单以 SQLite(managed_lists) 为准，config 旧值仅作首次迁移与回退兜底。
    _MANAGED_LIST_MAP = {
        "group_white": ("group_white_list", "group_white_list", "_group_white_set"),
        "group_black": ("group_black_list", "group_black_list", "_group_black_set"),
        "user_black": ("user_black_list", "user_black_list", "_user_black_set"),
        "user_white": ("user_white_list", "user_white_list", "_user_white_set"),
        "admin": ("admin_list", "admin_list", None),
    }

    def _migrate_and_load_managed_lists(self) -> None:
        """把单群管理类名单从 config 迁移到 DB，并以 DB 为准载入内存。

        迁移只执行一次（meta.lists_migrated 标记）。迁移只增不删 config 旧值，
        保证升级出问题时仍可回退。DB 为空且未迁移过时，回退读 config 旧值。
        admin 名单不在此设置内存（由 main 的 admin 同步逻辑处理），仅做 DB 迁移与回填。
        """
        migrated_flag = self._storage.get_meta("lists_migrated", "")
        for list_type, (cfg_key, list_attr, set_attr) in self._MANAGED_LIST_MAP.items():
            cfg_values = self.config.get(cfg_key, [])
            cfg_values = [str(v).strip() for v in (cfg_values if isinstance(cfg_values, list) else [cfg_values]) if str(v).strip()]
            # 首次迁移：DB 该类型为空时，把 config 旧值导入 DB
            if not migrated_flag and self._storage.count_managed_list(list_type) == 0 and cfg_values:
                self._storage.seed_managed_list(list_type, cfg_values)
            # 以 DB 为准读入；若 DB 为空（全新安装/无旧值）则回退 config
            db_values = self._storage.load_managed_list(list_type)
            values = db_values if db_values else cfg_values
            setattr(self, list_attr, list(values))
            if set_attr:
                setattr(self, set_attr, set(values))
        if not migrated_flag:
            self._storage.set_meta("lists_migrated", "1")
            logger.info("[GroupMgr] 单群管理类名单已迁移到 SQLite(managed_lists)")

    def _managed_list_add(self, list_type: str, value: str) -> bool:
        """向 DB 名单添加一项并同步内存 list/set。返回是否实际新增。"""
        cfg_key, list_attr, set_attr = self._MANAGED_LIST_MAP[list_type]
        value = str(value).strip()
        if not value:
            return False
        self._storage.add_managed_list_value(list_type, value)
        lst = getattr(self, list_attr, None)
        if isinstance(lst, list) and value not in lst:
            lst.append(value)
        if set_attr:
            getattr(self, set_attr).add(value)
        return True

    def _managed_list_remove(self, list_type: str, value: str) -> bool:
        """从 DB 名单移除一项并同步内存 list/set。返回是否实际移除。"""
        cfg_key, list_attr, set_attr = self._MANAGED_LIST_MAP[list_type]
        value = str(value).strip()
        removed = self._storage.remove_managed_list_value(list_type, value)
        lst = getattr(self, list_attr, None)
        if isinstance(lst, list):
            self._safe_list_remove(lst, value)
        if set_attr:
            getattr(self, set_attr).discard(value)
        return removed


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

    def _cfg(self, key: str, default: bool = True, group_id: str = None) -> bool:
        # 读取布尔配置：传 group_id 时优先用该群的覆盖值，否则用全局（config / schema 默认）。
        # 群覆盖值以字符串存储（"true"/"false"），实现真正的多群独立配置。
        if group_id:
            gv = self._get_group_override(group_id, key)
            if gv is not None:
                return self._parse_bool_str(gv)
        if key in self._config_schema:
            default = self._config_schema[key].get("default", default)
        value = self.config.get(key, default)
        if isinstance(value, str) and value.strip() == "":
            value = default
        return self._parse_bool_str(value)

    def _cfg_int(self, key: str, default: int = 0, group_id: str = None) -> int:
        # 读取整型配置：群覆盖优先，其次全局 config，再次 schema 默认值。
        if group_id:
            gv = self._get_group_override(group_id, key)
            if gv is not None:
                value = self._safe_int(gv, default)
                return self._clamp_cfg_int(key, value)
        if key in self._config_schema:
            default = self._config_schema[key].get("default", default)
        return self._clamp_cfg_int(key, self._safe_int(self.config.get(key, default), default))

    def _clamp_cfg_int(self, key: str, value: int) -> int:
        ranges_fn = getattr(self, "_config_int_ranges", None)
        if not callable(ranges_fn):
            return value
        lo, hi = ranges_fn().get(key, (None, None))
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
        return value

    def _cfg_str(self, key: str, default: str = "", group_id: str = None) -> str:
        # 读取字符串配置：群覆盖优先，其次全局 config，再次 schema 默认值。
        options = []
        if group_id:
            gv = self._get_group_override(group_id, key)
            if gv is not None:
                value = str(gv)
                meta = self._config_schema.get(key, {})
                options = [str(x) for x in (meta.get("options") or [])]
                return value if not options or value in options else str(meta.get("default", default))
        if key in self._config_schema:
            meta = self._config_schema[key]
            default = meta.get("default", default)
            options = [str(x) for x in (meta.get("options") or [])]
        value = str(self.config.get(key, default))
        return value if not options or value in options else str(default)

    @staticmethod
    def _parse_bool_str(v) -> bool:
        # 把群配置里存的字符串解析为布尔（兼容 bool/数字）。
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        return str(v).strip().lower() in ("1", "true", "yes", "on", "是")

    def _get_group_override(self, group_id: str, key: str):
        # 带缓存地读取某群对某配置项的覆盖值（字符串）；无覆盖返回 None。
        # 缓存粒度为整群一次性载入，命中后零 DB 开销，配置变更时由 _invalidate_group_cfg_cache 失效。
        cache = getattr(self, "_group_cfg_cache", None)
        if cache is None:
            cache = {}
            self._group_cfg_cache = cache
        if group_id not in cache:
            try:
                cache[group_id] = self._storage.get_group_configs(group_id)
            except Exception:
                cache[group_id] = {}
        return cache[group_id].get(key)

    def _invalidate_group_cfg_cache(self, group_id: str = "") -> None:
        # 清除群配置缓存：指定 group_id 清单群，否则清全部。WebUI 改配置后调用。
        cache = getattr(self, "_group_cfg_cache", None)
        if cache is None:
            return
        if group_id:
            cache.pop(group_id, None)
        else:
            cache.clear()

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

    def _clamp_int(self, value, default: int, minimum: int, maximum: int) -> int:
        # 安全转 int 后限制范围，适合处理 LLM/用户输入这类类型不稳定的参数。
        number = self._safe_int(value, default)
        return max(minimum, min(number, maximum))

    def _get_admin_list(self) -> list:
        # 管理员名单 v2.4.0 起以 SQLite(managed_lists) 为准，运行时缓存在 self.admin_list。
        admin_list = getattr(self, "admin_list", None)
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
            for key in ("messages", "members", "files", "folders", "notices", "data", "items", "list"):
                value = result.get(key)
                if isinstance(value, list):
                    return value
            return []
        return []

    def _cfg_check(self, key: str, name: str, group_id: str = None) -> Tuple[bool, str]:
        # 三级权限/功能检查：插件总开关 → 免责声明未同意 → 具体功能配置关闭，逐层短路返回错误。
        # 传 group_id 时按群读取开关，实现群管功能的按群独立开关。
        if not self._cfg("enabled", group_id=group_id):
            return False, "插件已禁用，所有功能不可用"
        if not self.config.get("disclaimer_agreed", False):
            return False, "您暂未阅读并同意免责声明，请在插件设置中阅读并同意免责声明后使用"
        if not self._cfg(key, group_id=group_id):
            return False, f"{name}功能已在配置中禁用"
        return True, ""

    def _check_api_result(self, result, action_name: str = "操作") -> Tuple[bool, str]:
        # 检查 OneBot API 返回值：status=="failed" 或 retcode!=0 视为失败。
        if result is None:
            return True, ""
        if isinstance(result, dict):
            status = result.get("status", "")
            raw_retcode = result.get("retcode", 0)
            try:
                retcode = 0 if raw_retcode is None else int(raw_retcode)
            except (TypeError, ValueError):
                retcode = raw_retcode
            if status == "failed" or retcode != 0:
                msg = result.get("msg", "") or result.get("message", "") or f"错误码: {retcode}"
                return False, msg
        return True, ""

    def _parse_join_verify_method(self, value) -> Tuple[int, str]:
        # OneBot set_group_add_option: 0=允许任何人，1=需要验证，2=禁止加群。
        raw = str(value or "").strip().lower()
        method_map = {
            "允许": (0, "允许加入"),
            "免审核": (0, "允许加入"),
            "allow": (0, "允许加入"),
            "0": (0, "允许加入"),
            "需要验证": (1, "需要验证"),
            "需审核": (1, "需要验证"),
            "need_verify": (1, "需要验证"),
            "verify": (1, "需要验证"),
            "1": (1, "需要验证"),
            "禁止": (2, "禁止加群"),
            "不允许": (2, "禁止加群"),
            "拒绝": (2, "禁止加群"),
            "deny": (2, "禁止加群"),
            "not_allow": (2, "禁止加群"),
            "2": (2, "禁止加群"),
        }
        return method_map.get(raw, (-1, ""))

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

    def _lexicon_switch_map(self, group_id: str = None) -> Dict[str, bool]:
        """统一计算各词库分类的启用状态，供审核阶段过滤复用。

        其中 supplement / livelihood / tencent_ban 跟随 other 开关，
        ad 分类始终启用（由独立的广告规则匹配器控制）。传 group_id 时优先使用群覆盖。
        """
        enable_other = self._cfg("lexicon_other_enabled", True, group_id=group_id)
        return {
            "political": self._cfg("lexicon_political_enabled", True, group_id=group_id),
            "porn": self._cfg("lexicon_porn_enabled", True, group_id=group_id),
            "violent_terror": self._cfg("lexicon_violent_enabled", True, group_id=group_id),
            "reactionary": self._cfg("lexicon_reactionary_enabled", True, group_id=group_id),
            "weapons": self._cfg("lexicon_weapons_enabled", True, group_id=group_id),
            "corruption": self._cfg("lexicon_corruption_enabled", True, group_id=group_id),
            "illegal_url": self._cfg("lexicon_illegal_url_enabled", True, group_id=group_id),
            "other": enable_other,
            "supplement": enable_other,
            "livelihood": enable_other,
            "tencent_ban": enable_other,
            "ad": True,
        }

    @staticmethod
    def _extract_lexicon_parts(cat_name: str, cat_data: Dict) -> List[str]:
        """把一个分类的关键词拆解为待编译进 AC 自动机的纯文本片段。

        illegal_url 最小长度 2、其它分类 3；含 '+' 的关键词按 '+' 拆成多个片段
        （illegal_url 例外，网址本身可能含 '+'）。
        """
        keywords = (cat_data or {}).get("keywords", [])
        min_len = 2 if cat_name == "illegal_url" else 3
        raw_parts: List[str] = []
        for kw in keywords:
            kw = kw.strip()
            if not kw:
                continue
            if "+" in kw and cat_name != "illegal_url":
                raw_parts.extend(p for p in (s.strip() for s in kw.split("+")) if len(p) >= min_len)
            elif len(kw) >= min_len:
                raw_parts.append(kw)
        return raw_parts

    def _compile_lexicon(self) -> Dict[str, "KeywordAutomaton"]:
        """全量编译所有分类的词库为 AC 自动机。

        分类开关在审核阶段过滤，而不是编译阶段过滤；否则全局关闭的分类无法被单群覆盖重新启用。
        """
        compiled = {}
        for cat_name, cat_data in self._lexicon.items():
            ac = self._build_category_automaton(cat_name, cat_data)
            if ac.count:
                compiled[cat_name] = ac
        return compiled

    @classmethod
    def _build_category_automaton(cls, cat_name: str, cat_data: Dict) -> "KeywordAutomaton":
        """根据分类关键词构建并 build 一个 AC 自动机（无关键词时返回空自动机）。"""
        ac = KeywordAutomaton()
        raw_parts = cls._extract_lexicon_parts(cat_name, cat_data)
        if raw_parts:
            ac.add_keywords(raw_parts)
            ac.build()
        return ac

    def _compile_lexicon_category(self, cat_name: str, cat_data: Dict) -> "KeywordAutomaton":
        """按单个分类增量编译 AC 自动机。"""
        return self._build_category_automaton(cat_name, cat_data)

    def _lexicon_category_enabled(self, cat_name: str) -> bool:
        """判断词库分类是否在当前配置中启用。"""
        return self._lexicon_switch_map().get(cat_name, True)

    def _check_lexicon(self, text: str) -> Dict[str, bool]:
        # 用 AC 自动机逐类扫描文本，返回各分类是否命中的 dict。
        result = {}
        for cat_name, automaton in self._compiled_lexicon.items():
            if not isinstance(automaton, KeywordAutomaton):
                continue
            matches = automaton.iter_matches(text)
            if matches:
                logger.debug(f"[GroupMgr] 词库命中 [{cat_name}]: {len(matches)} 个, 示例='{matches[0][1]}'")
                result[cat_name] = True
            else:
                result[cat_name] = False
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
        # 注意: AstrBot 的 message_obj.message 是 BaseMessageComponent 对象列表（不是 dict 列表），
        # 需先调用 toDict() 转为标准 OneBot 段字典。否则 str(seg) 会把对象所有字段（如 Reply 的
        # chain/message_str/text，即被引用消息的完整内容）都序列化出来，导致引用消息原文被
        # 算进当前发言者长度，触发长文本/重复消息等刷屏误判。
        if raw_message is None:
            return '[空消息]'
        if not isinstance(raw_message, list):
            return str(raw_message)
        parts = []
        for seg in raw_message:
            if not isinstance(seg, dict):
                # BaseMessageComponent 对象（Plain/At/Reply/Image 等）：
                # 优先调用 toDict() 转为标准 OneBot 段字典，避免 str(seg) 把对象全部字段
                # （特别是 Reply 的 chain/message_str）序列化进文本。
                to_dict_fn = getattr(seg, 'toDict', None)
                if callable(to_dict_fn):
                    try:
                        seg = to_dict_fn()
                    except Exception:
                        parts.append(f"[{type(seg).__name__}]")
                        continue
                else:
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
            "msg_preview": msg_text[:200],
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
