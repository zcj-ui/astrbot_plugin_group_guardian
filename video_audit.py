# -*- coding: utf-8 -*-
"""视频广告检测能力：下载视频 → OpenCV 抽帧 → 视觉模型识别 + 二维码解码。

设计原则：
- 完全复用图片审核的基础设施：视觉模型 OCR（``_call_llm_ocr``）、二维码解码
  （``_decode_qr_from_bytes``）、SSRF 防护下载（``_download_bytes`` / ``_is_safe_image_url``）、
  并发控制（``_map_image_work``），不新增第三方依赖（opencv-python-headless 已随插件安装）。
- 视频审核成本远高于图片（下载 + 抽帧 + 多帧视觉调用），默认关闭
  （``video_audit_enabled``），并受体积、下载超时、抽帧超时、总超时多重上限保护。
- 识别出的文本与图片 OCR 一样合并进审核正文，统一走
  「正则初筛 + LLM 二次判断 + 处罚」流程；任一环节失败都静默降级，不误杀正常消息。
"""

import asyncio
import base64
import math
import os
import tempfile
import time
import uuid

from astrbot.api import logger

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

try:
    from .image_audit import (
        IMAGE_WORKER_CONCURRENCY,
        _probe_qr_decoder,
    )
except ImportError:  # 独立加载 video_audit.py 的单元测试兼容路径
    from image_audit import (
        IMAGE_WORKER_CONCURRENCY,
        _probe_qr_decoder,
    )

# 单视频最大体积（默认 30MB），与 video_max_size_mb 配置联动
VIDEO_MAX_BYTES = 30 * 1024 * 1024
# 下载排队等待上限
VIDEO_QUEUE_TIMEOUT = 120.0
# 抽帧耗时上限（防止损坏/超大视频卡死事件循环线程）
VIDEO_FRAME_EXTRACT_TIMEOUT = 30.0
# 单条消息视频审核总超时（默认 60s），超过则放弃本次审核
VIDEO_AUDIT_TOTAL_TIMEOUT = 60.0
# 视频临时文件子目录名
VIDEO_TEMP_SUBDIR = "video_cache"
# 抽帧 JPEG 压缩质量
VIDEO_JPEG_QUALITY = 85
# 视频识别文本拼入正文的最大字符数
VIDEO_EVIDENCE_MAX_CHARS = 20_000
# 每个视频最多抽帧硬上限（即使配置写大也截断，保护 LLM 预算）
VIDEO_FRAMES_HARD_CAP = 10


