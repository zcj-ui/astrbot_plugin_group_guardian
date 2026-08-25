# -*- coding: utf-8 -*-
"""多协议群管操作平台路由层（v2.12.0）。

AIOCQHTTP（OneBot/QQ）平台的群管操作通过 ``call_action`` 调用 OneBot API，
由 ``OneBotMixin`` 完成。Telegram / Discord 等平台的事件 client 结构不同
（Telegram 是 ``telegram.ext.ExtBot``，Discord 是 ``discord.Bot`` 子类），
不能使用 ``call_action``。

本模块为 Telegram / Discord 提供等价群管操作封装，全部采用 duck typing
（``getattr`` + 方法调用 + try/except），**不强制 import 平台库**：
- 在纯 QQ 部署中即使未安装 python-telegram-bot / discord.py 也不会报错；
- 调用失败一律返回 False / "" 并记录日志，绝不影响主审核流程。

支持操作：撤回消息 / 禁言 / 解禁 / 踢人 / 查询成员角色（member/admin/owner）。
查询角色是「按角色分权限」的跨平台基础：启用后 Telegram/Discord 的群主与
群管理员同样获得群管操作权限与审核豁免，并按 role_*_require 配置分级。

平台路由业务入口在 ``OneBotMixin``（onebot.py）：全量模式的群管方法
（_recall_msg/_kick_member/_mute_member/_unban_member/_get_member_role）
在识别到非 AIOCQHTTP 平台时委托到本模块对应 ``_platform_*`` 方法。
"""
import asyncio
import time

from astrbot.api import logger

from .platforms import get_platform_name

# 平台调用统一超时（秒）
PLATFORM_OP_TIMEOUT = 15.0

# Telegram ChatMember.status 语义 -> 插件内部角色
_TG_STATUS_TO_ROLE = {
    "creator": "owner",
    "administrator": "admin",
}


