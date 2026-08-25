# -*- coding: utf-8 -*-
"""v2.36.8 云同步客户端：把误判复盘/修正建议/已确认违规同步到独立后台服务端。

- 服务端（gg_server）与插件分离部署（可在宝塔），集中保存数据，防止更换服务器丢失；
- 插件每次接入（启动 + 定时）双向同步：
  - push：本地有而服务器没有的自动上传（feedback / suggestions / violations）；
  - pull：服务器有而本地没有的自动拉取写入（新机器快速恢复原状）；
  - actions：管理员在服务端「确认违规/放行」产生的待执行动作，插件拉取后执行并回执。

配置（WebUI 设置）：
  sync_server_url   服务端地址，如 http://你的域名:9000
  sync_username     服务端管理员账号
  sync_password     服务端管理员密码
  sync_auto         开启自动同步（默认关）
  sync_interval     同步周期分钟（默认 10）
"""

import asyncio
import json
import os
import time
import uuid

from astrbot.api import logger

try:
    from .constants import PLUGIN_NAME, PLUGIN_VERSION
except ImportError:  # 独立加载 sync_client.py 的单元测试兼容路径
    PLUGIN_NAME = "astrbot_plugin_group_guardian"
    PLUGIN_VERSION = "v2.36.8"

_SYNC_TIMEOUT = 20


