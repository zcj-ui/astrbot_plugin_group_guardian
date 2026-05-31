# -*- coding: utf-8 -*-
"""WebUI 远程执行模块（v2.4.0）。

提供统一入口 _remote_execute(group_id, action, params)，让 WebUI 面板可以：
1. 对指定群、指定成员远程执行任意群管操作（禁言/踢人/设名片/头衔/精华/公告/群名…）；
2. 对一批成员批量执行同一操作（批量禁言/踢人/设名片…）。

设计说明：
- 不经过聊天指令解析，直接调用 OneBot client.call_action，复用 onebot.py 的 _call_group_api；
- 后台无 event，client 通过 _get_client(None) 的 platform_manager 回退获取；
- 每个 action 声明所需参数与所属功能开关，未开启的功能拒绝执行；
- 批量操作逐个执行并间隔 0.3s 防 API 限频，返回每个目标的成功/失败明细。
"""
import asyncio

from astrbot.api import logger

# 远程操作注册表：action -> (功能开关配置key, 中文名, 是否需要 user_id)
# 功能开关沿用插件已有的 *_enabled 配置，保证 WebUI 与聊天指令权限一致。
_REMOTE_ACTIONS = {
    "ban":            ("ban_enabled", "禁言", True),
    "unban":          ("unban_enabled", "解禁", True),
    "kick":           ("kick_enabled", "踢人", True),
    "set_card":       ("set_card_enabled", "设置名片", True),
    "set_title":      ("set_title_enabled", "设置头衔", True),
    "set_admin":      ("set_admin_enabled", "设置管理员", True),
    "unset_admin":    ("set_admin_enabled", "取消管理员", True),
    "whole_ban":      ("whole_ban_enabled", "全体禁言", False),
    "whole_unban":    ("whole_ban_enabled", "解除全体禁言", False),
    "set_group_name": ("set_group_name_enabled", "修改群名", False),
    "send_notice":    ("send_announcement_enabled", "发群公告", False),
    "recall":         ("recall_enabled", "撤回消息", False),
    "set_essence":    ("essence_enabled", "设精华", False),
    "del_essence":    ("essence_enabled", "取消精华", False),
}


