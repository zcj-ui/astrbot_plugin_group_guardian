# -*- coding: utf-8 -*-
import inspect
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
        # AstrBot 文档里的 aiocqhttp client 调用形态是 client.api.call_action()；旧版本/部分适配器
        # 也可能直接暴露 client.call_action()。这里统一归一化为"可直接 call_action 的对象"。
        if event:
            client = self._normalize_action_client(getattr(event, 'bot', None))
            if client:
                self._client = client
                return client
        cached = self._normalize_action_client(self._client)
        if cached:
            self._client = cached
            return cached
        try:
            pm = self.context.platform_manager
            if hasattr(pm, 'get_insts'):
                platforms = pm.get_insts() or []
            else:
                platforms = pm._platforms.values() if hasattr(pm, '_platforms') else []
            for platform in platforms:
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if inspect.isawaitable(client):
                        client = await client
                    client = self._normalize_action_client(client)
                    if client:
                        self._client = client
                        return client
                for attr in ("client", "bot", "api"):
                    client = self._normalize_action_client(getattr(platform, attr, None))
                    if client:
                        self._client = client
                        return client
        except Exception as e:
            logger.debug(f"[GroupMgr] 从 platform_manager 获取 client 失败: {e}")
        return None

    @staticmethod
    def _normalize_action_client(candidate):
        if not candidate:
            return None
        if hasattr(candidate, 'call_action'):
            return candidate
        api = getattr(candidate, 'api', None)
        if api and hasattr(api, 'call_action'):
            return api
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

    def _get_all_admin_ids(self) -> set:
        # 合并插件管理员名单(DB) + AstrBot 全局 admin_id
        try:
            astrbot_admin_ids = []
            ab_config = getattr(self.context, 'astrbot_config', None)
            if ab_config:
                astrbot_admin_ids = [str(x).strip() for x in (ab_config.get('admin_id', []) or []) if str(x).strip()]
            return set(self._get_admin_list()) | set(astrbot_admin_ids)
        except Exception as e:
            logger.warning(f"[GroupMgr] 读取管理员名单失败: {e}")
            return set(self._get_admin_list())

    def _is_group_admin_blocked(self, group_id: str, user_id: str) -> bool:
        if not group_id:
            return False
        try:
            return self._storage.is_group_admin_blocked(group_id, user_id)
        except Exception as e:
            logger.debug(f"[GroupMgr] 查询群权限黑名单失败: {e}")
            return False

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        # "群操作权限"判定，判定顺序：
        #   ① 群级 bot 权限黑名单(最高优先) → ② 全局管理员名单
        #   → ③ 群超管 → ④ 群角色授权（白名单内 + F5/legacy 开关）
        user_id = self._try_get_sender_id(event)
        if not user_id:
            logger.warning(f"[GroupMgr] _is_admin 无法获取user_id from {type(event).__name__}")
            return False

        group_id = self._get_group_id(event)

        # ① 群级 bot 权限黑名单
        if self._is_group_admin_blocked(group_id, user_id):
            return False

        # ② 全局管理员名单
        if user_id in self._get_all_admin_ids():
            return True

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

        # 群角色授权仅在"允许管理的群"内生效：配置了白名单时必须在白名单内；
        # 未配置白名单时，黑名单群一律不授权。这样群主/群管的群操作权限被限定在
        # 其拥有管理权且被允许的群，避免任意群的群管自动获得本插件群操作能力。
        if self._group_white_set:
            if group_id not in self._group_white_set:
                return False
        elif self._group_black_set and group_id in self._group_black_set:
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
        # 老行为兼容：未配置 F5 时，允许范围内的 owner/admin 默认拥有群操作权限（可由开关关闭）
        return self._cfg("legacy_role_admin_enabled", True)

    async def _is_plugin_admin(self, event: AstrMessageEvent) -> bool:
        """"插件全局管理员"判定：仅认全局插件管理员名单 + AstrBot 全局 admin_id。

        与 _is_admin 的区别：群主/群管理员/群超管的"群角色授权"不算插件管理员。
        用于真正的插件级操作（管理插件管理员名单、改全局运行开关等）。
        """
        user_id = self._try_get_sender_id(event)
        if not user_id:
            return False
        if self._is_group_admin_blocked(self._get_group_id(event), user_id):
            return False
        return user_id in self._get_all_admin_ids()

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

    async def _get_bot_uin(self, client) -> int:
        """获取当前 bot 自身 QQ 号（带缓存）。失败返回 0。"""
        cached = getattr(self, "_bot_uin_cache", 0)
        if cached:
            return cached
        try:
            info = await client.call_action("get_login_info")
            info = self._extract_data_result(info)
            uin = self._safe_int(info.get("user_id", 0), 0) if isinstance(info, dict) else 0
            if uin:
                self._bot_uin_cache = uin
            return uin
        except Exception as e:
            logger.debug(f"[GroupMgr] 获取 bot QQ 失败: {e}")
            return 0

    async def _get_role_by_id(self, client, group_id, user_id) -> str:
        """直接用 client 查某成员在群里的角色（member/admin/owner），无 event 版本。失败返回 ''。"""
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid or not client:
            return ""
        try:
            info = await client.call_action("get_group_member_info", group_id=gid, user_id=uid, no_cache=False)
            info = self._extract_data_result(info)
            if isinstance(info, dict):
                return info.get("role", "") or ""
        except Exception as e:
            logger.debug(f"[GroupMgr] 查询群成员角色失败({group_id}/{user_id}): {e}")
        return ""

    async def _precheck_member_action(self, client, group_id, target_uid, action: str) -> Tuple[bool, str]:
        """群成员操作前置校验：检查 bot 自身权限 + 目标角色，避免必然失败的调用。

        规则（OneBot/QQ 平台限制）：
          - bot 必须是管理员或群主，否则无法禁言/踢人/改名片等；
          - 不能对群主执行（禁言/踢/改名片）；
          - 普通管理员（bot 非群主）不能操作其他管理员。
        仅对写操作做检查；返回 (允许, 错误说明)。
        """
        # 仅这些操作需要目标角色保护
        if action not in ("ban", "kick", "set_card", "set_title", "set_admin", "unset_admin"):
            return True, ""
        bot_uin = await self._get_bot_uin(client)
        bot_role = await self._get_role_by_id(client, group_id, bot_uin) if bot_uin else ""
        if bot_role not in ("admin", "owner"):
            return False, "机器人在该群不是管理员/群主，无法执行群管操作"
        target_role = await self._get_role_by_id(client, group_id, target_uid)
        if target_role == "owner":
            return False, "目标是群主，无法操作"
        if target_role == "admin" and bot_role != "owner":
            return False, "目标是管理员，机器人需为群主才能操作"
        return True, ""

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
        precheck_action: str = "",
    ) -> Tuple[bool, str, object, int, int]:
        """统一准备群成员操作所需的权限、client、群号和目标 QQ 号。

        precheck_action 非空时，额外做 bot 自身权限 + 目标角色（群主/管理员）预检，
        避免对群主/管理员执行必然失败的操作。
        """
        ok, err = await self._check_admin_cfg_access(event, cfg_key, feature_name)
        if not ok:
            return False, err, None, 0, 0
        _, client, gid, err = await self._get_group_client(event, need_gid=True)
        if not client:
            return False, err, None, 0, 0
        uid = self._safe_int(user_id, 0)
        if not uid:
            return False, "用户QQ号格式无效", None, 0, 0
        if precheck_action:
            ok_pre, pre_msg = await self._precheck_member_action(client, gid, uid, precheck_action)
            if not ok_pre:
                return False, pre_msg, None, 0, 0
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
        ban_duration = duration if duration is not None else self._cfg_int("moderation_ban_duration", 1800, group_id=group_id)
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
