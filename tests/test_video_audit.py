"""Focused regression tests for video ad detection (video_audit.py).

The project does not require AstrBot at test collection time, so a tiny import
shim is used when the host package is unavailable.
"""

import asyncio
import base64
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _stub_astrbot():
    if "astrbot.core" in sys.modules:
        return
    if "astrbot" not in sys.modules:
        sys.modules["astrbot"] = types.ModuleType("astrbot")
    api = sys.modules.get("astrbot.api")
    if api is None:
        api = types.ModuleType("astrbot.api")
        sys.modules["astrbot.api"] = api
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = object
    sys.modules["astrbot.api.event"] = api_event
    core = types.ModuleType("astrbot.core")
    platform = types.ModuleType("astrbot.core.platform")
    sources = types.ModuleType("astrbot.core.platform.sources")
    aiocqhttp = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
    event_module = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    event_module.AiocqhttpMessageEvent = object
    sys.modules.update({
        "astrbot": sys.modules["astrbot"],
        "astrbot.api": api,
        "astrbot.core": core,
        "astrbot.core.platform": platform,
        "astrbot.core.platform.sources": sources,
        "astrbot.core.platform.sources.aiocqhttp": aiocqhttp,
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": event_module,
    })


def _load_image_audit():
    path = Path(__file__).resolve().parents[1] / "image_audit.py"
    spec = importlib.util.spec_from_file_location("group_guardian_image_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["image_audit"] = module
    return module


def _load_video_audit():
    _stub_astrbot()
    if "image_audit" not in sys.modules:
        _load_image_audit()
    path = Path(__file__).resolve().parents[1] / "video_audit.py"
    spec = importlib.util.spec_from_file_location("group_guardian_video_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_hash_audit():
    _stub_astrbot()
    path = Path(__file__).resolve().parents[1] / "hash_audit.py"
    spec = importlib.util.spec_from_file_location("group_guardian_hash_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


video_audit = _load_video_audit()


class _Event:
    def __init__(self, chain):
        self._chain = chain

    def get_messages(self):
        return self._chain


class _Video:
    """AstrBot Video 组件的轻量替身。"""

    def __init__(self, url="", file="", path="", convert_result=""):
        self.url = url
        self.file = file
        self.path = path
        self._convert_result = convert_result

    async def convert_to_file_path(self):
        return self._convert_result


class _Reply:
    def __init__(self, chain):
        self.chain = chain

class _Harness(video_audit.VideoAuditMixin):
    _AUDIT_MAX_CHARS = 100_000

    def __init__(self, cfg=None):
        self.config = cfg or {}
        self._config_schema = {}
        self._data_dir = None
        self.cfg_values = {}
        self._init_video_audit_resources(4)

    # 配置读取
    def _cfg(self, key, default=True, group_id=None):
        return self.cfg_values.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg_values.get(key, default))

    def _cfg_float(self, key, default=0.0, group_id=None):
        return float(self.cfg_values.get(key, default))

    def _get_group_override(self, group_id, key):
        return None

    # 组件解析（与 moderation.py 行为对齐的最小实现）
    @staticmethod
    def _component_type_data(component):
        if isinstance(component, dict):
            return component.get("type", ""), component.get("data", {})
        name = type(component).__name__.lower()
        name = name.lstrip("_")  # _Video -> video, _Reply -> reply（对齐 AstrBot 真实组件类名）
        return name, getattr(component, "data", {})

    @staticmethod
    def _component_url(component, data):
        if isinstance(component, dict):
            d = component.get("data", {}) or {}
            return str(d.get("url", "") or d.get("file", "") or "")
        return str(getattr(component, "url", "") or getattr(component, "file", "") or "")

    # 并发与文本截断
    @staticmethod
    async def _map_image_work(items, worker, concurrency=4):
        items = list(items or [])
        if not items:
            return []
        return await asyncio.gather(*(worker(item) for item in items))

    @staticmethod
    def _bounded_audit_text(text, max_chars):
        return str(text or "")[:max_chars]

    async def _call_llm_ocr(self, image_url, is_gif=False, is_sticker=False, group_id="", video_ad_mode=False):
        return ""

    async def _run_qr_decoder(self, data, decoder):
        return []


class VideoComponentCollectionTests(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_collect_dict_and_object(self):
        chain = [
            {"type": "video", "data": {"url": "https://example.com/a.mp4"}},
            _Video(url="https://example.com/b.mp4"),
            {"type": "text", "data": {"text": "hi"}},
        ]
        videos = self.h._collect_video_components(_Event(chain))
        self.assertEqual(len(videos), 2)
        markers = sorted(v[2] for v in videos)
        self.assertEqual(markers, [
            "https://example.com/a.mp4",
            "https://example.com/b.mp4",
        ])

    def test_deduplicate_same_marker(self):
        chain = [
            {"type": "video", "data": {"url": "https://example.com/a.mp4"}},
            {"type": "video", "data": {"url": "https://example.com/a.mp4"}},
        ]
        videos = self.h._collect_video_components(_Event(chain))
        self.assertEqual(len(videos), 1)

    def test_reply_embedded_video(self):
        chain = [_Reply([_Video(url="https://example.com/c.mp4")])]
        videos = self.h._collect_video_components(_Event(chain))
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0][2], "https://example.com/c.mp4")

    def test_empty_chain(self):
        videos = self.h._collect_video_components(_Event([]))
        self.assertEqual(videos, [])


class VideoSourceResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = _Harness()

    async def test_object_url(self):
        source = await self.h._resolve_video_source(
            None, _Video(url="https://example.com/a.mp4")
        )
        self.assertEqual(source, "https://example.com/a.mp4")

    async def test_object_convert_to_file_path(self):
        source = await self.h._resolve_video_source(
            None, _Video(convert_result="/tmp/cached.mp4")
        )
        self.assertEqual(source, "/tmp/cached.mp4")

    async def test_dict_url(self):
        comp = {"type": "video", "data": {"url": "https://example.com/a.mp4"}}
        source = await self.h._resolve_video_source(None, comp, comp.get("data", {}))
        self.assertEqual(source, "https://example.com/a.mp4")

    async def test_no_source_returns_empty(self):
        source = await self.h._resolve_video_source(None, _Video())
        self.assertEqual(source, "")

    async def test_file_prefix_non_existing_still_empty(self):
        # file:// 前缀被剥离，路径不存在且无 client → 返回空串
        source = await self.h._resolve_video_source(
            None, _Video(url="file:///tmp/not_exist_123.mp4")
        )
        self.assertEqual(source, "")

    async def test_get_file_api_fallback(self):
        class _Client:
            async def call_action(self, action, **kwargs):
                self.called = (action, kwargs)
                return {"file": "/tmp/real.mp4"}

        class _WithClient(_Harness):
            def __init__(self):
                super().__init__()
                self._client = _Client()

            async def _get_client(self, event):
                return self._client

        h = _WithClient()
        client = await h._get_client(None)
        source = await h._resolve_video_source(None, _Video(file="fid123"))
        self.assertEqual(source, "/tmp/real.mp4")
        self.assertEqual(client.called[0], "get_file")


class MiscHelperTests(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_strip_file_prefix(self):
        self.assertEqual(self.h._strip_file_prefix("file:///tmp/a.mp4"), "/tmp/a.mp4")
        self.assertEqual(self.h._strip_file_prefix("file://tmp/a.mp4"), "tmp/a.mp4")
        self.assertEqual(self.h._strip_file_prefix("https://x/y"), "https://x/y")

    def test_frame_to_data_url(self):
        raw = b"\xff\xd8fake-jpeg"
        url = self.h._frame_to_data_url(raw)
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), raw)

    def test_temp_file_roundtrip(self):
        data_dir = tempfile.mkdtemp(prefix="gg_video_test_")
        self.h._data_dir = data_dir
        path = self.h._write_video_temp_file(b"video-bytes")
        self.assertTrue(path)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"video-bytes")
        self.h._cleanup_video_temp_dir()
        self.assertFalse(os.path.exists(path))


