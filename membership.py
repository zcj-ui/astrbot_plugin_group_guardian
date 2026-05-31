# -*- coding: utf-8 -*-
"""入群自动审核（F1，v2.4.0）。

监听 OneBot 加群请求事件（post_type=request, request_type=group），按规则判定：
- 命中拒绝词 / 违禁词库 → 自动拒绝（带理由）
- 命中通过词 → 自动通过
- 都不命中 → 按默认动作（manual 转人工 / accept / reject）

规则来源：优先读 SQLite(join_audit_rules) 中该群规则，其次 'default' 全局规则，
最后回退到 _conf_schema.json 的全局配置项。所有动作写入审核日志。
"""
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class MembershipMixin:
    def _is_group_request_event(self, event: AstrMessageEvent) -> bool:
        """判断是否为加群申请事件。"""
        raw = self._get_raw_event(event)
        if not isinstance(raw, dict):
            return False
        return (raw.get("post_type") == "request"
                and raw.get("request_type") == "group"
                and raw.get("sub_type") in ("add", "invite"))

    @staticmethod
    def _get_raw_event(event: AstrMessageEvent):
        """取 OneBot 原始事件 dict（兼容 raw_event / message_obj.raw_message 两种来源）。"""
        raw = getattr(event, "raw_event", None)
        if isinstance(raw, dict):
            return raw
        msg_obj = getattr(event, "message_obj", None)
        raw = getattr(msg_obj, "raw_message", None) if msg_obj else None
        return raw if isinstance(raw, dict) else None

    def _resolve_join_rule(self, group_id: str) -> dict:
        """解析某群生效的入群审核规则：群级 → default → 全局配置兜底。"""
        rule = None
        try:
            rule = self._storage.get_join_audit_rule(group_id)
            if rule is None:
                rule = self._storage.get_join_audit_rule("default")
        except Exception as e:
            logger.debug(f"[GroupMgr] 读取入群规则失败: {e}")
        if rule and rule.get("enabled"):
            return rule
        # 回退全局配置
        return {
            "accept_keywords": self.config.get("join_accept_keywords", []) or [],
            "reject_keywords": self.config.get("join_reject_keywords", []) or [],
            "default_action": str(self.config.get("join_default_action", "manual") or "manual"),
            "reject_reason": str(self.config.get("join_reject_reason", "") or ""),
            "enabled": True,
        }

    async def _handle_group_request(self, event: AstrMessageEvent):
        """加群申请审核主流程。"""
        if not self.config.get("disclaimer_agreed", False):
            return
        raw = self._get_raw_event(event)
        if not isinstance(raw, dict):
            return
        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))
        flag = raw.get("flag", "")
        comment = str(raw.get("comment", "") or "")
        sub_type = raw.get("sub_type", "add")
        if not group_id or not flag:
            return
        # 入群审核开关按群生效（群可单独开关）
        if not self._cfg("join_audit_enabled", False, group_id=group_id):
            return
        # 群范围检查（复用黑白名单）
        allowed, _reason = self._check_group_access(event)
        if not allowed:
            return
        # 用户黑名单：直接拒绝
        if user_id and user_id in self._user_black_set:
            await self._process_group_request(event, flag, sub_type, False, "您在黑名单中，无法加入")
            self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", "用户黑名单", [])
            return

        rule = self._resolve_join_rule(group_id)
        comment_lower = comment.lower()

        # ① 拒绝词
        for kw in rule.get("reject_keywords", []):
            if kw and str(kw).lower() in comment_lower:
                reason = rule.get("reject_reason", "") or "申请信息命中拒绝规则"
                await self._process_group_request(event, flag, sub_type, False, reason)
                self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", f"命中拒绝词: {kw}", [])
                return

        # ② 违禁词库（可选）
        if self._cfg("join_reject_use_lexicon", True, group_id=group_id) and comment:
            lex_hit = self._check_lexicon(comment)
            ad_hit = self._is_ad_pattern(comment) if hasattr(self, "_is_ad_pattern") else False
            if any(lex_hit.values()) or ad_hit:
                reason = rule.get("reject_reason", "") or "申请信息含违规内容"
                await self._process_group_request(event, flag, sub_type, False, reason)
                self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", "命中违禁词库/广告", [])
                return

        # ③ 通过词
        for kw in rule.get("accept_keywords", []):
            if kw and str(kw).lower() in comment_lower:
                await self._process_group_request(event, flag, sub_type, True, "")
                self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群通过", f"命中通过词: {kw}", [])
                return

        # ④ 默认动作
        default_action = rule.get("default_action", "manual")
        if default_action == "accept":
            await self._process_group_request(event, flag, sub_type, True, "")
            self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群通过", "默认通过", [])
        elif default_action == "reject":
            reason = rule.get("reject_reason", "") or "不符合入群条件"
            await self._process_group_request(event, flag, sub_type, False, reason)
            self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", "默认拒绝", [])
        # manual：不处理，留给人工审核

    async def _process_group_request(self, event: AstrMessageEvent, flag: str, sub_type: str,
                                     approve: bool, reason: str = "") -> bool:
        """调用 OneBot set_group_add_request 处理加群申请。"""
        client = await self._get_client(event)
        if not client:
            return False
        try:
            payload = {"flag": flag, "sub_type": sub_type, "approve": approve}
            if not approve and reason:
                payload["reason"] = reason
            await client.call_action("set_group_add_request", **payload)
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 处理加群申请失败: {e}")
            return False
