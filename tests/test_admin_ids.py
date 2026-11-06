import unittest

from admin_ids import read_astrbot_admin_ids


class AdminIdReaderTests(unittest.TestCase):
    def test_official_no_argument_config_wins_and_is_normalized(self):
        class Context:
            astrbot_config = {"admin_id": ["stale"]}
            def __init__(self): self.calls = []
            def get_config(self, *args):
                self.calls.append(args)
                return {"admin_id": [" 12 ", 12, "platform:abc"]}
        ctx = Context()
        self.assertEqual(read_astrbot_admin_ids(ctx), ["12", "platform:abc"])
        self.assertEqual(ctx.calls, [()])
        self.assertEqual(ctx.astrbot_config, {"admin_id": ["stale"]})

    def test_empty_or_missing_official_mapping_is_authoritative(self):
        for config in ({}, {"admin_id": []}):
            ctx = type("Context", (), {"astrbot_config": {"admin_id": ["stale"]},
                                        "get_config": lambda self: config})()
            self.assertEqual(read_astrbot_admin_ids(ctx), [])

    def test_official_errors_and_malformed_values_fail_closed(self):
        cases = [RuntimeError("boom"), object(), {"admin_id": "single"},
                 {"admin_id": [True]}, {"admin_id": [None]},
                 {"admin_id": [1.25]}, {"admin_id": ["NaN"]}]
        for result in cases:
            def get_config(self, value=result):
                if isinstance(value, Exception): raise value
                return value
            ctx = type("Context", (), {"astrbot_config": {"admin_id": ["stale"]},
                                        "get_config": get_config})()
            self.assertEqual(read_astrbot_admin_ids(ctx), [])

    def test_legacy_fallback_only_when_accessor_is_unavailable(self):
        for accessor in (None, 42):
            ctx = type("Context", (), {"astrbot_config": {"admin_id": [" old ", 7, "old"]},
                                        "get_config": accessor})()
            self.assertEqual(read_astrbot_admin_ids(ctx), ["old", "7"])
        self.assertEqual(read_astrbot_admin_ids(type("Context", (), {
            "astrbot_config": {"admin_id": [False]}, "get_config": None})()), [])


if __name__ == "__main__":
    unittest.main()
