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
        # 三级判断：先查配置 admin_list（含同步的 AstrBot 管理员），再查群角色缓存，最后调 get_group_member_info 获取 role。
        user_id = self._try_get_sender_id(event)
        if not user_id:
            logger.warning(f"[GroupMgr] _is_admin 无法获取user_id from {type(event).__name__}")
            return False

        astrbot_admin_ids = []
        try:
            ab_config = getattr(self.context, 'astrbot_config', None)
            if ab_config:
                astrbot_admin_ids = [str(x).strip() for x in (ab_config.get('admin_id', []) or []) if str(x).strip()]
        except Exception as _e:
            logger.debug(f"[GroupMgr] 获取AstrBot管理员列表失败: {_e}")
        try:
            config_admins = self.config.get("admin_list", [])
            all_admins = set(astrbot_admin_ids) | set(str(a).strip() for a in config_admins if a)
            if user_id in all_admins:
                return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 读取config admin_list失败: {e}")

        group_id = self._get_group_id(event)
        if group_id:
            cache_key = f"{group_id}:{user_id}"
            if len(self._admin_role_cache) > 1000:
                now = time.time()
                self._admin_role_cache = {
                    k: v for k, v in self._admin_role_cache.items()
                    if now - v[1] < self._admin_role_cache_ttl
                }
            cached = self._admin_role_cache.get(cache_key)
            if cached:
                is_admin_val, ts = cached
                if time.time() - ts < self._admin_role_cache_ttl:
                    return is_admin_val

            group_id_int = self._safe_int(group_id, 0)
            user_id_int = self._safe_int(user_id, 0)
            if not group_id_int or not user_id_int:
                return False
            try:
                client = await self._get_client(event)
                if client:
                    info = await client.call_action('get_group_member_info', group_id=group_id_int, user_id=user_id_int, no_cache=False)
                    info = self._extract_data_result(info)
                    if info:
                        role = info.get('role', '')
                        is_admin_val = role in ('admin', 'owner')
                        self._admin_role_cache[cache_key] = (is_admin_val, time.time())
                        return is_admin_val
            except Exception as e:
                logger.debug(f"[GroupMgr] 获取群成员信息失败: {e}")

        return False

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
        # 复合检查：管理员身份 → _cfg_check（插件/功能启用状态）→ 群黑白名单，任一失败即拒绝。
        if need_admin and not await self._is_admin(event):
            return False, "仅管理员可以使用此功能"
        ok, msg = self._cfg_check(cfg_key, feature_name)
        if not ok:
            return False, msg
        allowed, reason = self._check_group_access(event)
        if not allowed:
            return False, reason
        return True, ""

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