class VideoAuditMixin:
    """视频广告检测能力，由 ``ModerationMixin`` 组合使用。"""

    # ============================================================
    # 资源生命周期
    # ============================================================

    @staticmethod
    async def _run_sync_in_thread(func, *args, **kwargs):
        """在线程中执行同步函数（v2.21.0，Python 3.8 兼容的 asyncio.to_thread）。"""
        import asyncio as _asyncio

        loop = _asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    def _init_video_audit_resources(self, llm_concurrency: int = 4) -> None:
        """初始化视频审核资源：下载并发闸门、任务追踪、临时目录槽位。"""
        concurrency = max(1, int(llm_concurrency))
        # v2.21.0：Semaphore 惰性创建——在无运行事件循环的同步初始化路径直接
        # asyncio.Semaphore() 会报 "There is no current event loop"（Python 3.8 尤其明显）。
        self._video_download_semaphore = None
        self._video_download_concurrency = min(4, concurrency)
        self._video_audit_closing = False
        self._video_audit_tasks = set()
        self._video_temp_dir = None
    async def _close_video_audit_resources(self) -> None:
        """取消在途视频审核分支并清理临时文件；重复调用是安全的。"""
        self._video_audit_closing = True
        current_task = asyncio.current_task()
        tasks = [
            task for task in getattr(self, "_video_audit_tasks", set())
            if task is not current_task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self._run_sync_in_thread(self._cleanup_video_temp_dir)
        except Exception as exc:
            logger.debug(f"[GroupMgr] 清理视频临时目录失败: {exc}")

    def _create_video_audit_task(self, coroutine):
        """追踪视频审核分支任务，便于插件卸载时取消。"""
        task = asyncio.create_task(coroutine)
        tasks = getattr(self, "_video_audit_tasks", None)
        if isinstance(tasks, set):
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return task

    def _video_temp_directory(self) -> str:
        """视频临时文件目录（惰性创建）。优先插件数据目录，兜底系统临时目录。"""
        directory = getattr(self, "_video_temp_dir", None)
        if directory:
            return directory
        data_dir = getattr(self, "_data_dir", "") or ""
        if data_dir:
            directory = os.path.join(str(data_dir), VIDEO_TEMP_SUBDIR)
        else:
            directory = os.path.join(
                tempfile.gettempdir(), "group_guardian_video_cache"
            )
        self._video_temp_dir = directory
        return directory

    def _cleanup_video_temp_dir(self) -> None:
        """清空视频临时目录中的全部文件（供插件卸载/测试收尾调用）。"""
        directory = getattr(self, "_video_temp_dir", None)
        if not directory or not os.path.isdir(directory):
            return
        try:
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                    elif os.path.isdir(path):
                        import shutil

                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            pass

    def _write_video_temp_file(self, data: bytes) -> str:
        """把视频字节写入临时目录，返回本地路径。"""
        directory = self._video_temp_directory()
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            logger.debug(f"[GroupMgr] 创建视频临时目录失败: {exc}")
            return ""
        filename = f"video_{int(time.time() * 1000)}_{uuid.uuid4().hex[:10]}.mp4"
        path = os.path.join(directory, filename)
        try:
            with open(path, "wb") as f:
                f.write(data)
            return path
        except OSError as exc:
            logger.debug(f"[GroupMgr] 写入视频临时文件失败: {exc}")
            return ""

    @staticmethod
    def _remove_temp_file(path: str) -> None:
        """安全删除单个临时文件。"""
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def _cfg_float(self, key: str, default: float = 0.0, group_id: str = None) -> float:
        """读取浮点配置：群覆盖优先，其次全局 config，再次 schema 默认值。"""
        if group_id:
            gv = self._get_group_override(group_id, key)
            if gv is not None:
                try:
                    return float(gv)
                except (TypeError, ValueError):
                    return default
        meta = self._config_schema.get(key, {}) if hasattr(self, "_config_schema") else {}
        try:
            return float(self.config.get(key, meta.get("default", default)))
        except (TypeError, ValueError):
            return default
    # ============================================================
    # 视频组件收集与源解析
    # ============================================================

    def _collect_video_components(self, event) -> list:
        """从消息链（含 Reply 引用）收集 video 段，返回 ``(component, data, marker)`` 列表。

        marker 用于同一消息链内去重；dict 段取 data.url/file，组件对象取 url/file 字段。
        """
        videos = []
        try:
            chain = event.get_messages() or []
        except Exception:
            return videos
        seen = set()

        def collect_from(comp) -> None:
            try:
                seg_type, data = self._component_type_data(comp)
            except Exception:
                return
            if seg_type != "video":
                return
            marker = self._component_url(comp, data)
            if marker and marker in seen:
                return
            if marker:
                seen.add(marker)
            videos.append((comp, data, marker))

        for comp in chain:
            try:
                seg_type, _data = self._component_type_data(comp)
            except Exception:
                continue
            if seg_type == "reply":
                # 引用消息中的视频同样纳入检查（部分协议端会内嵌 chain）
                sub_chain = getattr(comp, "chain", None) or []
                for sub in sub_chain:
                    collect_from(sub)
                continue
            collect_from(comp)
        return videos

    async def _resolve_video_source(self, event, component, data=None) -> str:
        """解析视频的真实来源，返回 http(s) URL 或本地已存在的文件路径；失败返回空串。

        解析顺序：
        1. 组件对象的 ``convert_to_file_path()``（协议端已缓存时最快）；
        2. url / path / file 字段（http(s) URL 或本地存在的路径，兼容 ``file://`` 前缀）；
        3. 协议端 ``get_file`` API 兜底（file_id → 真实路径/URL）。
        """
        # dict 段：从 data 或自身取 url/file/path
        if isinstance(component, dict):
            data = data if isinstance(data, dict) else component.get("data", {})
            for key in ("url", "file", "path"):
                cand = str(
                    (data or {}).get(key, "") or component.get(key, "")
                ).strip()
                if not cand:
                    continue
                cand = self._strip_file_prefix(cand)
                if cand.startswith(("http://", "https://")):
                    return cand
                if os.path.exists(cand):
                    return cand
            file_val = str(
                (data or {}).get("file", "") or (data or {}).get("url", "")
            ).strip()
        else:
            # 组件对象：优先 convert_to_file_path
            convert = getattr(component, "convert_to_file_path", None)
            if callable(convert):
                try:
                    result = await convert()
                    path = self._strip_file_prefix(str(result or "").strip())
                    if path:
                        return path
                except Exception as exc:
                    logger.debug(f"[GroupMgr] 视频 convert_to_file_path 失败: {exc}")
            for attr in ("url", "path", "file"):
                try:
                    cand = self._strip_file_prefix(
                        str(getattr(component, attr, "") or "").strip()
                    )
                except Exception:
                    continue
                if cand.startswith(("http://", "https://")):
                    return cand
                if cand and os.path.exists(cand):
                    return cand
            file_val = ""
            for attr in ("file", "url"):
                try:
                    file_val = str(getattr(component, attr, "") or "").strip()
                except Exception:
                    continue
                if file_val:
                    break

        # get_file API 兜底：部分协议端只给 file 标识，需向协议端换取真实路径
        client = await self._get_client(event) if hasattr(self, "_get_client") else None
        if client and file_val:
            try:
                info = await asyncio.wait_for(
                    client.call_action("get_file", file_id=file_val), timeout=20.0
                )
                fpath = self._strip_file_prefix(
                    str((info or {}).get("file") or "").strip()
                )
                if fpath:
                    # 协议端 get_file 返回的真实路径/URL，直接信任采用
                    return fpath
            except Exception as exc:
                logger.debug(f"[GroupMgr] get_file 获取视频失败: {exc}")
        return ""

    @staticmethod
    def _strip_file_prefix(value: str) -> str:
        """去除 ``file://`` / ``file:///`` 前缀，便于本地路径判断。

        ``file:///tmp/a.mp4`` 的路径部分是 ``/tmp/a.mp4``（host 为空），
        因此 ``file:///`` 前缀剥离后需保留前导 ``/``。
        """
        value = str(value or "")
        lower = value.lower()
        if lower.startswith("file:///"):
            return "/" + value[len("file:///"):]
        if lower.startswith("file://"):
            return value[len("file://"):]
        return value
    # ============================================================
    # 视频下载与抽帧
    # ============================================================

    async def _download_video(
        self, url: str, max_bytes: int, timeout: float
    ) -> str:
        """下载视频到临时文件，返回本地路径；任一环节失败返回空串。

        复用 ``_download_bytes`` 的 SSRF 防护（逐跳校验重定向地址、拒绝内网）与
        插件级 I/O 并发许可；仅把体积上限放宽到视频配置值。
        """
        # v2.21.0：Semaphore 惰性创建（首次在 async 上下文使用时才创建）
        semaphore = self._video_download_semaphore
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._video_download_concurrency)
            self._video_download_semaphore = semaphore
        acquired = False
        try:
            await asyncio.wait_for(
                semaphore.acquire(), timeout=VIDEO_QUEUE_TIMEOUT
            )
            acquired = True
            data = await self._download_bytes(
                url, max_bytes=max_bytes, timeout=timeout
            )
            if not data:
                return ""
            return await self._run_sync_in_thread(self._write_video_temp_file, data)
        except asyncio.TimeoutError:
            logger.debug(f"[GroupMgr] 视频下载排队或请求超时({url[:60]})\"")
            return ""
        except Exception as exc:
            logger.debug(f"[GroupMgr] 下载视频失败({url[:60]}): {exc}")
            return ""
        finally:
            if acquired:
                semaphore.release()

    @staticmethod
    def _is_meaningful_frame(
        frame_bytes: bytes,
        min_mean: float = 12.0,
    ) -> bool:
        """轻量判断帧是否有效内容：排除纯黑/纯白帧（v2.22.0）。

        录屏视频开头/结尾常有黑屏或白屏过渡帧，会浪费 `video_max_frames`
        的名额并挤掉中间的广告画面。仅以整体灰度均值判断（纯色但有内容的
        深色/浅色画面不会被误删）。判断失败时视为有效（不误删）。
        """
        if cv2 is None or not frame_bytes:
            return False
        try:
            import numpy as np

            arr = np.frombuffer(frame_bytes, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
            if frame is None:
                return False
            mean = float(frame.mean())
            if mean < min_mean or mean > 255.0 - min_mean:
                return False
            return True
        except Exception:
            return True

    def _filter_meaningful_frames(self, frames: list, max_frames: int) -> list:
        """过滤纯黑/纯白/低对比帧，保持顺序与数量上限。"""
        if not frames:
            return []
        kept = [f for f in frames if self._is_meaningful_frame(f)]
        return kept[:max_frames]

    def _extract_video_frames(
        self, video_path: str, max_frames: int, interval_sec: float,
        mode: str = "interval", scene_threshold: float = 30.0,
    ) -> list:
        """使用 OpenCV 抽帧，返回 JPEG 字节列表（线程池中执行）。

        mode:
          - "interval": 等间隔抽帧（默认，兼容旧行为）；
          - "scene": 场景切换抽帧，仅在画面明显变化时保留帧
            （广告信息通常集中在关键画面，可减少无效帧与视觉调用）；
          - "spans": 首/中/尾三段分段采样（v2.20.0），保证品牌露出（开头）
            与促销信息（结尾）不遗漏，适合无字幕口播广告。
        """
        if cv2 is None:
            logger.warning(
                "[GroupMgr] 视频抽帧依赖 opencv-python-headless 不可用，视频审核不生效。"
                "可重装: pip install opencv-python-headless numpy"
            )
            return []
        cap = None
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                cap.release()
                return []
            if str(mode).strip().lower() == "spans":
                frames = self._spans_extract_frames(cap, max_frames)
                return self._filter_meaningful_frames(frames, max_frames)
            if str(mode).strip().lower() == "scene":
                frames = self._scene_extract_frames(cap, max_frames, scene_threshold)
                if len(frames) < 3:
                    frames = self._scene_backup_frames(video_path, max_frames, frames)
                return self._filter_meaningful_frames(frames, max_frames)
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            if fps <= 0:
                fps = 30.0
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            step = max(1, int(fps * max(float(interval_sec), 0.1)))
            frames = []
            target_indices = set(range(0, total, step)) if total > 0 else None
            idx = 0
            while len(frames) < max_frames:
                ret, frame = cap.read()
                if not ret:
                    break
                if target_indices is None or idx in target_indices:
                    ok, buf = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_JPEG_QUALITY],
                    )
                    # v2.22.0：跳过纯黑/纯白/低对比帧，避免录屏首尾过渡帧占用名额
                    if ok and self._is_meaningful_frame(buf.tobytes()):
                        frames.append(buf.tobytes())
                idx += 1
            return frames
        except Exception as exc:
            logger.warning(f"[GroupMgr] 视频抽帧失败: {exc}")
            return []
        finally:
            if cap is not None:
                cap.release()

    def _scene_extract_frames(self, cap, max_frames: int, scene_threshold: float) -> list:
        """场景切换抽帧：仅保留画面明显变化的帧（首帧保留）。"""
        frames = []
        prev_gray = None
        try:
            while len(frames) < max_frames:
                ret, frame = cap.read()
                if not ret:
                    break
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                changed = prev_gray is None
                if prev_gray is not None:
                    diff = float(cv2.absdiff(gray, prev_gray).mean())
                    if diff > float(scene_threshold):
                        changed = True
                prev_gray = gray
                if changed:
                    ok, buf = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_JPEG_QUALITY],
                    )
                    if ok:
                        frames.append(buf.tobytes())
        except Exception:
            pass
        return frames

    def _spans_extract_frames(self, cap, max_frames: int) -> list:
        """首/中/尾三段分段采样：开头/中间/结尾各取约 1/3 帧数。

        广告视频的信息通常集中在开头（品牌露出/引流字幕）与结尾（联系方式/下单提示），
        等间隔采样可能漏掉这些关键画面。帧位置先收集去重再统一 seek，保证编码顺序。
        """
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total <= 1:
                # 极短视频：退化为顺序读前几帧
                frames = []
                for _ in range(max_frames):
                    ret, frame = cap.read()
                    if not ret:
                        break
                    ok, buf = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_JPEG_QUALITY],
                    )
                    if ok:
                        frames.append(buf.tobytes())
                return frames
            per = max(1, int(math.ceil(max_frames / 3)))
            third = total // 3
            positions = set()
            for seg, start in enumerate((0, third, total - third)):
                seg_count = max(1, min(per, max_frames - len(positions)))
                step = max(1, third // seg_count) if seg > 0 and seg_count else 1
                for k in range(seg_count):
                    positions.add(min(total - 1, start + k * step))
                if len(positions) >= max_frames:
                    break
            frames = []
            for pos in sorted(positions):
                if len(frames) >= max_frames:
                    break
                cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
                ret, frame = cap.read()
                if not ret:
                    continue
                ok, buf = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_JPEG_QUALITY],
                )
                if ok:
                    frames.append(buf.tobytes())
            return frames
        except Exception:
            return []

    def _scene_backup_frames(self, video_path: str, max_frames: int, existing: list) -> list:
        """场景抽帧不足 3 帧时，重开视频取前几帧补足，避免空结果。"""
        result = list(existing or [])
        if len(result) >= max_frames:
            return result
        backup_cap = None
        try:
            backup_cap = cv2.VideoCapture(video_path)
            idx = 0
            while len(result) < min(max_frames, 5) and idx < 30:
                ret, frame = backup_cap.read()
                if not ret:
                    break
                if idx < 5:
                    ok, buf = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_JPEG_QUALITY],
                    )
                    if ok:
                        result.append(buf.tobytes())
                idx += 1
        except Exception:
            pass
        finally:
            if backup_cap is not None:
                backup_cap.release()
        return result

    @staticmethod
    def _quick_precheck_frame(frame_bytes: bytes) -> float:
        """轻量预检：返回可疑度 0-1。

        高饱和度 / 纹理密集 / 文字区域占比高则分数高；分数低于
        video_precheck_threshold 的帧跳过视觉 API（省调用）。预检失败返回 1.0
        （视为可疑，走完整检测，不误漏）。
        """
        if cv2 is None:
            return 1.0
        try:
            import numpy as np

            arr = np.frombuffer(frame_bytes, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return 1.0
            score = 0.0
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            if float(hsv[:, :, 1].mean()) > 150.0:
                score += 0.3
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if float(cv2.Canny(gray, 100, 200).mean()) > 80.0:
                score += 0.3
            try:
                mser = cv2.MSER_create()
                regions, _ = mser.detectRegions(gray)
                total_px = gray.shape[0] * gray.shape[1]
                text_area = sum(int(r.shape[0] * r.shape[1]) for r in regions)
                if total_px and (text_area / total_px) > 0.05:
                    score += 0.4
            except Exception:
                pass
            return score
        except Exception:
            return 1.0

    @staticmethod
    def _subtitle_band_boost(frame_bytes: bytes):
        """字幕带增强：把画面下方 1/3 的字幕区裁剪并放大 1.5 倍后返回 JPEG。

        硬字幕广告（价格/联系方式滚动字幕）字号小，全帧 OCR 易漏；
        放大字幕带可显著提升小字识别率。失败返回 None（不阻断主流程）。
        """
        if cv2 is None:
            return None
        try:
            import numpy as np

            arr = np.frombuffer(frame_bytes, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return None
            h, w = frame.shape[:2]
            y0, y1 = int(h * 0.72), int(h * 0.97)
            if y1 - y0 < 8 or w < 8:
                return None
            band = frame[y0:y1, :]
            scaled = cv2.resize(
                band,
                (int(band.shape[1] * 1.5), int(band.shape[0] * 1.5)),
                interpolation=cv2.INTER_CUBIC,
            )
            ok, buf = cv2.imencode(
                ".jpg",
                scaled,
                [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_JPEG_QUALITY],
            )
            return buf.tobytes() if ok else None
        except Exception:
            return None

    def _video_fingerprint(self, video_path: str) -> str:
        """视频指纹：首/中/尾三帧感知哈希 + 时长分段桶（v2.20.0 多帧鲁棒版）。

        旧版（v2.9~v2.19）只取首帧哈希 + 总帧数，同一广告被裁剪/改时长就会漏命中；
        新版取三帧哈希 + 30 帧时长桶，缓存命中走「任一帧哈希相同」匹配，容忍裁剪。
        失败返回空串。
        """
        if cv2 is None:
            return ""
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return ""
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total <= 0:
                cap.release()
                return ""
            positions = sorted({0, max(0, total // 2), max(0, total - 1)})
            hashes = []
            for pos in positions:
                cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
                ret, frame = cap.read()
                if ret:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    phash = self._phash_from_gray(gray)
                    if phash:
                        hashes.append(phash)
            cap.release()
            if not hashes:
                return ""
            bucket = total // 30  # 30 帧时长桶：容忍裁剪/拼接导致的时长变化
            h2 = hashes[1] if len(hashes) > 1 else ""
            h3 = hashes[2] if len(hashes) > 2 else ""
            return f"{hashes[0]}_{h2}_{h3}_{bucket}"
        except Exception:
            return ""

    @staticmethod
    def _frame_to_data_url(frame_bytes: bytes) -> str:
        """JPEG 帧字节 → base64 data URL，供视觉模型识别。"""
        b64 = base64.b64encode(frame_bytes).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"

    # ============================================================
    # 视频审核主流程
    # ============================================================

    async def _apply_video_audit(
        self, text: str, videos: list, event, group_id: str
    ) -> str:
        """批量审核视频并把识别文本并入正文；开关关闭或失败时原样返回。"""
        # v2.23.0：疑似广告信号——每次审核开始前重置
        self._video_ad_review_signal = False
        self._video_ad_review_source = ""
        self._video_ad_review_fingerprint = ""
        if not videos:
            return text
        if getattr(self, "_video_audit_closing", False):
            return text
        if not self._cfg("video_audit_enabled", False, group_id=group_id):
            return text
        # 至少需要一种识别手段：LLM 视觉（识别帧内广告文字/画面）或二维码解码
        llm_enabled = self._cfg("llm_moderation_enabled", True, group_id=group_id)
        qr_enabled = self._cfg("qrcode_decode_enabled", False, group_id=group_id)
        if not llm_enabled and not qr_enabled:
            return text
        timeout = float(self._cfg_int("video_audit_timeout", 60, group_id=group_id))
        timeout = max(10.0, min(timeout, 300.0))
        try:
            results = await asyncio.wait_for(
                self._audit_all_videos(event, videos, group_id),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[GroupMgr] 视频审核总时长超限: group={group_id}")
            return text
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[GroupMgr] 视频审核失败: {exc}")
            return text
        video_text = "\n".join(r for r in results if r).strip()
        if not video_text:
            return text
        # v2.23.0：LLM 广告专用判定输出「疑似广告」→ 标记待管理员复核信号
        if "疑似广告" in video_text:
            self._video_ad_review_signal = True
            recent = getattr(self, "_recent_video_fingerprints", {})
            self._video_ad_review_fingerprint = (
                next(iter(recent)) if recent else ""
            )
            try:
                self._video_ad_review_source = str(videos[0][2] or "")
            except Exception:
                self._video_ad_review_source = ""
        text = (
            text + "\n[视频审核]\n" + video_text
            if text
            else "[视频审核]\n" + video_text
        )
        return self._bounded_audit_text(text, self._AUDIT_MAX_CHARS)

    async def _audit_all_videos(self, event, videos: list, group_id: str) -> list:
        """并发审核多个视频（每个视频内部逐帧串行），保持输入顺序返回结果。"""
        worker = lambda item: self._audit_one_video(
            event, item[0], item[1], group_id
        )
        return await self._map_image_work(videos, worker, concurrency=2)
    async def _audit_one_video(
        self, event, component, data, group_id: str
    ) -> str:
        """单个视频完整审核：解析源 → 下载/定位 → 抽帧 → 逐帧识别。"""
        if getattr(self, "_video_audit_closing", False):
            return ""
        source = await self._resolve_video_source(event, component, data)
        if not source:
            logger.debug("[GroupMgr] 视频源解析失败，跳过视频审核")
            return ""
        is_remote = source.startswith(("http://", "https://"))
        video_path = source
        temp_path = ""
        try:
            if is_remote:
                max_mb = max(1, min(
                    self._cfg_int("video_max_size_mb", 30, group_id=group_id), 200
                ))
                max_bytes = max_mb * 1024 * 1024
                timeout = float(max(5, min(
                    self._cfg_int("video_download_timeout", 25, group_id=group_id),
                    120,
                )))
                video_path = await self._download_video(source, max_bytes, timeout)
                if not video_path:
                    return ""
                temp_path = video_path
            elif not os.path.exists(video_path):
                logger.debug(
                    f"[GroupMgr] 视频本地路径不存在，跳过视频审核: {video_path}"
                )
                return ""

            # 视频指纹缓存（可选）：广告确认过的整段视频直接命中，跳过检测
            if self._cfg("video_fingerprint_cache", False, group_id=group_id):
                fingerprint = self._video_fingerprint(video_path)
                if fingerprint:
                    self._recent_video_fingerprints[fingerprint] = 1
                    if self._check_video_fp_cache(fingerprint):
                        return "[视频指纹] 已确认的广告视频（命中缓存）"
            max_frames = max(1, min(
                self._cfg_int("video_max_frames", 3, group_id=group_id),
                VIDEO_FRAMES_HARD_CAP,
            ))
            interval = self._cfg_float(
                "video_frame_interval_sec", 5.0, group_id=group_id
            )
            mode = self._cfg_str("video_frame_mode", "interval", group_id=group_id)
            scene_threshold = self._cfg_float(
                "video_scene_threshold", 30.0, group_id=group_id
            )
            frames = await asyncio.wait_for(
                self._run_sync_in_thread(
                    self._extract_video_frames,
                    video_path, max_frames, interval, mode, scene_threshold,                ),
                timeout=VIDEO_FRAME_EXTRACT_TIMEOUT,
            )
            if not frames:
                return ""
            return await self._recognize_video_frames(event, frames, group_id)
        except asyncio.TimeoutError:
            logger.debug("[GroupMgr] 视频抽帧超时，跳过视频审核")
            return ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[GroupMgr] 视频审核异常: {exc}")
            return ""
        finally:
            if temp_path:
                await self._run_sync_in_thread(self._remove_temp_file, temp_path)

    def _dedup_video_frames(self, frames: list, distance: int = 6) -> list:
        """相似帧去重（v2.22.0）：同一画面（结构段 dHash 距离 <= distance）
        只保留首个代表帧。录屏静止视频尤其明显——首/中/尾帧内容几乎相同，
        去重后可避免重复识别，并把检测预算花在唯一画面上。

        依赖感知哈希 mixin 的 ``_phash_from_data``（缺失时跳过，保持原行为）。
        """
        if not frames or len(frames) <= 1:
            return frames
        phash_fn = getattr(self, "_phash_from_data", None)
        if not callable(phash_fn):
            return frames
        hamming = getattr(self, "_hamming_distance", None)
        if not callable(hamming):
            hamming = lambda a, b: sum(
                1 for x, y in zip(str(a or ""), str(b or "")) if x != y
            )
        try:
            selected = []
            for frame in frames:
                ph = str(phash_fn(frame) or "")
                struct = ph[16:] if len(ph) == 64 else ph
                dup = False
                for chosen in selected:
                    ref = str(phash_fn(chosen) or "")
                    rstruct = ref[16:] if len(ref) == 64 else ref
                    if struct and rstruct and hamming(struct, rstruct) <= distance:
                        dup = True
                        break
                if not dup:
                    selected.append(frame)
            return selected or frames
        except Exception:
            return frames

    async def _recognize_video_frames(
        self, event, frames: list, group_id: str
    ) -> str:
        """对每一帧做视觉模型识别 + 本地二维码解码，返回带帧序号的拼接文本。"""
        # v2.22.0：相似帧去重——录屏静止视频（图片广告被录屏）首/中/尾帧
        # 内容几乎相同，去重后只检测每个唯一画面。
        frames = self._dedup_video_frames(frames)
        entries = []
        precheck_enabled = self._cfg("video_quick_precheck", False, group_id=group_id)
        precheck_threshold = self._cfg_float("video_precheck_threshold", 0.5, group_id=group_id)

        async def recognize(item) -> str:
            index, frame_bytes = item
            try:
                # 感知哈希广告黑名单快速命中（可选）：跳过视觉 API，缓存哈希供学习
                if self._cfg("ad_hash_blacklist_enabled", False, group_id=group_id):
                    phash = self._phash_from_data(frame_bytes)
                    if phash:
                        self._recent_media_hashes[f"video_frame:{index}"] = phash
                        distance = self._cfg_int(
                            "ad_hash_distance", 10, group_id=group_id
                        )
                        best = self._check_hash_blacklist(phash, distance)
                        if best <= distance:
                            return (
                                f"[视频第{index}帧] [已知广告帧] "
                                f"命中广告黑名单(相似度{64 - best}/64)"
                            )
                # 快速预检（可选）：低分帧跳过视觉 API，省调用
                if precheck_enabled:
                    pscore = self._quick_precheck_frame(frame_bytes)
                    if pscore < precheck_threshold:
                        return ""
                # 按 ocr_engine 选择识别引擎：本地RapidOCR / Umi-OCR / 云API / 云端视觉
                # 非 LLM 识别引擎（local/umi/cloud），或 auto 的本地优先
                engine = self._ad_engine(group_id)
                if engine in ("local", "umi", "cloud", "auto"):
                    media_text = await self._detect_media_text(frame_bytes, group_id)
                    if media_text:
                        local_lines = [media_text]
                        local_decoder = _probe_qr_decoder()
                        if local_decoder:
                            local_qr = await self._run_qr_decoder(frame_bytes, local_decoder) or []
                            local_clean = [str(v).strip() for v in local_qr if str(v).strip()]
                            if local_clean:
                                local_lines.append("二维码: " + " | ".join(local_clean))
                        tag = "[本地OCR] " if engine in ("local", "auto") else ""
                        if engine == "cloud":
                            tag = "[云API] "
                        # v2.20.0 字幕带放大增强（可选）：小字硬字幕全帧识别易漏
                        if self._cfg("video_subtitle_boost", False, group_id=group_id):
                            boosted = self._subtitle_band_boost(frame_bytes)
                            if boosted:
                                btext = await self._detect_media_text(boosted, group_id) or ""
                                btext = str(btext or "").strip()
                                if btext and btext not in local_lines:
                                    local_lines.append(f"[字幕增强] {btext}")
                        return f"[视频第{index}帧] {tag}" + "\n".join(local_lines)
                    if engine in ("local", "umi", "cloud"):
                        return ""
                # llm 引擎，或 auto 本地无结果时回退 LLM 视觉
                # v2.20.0：video_ad_visual_enabled 时用广告专用视觉判定 prompt（无文字广告也能判）
                video_ad_visual = bool(
                    self._cfg("video_ad_visual_enabled", False, group_id=group_id)
                )
                data_url = self._frame_to_data_url(frame_bytes)
                ocr_text = await self._call_llm_ocr(
                    data_url, group_id=group_id, video_ad_mode=video_ad_visual
                )
                ocr_text = str(ocr_text or "").strip()
                qr_values = []
                decoder = _probe_qr_decoder()
                if decoder:
                    qr_values = await self._run_qr_decoder(frame_bytes, decoder) or []
                lines = []
                if ocr_text:
                    lines.append(ocr_text)
                cleaned_qr = [str(v).strip() for v in qr_values if str(v).strip()]
                if cleaned_qr:
                    lines.append("二维码: " + " | ".join(cleaned_qr))
                # v2.20.0 字幕带放大增强（可选）：对小字硬字幕区裁剪放大后补识别
                if self._cfg("video_subtitle_boost", False, group_id=group_id):
                    boosted = self._subtitle_band_boost(frame_bytes)
                    if boosted:
                        btext = await self._call_llm_ocr(
                            self._frame_to_data_url(boosted),
                            group_id=group_id, video_ad_mode=video_ad_visual,
                        ) or ""
                        btext = str(btext or "").strip()
                        if btext and btext not in lines:
                            lines.append(f"[字幕增强] {btext}")
                text = "\n".join(lines).strip()
                if text:
                    return f"[视频第{index}帧] {text}"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"[GroupMgr] 视频第{index}帧识别失败: {exc}")
            return ""

        results = await self._map_image_work(
            list(enumerate(frames, start=1)),
            recognize,
            IMAGE_WORKER_CONCURRENCY,
        )
        for result in results:
            if result:
                entries.append(result)
        if not entries:
            return ""
        joined = "\n".join(entries)
        return self._bounded_audit_text(joined, VIDEO_EVIDENCE_MAX_CHARS)
