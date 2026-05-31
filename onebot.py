# -*- coding: utf-8 -*-
import time
from typing import Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


class OneBotMixin:
    # 统一封装 OneBot / AIOCQHTTP 客户端获取和 API 调用。
    # _get_client 有多级回退：先从事件中取 -> 从缓存的 self._client 取 -> 从 platform_manager 中获取。
    # _call_group_api 对所有群管理 API 做统一的返回值兼容处理。
    async def _get_client(self, event: AstrMessageEvent = None):
        # 三级回退：优先从 event.bot 取，其次用缓存的 self._client，最后遍历 platform_manager 查找可用实例。
        if event:
            client = getattr(event, 'bot', None)
            if client and hasattr(client, 'call_action'):
                self._client = client
                return client
        if self._client and hasattr(self._client, 'call_action'):
            return self._client
        try:
            pm = self.context.platform_manager
            if hasattr(pm, 'get_insts'):
                platforms = pm.get_insts() or []
            else:
                platforms = pm._platforms.values() if hasattr(pm, '_platforms') else []
            for platform in platforms:
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client and hasattr(client, 'call_action'):
                        self._client = client
                        return client
                elif hasattr(platform, 'client') and hasattr(platform.client, 'call_action'):
                    self._client = platform.client
                    return platform.client
        except Exception as e:
            logger.debug(f"[GroupMgr] 从 platform_manager 获取 client 失败: {e}")
        return None

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            if hasattr(event, 'group_id') and event.group_id:
                return str(event.group_id)
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'group_id'):
                return str(event.message_obj.group_id)
            if hasattr(event, 'raw_message') and hasattr(event.raw_message, 'group_id'):
                return str(event.raw_message.group_id)
            gid = event.get_group_id()
            if gid:
                return str(gid)
        except Exception as _e:
            logger.debug(f"[GroupMgr] _get_group_id fallback: {_e}")
        return ""

    def _try_get_sender_id(self, event: AstrMessageEvent) -> str:
        for getter in [
            lambda: str(event.get_sender_id()) if event.get_sender_id() else None,
            lambda: str(event.sender.user_id) if hasattr(event, 'sender') and hasattr(event.sender, 'user_id') else None,
            lambda: str(event.user_id) if hasattr(event, 'user_id') else None,
            lambda: str((getattr(event, 'raw_event', None) or {}).get('user_id') or (getattr(event, 'raw_event', None) or {}).get('sender', {}).get('user_id')) or None,
            lambda: str(event.message_obj.sender.user_id) if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'sender') and hasattr(event.message_obj.sender, 'user_id') else None,
        ]:
            try:
                result = getter()
                if result and result != 'None':
                    return result
            except Exception:
                pass
        return ""

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        # 判定顺序：① 群级 bot 权限黑名单(最高优先,群主可剥夺) → ② 全局插件管理员名单
        #          → ③ 群超管(该群专属) → ④ 群角色 + F5 动态授权 / 老行为开关。
        user_id = self._try_get_sender_id(event)
        if not user_id:
            logger.warning(f"[GroupMgr] _is_admin 无法获取user_id from {type(event).__name__}")
            return False

        group_id = self._get_group_id(event)

        # ① 群级 bot 权限黑名单：群主可移除本群某群管的 bot 管理权限，优先级最高
        if group_id:
            try:
                if self._storage.is_group_admin_blocked(group_id, user_id):
                    return False
            except Exception as e:
                logger.debug(f"[GroupMgr] 查询群权限黑名单失败: {e}")

        # ② 全局管理员名单：self.admin_list(DB为准) + AstrBot 全局 admin_id
        try:
            astrbot_admin_ids = []
            ab_config = getattr(self.context, 'astrbot_config', None)
            if ab_config:
                astrbot_admin_ids = [str(x).strip() for x in (ab_config.get('admin_id', []) or []) if str(x).strip()]
            all_admins = set(self._get_admin_list()) | set(astrbot_admin_ids)
            if user_id in all_admins:
                return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 读取管理员名单失败: {e}")

        if not group_id:
            return False

        # ③ 群超管：在 WebUI 为该群单独设置的专属管理员
        try:
            if self._storage.is_group_super_admin(group_id, user_id):
                return True
        except Exception as e:
            logger.debug(f"[GroupMgr] 查询群超管失败: {e}")

        # ④ 群角色判定
        role = await self._get_member_role(event, group_id, user_id)
        if role not in ("admin", "owner"):
            return False

        # F5 动态授权：若该群在授权表且启用，按 grant_owner/grant_admin 实时判定
        if self._cfg("group_admin_grant_enabled", False):
            grant = self._storage.get_group_admin_grant(group_id)
            if grant and grant.get("enabled"):
                if role == "owner" and grant.get("grant_owner"):
                    return True
                if role == "admin" and grant.get("grant_admin"):
                    return True
                return False  # 该群已显式配置授权，但当前角色不在授权范围
        # 老行为兼容：未配置 F5 时，任何群的 owner/admin 默认视为插件管理员（可由开关关闭）
        return self._cfg("legacy_role_admin_enabled", True)

    async def _get_member_role(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        """获取成员在群里的角色（member/admin/owner），带短 TTL 缓存。

        缓存存"角色字符串"而非"是否管理员"，使 F5 授权配置变更后无需等缓存过期即可反映；
        TTL 较短（默认 10 秒），保证"下管理"后很快失效。
        """
        cache_key = f"{group_id}:{user_id}"
        now = time.time()
        # 容量保护：超过 1000 条时清理过期项
        if len(self._admin_role_cache) > 1000:
            self._admin_role_cache = {
                k: v for k, v in self._admin_role_cache.items()
                if now - v[1] < self._admin_role_cache_ttl
            }
        cached = self._admin_role_cache.get(cache_key)
        if cached and now - cached[1] < self._admin_role_cache_ttl:
            return cached[0]

        group_id_int = self._safe_int(group_id, 0)
        user_id_int = self._safe_int(user_id, 0)
        if not group_id_int or not user_id_int:
            return ""
        try:
            client = await self._get_client(event)
            if client:
                info = await client.call_action('get_group_member_info', group_id=group_id_int, user_id=user_id_int, no_cache=False)
                info = self._extract_data_result(info)
                if info:
                    role = info.get('role', '') or ""
                    self._admin_role_cache[cache_key] = (role, now)
                    return role
        except Exception as e:
            logger.debug(f"[GroupMgr] 获取群成员信息失败: {e}")
        return ""

    def _check_group_access(self, event: AstrMessageEvent) -> Tuple[bool, str]:
        group_id = self._get_group_id(event)
        if not group_id:
            return True, ""
        if self._group_black_set and group_id in self._group_black_set:
            return False, f"群 {group_id} 在黑名单中"
        if self._group_white_set:
            if group_id not in self._group_white_set:
                return False, f"群 {group_id} 不在白名单中"
        return True, ""

    async def _check_admin_cfg_access(self, event: AstrMessageEvent, cfg_key: str, feature_name: str, need_admin: bool = True) -> Tuple[bool, str]:
        # 复合检查：管理员身份 → _cfg_check（插件/功能启用状态，按群）→ 群黑白名单，任一失败即拒绝。
        if need_admin and not await self._is_admin(event):
            return False, "仅管理员可以使用此功能"
        gid = self._get_group_id(event)
        ok, msg = self._cfg_check(cfg_key, feature_name, group_id=gid)
        if not ok:
            return False, msg
        allowed, reason = self._check_group_access(event)
        if not allowed:
            return False, reason
        return True, ""

    async def _prepare_group_member_action(
        self,
        event: AstrMessageEvent,
        cfg_key: str,
        feature_name: str,
        user_id,
    ) -> Tuple[bool, str, object, int, int]:
        """统一准备群成员操作所需的权限、client、群号和目标 QQ 号。"""
        ok, err = await self._check_admin_cfg_access(event, cfg_key, feature_name)
        if not ok:
            return False, err, None, 0, 0
        _, client, gid, err = await self._get_group_client(event, need_gid=True)
        if not client:
            return False, err, None, 0, 0
        uid = self._safe_int(user_id, 0)
        if not uid:
            return False, "用户QQ号格式无效", None, 0, 0
        return True, "", client, gid, uid

    async def _prepare_group_action(
        self,
        event: AstrMessageEvent,
        cfg_key: str,
        feature_name: str,
        need_admin: bool = True,
        need_gid: bool = True,
    ) -> Tuple[bool, str, object, int]:
        """统一准备群操作所需的权限、client 和可选群号。"""
        ok, err = await self._check_admin_cfg_access(event, cfg_key, feature_name, need_admin=need_admin)
        if not ok:
            return False, err, None, 0
        if need_gid:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                return False, err, None, 0
            return True, "", client, gid
        _, client, err = await self._get_group_client(event)
        if not client:
            return False, err, None, 0
        return True, "", client, 0

    async def _prepare_message_action(
        self,
        event: AstrMessageEvent,
        cfg_key: str,
        feature_name: str,
        message_id,
    ) -> Tuple[bool, str, object, int]:
        """统一准备基于 message_id 的操作。"""
        ok, err, client, _ = await self._prepare_group_action(
            event, cfg_key, feature_name, need_gid=False
        )
        if not ok:
            return False, err, None, 0
        mid = self._safe_int(message_id, 0)
        if not mid:
            return False, "消息ID格式无效", None, 0
        return True, "", client, mid

    async def _get_group_client(self, event: AstrMessageEvent, need_gid: bool = False) -> Tuple:
        # 同时获取 group_id（字符串）和 client，并按 need_gid 决定是否返回 int 格式的 gid。
        group_id = self._get_group_id(event)
        if not group_id:
            return (None, None, None, "无法获取群号") if need_gid else (None, None, "无法获取群号")
        client = await self._get_client(event)
        if not client:
            return (None, None, None, "无法获取QQ客户端") if need_gid else (None, None, "无法获取QQ客户端")
        if need_gid:
            gid = self._safe_int(group_id, 0)
            if not gid:
                return None, None, None, "群号格式无效"
            return group_id, client, gid, ""
        return group_id, client, ""

    async def _call_group_api(self, client, action: str, result_name: str = "", **kwargs) -> Tuple[bool, str]:
        # 调用 OneBot API 并用 _check_api_result 统一判断结果（status=failed 或 retcode!=0 视为失败）。
        try:
            result = await client.call_action(action, **kwargs)
            ok, err = self._check_api_result(result, result_name or action)
            if not ok:
                return False, err
            return True, ""
        except Exception as e:
            return False, str(e)

    async def _recall_msg(self, event: AiocqhttpMessageEvent, msg_id: str):
        mid = self._safe_int(msg_id)
        if not mid:
            return
        client = await self._get_client(event)
        if not client:
            return
        try:
            await client.call_action('delete_msg', message_id=mid)
        except Exception as e:
            logger.warning(f"[GroupMgr] 撤回消息失败: {e}")
            self._client = None

    async def _kick_member(self, event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        user_id = self._try_get_sender_id(event)
        if not group_id or not user_id:
            return
        client = await self._get_client(event)
        if not client:
            return
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return
        try:
            await client.call_action('set_group_kick', group_id=gid, user_id=uid)
        except Exception as e:
            logger.warning(f"[GroupMgr] 踢人失败: {e}")
            self._client = None

    async def _mute_member(self, event: AiocqhttpMessageEvent, duration: int = None):
        group_id = self._get_group_id(event)
        user_id = self._try_get_sender_id(event)
        if not group_id or not user_id:
            return
        client = await self._get_client(event)
        if not client:
            return
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return
        ban_duration = duration if duration is not None else self._safe_int(self.config.get("moderation_ban_duration", 1800), 1800)
        try:
            await client.call_action('set_group_ban', group_id=gid, user_id=uid, duration=ban_duration)
        except Exception as e:
            logger.warning(f"[GroupMgr] 禁言失败: {e}")
            self._client = None

    async def _send_private_msg(self, user_id: str, text: str, event: AstrMessageEvent = None) -> bool:
        # 给指定 QQ 发送私聊消息。非好友等情况可能失败，返回是否成功，调用方需容错。
        uid = self._safe_int(user_id, 0)
        if not uid or not text:
            return False
        client = await self._get_client(event)
        if not client:
            return False
        try:
            await client.call_action('send_private_msg', user_id=uid, message=str(text))
            return True
        except Exception as e:
            logger.debug(f"[GroupMgr] 私聊发送失败({user_id}): {e}")
            return False

    async def _unban_member(self, group_id, user_id, event: AstrMessageEvent = None) -> bool:
        # 解除某群成员禁言（set_group_ban duration=0）。用于定时解禁、申诉通过等场景。
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return False
        client = await self._get_client(event)
        if not client:
            return False
        try:
            await client.call_action('set_group_ban', group_id=gid, user_id=uid, duration=0)
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 解禁失败({group_id}/{user_id}): {e}")
            self._client = None
            return False

    async def _mute_member_by_id(self, group_id, user_id, duration: int, event: AstrMessageEvent = None) -> bool:
        # 按 群号+QQ 直接禁言（不依赖 event 的发送者），供批量禁言复用。duration 单位秒。
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return False
        client = await self._get_client(event)
        if not client:
            return False
        try:
            await client.call_action('set_group_ban', group_id=gid, user_id=uid, duration=int(duration))
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 禁言失败({group_id}/{user_id}): {e}")
            self._client = None
            return False

    async def _kick_member_by_id(self, group_id, user_id, event: AstrMessageEvent = None) -> bool:
        # 按 群号+QQ 直接踢人，供批量踢人复用。
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return False
        client = await self._get_client(event)
        if not client:
            return False
        try:
            await client.call_action('set_group_kick', group_id=gid, user_id=uid)
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 踢人失败({group_id}/{user_id}): {e}")
            self._client = None
            return False
