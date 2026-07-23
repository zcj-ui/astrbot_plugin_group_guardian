# -*- coding: utf-8 -*-
"""名片监控模块（v2.7.0）。

监听 OneBot notice 事件，覆盖四类能力（默认全部关闭，需在 WebUI「名片监控」页开启）：
- A 名片变更日志：group_card 事件（NapCat/LLOneBot 扩展）记录 谁的名片、旧→新、时间
- B 名片保护/还原：被保护成员的名片被改后自动改回预设值
- C 违规名片审核：与消息审核同款——词库/正则初筛 + 可选 LLM 上下文判断；
  另外任何店铺/推广链接直接还原为默认（清空名片或旧名片）
- D 管理员任免通知：group_admin 事件（OneBot v11 标准）群内通知谁被设/撤管理员

协议限制：group_card / group_admin 事件都不含 operator_id，无法区分「本人改」还是
「管理员改」，只能记录变更事实本身。

复用现有基础设施：_get_raw_event / _check_group_access（membership）、
_cfg / _cfg_int / _check_lexicon（utils）、_get_client / _call_group_api（onebot）、
_call_llm_safe（moderation）。
"""
import asyncio
import re
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# 链接/店铺特征：命中即违规，直接还原（不经 LLM）。
# 这是"仅拦链接"宽松模式唯一拦截的东西，所以只放【明确的网址/店铺域名/扫码下单】，
# 不放"加我"这类引流词——引流词交给 _PROMO_SUSPECT_RE + LLM 上下文判断。
_SHOP_LINK_RE = re.compile(
    r"(?:https?://|www\.)|"                                  # 任意网址
    r"(?:taobao|tmall|jd|pinduoduo|pdd|1688|xiaohongshu|douyin|kuaishou|weidian|youzan)\.|"  # 电商域名
    r"(?:\.(?:com|cn|net|org|shop|top|xyz|vip)\b)|"          # 裸域名
    r"(?:淘宝|天猫|拼多多|京东|微店|有赞|旗舰店|专卖店|扫码|二维码|领券|优惠券)",  # 明确店铺/扫码
    re.IGNORECASE,
)

# 引流/广告可疑特征：命中【不直接还原】，而是送 LLM 做上下文判断（同消息审核的"正则初筛+LLM二判"）。
# 覆盖"科技加我"这类：单看"加我"可能无辜，需 LLM 结合整体判断是不是引流名片。
_PROMO_SUSPECT_RE = re.compile(
    r"(?:加我|加个?[微薇威]|加[vV]|[vV][xX]|微信|薇信|威信|企[鹅Q]|私聊|私我|滴我|联系我)|"
    r"(?:代购|代练|代刷|接单|承接|出售|批发|招收?代理|招募|招商|收徒|带练|上分|刷单)|"
    r"(?:免费领|福利|优惠|折扣|低价|特价|秒杀|兼职|日结|月入|引流|推广|广告)",
    re.IGNORECASE,
)

_CARD_SNAPSHOT_UNSET = object()
_CARD_SNAPSHOT_ANY = object()
_CARD_PENDING_MAX_MISSES = 3


