# -*- coding: utf-8 -*-
"""感知哈希广告黑名单 + 广告分级处置。

1. 感知哈希（pHash）广告黑名单：
   - 图片/视频帧识别前先与历史广告样本库比对，命中则直接标记
     「已知广告」并跳过视觉 API 调用（省 GLM-4V 费用）。
   - 用 OpenCV 实现 pHash（缩放→DCT→低频→中值→64bit），不新增
     imagehash/Pillow 依赖。
   - 命中黑名单不直接处罚，而是作为强信号进入统一审核流程，
     LLM 文本复核兜底防误杀。
   - 广告被确认违规后自动学习新样本（ad_hash_auto_learn）。

2. 广告分级处置：
   - 按窗口内广告违规次数升级处罚：警告 → 禁言 → 踢出。
   - 阈值与窗口均可按群覆盖，默认关闭（保持原有直接处罚行为）。
"""

import json
import os
import time

from astrbot.api import logger

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

HASH_BLACKLIST_FILE = "hash_blacklist.json"
VIDEO_FP_CACHE_FILE = "video_fingerprint_cache.json"
VIDEO_FP_CACHE_MAX = 500
AD_ESCALATION_FILE = "ad_escalation.json"
PHASH_SIZE = 8            # 8x8 = 64 bit hash
PHASH_RESIZE = 32         # DCT 前缩放尺寸
DEFAULT_HASH_DISTANCE = 10
MAX_HASH_ENTRIES = 5000


