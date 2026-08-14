"""Focused regression tests for hash blacklist + ad escalation (hash_audit.py).

The project does not require AstrBot at test collection time, so a tiny import
shim is used when the host package is unavailable.
"""

import importlib.util
import sys
import tempfile
import time
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
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api})


def _load_hash_audit():
    _stub_astrbot()
    path = Path(__file__).resolve().parents[1] / "hash_audit.py"
    spec = importlib.util.spec_from_file_location("group_guardian_hash_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hash_audit = _load_hash_audit()


class _Harness(hash_audit.HashAuditMixin):
    def __init__(self, data_dir=None):
        self.config = {}
        self._config_schema = {}
        self._data_dir = data_dir or tempfile.mkdtemp(prefix="gg_hash_test_")
        self.cfg_values = {}
        self._init_hash_audit_resources()

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg_values.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg_values.get(key, default))


class PhashTests(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_hamming_distance(self):
        self.assertEqual(self.h._hamming_distance("0" * 64, "0" * 64), 0)
        self.assertEqual(self.h._hamming_distance("0" * 64, "1" * 64), 64)
        self.assertEqual(self.h._hamming_distance("", "0" * 64), 64)

    def test_phash_from_data_length(self):
        cv2 = hash_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np

        img = np.full((64, 64, 3), 128, np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        self.assertTrue(ok)
        phash = self.h._phash_from_data(buf.tobytes())
        self.assertEqual(len(phash), 64)
        self.assertTrue(set(phash) <= {"0", "1"})

    def test_similar_images_close_hash(self):
        cv2 = hash_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np

        base = np.full((64, 64, 3), 128, np.uint8)
        ok1, b1 = cv2.imencode(".jpg", base)
        base[5, 5] = (255, 255, 255)
        ok2, b2 = cv2.imencode(".jpg", base)
        h1 = self.h._phash_from_data(b1.tobytes())
        h2 = self.h._phash_from_data(b2.tobytes())
        self.assertLessEqual(self.h._hamming_distance(h1, h2), 3)

    def test_different_images_far_hash(self):
        cv2 = hash_audit.cv2
        if cv2 is None:
            self.skipTest("opencv-python-headless not installed")
        import numpy as np

        img_a = np.full((64, 64, 3), 30, np.uint8)
        img_b = np.full((64, 64, 3), 220, np.uint8)
        ok1, b1 = cv2.imencode(".jpg", img_a)
        ok2, b2 = cv2.imencode(".jpg", img_b)
        h1 = self.h._phash_from_data(b1.tobytes())
        h2 = self.h._phash_from_data(b2.tobytes())
        self.assertGreater(self.h._hamming_distance(h1, h2), 10)


class BlacklistTests(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_learn_and_check(self):
        self.h._learn_hash("0" * 64, 10)
        self.assertLessEqual(self.h._check_hash_blacklist("0" * 64, 10), 10)
        self.assertEqual(self.h._check_hash_blacklist("1" * 64, 10), 64)

    def test_learn_dedup_increments_count(self):
        self.h._learn_hash("0" * 64, 10)
        self.h._learn_hash("0" * 64, 10)
        hashes = self.h._hash_blacklist["hashes"]
        self.assertEqual(len(hashes), 1)
        self.assertEqual(hashes[0]["count"], 2)

    def test_learn_recent_skips_empty(self):
        self.h._recent_media_hashes = {"url1": "0" * 64, "url2": ""}
        self.h.cfg_values["ad_hash_distance"] = 10
        self.h._learn_recent_ad_hashes("100")
        hashes = self.h._hash_blacklist["hashes"]
        self.assertEqual(len(hashes), 1)

    def test_persistence_across_instances(self):
        data_dir = tempfile.mkdtemp(prefix="gg_hash_persist_")
        h1 = _Harness(data_dir)
        h1._learn_hash("0" * 64, 10)
        h2 = _Harness(data_dir)
        self.assertLessEqual(h2._check_hash_blacklist("0" * 64, 10), 10)


class AdEscalationTests(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_record_counts(self):
        self.assertEqual(self.h._record_ad_escalation("100", "200", 3600), 1)
        self.assertEqual(self.h._record_ad_escalation("100", "200", 3600), 2)
        self.assertEqual(self.h._record_ad_escalation("100", "201", 3600), 1)

    def test_window_resets(self):
        self.h._ad_escalation.setdefault("100", {})["200"] = {
            "count": 5, "first_ts": int(time.time()) - 99999
        }
        self.assertEqual(self.h._record_ad_escalation("100", "200", 60), 1)

    def test_is_ad_detection(self):
        self.assertTrue(self.h._ad_escalation_is_ad(hit_types={"ad": True}))
        self.assertTrue(self.h._ad_escalation_is_ad(hit_types={"image_scan": True}))
        self.assertTrue(self.h._ad_escalation_is_ad(hit_summary="广告, full_scan"))
        self.assertTrue(self.h._ad_escalation_is_ad(hit_summary="learned_ad"))
        self.assertFalse(self.h._ad_escalation_is_ad(hit_types={"swear": True}))
        self.assertFalse(self.h._ad_escalation_is_ad(hit_summary="swear"))

    def test_persistence_of_counts(self):
        data_dir = tempfile.mkdtemp(prefix="gg_hash_esc_")
        h1 = _Harness(data_dir)
        h1._record_ad_escalation("100", "200", 3600)
        h1._record_ad_escalation("100", "200", 3600)
        h2 = _Harness(data_dir)
        self.assertEqual(h2._record_ad_escalation("100", "200", 3600), 3)




class VideoFingerprintCacheTests(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_learn_and_check(self):
        self.h._learn_video_fingerprint("fp123_30")
        self.assertTrue(self.h._check_video_fp_cache("fp123_30"))
        self.assertFalse(self.h._check_video_fp_cache("fp999_10"))
        self.assertFalse(self.h._check_video_fp_cache(""))

    def test_learn_recent_skips_empty(self):
        self.h._recent_video_fingerprints = {"fp1_10": 1, "fp2_20": 1, "": 1}
        self.h._learn_recent_video_fingerprints()
        self.assertTrue(self.h._check_video_fp_cache("fp1_10"))
        self.assertTrue(self.h._check_video_fp_cache("fp2_20"))

    def test_persistence(self):
        data_dir = tempfile.mkdtemp(prefix="gg_vfp_")
        h1 = _Harness(data_dir)
        h1._learn_video_fingerprint("abc_50")
        h2 = _Harness(data_dir)
        self.assertTrue(h2._check_video_fp_cache("abc_50"))

    def test_lru_cap(self):
        self.h._video_fp_cache = {}
        for i in range(hash_audit.VIDEO_FP_CACHE_MAX + 50):
            self.h._learn_video_fingerprint(f"fp{i}_{i}")
        self.assertLessEqual(len(self.h._video_fp_cache), hash_audit.VIDEO_FP_CACHE_MAX)

if __name__ == "__main__":
    unittest.main()
