"""Regression coverage for the generated swear expansion and matcher fallback."""

import ast
import importlib.util
import re
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


automaton = _load("group_guardian_automaton", "automaton.py")
migration = _load("group_guardian_lexicon_migration", "lexicon_migration.py")


class SwearExpansionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = migration.generate_swear_rules(migration.TARGET_NEW_RULES)

    def test_generated_rules_are_unique_safe_literals(self):
        patterns = [pattern for pattern, _ in self.rules]

        self.assertGreaterEqual(len(patterns), 20_000)
        self.assertEqual(len(patterns), len(set(patterns)))
        self.assertFalse(any(re.search(r"[.^$*+?{}\[\]|\\]", pattern) for pattern in patterns))
        self.assertFalse(any(len(pattern) < 2 for pattern in patterns))
        self.assertFalse(set(patterns).intersection(migration.SHORT_ASCII_TOKENS))
        for neutral in ("白吃", "四妈", "麻的", "若智", "治章", "拉鸡", "机巴", "希八"):
            self.assertNotIn(neutral, patterns)

    def test_capped_separator_sampling_reaches_every_gap(self):
        base = "abcdef"
        variants = migration._separator_variants(base)

        self.assertEqual(len(variants), migration.MAX_SEPARATOR_VARIANTS_PER_CORE)
        for index in range(len(base) - 1):
            self.assertTrue(
                any(base[index:index + 2] not in variant for variant in variants),
                f"gap {index} was never varied",
            )

    def test_runtime_migration_is_versioned_and_idempotent(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE moderation_rules ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, "
            "pattern TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
            "description TEXT, UNIQUE(category, pattern))"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO moderation_rules(category, pattern) VALUES('swear', ?)",
            ("\u50bb\u903c",),
        )
        conn.commit()

        first = migration.ensure_swear_expansion(conn)
        second = migration.ensure_swear_expansion(conn)

        self.assertGreaterEqual(first["inserted"], 20_000)
        self.assertEqual(second["inserted"], 0)
        self.assertTrue(second["already_applied"])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) - COUNT(DISTINCT pattern) FROM moderation_rules WHERE category='swear'").fetchone()[0],
            0,
        )
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()

    def test_v1_unbounded_short_tokens_are_replaced_without_overwriting_custom_rules(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE moderation_rules ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, "
            "pattern TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
            "description TEXT, UNIQUE(category, pattern))"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('swear_expansion_version', ?)",
            (migration.LEGACY_DESCRIPTION_PREFIXES[0][len("auto:"):],),
        )
        conn.executemany(
            "INSERT INTO moderation_rules(category, pattern, description) VALUES('swear', ?, ?)",
            (
                ("白吃", f"{migration.LEGACY_DESCRIPTION_PREFIXES[0]}|core=白痴"),
                ("c_n_m", f"{migration.LEGACY_DESCRIPTION_PREFIXES[0]}|core=cnm"),
                ("cao", f"{migration.LEGACY_DESCRIPTION_PREFIXES[0]}|short-token=cao"),
                ("shabi", "管理员自定义"),
                (migration.LEGACY_SEED_SHORT_TOKEN_PATTERN, ""),
            ),
        )
        conn.commit()

        stats = migration.ensure_swear_expansion(conn)
        rows = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT pattern, description FROM moderation_rules WHERE category='swear'"
            ).fetchall()
        }

        self.assertEqual(stats["removed_legacy_rules"], 4)
        self.assertNotIn("白吃", rows)
        self.assertNotIn("c_n_m", rows)
        self.assertNotIn("cao", rows)
        self.assertNotIn(migration.LEGACY_SEED_SHORT_TOKEN_PATTERN, rows)
        self.assertEqual(rows["shabi"], "管理员自定义")
        self.assertTrue(set(migration.SHORT_TOKEN_REGEXES).issubset(rows))
        conn.close()

    def test_migration_does_not_commit_an_existing_outer_transaction(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE moderation_rules ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, "
            "pattern TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
            "description TEXT, UNIQUE(category, pattern))"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        conn.execute("INSERT INTO meta(key, value) VALUES('caller_pending', '1')")

        migration.ensure_swear_expansion(conn)

        self.assertTrue(conn.in_transaction)
        conn.rollback()
        self.assertIsNone(conn.execute("SELECT value FROM meta WHERE key='caller_pending'").fetchone())
        self.assertIsNone(
            conn.execute("SELECT value FROM meta WHERE key='swear_expansion_version'").fetchone()
        )
        conn.close()

    def test_packaged_database_contains_required_expansion(self):
        conn = sqlite3.connect(str(ROOT / "lexicon.db"))
        try:
            generated = conn.execute(
                "SELECT COUNT(*) FROM moderation_rules "
                "WHERE category='swear' AND description LIKE 'auto:swear-expansion-%'",
            ).fetchone()[0]
            version = conn.execute(
                "SELECT value FROM meta WHERE key='swear_expansion_version'"
            ).fetchone()
            required_patterns = (
                migration.SHORT_TOKEN_REGEXES[0],
                "\u5565\u5b50",
                "\u50bb\u5b50",
            )
            present = {
                row[0]
                for row in conn.execute(
                    "SELECT pattern FROM moderation_rules "
                    "WHERE category='swear' AND pattern IN (?, ?, ?)",
                    required_patterns,
                ).fetchall()
            }
            packaged_patterns = [
                row[0]
                for row in conn.execute(
                    "SELECT pattern FROM moderation_rules "
                    "WHERE category='swear' AND enabled=1 ORDER BY id"
                ).fetchall()
            ]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()

        self.assertGreaterEqual(generated, migration.MIN_NEW_RULES)
        self.assertEqual(version[0] if version else "", migration.EXPANSION_VERSION)
        self.assertEqual(present, set(required_patterns))
        self.assertEqual(integrity, "ok")

        packaged_matcher = automaton.HybridMatcher()
        packaged_matcher.add_regex_patterns(packaged_patterns)
        packaged_matcher.build()
        for neutral in (
            "白吃白喝", "我四妈来了", "这是麻的面料", "若智", "治章",
            "拉鸡", "机巴车站", "希八先生", "Macao", "cacao",
            "USB设备", "usb cable", "five people", "Gundam", "gunpla",
            "shitake mushroom",
        ):
            self.assertFalse(packaged_matcher.is_match(neutral), neutral)

    def test_short_and_obfuscated_forms_hit_without_substring_false_positive(self):
        patterns = [pattern for pattern, _ in self.rules]
        patterns.extend(migration.SHORT_TOKEN_REGEXES)
        patterns.extend(migration.LOW_CONFIDENCE_LITERALS)
        matcher = automaton.HybridMatcher()
        matcher.add_regex_patterns(patterns)
        matcher.build()

        positives = (
            "cs",
            "c-s",
            "c s",
            "c，s",
            "c/n/m",
            "n.m.s.l",
            "s h a b i",
            "\u5565\u5b50",
            "\u50bb\u5b50",
            "\u50bb\u903c",
            "\u50bb \u903c",
            "\u50bb_\u903c",
            "nmsl",
        )
        negatives = (
            "computer science",
            "class",
            "case",
            "cacao",
            "Macao",
            "nmslides",
            "shabird",
            "USB设备",
            "five people",
            "Gundam",
            "shitake mushroom",
            "\u767d\u5403\u767d\u559d",
            "\u6211\u56db\u5988\u6765\u4e86",
            "\u8fd9\u662f\u9ebb\u7684\u9762\u6599",
            "\u5f31\u8005\u667a\u6cbb",
            "\u673a\u5df4\u8f66\u7ad9",
            "\u5e0c\u516b\u5148\u751f",
            "\u5988\u5988\u4eca\u5929\u4e70\u9e21\u86cb",
            "\u8001\u5e08\u5728\u4e0a\u8bfe",
            "\u8349\u5730\u4e0a\u7684\u9a6c",
        )

        for text in positives:
            self.assertTrue(matcher.is_match(text), text)
        for text in negatives:
            self.assertFalse(matcher.is_match(text), text)


class TrieFallbackTests(unittest.TestCase):
    def test_overlapping_literals_preserve_end_positions(self):
        matcher = automaton.KeywordAutomaton()
        matcher.add_keywords(["he", "her", "hers", "she", "he"])
        self.assertFalse(matcher.exists("ushers"))
        matcher.build()

        self.assertTrue(matcher.exists("ushers"))
        self.assertEqual(matcher.count, 4)
        self.assertEqual(
            matcher.iter_matches("ushers"),
            [(3, "he"), (3, "she"), (4, "her"), (5, "hers")],
        )
        first_end, first_keyword = matcher.first_match("ushers")
        self.assertEqual(first_end, 3)
        self.assertIn(first_keyword, {"he", "she"})

    def test_regex_expansion_falls_back_when_semantics_are_not_literal(self):
        self.assertEqual(automaton.regex_to_literals(r"[^a]"), [])
        self.assertEqual(automaton.regex_to_literals(r"[\s_]"), [])
        self.assertEqual(automaton.regex_to_literals(r"(foo|bar.*)"), [])
        self.assertEqual(
            sorted(automaton.regex_to_literals(r"a(?:b|c)d")),
            ["abd", "acd"],
        )

        matcher = automaton.HybridMatcher()
        matcher.add_regex_patterns([r"(foo|bar.*)"])
        matcher.build()
        self.assertTrue(matcher.is_match("bar-with-suffix"))

    def test_plain_capturing_group_is_not_treated_as_literal_parentheses(self):
        self.assertEqual(automaton.regex_to_literals(r"(abc)"), ["abc"])
        self.assertEqual(automaton.regex_to_literals(r"("), [])

        matcher = automaton.HybridMatcher()
        matcher.add_regex_patterns([r"(abc)"])
        matcher.build()
        self.assertTrue(matcher.is_match("xxabcxx"))


class RuleMatcherCompositionTests(unittest.TestCase):
    def test_config_keywords_are_added_as_literals(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        method = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_rebuild_rule_matcher"
        )
        calls = {
            node.func.attr: node
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        regex_call = calls["add_regex_patterns"]
        literal_call = calls["add_literal_keywords"]
        self.assertIsInstance(regex_call.args[0], ast.Name)
        self.assertIsInstance(literal_call.args[0], ast.Name)
        self.assertEqual(regex_call.args[0].id, "patterns")
        self.assertEqual(literal_call.args[0].id, "custom")

        matcher = automaton.HybridMatcher()
        matcher.add_literal_keywords(["a.b", "x+y"])
        matcher.build()
        self.assertTrue(matcher.is_match("literal a.b value"))
        self.assertTrue(matcher.is_match("literal x+y value"))
        self.assertFalse(matcher.is_match("axb"))
        self.assertFalse(matcher.is_match("xy"))


if __name__ == "__main__":
    unittest.main()