class HashAuditMixin:
    """感知哈希黑名单与广告分级处置能力，由 ``ModerationMixin`` 组合使用。"""

    def _init_hash_audit_resources(self) -> None:
        """加载黑名单与分级记录；初始化本消息审核期的媒体哈希缓存。"""
        self._hash_blacklist = self._load_json_file(HASH_BLACKLIST_FILE, {"hashes": []})
        self._ad_escalation = self._load_json_file(AD_ESCALATION_FILE, {})
        self._recent_media_hashes = {}   # url -> phash，供确认广告后学习
        self._recent_video_fingerprints = {}   # 视频指纹 -> 1，供确认广告后学习
        self._video_fp_cache = self._load_json_file(VIDEO_FP_CACHE_FILE, {})

    # ============================================================
    # JSON 持久化
    # ============================================================

    def _data_file_path(self, filename: str) -> str:
        """返回插件数据目录下的文件路径（目录不存在时惰性创建）。"""
        data_dir = getattr(self, "_data_dir", None)
        if data_dir:
            try:
                os.makedirs(str(data_dir), exist_ok=True)
                return os.path.join(str(data_dir), filename)
            except Exception:
                pass
        return os.path.join(os.getcwd(), filename)

    def _load_json_file(self, filename: str, default: dict) -> dict:
        path = self._data_file_path(filename)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else default
        except Exception:
            return default

    def _save_json_file(self, filename: str, data: dict) -> None:
        path = self._data_file_path(filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception as exc:
            logger.debug(f"[GroupMgr] 保存 {filename} 失败: {exc}")

    # ============================================================
    # 感知哈希（pHash）
    # ============================================================

    @staticmethod
    def _phash_from_data(data: bytes, hash_size: int = PHASH_SIZE) -> str:
        """计算图片字节的感知哈希，返回 64 位 01 串；失败返回空串。"""
        if cv2 is None or not data:
            return ""
        try:
            import numpy as np

            arr = np.frombuffer(data, np.uint8)
            image = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
            if image is None:
                return ""
            return HashAuditMixin._phash_from_gray(image, hash_size)
        except Exception:
            return ""

    @staticmethod
    def _phash_from_path(path: str, hash_size: int = PHASH_SIZE) -> str:
        """计算本地图片文件的感知哈希；失败返回空串。"""
        if cv2 is None or not path:
            return ""
        try:
            image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if image is None:
                return ""
            return HashAuditMixin._phash_from_gray(image, hash_size)
        except Exception:
            return ""

    @staticmethod
    def _phash_from_gray(gray_image, hash_size: int = PHASH_SIZE) -> str:
        """感知哈希（v2.21.0 重构）：前 16 位亮度阈值编码 + 后 48 位 dHash 结构编码。

        修复（原 DCT-pHash 缺陷）：
        - 纯色图（亮度 30 vs 220）DCT 只有 DC 非零、哈希完全相同 → '不同图片远离' 失败；
        - JPEG 有损会把单像素亮点扩散成整块亮度变化，DCT 低频哈希大量翻转 → '相似图片接近' 失败。

        新方案：亮度用 16 个阈值(0/16/.../240)绝对编码（纯色图亮度不同则距离大），
        结构用 dHash 相邻像素比较（resize 9×8，对单像素/JPEG 噪声鲁棒）。
        返回 64 位 01 串，长度与历史一致。
        """
        try:
            import numpy as np

            img = cv2.resize(
                gray_image, (9, 8), interpolation=cv2.INTER_AREA
            ).astype(np.float32)
            # 亮度阈值编码：mean(0-255) 与 16 个阈值比较
            mean8 = int(round(float(img.mean())))
            mean_bits = "".join(
                "1" if mean8 > t else "0" for t in range(0, 256, 16)
            )  # 16 位
            # dHash：行内相邻像素比较（9×8 → 8×8 = 64 位，取前 48 位）
            diff = img[:, 1:] > img[:, :-1]
            ac_bits = "".join("1" if x else "0" for x in diff.flatten())[:48]
            return mean_bits + ac_bits
        except Exception:
            return ""

    @staticmethod
    def _hamming_distance(a: str, b: str) -> int:
        """计算两个 01 串的汉明距离；任一为空返回 64（视为不匹配）。"""
        if not a or not b:
            return 64
        return sum(1 for x, y in zip(a, b) if x != y)

    # ============================================================
    # 黑名单检查与学习
    # ============================================================

    def _check_hash_blacklist(self, phash: str, distance: int) -> int:
        """返回与黑名单的最小距离；未命中返回 64。

        v2.21.0 重构后的哈希为「16 位亮度阈值 + 48 位 dHash 结构」。
        录屏/加边框/不同背景截图等场景会使整体亮度明显变化（亮度段大比例
        翻转），整串汉明距离可能超过阈值，导致「图片广告被录屏成视频」后
        匹配不上原图的黑名单样本。因此在整串匹配之外追加「结构段匹配」：
        亮度段差异 <= 8 且结构段（后 48 位）差异 <= max(distance-4, 2)
        时也视为命中（命中仍走 LLM 复核兜底，不直接处罚）。
        """
        if not phash:
            return 64
        best = 64
        try:
            distance = max(0, int(distance))
        except Exception:
            distance = 10
        for entry in self._hash_blacklist.get("hashes", []):
            ref = str(entry.get("h", ""))
            d = self._hamming_distance(phash, ref)
            if d < best:
                best = d
            if best <= distance:
                return best
            if len(phash) == 64 and len(ref) == 64:
                bright_d = self._hamming_distance(phash[:16], ref[:16])
                struct_d = self._hamming_distance(phash[16:], ref[16:])
                struct_limit = max(distance - 4, 2)
                if bright_d <= 8 and struct_d <= struct_limit:
                    return max(0, min(distance, struct_d + bright_d))
        return best

    def _learn_hash(self, phash: str, distance: int) -> None:
        """学习新广告样本到黑名单；与已有样本足够近时仅累加计数。"""
        if not phash:
            return
        hashes = self._hash_blacklist.setdefault("hashes", [])
        now = int(time.time())
        for entry in hashes:
            if self._hamming_distance(phash, str(entry.get("h", ""))) <= distance:
                entry["count"] = int(entry.get("count", 0) or 0) + 1
                entry["last_ts"] = now
                self._save_json_file(HASH_BLACKLIST_FILE, self._hash_blacklist)
                return
        hashes.append({"h": phash, "count": 1, "first_ts": now, "last_ts": now})
        if len(hashes) > MAX_HASH_ENTRIES:
            hashes.sort(key=lambda e: int(e.get("last_ts", 0) or 0), reverse=True)
            del hashes[MAX_HASH_ENTRIES:]
        self._save_json_file(HASH_BLACKLIST_FILE, self._hash_blacklist)

    def _learn_recent_ad_hashes(self, group_id: str) -> None:
        """确认广告违规后，把本消息审核期缓存的媒体哈希批量学习入黑名单。"""
        recent = getattr(self, "_recent_media_hashes", {})
        if not recent:
            return
        distance = self._cfg_int("ad_hash_distance", DEFAULT_HASH_DISTANCE, group_id=group_id)
        for phash in recent.values():
            if phash:
                self._learn_hash(phash, distance)

    # ============================================================
    # 广告分级处置
    # ============================================================

    def _record_ad_escalation(self, group_id: str, user_id: str, window: int) -> int:
        """登记一次广告违规并返回窗口内累计次数；窗口外重置为 1。"""
        now = int(time.time())
        groups = self._ad_escalation
        if not isinstance(groups, dict):
            groups = {}
            self._ad_escalation = groups
        group_data = groups.setdefault(str(group_id), {})
        if not isinstance(group_data, dict):
            group_data = {}
            groups[str(group_id)] = group_data
        record = group_data.get(str(user_id))
        if not isinstance(record, dict) or (now - int(record.get("first_ts", 0) or 0)) > window:
            record = {"count": 0, "first_ts": now, "last_ts": now}
            group_data[str(user_id)] = record
        record["count"] = int(record.get("count", 0) or 0) + 1
        record["last_ts"] = now
        self._save_json_file(AD_ESCALATION_FILE, groups)
        return record["count"]

    def _ad_escalation_is_ad(self, hit_summary: str = "", hit_types: dict = None) -> bool:
        """判断当前违规是否属于广告类别（规则类别或 LLM 摘要）。"""
        if hit_types:
            for key in ("ad", "learned_ad", "ad_hash", "image_scan"):
                if hit_types.get(key):
                    return True
        if hit_summary:
            lowered = str(hit_summary).lower()
            for token in ("ad", "广告", "image_scan", "learned_ad"):
                if token in lowered:
                    return True
        return False

    async def _handle_ad_escalation(
        self,
        event,
        group_id: str,
        user_id: str,
        user_name: str,
        text: str,
        reason: str,
        image_urls: list,
    ) -> None:
        """分级处置：按窗口内累计次数升级 警告 → 禁言 → 踢出。

        处理完成后结束；可选地通过 async generator 产出群内提示。
        """
        _ = max(1, self._cfg_int("ad_escalation_warn_at", 1, group_id=group_id))
        ban_at = max(1, self._cfg_int("ad_escalation_ban_at", 2, group_id=group_id))
        kick_at = max(1, self._cfg_int("ad_escalation_kick_at", 3, group_id=group_id))
        window = max(60, self._cfg_int("ad_escalation_window_seconds", 604800, group_id=group_id))
        level = self._record_ad_escalation(group_id, user_id, window)
        ban_duration = self._cfg_int("moderation_ban_duration", 1800, group_id=group_id)
        action = ""
        if level >= kick_at:
            try:
                kick_ok = await self._kick_member(event)
            except Exception:
                kick_ok = False
            action = "撤回+踢出" if kick_ok else "撤回（踢出失败）"
        elif level >= ban_at:
            self._mark_moderation_penalty(group_id, user_id, ban_duration)
            mute_ok = await self._mute_member(event, ban_duration)
            if mute_ok:
                self._schedule_unban(group_id, user_id, ban_duration)
                action = "撤回+禁言"
            else:
                self._clear_moderation_penalty(group_id, user_id)
                action = "撤回（禁言失败）"
        else:
            action = "撤回+警告"
        self._log_moderation(
            group_id, user_id, user_name, text,
            f"{action}（广告第{level}次）", reason, image_urls,
        )
        if self._cfg("auto_moderate_notice", True, group_id=group_id):
            try:
                notice = (
                    f"[群管] {user_name}({user_id}) 检测到广告，{action}；"
                    f"该群广告违规第{level}次"
                )
                yield event.plain_result(
                    notice.replace("{name}", user_name).replace("{uid}", user_id)
                )
            except Exception as notice_err:
                logger.warning(f"[GroupMgr] 发送分级处置通知失败: {notice_err}")
        event.stop_event()
        return

    # ============================================================
    # 视频指纹缓存（广告确认过的整段视频直接命中）
    # ============================================================

    def _check_video_fp_cache(self, fingerprint: str) -> bool:
        """广告视频指纹缓存命中判断。

        v2.20.0 支持多帧鲁棒指纹（``h1_h2_h3_bucket``）与旧格式（``phash_total``）：
        - 整串精确命中（旧缓存直接兼容）；
        - 多帧格式：任一帧感知哈希（64 位 01 串）与缓存中的帧哈希相同即命中，
          容忍同一广告被裁剪、拼接或改时长导致的指纹变化。
        """
        if not fingerprint:
            return False
        if fingerprint in self._video_fp_cache:
            return True
        parts = fingerprint.split("_")
        if len(parts) < 4:
            return False
        try:
            cache_items = list(self._video_fp_cache.items())
        except Exception:
            return False
        try:
            for cached, _ts in cache_items:
                cparts = str(cached).split("_")
                if not cparts:
                    continue
                for a in parts[:3]:
                    if not a:
                        continue
                    for b in cparts[:3]:
                        # 只对比长度 >= 32 的哈希段（排除旧格式中的帧数字段）
                        if b and len(b) >= 32 and a == b:
                            return True
        except Exception:
            pass
        return False

    def _learn_video_fingerprint(self, fingerprint: str) -> None:
        """把确认广告的视频指纹写入缓存（LRU 上限裁剪）。"""
        if not fingerprint:
            return
        cache = self._video_fp_cache
        cache[fingerprint] = int(time.time())
        if len(cache) > VIDEO_FP_CACHE_MAX:
            stale_keys = sorted(cache, key=cache.get)[
                : len(cache) - VIDEO_FP_CACHE_MAX
            ]
            for stale in stale_keys:
                cache.pop(stale, None)
        self._save_json_file(VIDEO_FP_CACHE_FILE, cache)

    def _learn_recent_video_fingerprints(self) -> None:
        """广告确认后，把本消息审核期缓存的视频指纹批量写入缓存。"""
        recent = getattr(self, "_recent_video_fingerprints", {})
        for fingerprint in recent:
            if fingerprint:
                self._learn_video_fingerprint(fingerprint)
