# -*- coding: utf-8 -*-
"""广告 Web 管理后台（v2.21.0 起接入 AstrBot Dashboard）。

- 页面与接口统一注册到 AstrBot Dashboard（web.py register_web_api → /api/plug/ 下），
  鉴权由 Dashboard JWT 统一执行，不再有独立端口监听服务；
- 展示广告检测核心数据：今日/累计拦截统计、违规记录（含图片/视频证据）、
  感知哈希广告黑名单、视频指纹缓存、广告分级处置记录、关键配置状态。
"""

import os

try:
    from quart import jsonify, request as quart_request
except ImportError:  # pragma: no cover
    jsonify = None
    quart_request = None

try:
    from .hash_audit import (
        HASH_BLACKLIST_FILE,
        VIDEO_FP_CACHE_FILE,
        AD_ESCALATION_FILE,
    )
except ImportError:  # 独立加载 ad_backend.py 的单元测试兼容路径
    from hash_audit import (
        HASH_BLACKLIST_FILE,
        VIDEO_FP_CACHE_FILE,
        AD_ESCALATION_FILE,
    )

BACKEND_PAGE_REL = os.path.join("pages", "ad_backend", "index.html")
BACKEND_PAGE_CACHE_TTL = 10.0


class AdBackendMixin:
    """独立 Web 管理后台能力，由 ``Main`` 组合使用（需 Quart 可用）。"""

    def _init_ad_backend(self) -> None:
        """广告后台初始化（v2.21.0：独立 Quart 服务已移除）。

        页面与接口统一接入 AstrBot Dashboard（web.py 通过 register_web_api 注册到
        /api/plug/ 下），鉴权由 AstrBot Dashboard JWT 统一执行，不再存在独立端口监听。
        此处仅初始化数据缓存字段；ad_backend_enabled 保留作为旧配置兼容项。
        """
        self._ad_backend_app = None
        self._ad_backend_task = None
        self._ad_backend_page_cache = ("", 0.0)

    async def _stop_ad_backend(self) -> None:
        """停止广告后台（v2.21.0：已无独立监听服务，仅清空缓存字段）。"""
        self._ad_backend_task = None
        self._ad_backend_app = None
        self._ad_backend_page_cache = ("", 0.0)

    # ============================================================
    # 后台 API（v2.21.0 起由 web.py 通过 register_web_api 注册到 AstrBot Dashboard）
    # ============================================================

    async def _ad_backend_stats(self):
        """总览统计：今日/累计拦截、图片/视频拦截、最近记录。"""
        try:
            today_start = self._today_start()
            blocked = passed = 0
            img_blocked = 0
            video_blocked = 0
            user_hits = {}
            recent = []
            for log in list(self._moderation_logs):
                ts = int(log.get("ts", 0) or 0)
                action = str(log.get("action", ""))
                msg = str(log.get("msg_text", ""))
                urls = log.get("image_urls") or []
                is_ad = ("广告" in action) or ("广告" in msg) or ("视频" in msg)
                is_blocked = ("撤回" in action) or ("禁言" in action) or ("踢" in action)
                if is_ad and is_blocked:
                    blocked += 1
                    if "视频" in msg or "视频" in action:
                        video_blocked += 1
                    elif urls:
                        img_blocked += 1
                    uid = str(log.get("user_id", ""))
                    if uid:
                        user_hits[uid] = user_hits.get(uid, 0) + 1
                    if ts >= today_start and len(recent) < 20:
                        recent.append(log)
                elif is_blocked:
                    passed += 1
            total_logs = self._storage.count_logs()
            top_users = sorted(
                user_hits.items(), key=lambda kv: kv[1], reverse=True
            )[:10]
            return jsonify({
                "status": "success",
                "data": {
                    "today_blocked": blocked,
                    "today_video_blocked": video_blocked,
                    "today_image_blocked": img_blocked,
                    "total_logs": total_logs,
                    "recent": recent[:20],
                    "top_users": [{"user_id": k, "count": v} for k, v in top_users],
                },
            })
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})

    async def _ad_backend_logs(self):
        """广告相关违规记录（分页）。"""
        try:
            limit = min(int(quart_request.args.get("limit", 50)), 200)
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = max(0, int(quart_request.args.get("offset", 0) or 0))
        except (ValueError, TypeError):
            offset = 0
        try:
            action = str(quart_request.args.get("action", "") or "").strip()
            logs = self._storage.list_logs(limit=limit, offset=offset, action=action)
            total = self._storage.count_logs_filtered(action=action)
            return jsonify({"status": "success", "data": logs, "total": total})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})

    async def _ad_backend_blacklist(self):
        """感知哈希广告黑名单。"""
        try:
            hashes = self._hash_blacklist.get("hashes", [])
            data = sorted(
                hashes, key=lambda e: int(e.get("last_ts", 0) or 0), reverse=True
            )[:500]
            return jsonify({
                "status": "success",
                "data": data,
                "count": len(hashes),
                "enabled": bool(self.config.get("ad_hash_blacklist_enabled", False)),
            })
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})

    async def _ad_backend_blacklist_remove(self):
        """删除黑名单中的指定哈希。"""
        try:
            body = await quart_request.get_json(force=True, silent=True) or {}
            target = str(body.get("h", "") or "").strip()
            hashes = self._hash_blacklist.get("hashes", [])
            new_hashes = [e for e in hashes if str(e.get("h", "") or "") != target]
            if len(new_hashes) == len(hashes):
                return jsonify({"status": "error", "message": "未找到该哈希"})
            self._hash_blacklist["hashes"] = new_hashes
            self._save_json_file(HASH_BLACKLIST_FILE, self._hash_blacklist)
            return jsonify({"status": "success"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})

    async def _ad_backend_fingerprints(self):
        """广告视频指纹缓存。"""
        try:
            data = [
                {"fingerprint": k, "ts": v}
                for k, v in sorted(
                    self._video_fp_cache.items(), key=lambda kv: kv[1], reverse=True
                )
            ][:500]
            return jsonify({
                "status": "success",
                "data": data,
                "count": len(self._video_fp_cache),
                "enabled": bool(self.config.get("video_fingerprint_cache", False)),
            })
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})

    async def _ad_backend_fingerprints_clear(self):
        """清空广告视频指纹缓存。"""
        try:
            self._video_fp_cache = {}
            self._save_json_file(VIDEO_FP_CACHE_FILE, self._video_fp_cache)
            return jsonify({"status": "success"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})

    async def _ad_backend_escalation(self):
        """广告分级处置记录。"""
        try:
            rows = []
            for gid, users in self._ad_escalation.items():
                if not isinstance(users, dict):
                    continue
                for uid, record in users.items():
                    if isinstance(record, dict):
                        rows.append({
                            "group_id": gid,
                            "user_id": uid,
                            "count": record.get("count", 0),
                            "first_ts": record.get("first_ts", 0),
                            "last_ts": record.get("last_ts", 0),
                        })
            rows.sort(key=lambda r: int(r.get("last_ts", 0) or 0), reverse=True)
            return jsonify({
                "status": "success",
                "data": rows[:500],
                "enabled": bool(self.config.get("ad_escalation_enabled", False)),
            })
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})

    async def _ad_backend_escalation_reset(self):
        """重置指定用户的分级处置记录。"""
        try:
            body = await quart_request.get_json(force=True, silent=True) or {}
            gid = str(body.get("group_id", "") or "").strip()
            uid = str(body.get("user_id", "") or "").strip()
            if not gid or not uid:
                return jsonify({"status": "error", "message": "缺少 group_id/user_id"})
            if gid in self._ad_escalation and isinstance(self._ad_escalation[gid], dict):
                self._ad_escalation[gid].pop(uid, None)
            self._save_json_file(AD_ESCALATION_FILE, self._ad_escalation)
            return jsonify({"status": "success"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})

    async def _ad_backend_config(self):
        """关键配置状态。"""
        try:
            keys = [
                "video_audit_enabled", "video_max_frames", "video_frame_mode",
                "video_quick_precheck", "video_fingerprint_cache",
                "video_ad_visual_enabled", "video_subtitle_boost",
                "ad_hash_blacklist_enabled", "ad_hash_distance", "ad_hash_auto_learn",
                "ad_escalation_enabled", "ad_escalation_warn_at",
                "ad_escalation_ban_at", "ad_escalation_kick_at",
                "ocr_provider_id", "llm_moderation_enabled",
            ]
            data = {}
            for key in keys:
                meta = self._config_schema.get(key, {})
                data[key] = self.config.get(key, meta.get("default", None))
            return jsonify({"status": "success", "data": data})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})