class FrameExtractionTests(unittest.TestCase):
    def test_extract_frames_from_synthetic_video(self):
        cv2 = video_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np

        path = os.path.join(tempfile.gettempdir(), "gg_synth_video.mp4")
        writer = cv2.VideoWriter(
            path, cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64)
        )
        try:
            for _ in range(5):  # 开头黑屏（录屏过渡帧）
                writer.write(np.zeros((64, 64, 3), np.uint8))
            content = np.full((64, 64, 3), 128, np.uint8)
            content[20:44, 20:44] = (0, 0, 255)
            for _ in range(20):  # 中间内容帧
                writer.write(content)
            for _ in range(5):  # 结尾黑屏
                writer.write(np.zeros((64, 64, 3), np.uint8))
        finally:
            writer.release()
        try:
            h = _Harness()
            frames = h._extract_video_frames(path, max_frames=3, interval_sec=0.5)
            self.assertTrue(frames)
            self.assertLessEqual(len(frames), 3)
            # JPEG magic
            self.assertTrue(all(f.startswith(b"\xff\xd8") for f in frames))
            # v2.22.0：黑屏过渡帧已被过滤，保留的都是有效内容帧
            self.assertTrue(all(
                video_audit.VideoAuditMixin._is_meaningful_frame(f)
                for f in frames
            ))
        finally:
            os.remove(path)

    def test_extract_frames_bad_path(self):
        h = _Harness()
        frames = h._extract_video_frames(
            os.path.join(tempfile.gettempdir(), "gg_no_such.mp4"),
            max_frames=3,
            interval_sec=1.0,
        )
        self.assertEqual(frames, [])


class ApplyVideoAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = _Harness()

    async def test_disabled_returns_original(self):
        self.h.cfg_values["video_audit_enabled"] = False
        result = await self.h._apply_video_audit(
            "hello", [("v", {}, "u")], _Event([]), "100"
        )
        self.assertEqual(result, "hello")

    async def test_no_videos_returns_original(self):
        self.h.cfg_values["video_audit_enabled"] = True
        result = await self.h._apply_video_audit("hello", [], _Event([]), "100")
        self.assertEqual(result, "hello")

    async def test_no_recognizer_returns_original(self):
        self.h.cfg_values.update({
            "video_audit_enabled": True,
            "llm_moderation_enabled": False,
            "qrcode_decode_enabled": False,
        })
        result = await self.h._apply_video_audit(
            "hello", [("v", {}, "u")], _Event([]), "100"
        )
        self.assertEqual(result, "hello")

    async def test_enabled_merges_recognition_text(self):
        self.h.cfg_values.update({
            "video_audit_enabled": True,
            "llm_moderation_enabled": True,
        })

        async def fake_audit(event, component, data, group_id):
            return "[视频第1帧] 加群微信 xxxxx"

        self.h._audit_one_video = fake_audit
        result = await self.h._apply_video_audit(
            "", [("v", {}, "u")], _Event([]), "100"
        )
        self.assertIn("[视频审核]", result)
        self.assertIn("加群微信", result)

    async def test_all_videos_fail_returns_original(self):
        self.h.cfg_values.update({
            "video_audit_enabled": True,
            "llm_moderation_enabled": True,
        })

        async def fake_audit(event, component, data, group_id):
            return ""

        self.h._audit_one_video = fake_audit
        result = await self.h._apply_video_audit(
            "body", [("v", {}, "u")], _Event([]), "100"
        )
        self.assertEqual(result, "body")




