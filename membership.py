# -*- coding: utf-8 -*-
"""入群自动审核（F1，v2.4.0）。

监听 OneBot 加群请求事件（post_type=request, request_type=group），按规则判定：
- 命中拒绝词 / 违禁词库 → 自动拒绝（带理由）
- 命中通过词 → 自动通过
- 都不命中 → 按默认动作（manual 转人工 / accept / reject）

规则来源：优先读 SQLite(join_audit_rules) 中该群规则，其次 'default' 全局规则，
最后回退到 _conf_schema.json 的全局配置项。所有动作写入审核日志。
"""
import time

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
        comment_lower = comment.lower()

        for kw in rule.get("reject_keywords", []):
            if kw and str(kw).lower() in comment_lower:
                reason = rule.get("reject_reason", "") or "申请信息命中拒绝规则"
                await self._process_group_request(event, flag, sub_type, False, reason)
                self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", f"命中拒绝词: {kw}", [])
                await self._notify_join_audit(group_id, user_id, comment, False, f"命中拒绝词: {kw}")
                return True

        if self._cfg("join_reject_use_lexicon", True, group_id=group_id) and comment:
            lex_hit = self._check_lexicon(comment)
            switch_map = self._lexicon_switch_map(group_id=group_id)
            lex_blocked = any(hit and switch_map.get(cat, True) for cat, hit in lex_hit.items())
            ad_hit = self._is_ad_pattern(comment) if hasattr(self, "_is_ad_pattern") else False
            if lex_blocked or ad_hit:
                reason = rule.get("reject_reason", "") or "申请信息含违规内容"
                await self._process_group_request(event, flag, sub_type, False, reason)
                self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", "命中违禁词库/广告", [])
                await self._notify_join_audit(group_id, user_id, comment, False, "命中违禁词库/广告")
                return True

        for kw in rule.get("accept_keywords", []):
            if kw and str(kw).lower() in comment_lower:
                await self._process_group_request(event, flag, sub_type, True, "")
                self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群通过", f"命中通过词: {kw}", [])
                await self._notify_join_audit(group_id, user_id, comment, True, f"命中通过词: {kw}")
                return True

        # QQ等级自动审核：根据配置的最低等级要求自动通过或拒绝
        # 注意：get_stranger_info 对非好友陌生人通常返回 level=0（无法获取真实等级），
        # 因此等级为 0 时视为"未知"，跳过等级检查走默认动作，避免误拒。
        if self._cfg("join_qq_level_check_enabled", False, group_id=group_id):
            min_level = self._cfg_int("join_qq_level_min", 0, group_id=group_id)
            if min_level > 0:
                # 获取申请人QQ等级
                nickname, qq_level = await self._get_user_info(user_id)
                if qq_level == 0:
                    # 等级未知（陌生人API通常返回0），跳过等级检查，走默认动作
                    logger.debug(f"[GroupMgr] QQ等级获取为0（未知），跳过等级检查: {user_id}")
                elif qq_level >= min_level:
                    # 等级达标，自动通过
                    await self._process_group_request(event, flag, sub_type, True, "")
                    self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群通过", f"QQ等级达标({qq_level}>={min_level})", [])
                    await self._notify_join_audit(group_id, user_id, comment, True, f"QQ等级{qq_level}>=要求{min_level}")
                    return True
                else:
                    # 等级确实不足（获取到了非零值但低于阈值），自动拒绝
                    reason = rule.get("reject_reason", "") or f"QQ等级不足(需要{min_level}级)"
                    await self._process_group_request(event, flag, sub_type, False, reason)
                    self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "入群拒绝", f"QQ等级不足({qq_level}<{min_level})", [])
                    await self._notify_join_audit(group_id, user_id, comment, False, f"QQ等级{qq_level}<要求{min_level}")
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
        elif default_action == "manual":
            # 转人工：发送待审通知到群，等待管理员引用回复审核
            await self._send_pending_join_notification(group_id, user_id, comment, flag, sub_type)
            self._log_moderation(group_id, user_id, "", f"[加群申请] {comment}", "转人工审核", "等待管理员处理", [])
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

    async def _send_pending_join_notification(self, group_id: str, user_id: str, comment: str, flag: str, sub_type: str) -> None:
        """发送待审通知到群，并存储待审请求信息供管理员引用回复审核。

        Args:
            group_id: 群号
            user_id: 申请人QQ号
            comment: 验证信息
            flag: OneBot 加群请求的 flag 标识
            sub_type: 请求类型（add/invite）
        """
        gid_int = self._safe_int(group_id, 0)
        if not gid_int:
            return
        try:
            # 获取申请人信息（昵称和QQ等级）
            nickname, qq_level = await self._get_user_info(user_id)
            # 构建待审通知消息
            level_text = f"QQ等级: {qq_level}" if qq_level else "QQ等级: 未知"
            text = (
                f"[入群审核] 收到新的加群申请\n"
                f"申请人: {nickname}({user_id})\n"
                f"{level_text}\n"
                f"验证信息: {comment[:100] if comment else '无'}\n"
                f"请管理员引用此消息回复「通过」或「拒绝 [原因]」进行审核"
            )
            client = await self._get_client()
            if not client:
                return
            # 发送通知到群
            result = await client.call_action("send_group_msg", group_id=gid_int, message=text)
            result = self._extract_data_result(result)
            # 提取发送成功的消息 ID
            msg_id = ""
            if isinstance(result, dict):
                msg_id = str(result.get("message_id", "") or result.get("id", ""))
            if not msg_id:
                logger.debug(f"[GroupMgr] 发送待审通知失败: 无法获取 message_id")
                return
            # 存储待审请求信息，供管理员引用回复时使用
            pending_key = f"{group_id}:{msg_id}"
            now_ts = int(time.time())
            self._pending_join_requests[pending_key] = {
                "user_id": user_id,
                "flag": flag,
                "sub_type": sub_type,
                "comment": comment,
                "nickname": nickname,
                "timestamp": now_ts,
            }
            # 同步写入 DB，保证机器人重启后引用回复审核依然可用
            try:
                self._storage.save_pending_join_request(
                    pending_key, group_id, user_id, flag, sub_type, comment, nickname, now_ts
                )
            except Exception as ex:
                logger.debug(f"[GroupMgr] 待审请求写 DB 失败: {ex}")
            # 限制待审请求数量，防止内存泄漏（保留最近 100 条）
            if len(self._pending_join_requests) > 100:
                # 按时间戳排序，删除最旧的
                sorted_items = sorted(self._pending_join_requests.items(), key=lambda x: x[1].get("timestamp", 0))
                for old_key, _ in sorted_items[:20]:
                    del self._pending_join_requests[old_key]
                    try:
                        self._storage.delete_pending_join_request(old_key)
                    except Exception:
                        pass
            logger.debug(f"[GroupMgr] 已存储待审请求: {pending_key}")
        except Exception as e:
            logger.debug(f"[GroupMgr] 发送待审通知失败: {e}")

    async def _get_user_info(self, user_id: str) -> tuple:
        """获取用户昵称和QQ等级（通过 OneBot API 获取陌生人信息）。

        Args:
            user_id: 用户QQ号

        Returns:
            tuple: (昵称, QQ等级)，失败时返回 (QQ号, 0)
        """
        uid_int = self._safe_int(user_id, 0)
        if not uid_int:
            return user_id, 0
        try:
            client = await self._get_client()
            if not client:
                return user_id, 0
            result = await client.call_action("get_stranger_info", user_id=uid_int)
            result = self._extract_data_result(result)
            if isinstance(result, dict):
                nickname = result.get("nickname", "") or result.get("name", "") or user_id
                qq_level = self._safe_int(result.get("level", 0), 0)
                return nickname, qq_level
        except Exception as e:
            logger.debug(f"[GroupMgr] 获取用户信息失败: {e}")
        return user_id, 0

    async def _handle_join_reply(self, event: AstrMessageEvent) -> bool:
        """处理管理员引用回复待审通知消息的审核操作。

        当管理员引用一条待审通知消息并回复「通过」或「拒绝 [原因]」时，
        自动处理加群申请。

        Args:
            event: 消息事件对象

        Returns:
            bool: 是否已处理（True 表示已处理，应 stop_event）
        """
        # 检查是否为群消息
        group_id = self._get_group_id(event)
        if not group_id:
            return False
        # 检查操作者是否为管理员
        if not await self._is_admin(event):
            logger.debug(f"[GroupMgr] 入群审核回复: 操作者非管理员，跳过")
            return False
        # 获取被回复消息的 ID
        reply_msg_id = self._get_reply_message_id(event)
        if not reply_msg_id:
            return False
        # 检查是否为待审通知消息
        pending_key = f"{group_id}:{reply_msg_id}"
        pending_info = self._pending_join_requests.get(pending_key)
        if not pending_info:
            logger.debug(f"[GroupMgr] 入群审核回复: 未找到待审请求 {pending_key}（可能已处理或重启前未持久化）")
            return False
        # 获取回复内容：只取 text 段，避免 @机器人 前缀导致关键字命中失败
        reply_text = self._extract_plain_text(event)
        if not reply_text:
            # 回退到 message_str，兼容部分适配器
            reply_text = (event.message_str or "").strip()
        if not reply_text:
            return False
        # 解析审核动作
        approve = None
        reject_reason = ""
        if reply_text.startswith("通过") or reply_text == "同意" or reply_text == "批准":
            approve = True
        elif reply_text.startswith("拒绝"):
            approve = False
            # 提取拒绝原因（「拒绝」后面的内容）
            reject_reason = reply_text[2:].strip()
            if not reject_reason:
                reject_reason = "管理员拒绝"
        else:
            # 不是审核指令，不处理
            logger.debug(f"[GroupMgr] 入群审核回复: 回复内容「{reply_text[:30]}」不是审核指令，跳过")
            return False
        # 从存储中获取待审请求信息
        user_id = pending_info.get("user_id", "")
        flag = pending_info.get("flag", "")
        sub_type = pending_info.get("sub_type", "add")
        # 移除已处理的待审请求（内存 + DB 同步删除）
        del self._pending_join_requests[pending_key]
        try:
            self._storage.delete_pending_join_request(pending_key)
        except Exception as ex:
            logger.debug(f"[GroupMgr] 删除待审请求 DB 记录失败: {ex}")
        # 执行审核操作
        try:
            await self._process_group_request(event, flag, sub_type, approve, reject_reason)
            # 记录审核日志
            action_text = "入群通过" if approve else "入群拒绝"
            reason_text = "管理员手动通过" if approve else f"管理员手动拒绝: {reject_reason}"
            self._log_moderation(group_id, user_id, pending_info.get("nickname", ""),
                                 f"[加群申请] {pending_info.get('comment', '')}",
                                 action_text, reason_text, [])
            # 发送审核结果通知
            operator_id = self._try_get_sender_id(event)
            operator_nickname = self._try_get_sender_nickname(event)
            operator_display = f"{operator_nickname}({operator_id})" if operator_nickname else operator_id
            nickname = pending_info.get("nickname", "") or user_id
            if approve:
                notice = f"[入群审核] {nickname}({user_id}) 的申请已通过\n操作人: {operator_display}"
            else:
                notice = f"[入群审核] {nickname}({user_id}) 的申请已被拒绝\n操作人: {operator_display}\n拒绝原因: {reject_reason}"
            # 发送通知到群
            gid_int = self._safe_int(group_id, 0)
            if gid_int:
                client = await self._get_client()
                if client:
                    await client.call_action("send_group_msg", group_id=gid_int, message=notice)
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 处理管理员审核失败: {e}")
            return False

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
