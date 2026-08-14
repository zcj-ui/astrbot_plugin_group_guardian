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

        # 写操作前置校验：bot 自身权限 + 目标角色（群主/管理员保护），避免必然失败的调用
        if uid:
            ok_pre, pre_msg = await self._precheck_member_action(client, gid, uid, action)
            if not ok_pre:
                return False, pre_msg

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

    def _resolve_operator_from_bindings(self, operator_name: str = "", operator_qq: str = ""):
        """从 web_operator_bindings（用户名:QQ号,用户名2:QQ2）解析操作者身份。

        返回 (operator_name, operator_qq)。规则：
        - 若前端已传 operator_qq，直接采用；
        - 否则若传了 operator_name（Dashboard 登录用户名），按绑定映射到 QQ；
        - 都没有则返回空（是否放行由 web_remote_require_operator 决定）。
        """
        try:
            bindings = self._cfg_str("web_operator_bindings", "")
            mapping = {}
            for pair in str(bindings or "").replace("；", ";").replace("，", ",").split(";"):
                for sub in pair.split(","):
                    sub = sub.strip()
                    if ":" not in sub:
                        continue
                    name, qq = sub.split(":", 1)
                    name, qq = name.strip(), qq.strip()
                    if name and qq and name not in mapping:
                        mapping[name] = qq
            if operator_qq:
                return operator_name or "", str(operator_qq)
            if operator_name and operator_name in mapping:
                return operator_name, mapping[operator_name]
        except Exception as e:
            logger.debug(f"[GroupMgr] 解析操作者绑定失败: {e}")
        return operator_name or "", ""

    def _record_web_audit(self, operator_name: str, operator_qq: str, group_id: str,
                          action: str, target_user: str, params: str,
                          result: str, message: str) -> None:
        """记录 WebUI 远程操作审计日志（失败静默）。"""
        try:
            self._storage.record_web_audit(
                operator_name=operator_name, operator_qq=operator_qq,
                group_id=group_id, action=action, target_user=target_user,
                params=params, result=result, message=message,
            )
        except Exception as e:
            logger.debug(f"[GroupMgr] 审计记录失败: {e}")

    async def _check_remote_operator(self, group_id: str, operator_qq: str):
        """远程写操作授权校验：操作者（QQ 身份）是否可操作目标群。

        权限模型（自上而下，v2.15.0 明确）：
          1. plugin_admin：插件全局管理员 / AstrBot 全局 admin_id / web_operator_bindings 绑定用户
             → 可操作【所有群】（产品设计的全局管理模型）；
          2. group_super_admin：目标群的群超管（WebUI 为该群单独设置）→ 可操作该群；
          3. owner/admin：目标群的群主 / 群管理员（按 QQ 号查群角色）→ 可操作该群；
          4. 其余拒绝。

        返回 (ok, role, msg)。
        """
        if not operator_qq:
            return False, "", ("缺少操作者身份(operator_qq)：请在 WebUI 配置 web_operator_bindings "
                               "绑定 Dashboard 用户与 QQ，或在远程操作请求中携带操作者QQ")
        qq = str(operator_qq).strip()
        # ① 插件全局管理员 / AstrBot 全局 admin：可操作所有群
        try:
            if qq in self._get_all_admin_ids():
                return True, "plugin_admin", ""
        except Exception as e:
            logger.debug(f"[GroupMgr] 远程操作者管理员判定失败: {e}")
        # ② 群超管（目标群专属）
        try:
            if self._storage.is_group_super_admin(group_id, qq):
                return True, "group_super_admin", ""
        except Exception:
            pass
        # ③ 群主 / 群管理员（按 QQ 号查目标群角色）
        try:
            client = await self._get_client(None)
            if client:
                role = await self._get_role_by_id(client, group_id, qq)
                if role in ("owner", "admin"):
                    return True, role, ""
        except Exception as e:
            logger.debug(f"[GroupMgr] 远程操作者角色查询失败: {e}")
        return False, "member", ("权限不足：操作者非全局插件管理员，也不是该群的群超管/群主/群管理员，"
                                 "无法远程操作该群")

    async def _remote_execute(self, group_id: str, action: str, params: dict,
                              operator_qq: str = "", operator_name: str = "") -> dict:
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
        gid = self._safe_int(group_id, 0)
        if not gid:
            return {"ok": False, "message": "群号无效"}
        gid_str = str(gid)
        if self._group_black_set and gid_str in self._group_black_set:
            return {"ok": False, "message": f"群 {gid_str} 在黑名单中"}
        if self._group_white_set and gid_str not in self._group_white_set:
            return {"ok": False, "message": f"群 {gid_str} 不在白名单中"}
        # 三级检查：插件总开关 + 免责声明 + 该功能开关，按目标群读取独立配置。
        ok, msg = self._cfg_check(cfg_key, cn_name, group_id=gid_str)
        if not ok:
            return {"ok": False, "message": msg}
        # v2.15.0 远程写操作授权校验：开启 web_remote_require_operator 或提供了操作者身份时，
        # 必须校验操作者对目标群的授权（plugin_admin 全局 / 群超管 / 群主 / 群管理员）。
        if self._cfg("web_remote_require_operator", False) or operator_qq:
            op_ok, op_role, op_err = await self._check_remote_operator(gid_str, operator_qq)
            if not op_ok:
                self._record_web_audit(operator_name, operator_qq, gid_str, action,
                                       "", str(params)[:200], "拒绝", op_err)
                return {"ok": False, "message": op_err}
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

        # 审计日志（v2.15.0）：记录操作者身份、目标群、操作与结果
        self._record_web_audit(operator_name, operator_qq, gid_str, action,
                               ",".join(targets[:50]), str(params)[:200],
                               "成功" if success > 0 else "失败", f"{cn_name} 成功 {success}/{len(targets)}")
        # 兼容旧 moderation_logs 记录（附带操作者身份便于追溯）
        try:
            self._log_moderation(str(gid), targets[0] if targets else "", "",
                                 f"[远程操作] {cn_name} x{len(targets)} (操作者:{operator_name or operator_qq or '未知'})",
                                 f"远程{cn_name}", f"成功{success}/{len(targets)}", [])
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
