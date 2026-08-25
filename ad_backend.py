# -*- coding: utf-8 -*-
"""广告 Web 管理后台（v2.21.0 起接入 AstrBot Dashboard）。

- 页面与接口统一注册到 AstrBot Dashboard（web.py register_web_api → /api/plug/ 下），
  鉴权由 Dashboard JWT 统一执行，不再有独立端口监听服务与独立页面
  （v2.31.0：删除 v2.10.x 遗留的 pages/ad_backend/index.html 独立页面入口，
  广告后台统一在主面板「广告后台」页签内完成）；
- 展示广告检测核心数据：今日/累计拦截统计、违规记录（含图片/视频证据）、
  感知哈希广告黑名单、视频指纹缓存、广告分级处置记录、关键配置状态。
"""

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


class AdBackendMixin:
    """广告后台 API 能力，由 ``Main`` 组合使用（需 Quart 可用）。"""

    # ============================================================
    # 后台 API（v2.21.0 起由 web.py 通过 register_web_api 注册到 AstrBot Dashboard）
    # ============================================================

    async def _ad_backend_stats(self):
        """总览统计：今日/累计拦截、图片/视频拦截、最近记录。

        v2.27.0：数据源改为 SQLite（持久化，重启不丢）；广告判定基于
        reason 类别（"ad"/"广告"）而非 msg/action 文字——修复图片广告
        OCR 识别文本不含"广告"字样导致面板不显示的 bug。
        v2.30.0：修复主面板「累计拦截/今日图片/今日视频」显示 undefined——
        补充 total_blocked / today_img / today_video 字段并保留旧字段名兼容；
        today_* 按今日过滤，blocked 为最近 2000 条内广告拦截总数；
        「放行」不属于拦截动作，不再计入拦截。
        """
        try:
            today_start = self._today_start()
            blocked = 0
            today_blocked = 0
            img_blocked = 0
            today_image_blocked = 0
            video_blocked = 0
            today_video_blocked = 0
            user_hits = {}
            recent = []
            logs = self._storage.list_logs(limit=2000, offset=0)
            for log in logs:
                ts = int(log.get("ts", 0) or 0)
                action = str(log.get("action", ""))
                msg = str(log.get("msg_text", ""))
                reason = str(log.get("reason", ""))
                urls = log.get("image_urls") or []
                is_ad = (
                    "ad" in reason.lower()
                    or "广告" in reason
                    or "广告" in action
                    or "广告" in msg
                    or "视频" in msg
                    or "视频" in action
                    or "复核" in action
                )
                # 拦截动作：撤回/禁言/踢/待复核；「放行」不是拦截，不计入
                is_blocked = (
                    "撤回" in action or "禁言" in action or "踢" in action
                    or "待复核" in action
                )
                if not (is_ad and is_blocked):
                    continue
                blocked += 1
                is_video = "视频" in msg or "视频" in action or "视频" in reason
                is_image = bool(urls) or "图片" in action or "图片" in reason or "图片" in msg
                if is_video:
                    video_blocked += 1
                elif is_image:
                    img_blocked += 1
                uid = str(log.get("user_id", ""))
                if uid:
                    user_hits[uid] = user_hits.get(uid, 0) + 1
                if ts >= today_start:
                    today_blocked += 1
                    if is_video:
                        today_video_blocked += 1
                    elif is_image:
                        today_image_blocked += 1
                    if len(recent) < 20:
                        recent.append(log)
            total_logs = self._storage.count_logs()
            top_users = sorted(
                user_hits.items(), key=lambda kv: kv[1], reverse=True
            )[:10]
            return jsonify({
                "status": "success",
                "data": {
                    "today_blocked": today_blocked,
                    "total_blocked": blocked,
                    "today_image_blocked": today_image_blocked,
                    "today_video_blocked": today_video_blocked,
                    "today_img": today_image_blocked,
                    "today_video": today_video_blocked,
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

    # ============================================================
    # v2.23.0 不确定视频广告管理员复核
    # ============================================================

    @staticmethod
    def _video_review_id(value):
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0

    async def _apply_video_ad_review_confirmed(self, item: dict):
        """确认违规的统一动作：学习视频指纹 + 禁言 + 记录。

        供 WebUI 复核接口与 QQ 管理群命令共用。返回 ``(banned, learned)``。
        """
        banned = False
        learned = False
        fp = str(item.get("fingerprint", "") or "")
        if fp:
            try:
                self._learn_video_fingerprint(fp)
                learned = True
            except Exception:
                pass
        group_id = str(item.get("group_id", "") or "")
        user_id = str(item.get("user_id", "") or "")
        try:
            gid = self._video_review_id(group_id)
            uid = self._video_review_id(user_id)
            duration = self._cfg_int(
                "moderation_ban_duration", 1800, group_id=group_id
            )
            client = await self._get_client()
            if client and gid and uid:
                ok, _err = await self._call_group_api(
                    client, "set_group_ban", "禁言",
                    group_id=gid, user_id=uid, duration=max(60, int(duration)),
                )
                banned = ok
        except Exception:
            pass
        try:
            self._log_moderation(
                group_id, user_id, str(item.get("user_name", "") or ""),
                str(item.get("msg_text", "") or ""),
                "管理员复核确认广告（已禁言）" if banned else "管理员复核确认广告（禁言失败）",
                "视频广告管理员复核确认违规",
                [],
            )
        except Exception:
            pass
        return banned, learned

    async def _ad_backend_video_reviews(self):
        """待复核的视频广告队列。"""
        try:
            try:
                limit = min(int(quart_request.args.get("limit", 50)), 200)
            except (ValueError, TypeError):
                limit = 50
            data = self._storage.list_pending_video_ad_reviews(limit)
            enabled = bool(self.config.get("video_ad_review_enabled", False))
            return jsonify({
                "status": "success",
                "data": data,
                "count": len(data),
                "enabled": enabled,
            })
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})

    async def _ad_backend_video_reviews_confirm(self):
        """确认违规：学习视频指纹 + 禁言 + 记录。"""
        try:
            body = await quart_request.get_json(force=True, silent=True) or {}
            review_id = self._video_review_id(body.get("review_id", 0))
            reviewer = str(body.get("reviewer", "") or "")[:64]
            if review_id <= 0:
                return jsonify({"status": "error", "message": "缺少 review_id"})
            item = self._storage.get_video_ad_review(review_id)
            if not item:
                return jsonify({"status": "error", "message": "未找到该复核记录"})
            if item.get("status") != "pending":
                return jsonify({"status": "error", "message": "该记录已处理"})
            ok = self._storage.resolve_video_ad_review(review_id, "confirmed", reviewer)
            if not ok:
                return jsonify({"status": "error", "message": "处理失败（可能已被处理）"})
            banned, learned = await self._apply_video_ad_review_confirmed(item)
            return jsonify({
                "status": "success",
                "banned": banned,
                "learned_fingerprint": learned,
            })
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})

    async def _ad_backend_video_reviews_clear(self):
        """确认正常 / 放行。"""
        try:
            body = await quart_request.get_json(force=True, silent=True) or {}
            review_id = self._video_review_id(body.get("review_id", 0))
            reviewer = str(body.get("reviewer", "") or "")[:64]
            if review_id <= 0:
                return jsonify({"status": "error", "message": "缺少 review_id"})
            item = self._storage.get_video_ad_review(review_id)
            if not item:
                return jsonify({"status": "error", "message": "未找到该复核记录"})
            if item.get("status") != "pending":
                return jsonify({"status": "error", "message": "该记录已处理"})
            ok = self._storage.resolve_video_ad_review(review_id, "cleared", reviewer)
            if not ok:
                return jsonify({"status": "error", "message": "处理失败（可能已被处理）"})
            try:
                self._log_moderation(
                    str(item.get("group_id", "") or ""),
                    str(item.get("user_id", "") or ""),
                    str(item.get("user_name", "") or ""),
                    str(item.get("msg_text", "") or ""),
                    "管理员复核放行（非广告）",
                    "管理员在 WebUI 广告后台-视频复核确认正常",
                    [],
                )
            except Exception:
                pass
            return jsonify({"status": "success"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)})