class QuickPrecheckAndFingerprintTests(unittest.TestCase):
    def test_precheck_scores(self):
        cv2 = video_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np
        plain = np.full((64, 64, 3), 128, np.uint8)
        ok, buf = cv2.imencode(".jpg", plain)
        self.assertTrue(ok)
        low = video_audit.VideoAuditMixin._quick_precheck_frame(buf.tobytes())
        self.assertLess(low, 0.5)
        colorful = np.zeros((64, 64, 3), np.uint8)
        colorful[:, :, 0] = 255
        ok2, buf2 = cv2.imencode(".jpg", colorful)
        self.assertTrue(ok2)
        high = video_audit.VideoAuditMixin._quick_precheck_frame(buf2.tobytes())
        self.assertGreaterEqual(high, 0.3)

    def test_video_fingerprint(self):
        cv2 = video_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np
        path = os.path.join(tempfile.gettempdir(), "gg_fp_video.mp4")
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
        try:
            for _ in range(15):
                writer.write(np.zeros((64, 64, 3), np.uint8))
        finally:
            writer.release()
        try:
            h = _Harness()
            h._phash_from_gray = lambda gray, hash_size=8: "1" * 64
            fp = h._video_fingerprint(path)
            self.assertTrue(fp)
            # v2.20.0 多帧鲁棒指纹：h1_h2_h3_bucket 四段
            parts = fp.split("_")
            self.assertEqual(4, len(parts))
            self.assertEqual("1" * 64, parts[0])
            self.assertEqual("1" * 64, parts[1])
            self.assertEqual("1" * 64, parts[2])
            self.assertTrue(parts[3].isdigit())
        finally:
            os.remove(path)

    def test_extract_frames_spans_mode(self):
        cv2 = video_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np
        path = os.path.join(tempfile.gettempdir(), "gg_spans_video.mp4")
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
        try:
            for i in range(90):
                writer.write(np.full((64, 64, 3), i % 255, np.uint8))
        finally:
            writer.release()
        try:
            h = _Harness()
            frames = h._extract_video_frames(path, max_frames=3, interval_sec=5.0, mode="spans")
            self.assertTrue(frames)
            self.assertLessEqual(len(frames), 3)
            self.assertTrue(all(f.startswith(b"\xff\xd8") for f in frames))
        finally:
            os.remove(path)

    def test_subtitle_band_boost_returns_jpeg(self):
        cv2 = video_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np
        frame = np.zeros((120, 200, 3), np.uint8)
        ok, buf = cv2.imencode(".jpg", frame)
        self.assertTrue(ok)
        boosted = video_audit.VideoAuditMixin._subtitle_band_boost(buf.tobytes())
        self.assertIsNotNone(boosted)
        self.assertTrue(boosted.startswith(b"\xff\xd8"))

    def test_subtitle_band_boost_bad_bytes(self):
        boosted = video_audit.VideoAuditMixin._subtitle_band_boost(b"not-a-jpeg")
        self.assertIsNone(boosted)


