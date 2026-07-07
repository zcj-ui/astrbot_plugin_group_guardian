# -*- coding: utf-8 -*-
"""入群自动审核（F1，v2.4.0）。

监听 OneBot 加群请求事件（post_type=request, request_type=group），按规则判定：
- 命中拒绝词 / 违禁词库 → 自动拒绝（带理由）
- 命中通过词 → 自动通过
- 都不命中 → 按默认动作（manual 转人工 / accept / reject）

规则来源：优先读 SQLite(join_audit_rules) 中该群规则，其次 'default' 全局规则，
最后回退到 _conf_schema.json 的全局配置项。所有动作写入审核日志。
"""
import re as _re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class MembershipMixin:
    @staticmethod
    def _extract_join_answer(comment: str) -> str:
        """从加群申请 comment 中提取用户实际填写的答案（Issue #41）。

        群设置了验证问题时，OneBot 上报的 comment 是「问题：xxx\\n答案：yyy」的拼接，
        直接对全文做关键词匹配会把问题原文里的词（如"本群"的"群"）误判为用户输入。
        无法识别格式时原样返回，向后兼容无验证问题的场景。
        """
        if not comment:
            return comment
        m = _re.search(r"答案[:：]\s*(.*)\s*$", comment, _re.DOTALL)
        if m:
            return m.group(1).strip()
        return comment
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
        if rule is not None:
            return rule
        # 回退全局配置
        return {
            "accept_keywords": self.config.get("join_accept_keywords", []) or [],
            "reject_keywords": self.config.get("join_reject_keywords", []) or [],
            "default_action": self._cfg_str("join_default_action", "manual", group_id=group_id) or "manual",
            "reject_reason": self._cfg_str("join_reject_reason", "", group_id=group_id),
            "enabled": True,
        }

    async def _handle_group_request(self, event: AstrMessageEvent) -> bool:
        """加群申请审核主流程。返回 True 表示已处理（应 stop_event），False 表示未介入。"""
        if not self.config.get("disclaimer_agreed", False):
            return False
        raw = self._get_raw_event(event)
        if not isinstance(raw, dict):
            return False
        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))
        flag = raw.get("flag", "")
        comment = str(raw.get("comment", "") or "")
        sub_type = raw.get("sub_type", "add")
        if not group_id or not flag:
            return False
        if not self._cfg("join_audit_enabled", False, group_id=group_id):
            return False
        allowed, _reason = self._check_group_access(event)
        if not allowed:
            return False
        if user_id and user_id in self._user_black_set:
            await self._process_group_request(event, flag, sub_type, False, "您在黑名单中，无法加入")
            self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", "用户黑名单", [])
            await self._notify_join_audit(group_id, user_id, comment, False, "用户黑名单")
            return True

        rule = self._resolve_join_rule(group_id)
        if not rule.get("enabled", True):
            return False
        # Issue #41：所有匹配只针对用户实际填写的答案，剥离验证问题原文，
        # 避免问题里的词（如"你从哪里知道本群的"中的"群"）被误判为用户输入
        answer = self._extract_join_answer(comment)
        answer_lower = answer.lower()

        for kw in rule.get("reject_keywords", []):
            if kw and str(kw).lower() in answer_lower:
                reason = rule.get("reject_reason", "") or "申请信息命中拒绝规则"
                await self._process_group_request(event, flag, sub_type, False, reason)
                self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", f"命中拒绝词: {kw}", [])
                await self._notify_join_audit(group_id, user_id, comment, False, f"命中拒绝词: {kw}")
                return True

        if self._cfg("join_reject_use_lexicon", True, group_id=group_id) and answer:
            lex_hit = self._check_lexicon(answer)
            switch_map = self._lexicon_switch_map(group_id=group_id)
            lex_blocked = any(hit and switch_map.get(cat, True) for cat, hit in lex_hit.items())
            ad_hit = self._is_ad_pattern(answer) if hasattr(self, "_is_ad_pattern") else False
            if lex_blocked or ad_hit:
                reason = rule.get("reject_reason", "") or "申请信息含违规内容"
                await self._process_group_request(event, flag, sub_type, False, reason)
                self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", "命中违禁词库/广告", [])
                await self._notify_join_audit(group_id, user_id, comment, False, "命中违禁词库/广告")
                return True

        for kw in rule.get("accept_keywords", []):
            if kw and str(kw).lower() in answer_lower:
                await self._process_group_request(event, flag, sub_type, True, "")
                self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群通过", f"命中通过词: {kw}", [])
                await self._notify_join_audit(group_id, user_id, comment, True, f"命中通过词: {kw}")
                return True

        default_action = rule.get("default_action", "manual")
        if default_action == "accept":
            await self._process_group_request(event, flag, sub_type, True, "")
            self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群通过", "默认通过", [])
            await self._notify_join_audit(group_id, user_id, comment, True, "默认通过")
            return True
        elif default_action == "reject":
            reason = rule.get("reject_reason", "") or "不符合入群条件"
            await self._process_group_request(event, flag, sub_type, False, reason)
            self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", "默认拒绝", [])
            await self._notify_join_audit(group_id, user_id, comment, False, "默认拒绝")
            return True
        return False

    async def _notify_join_audit(self, group_id: str, user_id: str, comment: str, approved: bool, reason: str) -> None:
        if not self._cfg("join_audit_notify", True, group_id=group_id):
            return
        action = "通过" if approved else "拒绝"
        text = f"[入群审核] {user_id} 申请加群已{action}\n验证信息: {comment[:80] if comment else '无'}\n原因: {reason}"
        try:
            from astrbot.api.event import MessageChain
            gid_int = self._safe_int(group_id, 0)
            if gid_int:
                client = await self._get_client()
                if client:
                    await client.call_action("send_group_msg", group_id=gid_int, message=text)
        except Exception as e:
            logger.debug(f"[GroupMgr] 入群审核通知发送失败: {e}")

    async def _process_group_request(self, event: AstrMessageEvent, flag: str, sub_type: str,
                                     approve: bool, reason: str = "") -> bool:
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
