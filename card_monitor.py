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


class CardMonitorMixin:
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
        prompt = (
            "判断下面这个群名片是否违规（违规则会被自动还原）。名片是短文本，请结合整体语义判断，不要只看单个词。\n"
            f"【判定规则】{rule}\n"
            f"【群名片】<<<{card}>>>\n"
            f"【正则初筛命中的可疑类型】{suspect}（仅供参考，最终由你判断；初筛命中不等于违规）\n"
            '严格返回：{"violation": true/false, "reason": "简要理由"}'
        )
        try:
            import asyncio
            import json as _json
            async with self._llm_semaphore:
                resp = await asyncio.wait_for(self._call_llm_safe(system_prompt, prompt), timeout=30.0)
            m = re.search(r'\{[^{}]*"violation"[^{}]*\}', resp, re.DOTALL) or re.search(r'\{.*\}', resp, re.DOTALL)
            if m:
                data = _json.loads(m.group())
                return self._normalize_llm_moderation_result(data).get("violation", False)
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
                await client.call_action("send_group_msg", group_id=gid, message=text)
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
        logger.info(f"[GroupMgr] 收到名片变更事件: 群{group_id} 用户{user_id} 「{card_old}」->「{card_new}」")
        # 总开关（默认关闭），且复用群黑白名单范围
        if not self._cfg("card_monitor_enabled", False, group_id=group_id):
            logger.debug(f"[GroupMgr] 名片监控总开关未开启，跳过（群{group_id}）")
            return False
        allowed, _reason = self._check_group_access(event)
        if not allowed:
            logger.debug(f"[GroupMgr] 名片监控跳过：{_reason}")
            return False
        if card_old == card_new:
            return False

        action = "记录"
        restored = False

        # B 名片保护/还原：被保护成员名片被改 → 改回预设值
        if self._cfg("card_protect_enabled", False, group_id=group_id):
            protected = None
            try:
                protected = self._storage.get_card_protected(group_id, user_id)
            except Exception:
                protected = None
            if protected is not None and card_new != protected:
                if await self._restore_card(group_id, user_id, protected):
                    action = "保护还原"
                    restored = True
                    if self._cfg("card_monitor_notify", True, group_id=group_id):
                        await self._notify_card_group(
                            group_id, f"[名片监控] {user_id} 的名片被改为「{card_new}」，已还原为受保护名片「{protected}」")

        # C 违规名片审核。两档模式，【开得越多必须拦得越严】：
        #   - card_audit_link_only（宽松）：只拦明确的链接/店铺/扫码，放行"科技加我""xxxrxxx"等引流昵称
        #   - card_audit_enabled（严格/全量）：链接照拦，另外引流/违禁词经"正则初筛+LLM上下文判断"也拦
        #     配 card_audit_llm_always 时，初筛没命中也送 LLM，识别 ldxp/catfk 这类无规律缩写黑话
        # 两个都开 => 严格模式生效（严格是宽松的超集）。绝不能因为多开了 link_only 反而放行引流。
        link_only = self._cfg("card_audit_link_only", False, group_id=group_id)
        full_audit = self._cfg("card_audit_enabled", False, group_id=group_id)
        if not restored and (link_only or full_audit):
            is_violation = False
            reason = ""
            if self._is_shop_link_card(card_new):
                # 两种模式都直接拦链接，不经 LLM
                is_violation = True
                reason = "含链接/店铺"
            elif full_audit:
                # 严格模式：正则/词库初筛 → LLM 上下文二判（同消息审核流程）
                hit_types = self._card_lexicon_hit(group_id, card_new)
                force_llm = self._cfg("card_audit_llm_always", False, group_id=group_id)
                if hit_types or force_llm:
                    is_violation = await self._card_llm_violation(
                        group_id, user_id, card_new, hit_types, allow_promo=False)
                    reason = "、".join(hit_types.keys()) if hit_types else "LLM 判定"
            if is_violation:
                target = card_old
                if self._is_shop_link_card(card_old) or (full_audit and self._card_lexicon_hit(group_id, card_old)):
                    target = ""
                if await self._restore_card(group_id, user_id, target):
                    action = "违规还原"
                    restored = True
                    if self._cfg("card_monitor_notify", True, group_id=group_id):
                        shown = target if target else "(空名片)"
                        await self._notify_card_group(
                            group_id, f"[名片监控] {user_id} 的新名片「{card_new}」违规（{reason}），已还原为「{shown}」")

        # A 名片变更日志：始终记录（无论是否还原）
        if self._cfg("card_log_enabled", True, group_id=group_id):
            self._log_card_change("card", group_id, user_id, "", card_old, card_new, action)

        return restored

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
        if not self._cfg("card_monitor_enabled", False, group_id=group_id):
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