class VideoFingerprintCacheTests(unittest.TestCase):
    """v2.20.0 多帧鲁棒指纹缓存匹配（含旧格式兼容）。"""

    def setUp(self):
        self.hash_audit = _load_hash_audit()

        class _H(self.hash_audit.HashAuditMixin):
            def __init__(self):
                self._video_fp_cache = {}

        self.h = _H()

    def test_exact_match(self):
        self.h._video_fp_cache = {f"{'1'*64}_{'0'*64}_{'1'*64}_3": 1}
        self.assertTrue(self.h._check_video_fp_cache(f"{'1'*64}_{'0'*64}_{'1'*64}_3"))

    def test_any_frame_match_after_crop(self):
        # 被裁剪：首帧哈希不同，但中间/尾帧哈希相同 → 应命中
        self.h._video_fp_cache = {f"{'1'*64}_{'0'*64}_{'1'*64}_3": 1}
        self.assertTrue(self.h._check_video_fp_cache(f"{'0'*64}_{'0'*64}_{'1'*64}_4"))

    def test_legacy_format_compat(self):
        # 旧缓存 phash_total（2 段），新指纹首段相同 → 命中
        self.h._video_fp_cache = {f"{'1'*64}_300": 1}
        self.assertTrue(self.h._check_video_fp_cache(f"{'1'*64}_{'0'*64}_{'1'*64}_3"))

    def test_no_match(self):
        a, b, c = "0" * 64, "1" * 64, "01" * 32
        d, e, f = "10" * 32, "0" * 32 + "1" * 32, "1" * 32 + "0" * 32
        self.h._video_fp_cache = {f"{a}_{b}_{c}_3": 1}
        # 三个帧哈希全部不同 → 不命中
        self.assertFalse(self.h._check_video_fp_cache(f"{d}_{e}_{f}_3"))

    def test_short_fingerprint_returns_false(self):
        self.h._video_fp_cache = {f"{'0'*64}_500": 1}
        # 旧版 2 段格式指纹（无多帧信息）不在缓存时不做部分匹配
        self.assertFalse(self.h._check_video_fp_cache(f"{'1'*64}_300"))


class V222StillFrameAndDedupTests(unittest.TestCase):
    """v2.22.0：图片广告被录屏成视频的检测增强——
    无效帧过滤 + 相似帧去重 + 录屏帧命中原图黑名单。"""

    def test_is_meaningful_frame_filters_black_and_white(self):
        cv2 = video_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np

        black = np.zeros((64, 64, 3), np.uint8)
        ok, bb = cv2.imencode(".jpg", black)
        self.assertTrue(ok)
        self.assertFalse(
            video_audit.VideoAuditMixin._is_meaningful_frame(bb.tobytes())
        )

        white = np.full((64, 64, 3), 255, np.uint8)
        ok2, wb = cv2.imencode(".jpg", white)
        self.assertTrue(ok2)
        self.assertFalse(
            video_audit.VideoAuditMixin._is_meaningful_frame(wb.tobytes())
        )

        content = np.full((64, 64, 3), 128, np.uint8)
        content[10:50, 10:50] = (0, 0, 255)
        ok3, cb = cv2.imencode(".jpg", content)
        self.assertTrue(ok3)
        self.assertTrue(
            video_audit.VideoAuditMixin._is_meaningful_frame(cb.tobytes())
        )

    def test_filter_meaningful_frames(self):
        cv2 = video_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np

        black = np.zeros((64, 64, 3), np.uint8)
        ok, bb = cv2.imencode(".jpg", black)
        self.assertTrue(ok)
        content = np.full((64, 64, 3), 128, np.uint8)
        content[10:50, 10:50] = (0, 0, 255)
        ok2, cb = cv2.imencode(".jpg", content)
        self.assertTrue(ok2)
        h = _Harness()
        kept = h._filter_meaningful_frames(
            [bb.tobytes(), cb.tobytes(), bb.tobytes()], 5
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0], cb.tobytes())

    def test_dedup_video_frames_collapses_identical_frames(self):
        import hashlib

        def fake_phash(data):
            bits = bin(int(hashlib.md5(data).hexdigest(), 16))[2:].zfill(64)
            return bits

        h = _Harness()
        h._phash_from_data = fake_phash
        f1 = b"frame-one-bytes"
        f2 = b"frame-two-bytes"
        dedup = h._dedup_video_frames([f1, f1, f2])
        self.assertEqual(len(dedup), 2)
        self.assertEqual(dedup[0], f1)
        self.assertEqual(dedup[1], f2)

    def test_dedup_no_phash_fallback_keeps_all(self):
        h = _Harness()  # 无 _phash_from_data