class SyncClientMixin:
    """云同步能力，由 Main 组合使用。所有同步失败均静默降级，不影响审核主流程。"""

    # ============================================================
    # 配置 / 状态
    # ============================================================

    def _init_sync_client(self) -> None:
        self._sync_token = None
        self._sync_token_ts = 0.0
        self._sync_lock = asyncio.Lock()

    def _sync_config(self):
        url = str(self._cfg_str("sync_server_url", "") or "").strip().rstrip("/")
        user = str(self._cfg_str("sync_username", "") or "").strip()
        pwd = str(self._cfg_str("sync_password", "") or "").strip()
        return url, user, pwd

    def _sync_enabled(self) -> bool:
        url, user, pwd = self._sync_config()
        return bool(url and user and pwd)

    def _sync_client_id(self) -> str:
        """持久化的插件实例 ID（存于 data_dir，换机器后重新生成并重新同步）。"""
        try:
            data_dir = getattr(self, "_data_dir", "") or ""
            if not data_dir:
                return "unknown"
            path = os.path.join(str(data_dir), "sync_client_id.txt")
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    cid = f.read().strip()
                if cid:
                    return cid
            cid = uuid.uuid4().hex[:12]
            with open(path, "w", encoding="utf-8") as f:
                f.write(cid)
            return cid
        except Exception:
            return "unknown"

    # ============================================================
    # HTTP
    # ============================================================

    async def _sync_login(self) -> bool:
        import urllib.request

        url, user, pwd = self._sync_config()
        if not url:
            return False
        try:
            body = json.dumps({"username": user, "password": pwd}).encode("utf-8")
            req = urllib.request.Request(
                url + "/api/auth/login", data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=_SYNC_TIMEOUT) as resp:
                res = json.loads(resp.read().decode("utf-8", "replace"))
            if res.get("status") != "success":
                logger.warning(f"[GroupMgr] 云同步登录失败: {res.get('message', '')}")
                return False
            self._sync_token = res.get("token", "")
            self._sync_token_ts = time.time()
            return bool(self._sync_token)
        except Exception as exc:
            logger.debug(f"[GroupMgr] 云同步登录异常: {exc}")
            return False

    def _sync_http(self, method: str, path: str, body: dict = None):
        import urllib.request
        import urllib.error

        url, _, _ = self._sync_config()
        if not url:
            return None
        headers = {"Authorization": "Bearer " + (self._sync_token or "")}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_SYNC_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return {"status": "error", "message": f"HTTP {e.code}"}
        except Exception as exc:
            logger.debug(f"[GroupMgr] 云同步请求 {path} 失败: {exc}")
            return None

    # ============================================================
    # 收集本地数据（push）
    # ============================================================

    def _sync_collect_feedback(self) -> list:
        """收集误判复盘（feedback）。"""
        try:
            rows = self._storage.list_moderation_feedback("", 500, 0)
            items = []
            for r in rows or []:
                items.append({
                    "local_id": int(r.get("log_id", 0) or 0),
                    "group_id": r.get("group_id"), "user_id": r.get("user_id"),
                    "user_name": r.get("user_name"), "msg_text": r.get("msg_text"),
                    "action": r.get("action"), "original_reason": r.get("original_reason"),
                    "verdict": r.get("verdict", ""), "note": r.get("note"),
                    "reviewer": r.get("reviewer"), "review_status": r.get("review_status", "pending"),
                    "suggestion_id": r.get("suggestion_id"),
                    "created_at": int(r.get("created_at", 0) or 0),
                    "updated_at": int(r.get("updated_at", 0) or 0),
                })
            return items
        except Exception as exc:
            logger.debug(f"[GroupMgr] 收集误判复盘失败: {exc}")
            return []

    def _sync_collect_suggestions(self) -> list:
        """收集修正规则建议（suggestions）。"""
        try:
            rows = self._storage.list_prompt_suggestions(200)
            items = []
            for r in rows or []:
                items.append({
                    "local_id": int(r.get("id", 0) or 0),
                    "sample_count": int(r.get("sample_count", 0) or 0),
                    "sample_ids": r.get("sample_ids") or [],
                    "summary": r.get("summary"),
                    "suggested_guidance": r.get("suggested_guidance", ""),
                    "previous_guidance": r.get("previous_guidance"),
                    "status": r.get("status", "pending"),
                    "applied_at": int(r.get("applied_at", 0) or 0),
                    "actor": r.get("actor"), "audit_note": r.get("audit_note"),
                    "created_at": int(r.get("created_at", 0) or 0),
                    "updated_at": int(r.get("updated_at", 0) or 0),
                })
            return items
        except Exception as exc:
            logger.debug(f"[GroupMgr] 收集修正建议失败: {exc}")
            return []


    def _sync_collect_violations(self) -> list:
        """收集已确认违规/审核信息（来自审核日志中已处罚记录 + ad_reviews 已处理）。"""
        items = []
        try:
            logs = self._storage.list_logs(500, 0)
            punish_keys = ("撤回", "禁言", "踢出", "拒绝", "确认")
            for r in logs or []:
                action = str(r.get("action", "") or "")
                if not any(k in action for k in punish_keys):
                    continue
                items.append({
                    "local_id": int(r.get("id", 0) or 0),
                    "group_id": r.get("group_id"), "user_id": r.get("user_id"),
                    "user_name": r.get("user_name"), "msg_text": r.get("msg_text"),
                    "action": action, "reason": r.get("reason"),
                    "source": "log", "status": "confirmed",
                    "created_at": int(r.get("ts", 0) or 0),
                    "updated_at": int(r.get("ts", 0) or 0),
                })
        except Exception as exc:
            logger.debug(f"[GroupMgr] 收集审核日志违规失败: {exc}")
        try:
            pending_ids = {int(x.get("id", 0)) for x in
                           (self._storage.list_pending_ad_reviews(500) or [])}
            ad_all = getattr(self._storage, "list_ad_reviews_for_sync", None)
            if callable(ad_all):
                for r in (ad_all(500) or []):
                    rid = int(r.get("id", 0) or 0)
                    if rid in pending_ids:
                        continue
                    items.append({
                        "local_id": rid,
                        "group_id": r.get("group_id"), "user_id": r.get("user_id"),
                        "user_name": r.get("user_name"), "msg_text": r.get("msg_text"),
                        "action": "广告复核",
                        "reason": "管理员处理（确认/放行）",
                        "source": r.get("source", "text"),
                        "status": r.get("status", "confirmed"),
                        "created_at": int(r.get("ts", 0) or 0),
                        "updated_at": int(r.get("ts", 0) or 0),
                    })
        except Exception as exc:
            logger.debug(f"[GroupMgr] 收集广告复核记录失败: {exc}")
        return items

    def _sync_collect_audit_logs(self) -> list:
        """收集完整审核日志（所有动作：处罚/放行/待复核/误判等），供服务器后台查看。
        与 violations（仅处罚类）不同，audit_logs 为全量日志。"""
        try:
            rows = self._storage.list_logs(1000, 0)
            items = []
            for r in rows or []:
                items.append({
                    "local_id": int(r.get("id", 0) or 0),
                    "group_id": r.get("group_id"), "user_id": r.get("user_id"),
                    "user_name": r.get("user_name"), "msg_text": r.get("msg_text"),
                    "action": r.get("action"), "reason": r.get("reason"),
                    "created_at": int(r.get("ts", 0) or 0),
                    "updated_at": int(r.get("ts", 0) or 0),
                })
            return items
        except Exception as exc:
            logger.debug(f"[GroupMgr] 收集审核日志失败: {exc}")
            return []


    # ============================================================
    # push / pull / actions
    # ============================================================

    async def _sync_push(self) -> None:
        """上传本地数据（服务器缺少的自动增加）。"""
        try:
            payload = {
                "feedback": self._sync_collect_feedback(),
                "suggestions": self._sync_collect_suggestions(),
                "violations": self._sync_collect_violations(),
                "audit_logs": self._sync_collect_audit_logs(),
            }
            res = self._sync_http("POST", "/api/sync/push", {
                "client_id": self._sync_client_id(),
                "client_name": PLUGIN_NAME,
                "client_version": PLUGIN_VERSION,
                "scopes": payload,
            })
            if res and res.get("status") == "success":
                logger.info(
                    f"[GroupMgr] 云同步上传完成: feedback={len(payload['feedback'])}, "
                    f"suggestions={len(payload['suggestions'])}, violations={len(payload['violations'])}, "
                    f"audit_logs={len(payload['audit_logs'])}"
                )
            else:
                logger.debug(f"[GroupMgr] 云同步上传未成功: {res}")
        except Exception as exc:
            logger.debug(f"[GroupMgr] 云同步上传异常: {exc}")

    async def _sync_pull(self) -> None:
        """拉取服务器数据并写回本地（新机器快速恢复原状）。"""
        try:
            res = self._sync_http("GET", "/api/sync/pull?client_id=" +
                                  self._sync_client_id())
            if not res or res.get("status") != "success":
                return
            data = res.get("data", {}) or {}
            feedback = data.get("feedback", []) or []
            suggestions = data.get("suggestions", []) or []
            violations = data.get("violations", []) or []
            self._sync_restore_feedback(feedback)
            self._sync_restore_violations(violations)
            logger.info(
                f"[GroupMgr] 云同步拉取完成: feedback={len(feedback)}, "
                f"suggestions={len(suggestions)}, violations={len(violations)}"
            )
        except Exception as exc:
            logger.debug(f"[GroupMgr] 云同步拉取异常: {exc}")


    def _sync_restore_feedback(self, feedback: list) -> None:
        """把服务器误判复盘写回本地（按 log_id 幂等恢复）。"""
        for item in feedback or []:
            try:
                log_id = int(item.get("local_id", 0) or 0)
                if log_id <= 0:
                    continue
                exists = self._storage.get_log(log_id)
                if not exists:
                    self._storage.add_log({
                        "id": log_id, "ts": int(item.get("created_at", 0) or 0),
                        "group_id": item.get("group_id"), "user_id": item.get("user_id"),
                        "user_name": item.get("user_name"), "msg_text": item.get("msg_text"),
                        "msg_preview": str(item.get("msg_text", "") or "")[:120],
                        "action": item.get("action", ""), "reason": item.get("original_reason", ""),
                        "image_urls": [],
                    })
                verdict = str(item.get("verdict", ""))
                if verdict in ("false_positive", "confirmed_violation"):
                    self._storage.mark_moderation_feedback(
                        log_id, verdict, str(item.get("note", "") or ""),
                        str(item.get("reviewer", "") or "sync"),
                    )
            except Exception as exc:
                logger.debug(f"[GroupMgr] 恢复误判复盘 #{item.get('local_id')} 失败: {exc}")

    def _sync_restore_violations(self, violations: list) -> None:
        """把服务器已确认违规写回本地审核日志（按 id 幂等恢复）。"""
        for item in violations or []:
            try:
                log_id = int(item.get("local_id", 0) or 0)
                if log_id <= 0 or self._storage.get_log(log_id):
                    continue
                self._storage.add_log({
                    "id": log_id, "ts": int(item.get("created_at", 0) or 0),
                    "group_id": item.get("group_id"), "user_id": item.get("user_id"),
                    "user_name": item.get("user_name"), "msg_text": item.get("msg_text"),
                    "msg_preview": str(item.get("msg_text", "") or "")[:120],
                    "action": item.get("action", "违规"), "reason": item.get("reason", ""),
                    "image_urls": [],
                })
            except Exception as exc:
                logger.debug(f"[GroupMgr] 恢复违规记录 #{item.get('local_id')} 失败: {exc}")


    async def _sync_actions(self) -> None:
        """拉取服务器端管理员产生的待执行动作并执行，执行后回执。"""
        try:
            res = self._sync_http("GET", "/api/sync/pull?client_id=" +
                                  self._sync_client_id())
            if not res or res.get("status") != "success":
                return
            actions = (res.get("data", {}) or {}).get("actions", []) or []
            for action in actions:
                result = await self._sync_execute_action(action)
                self._sync_http("POST", "/api/data/actions/{0}".format(
                    int(action.get("id", 0) or 0)
                ), {"result": result})
        except Exception as exc:
            logger.debug(f"[GroupMgr] 云同步动作处理异常: {exc}")

    async def _sync_execute_action(self, action: dict) -> str:
        """执行一条服务器动作。返回结果字符串。"""
        try:
            act = str(action.get("action", "") or "")
            try:
                payload = json.loads(action.get("payload") or "{}")
            except (TypeError, ValueError):
                payload = {}
            if act == "violation_confirm":
                ok = await self._sync_apply_violation(payload, confirm=True)
                return "ok" if ok else "error: 确认动作执行失败（本地无待复核记录且禁言失败）"
            if act == "violation_clear":
                ok = await self._sync_apply_violation(payload, confirm=False)
                return "ok" if ok else "error: 放行动作执行失败（本地无待复核记录）"
            if act == "unban":
                await self._sync_execute_unban(payload)
                return "ok"
            if act == "suggestion_apply":
                ok = await self._sync_apply_suggestion(payload)
                return "ok" if ok else "error: 修正建议应用失败（本地无对应建议或配置保存失败）"
            if act == "suggestion_reject":
                ok = await self._sync_reject_suggestion(payload)
                return "ok" if ok else "error: 修正建议拒绝失败（本地无对应建议）"
            return "skipped"
        except Exception as exc:
            logger.debug(f"[GroupMgr] 执行服务器动作失败: {exc}")
            return "error"

    async def _sync_apply_suggestion(self, payload: dict) -> bool:
        """服务端「应用」修正建议：调用本地应用逻辑，把 suggested_guidance 并入规则。

        v2.36.12：服务端「修正建议」页新增「应用」按钮，生成 suggestion_apply
        动作由插件执行；本地建议状态 pending → applied（幂等兼容已应用）。
        """
        try:
            sid = int(payload.get("suggestion_id", 0) or 0)
            if not sid:
                return False
            fn = getattr(self, "_apply_moderation_prompt_suggestion", None)
            if not callable(fn):
                logger.debug("[GroupMgr] 本地无应用建议方法，跳过")
                return False
            res = fn(sid, actor="sync_server")
            ok = bool(res and res.get("ok"))
            if not ok:
                logger.debug(f"[GroupMgr] 服务端应用修正建议 #{sid} 失败: {res}")
            else:
                logger.info(f"[GroupMgr] 服务端应用修正建议 #{sid} 成功")
            return ok
        except Exception as exc:
            logger.debug(f"[GroupMgr] 应用修正建议异常: {exc}")
            return False

    async def _sync_reject_suggestion(self, payload: dict) -> bool:
        """服务端「拒绝」修正建议：本地 pending → rejected，与服务端状态一致。"""
        try:
            sid = int(payload.get("suggestion_id", 0) or 0)
            if not sid:
                return False
            fn = getattr(self, "_reject_moderation_prompt_suggestion", None)
            if not callable(fn):
                logger.debug("[GroupMgr] 本地无拒绝建议方法，跳过")
                return False
            res = fn(sid, actor="sync_server", note="管理员在服务端拒绝")
            ok = bool(res and res.get("ok"))
            if not ok:
                logger.debug(f"[GroupMgr] 服务端拒绝修正建议 #{sid} 失败: {res}")
            return ok
        except Exception as exc:
            logger.debug(f"[GroupMgr] 拒绝修正建议异常: {exc}")
            return False

    async def _sync_execute_unban(self, payload: dict) -> None:
        """执行解禁动作：管理员在服务端标记误封/通过申诉后，解除该用户禁言。"""
        try:
            group_id = str(payload.get("group_id", "") or "")
            user_id = str(payload.get("user_id", "") or "")
            if not group_id or not user_id:
                return
            try:
                gid = int(group_id)
                uid = int(user_id)
            except (TypeError, ValueError):
                gid = uid = 0
            if not gid or not uid:
                return
            client = await self._get_client()
            if not client:
                return
            ok, error = await self._call_group_api(
                client, "set_group_ban", "解除禁言",
                group_id=gid, user_id=uid, duration=0,
            )
            self._log_moderation(
                group_id, str(uid), str(payload.get("user_name", "") or ""),
                str(payload.get("msg_text", "") or ""),
                "误封解禁" if ok else "解禁失败",
                "管理员在服务端确认误封，自动解除禁言" if ok
                else f"解禁失败: {error}",
                [],
            )
        except Exception as exc:
            logger.debug(f"[GroupMgr] 执行解禁失败: {exc}")

    async def _sync_apply_violation(self, payload: dict, confirm: bool) -> bool:
        """按 payload 中的 group/user/msg 匹配本地待复核记录并确认/放行。

        返回是否成功执行（确认违规=已撤回+禁言+学习，放行=学习为正常）。

        修复「确认违规后指令没下发到机器人封禁」：
        - 旧版只在本地 pending 待复核记录里做严格匹配，匹配不到就静默 return，
          且上层无脑回执 "ok"，导致服务端动作被吞掉、机器人从未收到封禁指令；
        - 现改为：优先匹配本地待复核记录；匹配不到（例如服务端确认的是
          处罚类审核日志、已被清理的记录、或异地节点已处理）时，仍按
          payload 的 群+用户 直接执行服务端确认的处罚，保证动作真实下发。
        """
        try:
            group_id = str(payload.get("group_id", "") or "")
            user_id = str(payload.get("user_id", "") or "")
            msg_text = str(payload.get("msg_text", "") or "")
            if not group_id or not user_id:
                logger.debug("[GroupMgr] 服务端动作缺少 群/用户，跳过")
                return False
            target = None
            try:
                pending = self._storage.list_pending_ad_reviews(500)
                for item in pending or []:
                    if (str(item.get("group_id", "") or "") == group_id
                            and str(item.get("user_id", "") or "") == user_id
                            and (not msg_text
                                 or str(item.get("msg_text", "") or "").startswith(msg_text[:40]))):
                        target = item
                        break
            except Exception as exc:
                logger.debug(f"[GroupMgr] 查询本地待复核记录失败: {exc}")
            if target:
                rid = int(target.get("id", 0) or 0)
                try:
                    self._storage.resolve_ad_review(
                        rid, "confirmed" if confirm else "released", "sync_server")
                except Exception as exc:
                    logger.debug(f"[GroupMgr] 更新待复核状态失败: {exc}")
                if confirm:
                    try:
                        await self._recall_ad_review_message(target)
                    except Exception as exc:
                        logger.debug(f"[GroupMgr] 服务器确认撤回原消息失败: {exc}")
                    try:
                        banned = await self._ban_ad_review_user(group_id, user_id)
                    except Exception as exc:
                        logger.debug(f"[GroupMgr] 服务器确认禁言失败: {exc}")
                        banned = False
                    try:
                        self._ad_review_learn_text(
                            str(target.get("msg_text", "") or ""), "ad", group_id)
                    except Exception:
                        pass
                    return banned
                else:
                    try:
                        self._ad_review_learn_text(
                            str(target.get("msg_text", "") or ""), "ok", group_id)
                    except Exception:
                        pass
                return True
            # 本地没有待复核记录：服务端已确认违规 → 直接按 群+用户 执行禁言，
            # 保证管理员的操作真实下发到机器人（不因本地无匹配记录而静默吞掉）。
            if confirm:
                try:
                    banned = await self._ban_ad_review_user(group_id, user_id)
                except Exception as exc:
                    logger.debug(f"[GroupMgr] 服务端确认（无待复核记录）禁言失败: {exc}")
                    banned = False
                if not banned:
                    return False
                try:
                    self._log_moderation(
                        group_id, user_id,
                        str(payload.get("user_name", "") or ""),
                        msg_text, "服务器确认违规",
                        "管理员在服务端确认违规，插件按 群+用户 直接执行禁言"
                        "（本地无待复核记录，可能已被清理或为处罚类日志）", [],
                    )
                except Exception:
                    pass
                return True
            return False  # 放行且本地无记录：无可学习内容，视为无需处理
        except Exception as exc:
            logger.debug(f"[GroupMgr] 服务器动作应用失败: {exc}")
            return False

    async def _sync_run(self) -> None:
        """一次完整同步：登录 → push → pull → actions。"""
        if not self._sync_enabled():
            return
        try:
            async with self._sync_lock:
                if not self._sync_token:
                    await self._sync_login()
                if not self._sync_token:
                    return
                await self._sync_push()
                await self._sync_pull()
                await self._sync_actions()
        except Exception as exc:
            logger.debug(f"[GroupMgr] 云同步执行异常: {exc}")