class RemoteMixin:
    def _remote_actions_meta(self) -> list:
        """返回所有可远程执行的操作元数据，供 WebUI 渲染下拉菜单。"""
        return [
            {"action": a, "name": meta[1], "need_user": meta[2], "enabled": self._cfg(meta[0], True)}
            for a, meta in _REMOTE_ACTIONS.items()
        ]

    async def _remote_execute_single(self, client, gid: int, action: str, user_id: str, params: dict) -> tuple:
        """执行单个远程操作，返回 (ok, msg)。"""
        params = params or {}
        uid = self._safe_int(user_id, 0) if user_id else 0

        if action == "ban":
            minutes = self._clamp_int(params.get("duration_minutes", 10), 10, 1, 43200)
            ok, err = await self._call_group_api(client, "set_group_ban", "禁言",
                                                 group_id=gid, user_id=uid, duration=minutes * 60)
            if ok:
                self._schedule_unban(str(gid), str(user_id), minutes * 60)
            return ok, err
        if action == "unban":
            ok, err = await self._call_group_api(client, "set_group_ban", "解禁",
                                                 group_id=gid, user_id=uid, duration=0)
            return ok, err
        if action == "kick":
            return await self._call_group_api(client, "set_group_kick", "踢人", group_id=gid, user_id=uid)
        if action == "set_card":
            return await self._call_group_api(client, "set_group_card", "设置名片",
                                              group_id=gid, user_id=uid, card=str(params.get("card", "")))
        if action == "set_title":
            return await self._call_group_api(client, "set_group_special_title", "设置头衔",
                                              group_id=gid, user_id=uid, special_title=str(params.get("title", "")), duration=-1)
        if action == "set_admin":
            return await self._call_group_api(client, "set_group_admin", "设置管理员",
                                              group_id=gid, user_id=uid, enable=True)
        if action == "unset_admin":
            return await self._call_group_api(client, "set_group_admin", "取消管理员",
                                              group_id=gid, user_id=uid, enable=False)
        if action == "whole_ban":
            return await self._call_group_api(client, "set_group_whole_ban", "全体禁言", group_id=gid, enable=True)
        if action == "whole_unban":
            return await self._call_group_api(client, "set_group_whole_ban", "解除全体禁言", group_id=gid, enable=False)
        if action == "set_group_name":
            return await self._call_group_api(client, "set_group_name", "修改群名",
                                              group_id=gid, group_name=str(params.get("group_name", "")))
        if action == "send_notice":
            return await self._call_group_api(client, "_send_group_notice", "发群公告",
                                              group_id=gid, content=str(params.get("content", "")))
        if action == "recall":
            mid = self._safe_int(params.get("message_id", 0), 0)
            if not mid:
                return False, "缺少 message_id"
            return await self._call_group_api(client, "delete_msg", "撤回消息", message_id=mid)
        if action == "set_essence":
            mid = self._safe_int(params.get("message_id", 0), 0)
            if not mid:
                return False, "缺少 message_id"
            return await self._call_group_api(client, "set_essence_msg", "设精华", message_id=mid)
        if action == "del_essence":
            mid = self._safe_int(params.get("message_id", 0), 0)
            if not mid:
                return False, "缺少 message_id"
            return await self._call_group_api(client, "delete_essence_msg", "取消精华", message_id=mid)
        return False, f"未知操作: {action}"

    async def _remote_execute(self, group_id: str, action: str, params: dict) -> dict:
        """WebUI 远程执行统一入口。

        params 约定：
          - 单个目标：{"user_id": "123", ...其它参数}
          - 批量目标：{"user_ids": ["1","2",...], ...其它参数}
          - 无目标操作（全体禁言/改群名等）：仅其它参数

        返回：{"ok": bool, "total": n, "success": n, "fail": n, "results": [...], "message": str}
        """
        params = params or {}
        meta = _REMOTE_ACTIONS.get(action)
        if not meta:
            return {"ok": False, "message": f"未知操作: {action}"}
        cfg_key, cn_name, need_user = meta
        # 三级检查：插件总开关 + 免责声明 + 该功能开关
        ok, msg = self._cfg_check(cfg_key, cn_name)
        if not ok:
            return {"ok": False, "message": msg}
        gid = self._safe_int(group_id, 0)
        if not gid:
            return {"ok": False, "message": "群号无效"}
        client = await self._get_client(None)
        if not client:
            return {"ok": False, "message": "无法获取 QQ 客户端，请确保已连接"}

        # 目标列表：批量优先，其次单个
        targets = []
        if need_user:
            raw_ids = params.get("user_ids")
            if isinstance(raw_ids, list) and raw_ids:
                targets = [str(x).strip() for x in raw_ids if str(x).strip().isdigit()]
            elif params.get("user_id"):
                uid = str(params.get("user_id")).strip()
                if uid.isdigit():
                    targets = [uid]
            if not targets:
                return {"ok": False, "message": "请提供有效的成员 QQ 号"}
            targets = targets[:50]  # 批量上限保护
        else:
            targets = [""]  # 无目标操作占位执行一次

        results = []
        success = 0
        for uid in targets:
            try:
                done, err = await self._remote_execute_single(client, gid, action, uid, params)
            except Exception as e:
                done, err = False, str(e)
            if done:
                success += 1
            results.append({"user_id": uid, "ok": done, "error": "" if done else err})
            if len(targets) > 1:
                await asyncio.sleep(0.3)  # 批量防限频

        # 记录一条操作日志便于审计
        try:
            self._log_moderation(str(gid), targets[0] if targets else "", "",
                                 f"[远程操作] {cn_name} x{len(targets)}", f"远程{cn_name}",
                                 f"成功{success}/{len(targets)}", [])
        except Exception:
            pass

        return {
            "ok": success > 0,
            "total": len(targets),
            "success": success,
            "fail": len(targets) - success,
            "results": results,
            "message": f"{cn_name}：成功 {success}/{len(targets)}",
        }
