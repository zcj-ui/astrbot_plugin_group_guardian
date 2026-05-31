# -*- coding: utf-8 -*-
"""刷屏申诉工作流（F2，v2.4.0）。

流程：
1. 防刷屏/审核处罚成功后，若开启申诉，调用 _open_appeal：
   - 登记一条 waiting 申诉到 SQLite(appeals)
   - 在群内 @ 当事人，要求其私聊机器人说明原因
2. 当事人私聊机器人 → _handle_private_appeal 命中其 waiting 申诉：
   - 抓取该用户在涉事群最近 N 条上下文（不足则尽量取）
   - 组装「申诉理由 + 群内上下文 + 原处罚」交给 LLM 复合审核
   - 申诉成立 → 解禁 + 标记 approved；不成立 → 维持 + 标记 rejected
3. 超时无申诉 → 后台任务（scheduler）标记 expired，维持原处罚。

跨群/私聊场景下不用 SessionController，改用 SQLite 状态机跟踪。
"""
import json
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class AppealMixin:
    async def _open_appeal(self, event: AstrMessageEvent, group_id: str, user_id: str,
                           user_name: str, reason: str, penalty: str, mute_duration: int) -> None:
        """处罚后登记申诉并群内 @ 当事人。失败不影响已执行的处罚。"""
        if not self._cfg("appeal_enabled", False, group_id=group_id):
            return
        if not group_id or not user_id:
            return
        window_min = self._cfg_int("appeal_window_minutes", 10, group_id=group_id)
        window_min = max(1, min(window_min, 1440))
        now = int(time.time())
        expire_at = now + window_min * 60
        try:
            self._storage.open_appeal(group_id, user_id, reason, penalty, mute_duration, now, expire_at)
        except Exception as e:
            logger.warning(f"[GroupMgr] 登记申诉失败: {e}")
            return
        # 群内 @ 公示
        tmpl = self._cfg_str("appeal_at_template", "", group_id=group_id) or (
            "{name} 你被判定为刷屏并已处理。若有异议，请在 {minutes} 分钟内私聊我说明原因，我会复核。"
        )
        text = tmpl.replace("{name}", user_name or user_id).replace("{minutes}", str(window_min))
        try:
            import astrbot.api.message_components as Comp
            chain = [Comp.At(qq=user_id), Comp.Plain(" " + text)]
            await event.send(event.chain_result(chain))
        except Exception as e:
            logger.debug(f"[GroupMgr] 申诉@公示发送失败: {e}")

    def _has_waiting_appeal(self, user_id: str) -> bool:
        """快速判断某用户是否有 waiting 申诉（私聊 handler 用来决定是否进入裁决）。"""
        if not self._cfg("appeal_enabled", False) or not user_id:
            return False
        try:
            return self._storage.get_waiting_appeal(user_id) is not None
        except Exception:
            return False

    async def _handle_private_appeal(self, event: AstrMessageEvent):
        """私聊裁决：拉取该用户群内上下文 + LLM 复合审核，给出通过/驳回。

        调用前应先用 _has_waiting_appeal 确认存在 waiting 申诉。
        本方法是 async generator，只负责 yield 回复，不返回值。
        """
        user_id = self._try_get_sender_id(event)
        if not user_id:
            return
        appeal = self._storage.get_waiting_appeal(user_id)
        if not appeal:
            return
        # 过期保护：私聊来得太晚
        if appeal.get("expire_at", 0) and int(time.time()) > appeal["expire_at"]:
            self._storage.set_appeal_status(appeal["id"], "expired", int(time.time()))
            yield event.plain_result("你的申诉已超时，处罚维持。")
            return

        statement = (event.message_str or "").strip()
        if not statement:
            yield event.plain_result("请用文字说明你的申诉理由。")
            return

        group_id = appeal.get("group_id", "")
        yield event.plain_result("已收到你的申诉，正在结合群内记录复核，请稍候…")

        try:
            verdict = await self._judge_appeal(group_id, user_id, statement, appeal)
        except Exception as e:
            logger.warning(f"[GroupMgr] 申诉复核出错: {e}")
            yield event.plain_result("复核过程出错，处罚暂维持，请稍后联系管理员。")
            return

        now = int(time.time())
        if verdict.get("appeal_valid"):
            unbanned = await self._unban_member(group_id, user_id, event)
            try:
                self._storage.delete_scheduled_unban_by_target(group_id, user_id)
            except Exception:
                pass
            self._storage.set_appeal_status(appeal["id"], "approved", now)
            self._log_moderation(group_id, user_id, event.get_sender_name(),
                                 f"[申诉] {statement[:100]}", "申诉通过",
                                 verdict.get("reason", ""), [])
            tip = "申诉通过，已为你解除禁言。" if unbanned else "申诉通过。（解禁可能需要机器人具备管理员权限）"
            yield event.plain_result(f"{tip}\n复核说明：{verdict.get('reason', '')}")
        else:
            self._storage.set_appeal_status(appeal["id"], "rejected", now)
            self._log_moderation(group_id, user_id, event.get_sender_name(),
                                 f"[申诉] {statement[:100]}", "申诉驳回",
                                 verdict.get("reason", ""), [])
            yield event.plain_result(f"申诉未通过，处罚维持。\n复核说明：{verdict.get('reason', '')}")

    async def _judge_appeal(self, group_id: str, user_id: str, statement: str, appeal: dict) -> dict:
        """LLM 复合审核：结合申诉理由 + 群内上下文 + 原处罚，返回 {appeal_valid, reason}。"""
        count = self._cfg_int("appeal_context_count", 30, group_id=group_id)
        count = max(1, min(count, 100))
        context_text = await self._fetch_user_context(group_id, user_id, count)
        penalty = appeal.get("penalty", "")
        orig_reason = appeal.get("reason", "")

        system_prompt = (
            "你是群聊处罚申诉复核员。请结合「申诉人陈述」「该用户在群内的近期发言」「原处罚信息」，"
            "判断这次处罚是否应当撤销。只返回严格 JSON：{\"appeal_valid\": true/false, \"reason\": \"简要理由\"}。"
        )
        prompt = (
            "【原处罚信息】\n"
            f"处罚类型：{penalty}\n"
            f"处罚原因：{orig_reason}\n\n"
            "【申诉人陈述】\n"
            f"{statement}\n\n"
            "【该用户群内近期发言】\n"
            f"{context_text or '（未能获取到群内记录）'}\n\n"
            "判断标准：若用户确属误判（如正常聊天被刷屏规则误伤、解释合理），appeal_valid=true 撤销处罚；"
            "若确有刷屏/违规且申诉理由不成立，appeal_valid=false 维持。请只返回 JSON。"
        )
        async with self._llm_semaphore:
            resp = await self._call_llm_safe(system_prompt, prompt)
        return self._parse_appeal_verdict(resp)

    @staticmethod
    def _parse_appeal_verdict(resp: str) -> dict:
        """从 LLM 文本里解析裁决 JSON，做布尔归一化与容错。"""
        if not resp:
            return {"appeal_valid": False, "reason": "复核无响应，维持处罚"}
        match = re.search(r'\{[^{}]*"appeal_valid"[^{}]*\}', resp, re.DOTALL)
        if not match:
            match = re.search(r'\{.*\}', resp, re.DOTALL)
        if not match:
            return {"appeal_valid": False, "reason": "复核结果无法解析，维持处罚"}
        try:
            data = json.loads(match.group())
        except Exception:
            return {"appeal_valid": False, "reason": "复核结果解析失败，维持处罚"}
        raw = data.get("appeal_valid", False)
        if isinstance(raw, bool):
            valid = raw
        elif isinstance(raw, (int, float)):
            valid = raw != 0
        elif isinstance(raw, str):
            valid = raw.strip().lower() in ("true", "1", "yes", "是", "成立", "通过")
        else:
            valid = False
        return {"appeal_valid": valid, "reason": str(data.get("reason", "") or "无理由")}

    async def _fetch_user_context(self, group_id: str, user_id: str, count: int) -> str:
        """抓取某用户在指定群的最近发言（不足则尽量取），格式化为文本。"""
        if not group_id:
            return ""
        # 复用审核管线的历史拉取，多取一些再按用户过滤
        msgs = await self._fetch_context_messages(group_id, current_msg_id="", count=min(count * 3, 100))
        lines = []
        for m in msgs:
            sender = m.get("sender") or {}
            uid = str(sender.get("user_id", "")) if isinstance(sender, dict) else ""
            if uid != str(user_id):
                continue
            content = self._format_message_content(m.get("message", ""))
            if content:
                lines.append(content[:200])
            if len(lines) >= count:
                break
        return "\n".join(lines)

    async def _expire_appeals(self) -> None:
        """后台任务调用：把过期仍 waiting 的申诉标记 expired（维持处罚）。"""
        now = int(time.time())
        try:
            expired = self._storage.list_expired_waiting_appeals(now)
        except Exception as e:
            logger.debug(f"[GroupMgr] 查询过期申诉失败: {e}")
            return
        for ap in expired:
            self._storage.set_appeal_status(ap["id"], "expired", now)