class PlatformOpsMixin:
    """Telegram / Discord 群管操作路由。依赖 ``OneBotMixin`` 提供的通用工具。"""

    # ------------------------------------------------------------------
    # 平台识别
    # ------------------------------------------------------------------
    @staticmethod
    def _get_platform(event) -> str:
        return get_platform_name(event)

    def _platform_is_tg(self, event) -> bool:
        return get_platform_name(event) == "telegram"

    def _platform_is_dc(self, event) -> bool:
        return get_platform_name(event) == "discord"

    @staticmethod
    def _get_platform_client(event):
        """从事件取平台 client（Telegram ExtBot / Discord Bot / aiocqhttp 原生对象）。"""
        for attr in ("client", "bot"):
            client = getattr(event, attr, None)
            if client is not None:
                return client
        return None

    def _platform_routeable(self, event) -> bool:
        """该事件是否进入本模块路由（Telegram / Discord）。"""
        platform = get_platform_name(event)
        return platform in ("telegram", "discord")

    # ------------------------------------------------------------------
    # 成员角色查询（按角色分权限的跨平台基础）
    # ------------------------------------------------------------------
    async def _platform_get_member_role(self, event, group_id: str, user_id: str) -> str:
        """按平台查询成员角色，返回 member/admin/owner；失败返回 ''（表示未知）。"""
        platform = get_platform_name(event)
        if platform == "telegram":
            return await self._tg_get_member_role(event, group_id, user_id)
        if platform == "discord":
            return await self._dc_get_member_role(event, group_id, user_id)
        return ""

    async def _tg_get_member_role(self, event, group_id: str, user_id: str) -> str:
        client = self._get_platform_client(event)
        if client is None:
            return ""
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return ""
        try:
            member = await asyncio.wait_for(
                client.get_chat_member(chat_id=gid, user_id=uid),
                timeout=PLATFORM_OP_TIMEOUT,
            )
            status = str(getattr(member, "status", "") or "")
            return _TG_STATUS_TO_ROLE.get(status, "member")
        except Exception as e:
            logger.debug(f"[GroupMgr] Telegram 查询成员角色失败({group_id}/{user_id}): {e}")
            return ""

    async def _dc_get_member_role(self, event, group_id: str, user_id: str) -> str:
        client = self._get_platform_client(event)
        if client is None:
            return ""
        try:
            guild = await self._dc_get_guild(client, group_id)
            if guild is None:
                return ""
            uid = self._safe_int(user_id, 0)
            if not uid:
                return ""
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await asyncio.wait_for(
                        guild.fetch_member(uid), timeout=PLATFORM_OP_TIMEOUT
                    )
                except Exception:
                    member = None
            if member is None:
                return "member"
            if str(getattr(guild, "owner_id", "")) == str(getattr(member, "id", "")):
                return "owner"
            perms = getattr(member, "guild_permissions", None)
            if perms is not None and bool(getattr(perms, "administrator", False)):
                return "admin"
            return "member"
        except Exception as e:
            logger.debug(f"[GroupMgr] Discord 查询成员角色失败({group_id}/{user_id}): {e}")
            return ""

    async def _dc_get_guild(self, client, group_id: str):
        """Discord 取 guild（服务器）；get 不到时 fetch。失败返回 None。"""
        gid = self._safe_int(group_id, 0)
        if not gid:
            return None
        try:
            guild = client.get_guild(gid)
            if guild is None and hasattr(client, "fetch_guild"):
                guild = await asyncio.wait_for(
                    client.fetch_guild(gid), timeout=PLATFORM_OP_TIMEOUT
                )
            return guild
        except Exception as e:
            logger.debug(f"[GroupMgr] Discord 获取服务器失败({group_id}): {e}")
            return None


    # ------------------------------------------------------------------
    # 撤回消息
    # ------------------------------------------------------------------
    async def _platform_recall_msg(self, event, msg_id: str) -> bool:
        platform = get_platform_name(event)
        if platform == "telegram":
            return await self._tg_recall_msg(event, msg_id)
        if platform == "discord":
            return await self._dc_recall_msg(event, msg_id)
        return False

    async def _tg_recall_msg(self, event, msg_id: str) -> bool:
        client = self._get_platform_client(event)
        if client is None:
            return False
        gid = self._safe_int(self._get_group_id(event), 0)
        mid = self._safe_int(msg_id, 0)
        if not gid or not mid:
            return False
        try:
            await asyncio.wait_for(
                client.delete_message(chat_id=gid, message_id=mid),
                timeout=PLATFORM_OP_TIMEOUT,
            )
            logger.info(f"[GroupMgr] Telegram 已撤回消息 {msg_id}")
            return True
        except Exception as e:
            logger.debug(f"[GroupMgr] Telegram 撤回失败({msg_id}): {e}")
            return False

    async def _dc_recall_msg(self, event, msg_id: str) -> bool:
        client = self._get_platform_client(event)
        if client is None:
            return False
        mid = self._safe_int(msg_id, 0)
        if not mid:
            return False
        # Discord 撤回需要频道上下文：频道 id 取事件的 session_id / message_obj
        channel_id = self._safe_int(getattr(event, "session_id", 0), 0)
        if not channel_id:
            msg_obj = getattr(event, "message_obj", None)
            channel_id = self._safe_int(getattr(msg_obj, "session_id", 0), 0)
        if not channel_id:
            logger.debug("[GroupMgr] Discord 撤回缺少频道上下文(session_id)")
            return False
        try:
            channel = client.get_channel(channel_id)
            if channel is None and hasattr(client, "fetch_channel"):
                channel = await asyncio.wait_for(
                    client.fetch_channel(channel_id), timeout=PLATFORM_OP_TIMEOUT
                )
            if channel is None:
                return False
            message = await asyncio.wait_for(
                channel.fetch_message(mid), timeout=PLATFORM_OP_TIMEOUT
            )
            await asyncio.wait_for(message.delete(), timeout=PLATFORM_OP_TIMEOUT)
            logger.info(f"[GroupMgr] Discord 已撤回消息 {msg_id}")
            return True
        except Exception as e:
            logger.debug(f"[GroupMgr] Discord 撤回失败({msg_id}): {e}")
            return False

    # ------------------------------------------------------------------
    # 禁言 / 解禁
    # ------------------------------------------------------------------
    async def _platform_mute_member(self, event, group_id: str, user_id: str, duration: int) -> bool:
        platform = get_platform_name(event)
        if platform == "telegram":
            return await self._tg_mute_member(event, group_id, user_id, duration)
        if platform == "discord":
            return await self._dc_mute_member(event, group_id, user_id, duration)
        return False

    async def _tg_mute_member(self, event, group_id: str, user_id: str, duration: int) -> bool:
        client = self._get_platform_client(event)
        if client is None:
            return False
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return False
        try:
            # Telegram 无独立禁言：以"到时自动解封"的临时 ban 模拟（until_date 时间戳）
            until = int(time.time()) + max(1, int(duration or 0))
            await asyncio.wait_for(
                client.ban_chat_member(chat_id=gid, user_id=uid, until_date=until),
                timeout=PLATFORM_OP_TIMEOUT,
            )
            logger.info(f"[GroupMgr] Telegram 已禁言 {user_id} {duration}s")
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] Telegram 禁言失败({user_id}): {e}")
            return False

    async def _dc_mute_member(self, event, group_id: str, user_id: str, duration: int) -> bool:
        client = self._get_platform_client(event)
        if client is None:
            return False
        try:
            guild = await self._dc_get_guild(client, group_id)
            if guild is None:
                return False
            uid = self._safe_int(user_id, 0)
            if not uid:
                return False
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await asyncio.wait_for(
                        guild.fetch_member(uid), timeout=PLATFORM_OP_TIMEOUT
                    )
                except Exception:
                    member = None
            if member is None:
                return False
            # Discord 禁言 = 超时（timeout），需要 datetime 对象
            from datetime import datetime, timedelta

            until = datetime.now() + timedelta(seconds=max(1, int(duration or 0)))
            await asyncio.wait_for(
                member.timeout(until), timeout=PLATFORM_OP_TIMEOUT
            )
            logger.info(f"[GroupMgr] Discord 已禁言 {user_id} {duration}s")
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] Discord 禁言失败({user_id}): {e}")
            return False

    async def _platform_unban_member(self, event, group_id: str, user_id: str) -> bool:
        platform = get_platform_name(event)
        if platform == "telegram":
            return await self._tg_unban_member(event, group_id, user_id)
        if platform == "discord":
            return await self._dc_unban_member(event, group_id, user_id)
        return False

    async def _tg_unban_member(self, event, group_id: str, user_id: str) -> bool:
        client = self._get_platform_client(event)
        if client is None:
            return False
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return False
        try:
            await asyncio.wait_for(
                client.unban_chat_member(chat_id=gid, user_id=uid, only_if_banned=True),
                timeout=PLATFORM_OP_TIMEOUT,
            )
            return True
        except Exception as e:
            logger.debug(f"[GroupMgr] Telegram 解禁失败({user_id}): {e}")
            return False

    async def _dc_unban_member(self, event, group_id: str, user_id: str) -> bool:
        client = self._get_platform_client(event)
        if client is None:
            return False
        try:
            guild = await self._dc_get_guild(client, group_id)
            if guild is None:
                return False
            uid = self._safe_int(user_id, 0)
            if not uid:
                return False
            member = guild.get_member(uid)
            if member is None:
                return False
            # 解除超时（timeout(None)）
            await asyncio.wait_for(member.timeout(None), timeout=PLATFORM_OP_TIMEOUT)
            return True
        except Exception as e:
            logger.debug(f"[GroupMgr] Discord 解禁失败({user_id}): {e}")
            return False

    # ------------------------------------------------------------------
    # 踢人
    # ------------------------------------------------------------------
    async def _platform_kick_member(self, event, group_id: str, user_id: str) -> bool:
        platform = get_platform_name(event)
        if platform == "telegram":
            return await self._tg_kick_member(event, group_id, user_id)
        if platform == "discord":
            return await self._dc_kick_member(event, group_id, user_id)
        return False

    async def _tg_kick_member(self, event, group_id: str, user_id: str) -> bool:
        client = self._get_platform_client(event)
        if client is None:
            return False
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return False
        try:
            # Telegram 无独立踢人：永久 ban 即踢出；随后解封以便其可重新申请入群
            await asyncio.wait_for(
                client.ban_chat_member(chat_id=gid, user_id=uid),
                timeout=PLATFORM_OP_TIMEOUT,
            )
            try:
                await asyncio.wait_for(
                    client.unban_chat_member(chat_id=gid, user_id=uid, only_if_banned=True),
                    timeout=PLATFORM_OP_TIMEOUT,
                )
            except Exception:
                pass
            logger.info(f"[GroupMgr] Telegram 已踢出 {user_id}")
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] Telegram 踢人失败({user_id}): {e}")
            return False

    async def _dc_kick_member(self, event, group_id: str, user_id: str) -> bool:
        client = self._get_platform_client(event)
        if client is None:
            return False
        try:
            guild = await self._dc_get_guild(client, group_id)
            if guild is None:
                return False
            uid = self._safe_int(user_id, 0)
            if not uid:
                return False
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await asyncio.wait_for(
                        guild.fetch_member(uid), timeout=PLATFORM_OP_TIMEOUT
                    )
                except Exception:
                    member = None
            if member is None:
                return False
            await asyncio.wait_for(
                member.kick(reason="群守护: 违规踢出"), timeout=PLATFORM_OP_TIMEOUT
            )
            logger.info(f"[GroupMgr] Discord 已踢出 {user_id}")
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] Discord 踢人失败({user_id}): {e}")
            return False