class VideoAuditCpuProtectionTests(unittest.TestCase):
    """v2.36.6：视频审核并发配置化 + 帧识别串行短路（CPU 防护）。"""

    def _harness(self, cfg_values=None):
        h = _Harness()
        h.cfg_values.update(cfg_values or {})
        return h

    def test_audit_all_videos_defaults_to_one(self):
        h = self._harness({})
        captured = {}

        async def _map_image_work(items, worker, concurrency=4):
            captured["concurrency"] = concurrency
            return [await worker(it) for it in items]

        h._map_image_work = _map_image_work

        async def _audit_one_video(event, component, data, group_id):
            return "v"

        h._audit_one_video = _audit_one_video
        asyncio.run(h._audit_all_videos(None, [(_Video(), {}, "u")], "100"))
        self.assertEqual(1, captured["concurrency"])

    def test_audit_all_videos_uses_configured_concurrency(self):
        h = self._harness({"video_audit_concurrency": 3})
        captured = {}

        async def _map_image_work(items, worker, concurrency=4):
            captured["concurrency"] = concurrency
            return [await worker(it) for it in items]

        h._map_image_work = _map_image_work

        async def _audit_one_video(event, component, data, group_id):
            return "v"

        h._audit_one_video = _audit_one_video
        asyncio.run(h._audit_all_videos(None, [(_Video(), {}, "u")], "100"))
        self.assertEqual(3, captured["concurrency"])

    def test_recognize_frames_serial_and_short_circuit_on_ad(self):
        h = self._harness({
            "video_quick_precheck": False,
            "ad_hash_blacklist_enabled": False,
            "video_subtitle_boost": False,
        })
        h._dedup_video_frames = lambda frames: frames
        h._ad_engine = lambda group_id=None: "llm"
        h._frame_to_data_url = lambda data: "data:img"
        h._bounded_audit_text = lambda text, limit: str(text or "")[:limit]
        calls = []

        async def _call_llm_ocr(data_url, group_id=None, video_ad_mode=False):
            calls.append(1)
            return "广告：联系微信加群"

        h._call_llm_ocr = _call_llm_ocr
        video_audit._probe_qr_decoder = lambda: None
        # 帧1 判定「广告：」→ 短路，帧2 不再识别
        result = asyncio.run(h._recognize_video_frames(None, [b"f1", b"f2"], "100"))
        self.assertIn("广告：", result)
        self.assertEqual(1, len(calls))

    def test_recognize_frames_continues_when_not_ad(self):
        h = self._harness({
            "video_quick_precheck": False,
            "ad_hash_blacklist_enabled": False,
            "video_subtitle_boost": False,
        })
        h._dedup_video_frames = lambda frames: frames
        h._ad_engine = lambda group_id=None: "llm"
        h._frame_to_data_url = lambda data: "data:img"
        h._bounded_audit_text = lambda text, limit: str(text or "")[:limit]
        calls = []

        async def _call_llm_ocr(data_url, group_id=None, video_ad_mode=False):
            calls.append(1)
            return "正常画面"

        h._call_llm_ocr = _call_llm_ocr
        video_audit._probe_qr_decoder = lambda: None
        result = asyncio.run(h._recognize_video_frames(None, [b"f1", b"f2"], "100"))
        self.assertIn("正常画面", result)
        self.assertEqual(2, len(calls))  # 非广告帧全部识别

        frames = [b"a", b"b", b"c"]
        self.assertEqual(h._dedup_video_frames(frames), frames)

    def test_extract_frames_interval_skips_black_frames(self):
        cv2 = video_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np

        path = os.path.join(tempfile.gettempdir(), "gg_blacklead_video.mp4")
        writer = cv2.VideoWriter(
            path, cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64)
        )
        try:
            for _ in range(5):  # 前 0.5s 黑屏（录屏过渡帧）
                writer.write(np.zeros((64, 64, 3), np.uint8))
            content = np.full((64, 64, 3), 128, np.uint8)
            content[20:44, 20:44] = (0, 0, 255)
            for _ in range(10):  # 中间内容帧（图片广告画面）
                writer.write(content)
            for _ in range(5):  # 尾部黑屏
                writer.write(np.zeros((64, 64, 3), np.uint8))
        finally:
            writer.release()
        try:
            h = _Harness()
            frames = h._extract_video_frames(
                path, max_frames=3, interval_sec=0.5, mode="interval"
            )
            self.assertTrue(frames)
            self.assertLessEqual(len(frames), 3)
            for f in frames:
                self.assertTrue(
                    video_audit.VideoAuditMixin._is_meaningful_frame(f)
                )
        finally:
            os.remove(path)



