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
    if "astrbot.api" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    core = types.ModuleType("astrbot.core")
    platform = types.ModuleType("astrbot.core.platform")
    sources = types.ModuleType("astrbot.core.platform.sources")
    aiocqhttp = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
    event_module = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    event_module.AiocqhttpMessageEvent = object
    sys.modules.update({
        "astrbot": astrbot,
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

    async def _call_llm_ocr(self, image_url, is_gif=False, is_sticker=False, group_id=""):
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
            for _ in range(30):
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
            self.assertIn("_", fp)
        finally:
            os.remove(path)

if __name__ == "__main__":
    unittest.main()