class CardMonitorMixin:
    def _card_monitor_active(self, group_id: str) -> bool:
        """Return whether card monitoring may act in this group."""
        config = getattr(self, "config", {}) or {}
        if not config.get("disclaimer_agreed", False):
            return False
        return (
            self._cfg("enabled", True, group_id=group_id)
            and self._cfg("card_monitor_enabled", False, group_id=group_id)
        )

    def _is_group_card_notice(self, event: AstrMessageEvent) -> bool:
        raw = self._get_raw_event(event)
        return (isinstance(raw, dict)
                and raw.get("post_type") == "notice"
                and raw.get("notice_type") == "group_card")

    def _is_group_admin_notice(self, event: AstrMessageEvent) -> bool:
        raw = self._get_raw_event(event)
        return (isinstance(raw, dict)
                and raw.get("post_type") == "notice"
                and raw.get("notice_type") == "group_admin")

    def _is_group_increase_notice(self, event: AstrMessageEvent) -> bool:
        """判断成员入群通知。

        `group_increase` 是 OneBot v11 标准通知，和非标准的 `group_card`
        不同，通常所有协议端都会上报。入群时立即查询名片，避免新成员必须
        先发言才能进入审核链路。
        """
        raw = self._get_raw_event(event)
        return (isinstance(raw, dict)
                and raw.get("post_type") == "notice"
                and raw.get("notice_type") == "group_increase")

    def _ensure_card_sync_state(self) -> None:
        """延迟初始化快照状态，兼容旧版 AstrBot 的事件循环生命周期。"""
        if not isinstance(getattr(self, "_card_snapshots", None), dict):
            self._card_snapshots = {}
        if not isinstance(getattr(self, "_card_sync_known_groups", None), set):
            self._card_sync_known_groups = set()
        if not isinstance(getattr(self, "_card_pending_members", None), set):
            self._card_pending_members = set()
        if not isinstance(getattr(self, "_card_pending_misses", None), dict):
            self._card_pending_misses = {}
        if not isinstance(getattr(self, "_card_recent_inputs", None), dict):
            self._card_recent_inputs = {}
        if not isinstance(getattr(self, "_card_change_lock", None), asyncio.Lock):
            # asyncio.Lock 在创建时不绑定事件循环，放在首次实际调用时初始化。
            self._card_change_lock = asyncio.Lock()

    def _mark_card_pending(self, group_id: str, user_id: str) -> None:
        self._ensure_card_sync_state()
        key = (str(group_id), str(user_id))
        self._card_pending_members.add(key)
        self._card_pending_misses[key] = 0

    def _clear_card_pending(self, group_id: str, user_id: str) -> None:
        self._ensure_card_sync_state()
        key = (str(group_id), str(user_id))
        self._card_pending_members.discard(key)
        self._card_pending_misses.pop(key, None)

    def _card_group_allowed(self, group_id: str) -> bool:
        """无 event 场景下复用群黑白名单判断（周期同步使用）。"""
        gid = str(group_id or "")
        if not gid:
            return False
        black = {str(x) for x in (getattr(self, "_group_black_set", set()) or set())}
        if gid in black:
            return False
        white = {str(x) for x in (getattr(self, "_group_white_set", set()) or set())}
        return not white or gid in white

    def _remember_card_snapshot(self, group_id: str, user_id: str, card: str) -> None:
        self._ensure_card_sync_state()
        gid, uid = str(group_id), str(user_id)
        self._card_snapshots.setdefault(gid, {})[uid] = str(card or "")
        self._card_sync_known_groups.add(gid)

    async def _fetch_member_card(self, client, group_id: str, user_id: str):
        """通过标准 get_group_member_info 获取名片；失败返回 None。"""
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not client or not gid or not uid:
            return None
        last_error = None
        # 入群通知可能早于协议端成员缓存落地，短暂重试避免把违规新成员
        # 当作普通基线吞掉；周期同步仍会接管最终兜底。
        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    client.call_action("get_group_member_info", group_id=gid, user_id=uid, no_cache=True),
                    timeout=10.0,
                )
                ok, error = self._check_api_result(result, "查询成员名片")
                if not ok:
                    last_error = error
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                    continue
                result = self._extract_data_result(result)
                if isinstance(result, dict) and "card" in result:
                    # `card` 为空是合法默认名片；字段缺失不能按空名片处理。
                    return str(result.get("card", "") or ""), str(result.get("nickname", "") or "")
                last_error = "响应缺少 card 字段"
            except Exception as e:
                last_error = e
            if attempt == 0:
                await asyncio.sleep(0.5)
        logger.debug(f"[GroupMgr] 查询成员名片失败({group_id}/{user_id}): {last_error}")
        return None

    async def _process_card_values(
        self,
        group_id: str,
        user_id: str,
        card_old: str,
        card_new: str,
        user_name: str = "",
        event: AstrMessageEvent = None,
        source: str = "event",
        force: bool = False,
        expected_snapshot=_CARD_SNAPSHOT_ANY,
    ) -> bool:
        """统一处理事件、入群即时查询和周期快照发现的名片变化。

        `group_card` 通知并非 OneBot v11 标准，部分协议端不会上报。把实际
        审核逻辑抽到这里后，标准 `group_increase` 和周期同步可以共用同一套
        保护/违规还原与日志逻辑，不再依赖成员发言。
        """
        self._ensure_card_sync_state()
        group_id, user_id = str(group_id or ""), str(user_id or "")
        card_old, card_new = str(card_old or ""), str(card_new or "")
        if not group_id or not user_id:
            return False

        # 同一个新值可能同时由扩展事件和轮询发现；串行化可避免重复 LLM/还原。
        async with self._card_change_lock:
            known = self._card_snapshots.setdefault(group_id, {}).get(
                user_id, _CARD_SNAPSHOT_UNSET
            )
            if (expected_snapshot is not _CARD_SNAPSHOT_ANY
                    and known != expected_snapshot):
                # 轮询请求等待期间实时 group_card 已写入更近快照，丢弃旧响应。
                return False
            if not force and known is not None and known == card_new:
                return False
            recent_key = (group_id, user_id)
            recent = self._card_recent_inputs.get(recent_key)
            if (not force and recent and recent[0] == card_new
                    and time.monotonic() - recent[1] < 30):
                return False
            logger.info(
                f"[GroupMgr] 收到名片变更({source}): 群{group_id} 用户{user_id} "
                f"「{card_old}」->「{card_new}」"
            )
            if not self._card_monitor_active(group_id):
                if source == "join":
                    self._mark_card_pending(group_id, user_id)
                logger.debug(f"[GroupMgr] 名片监控未生效，跳过（群{group_id}）")
                return False
            # notice 事件的群号有时只存在 raw_event 中，而 _check_group_access
            # 依赖 AstrBot 事件对象暴露 group_id。始终先按已解析出的显式群号
            # 校验，避免这类包装差异绕过群黑白名单。
            allowed = self._card_group_allowed(group_id)
            reason = "群黑白名单限制"
            if allowed and event is not None:
                allowed, reason = self._check_group_access(event)
            if not allowed:
                if source == "join":
                    self._mark_card_pending(group_id, user_id)
                logger.debug(f"[GroupMgr] 名片监控跳过：{reason}")
                return False
            protected = None
            if self._cfg("card_protect_enabled", False, group_id=group_id):
                try:
                    protected = self._storage.get_card_protected(group_id, user_id)
                except Exception:
                    protected = None
            protection_needed = protected is not None and card_new != protected
            if card_old == card_new and not protection_needed:
                self._remember_card_snapshot(group_id, user_id, card_new)
                return False

            action = "记录"
            restored = False
            effective_card = card_new
            protection_failed = False

            # B 名片保护/还原：被保护成员名片被改 -> 改回预设值
            if protection_needed:
                if await self._restore_card(group_id, user_id, protected):
                    action = "保护还原"
                    restored = True
                    effective_card = str(protected or "")
                    if self._cfg("card_monitor_notify", True, group_id=group_id):
                        await self._notify_card_group(
                            group_id,
                            f"[名片监控] {user_id} 的名片被改为「{card_new}」，"
                            f"已还原为受保护名片「{protected}」",
                        )
                else:
                    action = "保护还原失败"
                    protection_failed = True
                    # 快照记录应有值，使下一轮看到实际违规值时能再次尝试。
                    effective_card = str(protected or "")

            # C 违规名片审核。两档模式：链接直接拦，其余严格模式走初筛+LLM。
            link_only = self._cfg("card_audit_link_only", False, group_id=group_id)
            full_audit = self._cfg("card_audit_enabled", False, group_id=group_id)
            if not restored and not protection_failed and (link_only or full_audit):
                is_violation = False
                reason = ""
                if self._is_shop_link_card(card_new):
                    is_violation = True
                    reason = "含链接/店铺"
                elif full_audit:
                    hit_types = self._card_lexicon_hit(group_id, card_new)
                    force_llm = self._cfg("card_audit_llm_always", False, group_id=group_id)
                    if hit_types or force_llm:
                        is_violation = await self._card_llm_violation(
                            group_id, user_id, card_new, hit_types, allow_promo=False)
                        reason = "、".join(hit_types.keys()) if hit_types else "LLM 判定"
                if is_violation:
                    target = card_old
                    if self._is_shop_link_card(card_old) or (
                        full_audit and self._card_lexicon_hit(group_id, card_old)
                    ):
                        target = ""
                    if await self._restore_card(group_id, user_id, target):
                        action = "违规还原"
                        restored = True
                        effective_card = str(target or "")
                        if self._cfg("card_monitor_notify", True, group_id=group_id):
                            shown = target if target else "(空名片)"
                            await self._notify_card_group(
                                group_id,
                                f"[名片监控] {user_id} 的新名片「{card_new}」违规（{reason}），"
                                f"已还原为「{shown}」",
                            )
                    else:
                        action = "违规还原失败"
                        # 与保护还原相同，保留目标快照以便周期同步重试。
                        effective_card = str(target or "")

            if self._cfg("card_log_enabled", True, group_id=group_id):
                self._log_card_change("card", group_id, user_id, user_name, card_old, card_new, action)
            self._remember_card_snapshot(group_id, user_id, effective_card)
            self._card_recent_inputs[(group_id, user_id)] = (card_new, time.monotonic())
            if len(self._card_recent_inputs) > 10000:
                cutoff = time.monotonic() - 60
                self._card_recent_inputs = {
                    key: value for key, value in self._card_recent_inputs.items()
                    if value[1] >= cutoff
                }
            return restored

    @staticmethod
    def _is_shop_link_card(text: str) -> bool:
        """名片是否含店铺/推广链接 —— 命中直接还原，不走 LLM。"""
        return bool(text and _SHOP_LINK_RE.search(text))

    def _card_lexicon_hit(self, group_id: str, text: str) -> dict:
        """名片文本词库/正则初筛，返回命中的可疑类型 dict（供 LLM 二判用），无命中返回 {}。"""
        hits = {}
        if not text:
            return hits
        try:
            switch_map = self._lexicon_switch_map(group_id=group_id)
            for cat, hit in self._check_lexicon(text).items():
                if hit and switch_map.get(cat, True):
                    hits[cat] = True
            if hasattr(self, "_is_ad_pattern") and self._is_ad_pattern(text):
                hits["ad"] = True
            if hasattr(self, "_swear_matcher") and self._swear_matcher.is_match(text):
                hits["swear"] = True
        except Exception as e:
            logger.debug(f"[GroupMgr] 名片违禁词检测失败: {e}")
        # 引流可疑（加我/vx/代购/兼职…）：单独一类，不直接判违规，交 LLM 上下文定夺
        if _PROMO_SUSPECT_RE.search(text):
            hits["promo"] = True
        return hits

    async def _card_llm_violation(self, group_id: str, user_id: str, card: str, hit_types: dict,
                                  allow_promo: bool = False) -> bool:
        """LLM 上下文判断名片是否违规（与消息审核同款：正则初筛 → LLM 二判）。

        LLM 关闭/失败时回退为"按初筛命中即违规"。
        allow_promo 预留：True 时放行引流仅拦链接（当前严格模式恒传 False）。
        """
        if not self._cfg("llm_moderation_enabled", True, group_id=group_id):
            # 未开 LLM：只能按初筛判定。initial-screen 没命中(纯 llm_always 触发)时不误杀。
            return bool(hit_types)
        suspect = "、".join(hit_types.keys()) if hit_types else "无"
        system_prompt = "你是群名片审核员。判断群成员的名片是否违规。只返回严格 JSON。"
        if allow_promo:
            rule = ("本群【允许】引流、联系方式暗示、自定义昵称（如 xxxrxxx 这类分隔式联系方式），这些【不算违规】。"
                    "【只有】含明确网址链接、二维码、店铺域名/店铺名、扫码下单引导时才判违规。")
        else:
            rule = ("【违规】：引流广告（如 科技加我 / 加V123 / 招收代理 / 兼职日结）、店铺推广、"
                    "联系方式（微信/QQ/手机号）、网址链接、辱骂、色情、违禁内容；"
                    "以及看似无意义但实为引流暗号的字母缩写（如 ldxp、catfk 这类黑话）。\n"
                    "【不违规】：正常人名/昵称/网名、正常自我介绍、部门-姓名 这类规范群名片、"
                    "普通中英文昵称、游戏ID。拿不准时倾向【不违规】，避免误杀正常成员。")
        safe_card = str(card or "").translate(str.maketrans({"<": "＜", ">": "＞"}))[:2000]
        prompt = (
            "判断下面这个群名片是否违规（违规则会被自动还原）。名片是短文本，请结合整体语义判断，不要只看单个词。\n"
            f"【判定规则】{rule}\n"
            f"【群名片】<<<{safe_card}>>>\n"
            f"【正则初筛命中的可疑类型】{suspect}（仅供参考，最终由你判断；初筛命中不等于违规）\n"
            '严格返回：{"violation": true/false, "reason": "简要理由"}'
        )
        try:
            import asyncio
            import json as _json
            runner = getattr(self, "_run_llm_with_limits", None)
            if callable(runner):
                resp = await runner(
                    lambda: self._call_llm_safe(system_prompt, prompt), timeout=30.0
                )
            else:
                semaphore = getattr(self, "_llm_semaphore", None)
                acquired = False
                if semaphore is not None and hasattr(semaphore, "acquire"):
                    await asyncio.wait_for(semaphore.acquire(), timeout=10.0)
                    acquired = True
                try:
                    resp = await asyncio.wait_for(
                        self._call_llm_safe(system_prompt, prompt), timeout=30.0
                    )
                finally:
                    if acquired:
                        semaphore.release()
            m = re.search(r'\{[^{}]*"violation"[^{}]*\}', resp, re.DOTALL) or re.search(r'\{.*\}', resp, re.DOTALL)
            if m:
                data = _json.loads(m.group())
                normalized = self._normalize_llm_moderation_result(data)
                if normalized.get("fallback", False):
                    return bool(hit_types)
                return bool(normalized.get("violation", False))
        except Exception as e:
            logger.debug(f"[GroupMgr] 名片 LLM 审核失败，回退按初筛判定: {e}")
        # LLM 失败/返回无法解析：回退到初筛结论。
        # 必须是 bool(hit_types) 而不是 True——否则开了 card_audit_llm_always 时，
        # 初筛没命中的正常名片会因 LLM 不可用被全部误还原。
        return bool(hit_types)

    async def _restore_card(self, group_id: str, user_id: str, card: str) -> bool:
        """把某成员名片改回指定值。card 为空串表示清除名片。"""
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return False
        client = await self._get_client()
        if not client:
            return False
        ok, err = await self._call_group_api(client, 'set_group_card', "还原名片",
                                             group_id=gid, user_id=uid, card=card or "")
        if not ok:
            logger.debug(f"[GroupMgr] 名片还原失败({group_id}/{user_id}): {err}")
        return ok

    async def _notify_card_group(self, group_id: str, text: str) -> None:
        try:
            gid = self._safe_int(group_id, 0)
            if not gid:
                return
            client = await self._get_client()
            if client:
                ok, error = await self._call_group_api(
                    client, "send_group_msg", "发送名片监控通知",
                    group_id=gid, message=text,
                )
                if not ok:
                    logger.debug(f"[GroupMgr] 名片监控通知发送失败: {error}")
        except Exception as e:
            logger.debug(f"[GroupMgr] 名片监控通知发送失败: {e}")

    def _log_card_change(self, kind: str, group_id: str, user_id: str, user_name: str,
                         old_value: str, new_value: str, action: str) -> None:
        try:
            now = int(time.time())
            self._storage.add_card_change_log(
                kind, group_id, user_id, user_name, old_value, new_value, action,
                now, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            # 日志上限保护：偶发裁剪，避免每次都跑 DELETE
            self._card_log_seq = getattr(self, "_card_log_seq", 0) + 1
            if self._card_log_seq % 200 == 0:
                self._storage.prune_card_change_logs(5000)
        except Exception as e:
            logger.debug(f"[GroupMgr] 写名片日志失败: {e}")

    async def _handle_group_card_change(self, event: AstrMessageEvent) -> bool:
        """处理 group_card（名片变更）通知。返回 True 表示已介入。"""
        raw = self._get_raw_event(event)
        if not isinstance(raw, dict):
            return False
        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))
        if not group_id or not user_id:
            return False
        card_old = str(raw.get("card_old", "") or "")
        card_new = str(raw.get("card_new", "") or "")
        # 诊断日志：放在所有开关判断【之前】，用于区分两类故障——
        #   改名片后日志无此行 => 协议端根本没上报 group_card 事件（NapCat 配置/版本问题）
        #   有此行但无后续动作 => 开关未生效或判定逻辑问题
        return await self._process_card_values(
            group_id, user_id, card_old, card_new, event=event, source="event"
        )

    async def _handle_group_increase(self, event: AstrMessageEvent) -> bool:
        """成员入群后立即查询并审核名片，不要求成员先发言。"""
        raw = self._get_raw_event(event)
        if not isinstance(raw, dict):
            return False
        group_id = str(raw.get("group_id", "") or "")
        user_id = str(raw.get("user_id", "") or "")
        if not group_id or not user_id:
            return False
        if str(raw.get("self_id", "") or "") == user_id:
            # 机器人自身受邀/加入群时也会收到 group_increase；无需审核自己的名片。
            return False
        if not self._card_monitor_active(group_id):
            return False
        if not self._card_group_allowed(group_id):
            return False
        client = await self._get_client(event)
        member = await self._fetch_member_card(client, group_id, user_id)
        if member is None:
            # 少数协议端会把名片直接放在通知里，作为查询失败时的兼容回退。
            if "card" not in raw:
                self._ensure_card_sync_state()
                self._mark_card_pending(group_id, user_id)
                logger.debug(f"[GroupMgr] 入群名片查询无结果({group_id}/{user_id})")
                return False
            member = (str(raw.get("card", "") or ""), str(raw.get("nickname", "") or ""))
        card_new, user_name = member
        # 入群前没有可可靠取得的旧名片；违规时安全回退为空名片。
        restored = await self._process_card_values(
            group_id, user_id, "", card_new, user_name=user_name,
            event=event, source="join", force=True,
        )
        self._ensure_card_sync_state()
        self._clear_card_pending(group_id, user_id)
        return restored

    async def _sync_group_cards(self) -> int:
        """低频轮询群成员名片，弥补协议端不发送 group_card 的情况。

        首次看到某个成员只建立基线，后续轮询发现值变化才进入审核，避免
        插件启动时对整个群历史名片造成大量误处罚。入群通知路径仍会立即审查
        新成员，不受该基线策略影响。
        """
        self._ensure_card_sync_state()
        client = await self._get_client()
        if not client:
            return 0
        groups = set()
        white = {str(x) for x in (getattr(self, "_group_white_set", set()) or set())}
        if white:
            groups.update(white)
        try:
            result = await asyncio.wait_for(client.call_action("get_group_list"), timeout=20.0)
            ok, error = self._check_api_result(result, "获取群列表")
            if not ok:
                raise RuntimeError(error)
            for item in self._extract_list_result(result):
                if isinstance(item, dict) and item.get("group_id") is not None:
                    groups.add(str(item.get("group_id")))
        except Exception as e:
            logger.debug(f"[GroupMgr] 获取群列表用于名片同步失败: {e}")
            groups.update(self._card_sync_known_groups)
        # 保护名单所在群即使不在快照里也应被轮询。
        try:
            groups.update(str(item.get("group_id")) for item in self._storage.list_card_protected() if item.get("group_id"))
        except Exception:
            pass
        try:
            # 群级覆盖可能把 card_monitor_enabled 从全局关闭改为开启；即使
            # 当前还没有快照，也要把这些已配置群纳入一次查询。
            groups.update(str(gid) for gid in self._storage.list_configured_groups() if gid)
        except Exception:
            pass

        changed = 0
        for group_id in sorted(groups):
            if not self._card_group_allowed(group_id):
                continue
            if not self._card_monitor_active(group_id):
                continue
            if not self._cfg("card_sync_enabled", True, group_id=group_id):
                continue
            # 在发起 API 请求前保存比较基线。若等待响应期间扩展通知更新了
            # 快照，末尾合并时会保留较新的事件值，而不会被轮询结果覆盖。
            previous = dict(self._card_snapshots.setdefault(group_id, {}))
            pending_before = {
                item for item in self._card_pending_members if item[0] == group_id
            }
            try:
                gid = self._safe_int(group_id, 0)
                if not gid:
                    continue
                result = await asyncio.wait_for(
                    client.call_action("get_group_member_list", group_id=gid), timeout=20.0
                )
                ok, error = self._check_api_result(result, "获取群成员列表")
                if not ok:
                    logger.debug(f"[GroupMgr] 获取群成员列表失败({group_id}): {error}")
                    continue
                payload = self._extract_data_result(result)
                if not isinstance(payload, (list, dict)):
                    logger.debug(
                        f"[GroupMgr] 获取群成员列表返回不兼容数据({group_id}): "
                        f"{type(payload).__name__}"
                    )
                    continue
                members = self._extract_list_result(result)
                # 一个可查询的群至少包含机器人自身；空列表通常是协议端失败但
                # 未设置 retcode/status。保留旧快照，避免下轮把全群当首次基线。
                if not isinstance(members, list) or not members:
                    logger.debug(f"[GroupMgr] 获取群成员列表为空({group_id})，保留旧快照")
                    continue
            except Exception as e:
                logger.debug(f"[GroupMgr] 名片同步获取成员失败({group_id}): {e}")
                continue

            protected_cards = {}
            if self._cfg("card_protect_enabled", False, group_id=group_id):
                try:
                    protected_cards = {
                        str(item.get("user_id")): str(item.get("protected_card", ""))
                        for item in self._storage.list_card_protected(group_id)
                        if item.get("user_id") is not None
                    }
                except Exception as e:
                    logger.debug(f"[GroupMgr] 读取名片保护名单失败({group_id}): {e}")

            current = {}
            seen_member_ids = set()
            for member in members:
                if not isinstance(member, dict) or member.get("user_id") is None:
                    continue
                user_id = str(member.get("user_id"))
                seen_member_ids.add(user_id)
                # card 字段是 OneBot 标准；没有该字段的响应不应把默认值误当成清空。
                if "card" not in member:
                    baseline = previous.get(user_id, _CARD_SNAPSHOT_UNSET)
                    if baseline is not _CARD_SNAPSHOT_UNSET:
                        current[user_id] = baseline
                    continue
                card_new = str(member.get("card", "") or "")
                user_name = str(member.get("nickname", "") or "")
                old = previous.get(user_id)
                baseline = previous.get(user_id, _CARD_SNAPSHOT_UNSET)
                current[user_id] = card_new
                pending = (group_id, user_id) in self._card_pending_members
                protected = protected_cards.get(user_id, _CARD_SNAPSHOT_UNSET)
                protection_needed = (
                    protected is not _CARD_SNAPSHOT_UNSET and card_new != protected
                )
                if old is None and not pending and not protection_needed:
                    # 普通成员首次同步只建立基线；保护名单有明确目标值，
                    # 即使插件重启后首次看到也必须校正。
                    continue
                if pending or old != card_new or protection_needed:
                    latest = self._card_snapshots.get(group_id, {}).get(
                        user_id, _CARD_SNAPSHOT_UNSET
                    )
                    if latest != baseline:
                        # 成员列表请求返回前，实时 group_card 已写入更新值。
                        # 这里先跳过陈旧轮询，_process_card_values 内仍保留锁内
                        # expected_snapshot 校验，覆盖检查后到加锁前的竞态窗口。
                        if pending:
                            self._clear_card_pending(group_id, user_id)
                        continue
                    audit_old = "" if pending else old
                    if await self._process_card_values(
                        group_id, user_id, audit_old, card_new,
                        user_name=user_name, source="sync",
                        force=(pending or protection_needed),
                        expected_snapshot=baseline,
                    ):
                        changed += 1
                    # _process_card_values 会把还原后的有效值写入快照。
                    current[user_id] = self._card_snapshots.get(group_id, {}).get(user_id, card_new)
                self._clear_card_pending(group_id, user_id)
            active_member_ids = seen_member_ids
            # 只清理由本轮请求开始前就存在的 pending。请求期间新到达的入群
            # 事件可能尚未出现在这份成员列表里，必须留给下一轮重试。
            for pending_item in pending_before:
                if pending_item[1] not in active_member_ids:
                    misses = self._card_pending_misses.get(pending_item, 0) + 1
                    self._card_pending_misses[pending_item] = misses
                    if misses >= _CARD_PENDING_MAX_MISSES:
                        self._clear_card_pending(*pending_item)
            async with self._card_change_lock:
                latest = self._card_snapshots.setdefault(group_id, {})
                merged = {}
                missing = object()
                for user_id, polled_card in current.items():
                    old_card = previous.get(user_id, missing)
                    latest_card = latest.get(user_id, missing)
                    # 未并发更新过的成员采用本轮结果；事件路径写入的新值优先。
                    if latest_card is missing or latest_card == old_card:
                        merged[user_id] = polled_card
                    else:
                        merged[user_id] = latest_card
                for user_id, latest_card in latest.items():
                    if user_id in merged:
                        continue
                    old_card = previous.get(user_id, missing)
                    # 原快照中已有但本轮已不在成员列表的用户会被清理；本轮期间
                    # 新增或发生变化的事件快照则保留到下一轮确认。
                    if old_card is missing or latest_card != old_card:
                        merged[user_id] = latest_card
                self._card_snapshots[group_id] = merged
                self._card_sync_known_groups.add(group_id)
        if changed:
            logger.info(f"[GroupMgr] 名片周期同步处理 {changed} 条变更")
        return changed

    async def _handle_group_admin_change(self, event: AstrMessageEvent) -> bool:
        """处理 group_admin（管理员任免）通知。返回 True 表示已介入。"""
        raw = self._get_raw_event(event)
        if not isinstance(raw, dict):
            return False
        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))
        sub_type = str(raw.get("sub_type", ""))  # set / unset
        if not group_id or not user_id or sub_type not in ("set", "unset"):
            return False
        logger.info(f"[GroupMgr] 收到管理员任免事件: 群{group_id} 用户{user_id} {sub_type}")
        if not self._card_monitor_active(group_id):
            return False
        allowed, _reason = self._check_group_access(event)
        if not allowed:
            return False
        if not self._cfg("admin_change_notify_enabled", False, group_id=group_id):
            # 未开启管理员任免通知时，仍可选择记日志
            if self._cfg("card_log_enabled", True, group_id=group_id):
                self._log_card_change("admin", group_id, user_id, "", "", sub_type, "记录")
            return False

        action_cn = "被设为管理员" if sub_type == "set" else "被取消管理员"
        # 管理员角色缓存需失效（其他模块依赖群角色判定）
        try:
            self._admin_role_cache.clear()
        except Exception:
            pass
        if self._cfg("card_log_enabled", True, group_id=group_id):
            self._log_card_change("admin", group_id, user_id, "", "", sub_type, "通知")
        await self._notify_card_group(group_id, f"[群管变动] {user_id} {action_cn}")
        return True