class V225ShortQrSignalTests(unittest.TestCase):
    """v2.25.0：短视频+引流二维码快速强信号。"""

    def test_is_drain_qr_value(self):
        self.assertTrue(
            video_audit.VideoAuditMixin._is_drain_qr_value("https://t.cn/x")
        )
        self.assertTrue(
            video_audit.VideoAuditMixin._is_drain_qr_value("vx: abc123")
        )
        self.assertTrue(
            video_audit.VideoAuditMixin._is_drain_qr_value("加群 123456")
        )
        self.assertFalse(
            video_audit.VideoAuditMixin._is_drain_qr_value("hello world")
        )

    def test_short_qr_signal_set(self):
        h = _Harness()
        h.cfg_values["video_short_qr_fast_hit"] = True
        h.cfg_values["video_short_qr_max_sec"] = 10
        h._video_audit_seconds = 5.0
        h._maybe_set_short_qr_signal(["https://t.cn/x"], "100")
        self.assertTrue(h._video_short_qr_hit)

    def test_short_qr_signal_long_video(self):
        h = _Harness()
        h.cfg_values["video_short_qr_fast_hit"] = True
        h._video_audit_seconds = 60.0
        h._maybe_set_short_qr_signal(["https://t.cn/x"], "100")
        self.assertFalse(h._video_short_qr_hit)

    def test_short_qr_signal_non_drain(self):
        h = _Harness()
        h.cfg_values["video_short_qr_fast_hit"] = True
        h._video_audit_seconds = 5.0
        h._maybe_set_short_qr_signal(["hello"], "100")
        self.assertFalse(h._video_short_qr_hit)

    def test_short_qr_signal_disabled(self):
        h = _Harness()
        h._video_audit_seconds = 5.0
        h._maybe_set_short_qr_signal(["https://t.cn/x"], "100")
        self.assertFalse(h._video_short_qr_hit)


class V220StaticChecks(unittest.TestCase):
    """v2.20.0 新能力与配置项的静态结构检查。"""

    def test_video_audit_has_new_capabilities(self):
        src = (Path(__file__).resolve().parents[1] / "video_audit.py").read_text(encoding="utf-8")
        self.assertIn("_spans_extract_frames", src)
        self.assertIn("_subtitle_band_boost", src)
        self.assertIn("video_ad_visual_enabled", src)
        self.assertIn("video_subtitle_boost", src)

    def test_image_audit_has_ad_prompt(self):
        src = (Path(__file__).resolve().parents[1] / "image_audit.py").read_text(encoding="utf-8")
        self.assertIn("_VIDEO_AD_SYSTEM_PROMPT", src)
        self.assertIn("video_ad_mode", src)

    def test_hash_audit_supports_multi_frame(self):
        src = (Path(__file__).resolve().parents[1] / "hash_audit.py").read_text(encoding="utf-8")
        self.assertIn("_check_video_fp_cache", src)
        self.assertIn("len(b) >= 32", src)

    def test_schema_has_new_configs(self):
        import json as _json
        schema = _json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertFalse(schema["video_ad_visual_enabled"]["default"])
        self.assertFalse(schema["video_subtitle_boost"]["default"])
        self.assertIn("spans", schema["video_frame_mode"]["hint"])


if __name__ == "__main__":
    unittest.main()
