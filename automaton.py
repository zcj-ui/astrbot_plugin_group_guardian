# -*- coding: utf-8 -*-
"""Aho-Corasick 自动机封装。支持纯文本 AC 匹配 + 正则不可拆解部分的回退。"""

import re
from typing import Callable, List, Optional, Tuple

try:
    from astrbot.api import logger
except Exception:  # 脱离 AstrBot 运行时（如单测）的兜底 logger
    import logging
    logger = logging.getLogger("group_guardian.automaton")

try:
    import ahocorasick
except ImportError:
    ahocorasick = None
    logger.warning(
        "[GroupMgr] pyahocorasick 未安装，词库 AC 自动机不可用，"
        "已降级为纯 Python Trie 字面量扫描 + 正则回退。"
        "建议安装依赖以获得高性能: pip install pyahocorasick"
    )

# 正则元字符集，用于判断 pattern 是否为纯文本
_REGEX_META = re.compile(r"[().^$*+?{}\[\]|\\]")


def is_literal_pattern(pattern: str) -> bool:
    """判断 pattern 是否不含正则元字符，可直接逐字匹配。"""
    return not _REGEX_META.search(pattern)


# 正则展开的候选数上限：多个 (a|b|c)/[abc] 相乘呈指数增长，超限整体回退为正则匹配（扫描#38 m2）
_MAX_EXPAND_CANDIDATES = 1000


def _extract_alternatives(pattern: str) -> List[str]:
    """从简单正则中提取纯文本候选。

    支持的语法：
    - 字面量字符串
    - (?:a|b|c) → 展开为 a,b,c
    - [abc] → 展开为 a,b,c
    - 组合：a(?:b|c)d → a b d, a c d
    """
    candidates = [""]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "(":
            end = pattern.find(")", i)
            if end == -1:
                return []
            inner = pattern[i + 1 : end]
            if inner.startswith("?:"):
                inner = inner[2:]
            options = inner.split("|")
            # Only expand when every branch is a non-empty literal.  Dropping an
            # unsupported branch changes the regex semantics and can create both
            # false positives and false negatives.
            if not options or any(not o or _REGEX_META.search(o) for o in options):
                return []
            if len(candidates) * len(options) > _MAX_EXPAND_CANDIDATES:
                return []  # 超限回退为正则匹配，防止笛卡尔积撑爆内存
            new_candidates = []
            for c in candidates:
                for opt in options:
                    new_candidates.append(c + opt)
            candidates = new_candidates
            i = end + 1
        elif ch == "[":
            end = pattern.find("]", i)
            if end == -1:
                return []
            body = pattern[i + 1 : end]
            # Negated and escaped classes need the regex engine.  Treating '^'
            # or '\\s' as literal alternatives silently broadens the rule.
            if not body or body.startswith("^") or "\\" in body:
                return []
            chars = []
            j = i + 1
            while j < end:
                if j + 2 < end and pattern[j + 1] == "-":
                    start_c = pattern[j]
                    end_c = pattern[j + 2]
                    range_size = ord(end_c) - ord(start_c) + 1
                    if range_size <= 0:
                        return []
                    # Check before materialising the range; a user supplied
                    # Unicode-wide range must not allocate millions of entries.
                    if len(candidates) * (len(chars) + range_size) > _MAX_EXPAND_CANDIDATES:
                        return []
                    for c in range(ord(start_c), ord(end_c) + 1):
                        chars.append(chr(c))
                    j += 3
                else:
                    chars.append(pattern[j])
                    j += 1
            if not chars:
                return []
            if len(candidates) * len(chars) > _MAX_EXPAND_CANDIDATES:
                return []  # 超限回退为正则匹配
            new_candidates = []
            for c in candidates:
                for ch_char in chars:
                    new_candidates.append(c + ch_char)
            candidates = new_candidates
            i = end + 1
        elif _REGEX_META.match(ch):
            return []
        else:
            candidates = [c + ch for c in candidates]
            i += 1
    return candidates


def regex_to_literals(pattern: str, min_len: int = 1) -> List[str]:
    """将正则 pattern 拆解为纯文本关键词列表。

    能拆的返回展开后的关键词，不能拆的返回空列表。
    """
    if is_literal_pattern(pattern):
        return [pattern.strip().lower()] if pattern.strip() else []
    try:
        # Invalid regular expressions must never be accepted merely because the
        # lightweight expander happened to recognise part of their syntax.
        re.compile(pattern)
        expanded = _extract_alternatives(pattern.strip())
        return [e.lower() for e in expanded if len(e) >= min_len]
    except (Exception, re.error):
        return []


class KeywordAutomaton:
    """AC 自动机：构建 Trie + fail 指针，一次扫描命中所有关键词。"""

    def __init__(self):
        self._auto = None
        self._count = 0
        self._built = False
        # pyahocorasick 不可用时使用轻量 Trie。此前的降级路径会为每个
        # 字面量编译一个正则，词库扩大后会造成 O(关键词数) 的逐条扫描。
        # None 作为终止标记，文本字符始终是 str，不会与其冲突。
        self._fallback_trie = {}
        self._seen = set()

    def add_keywords(self, keywords: List[str]) -> None:
        """批量添加纯文本关键词，自动跳过空值和重复。"""
        for kw in keywords:
            kw = kw.strip().lower()
            if not kw or kw in self._seen:
                continue
            self._seen.add(kw)
            if ahocorasick is not None:
                if self._auto is None:
                    self._auto = ahocorasick.Automaton()
                self._auto.add_word(kw, kw)
            else:
                node = self._fallback_trie
                for char in kw:
                    node = node.setdefault(char, {})
                node[None] = kw
            self._count += 1
        self._built = False

    def build(self) -> None:
        """构建 fail 指针，调用后不可再添加关键词。"""
        if self._auto is not None and not self._built:
            self._auto.make_automaton()
            self._built = True
        elif ahocorasick is None:
            # Trie 在插入时已经完成构建；保留显式 build 调用以兼容原 API。
            self._built = True

    def first_match(self, text: str) -> Optional[Tuple[int, str]]:
        """Return one match without materialising every overlapping result."""
        if not self._built:
            return None
        if self._auto is None:
            text_lower = text.lower()
            for start in range(len(text_lower)):
                node = self._fallback_trie
                for end in range(start, len(text_lower)):
                    node = node.get(text_lower[end])
                    if node is None:
                        break
                    keyword = node.get(None)
                    if keyword is not None:
                        return end, keyword
            return None
        text_lower = text.lower()
        try:
            return next(self._auto.iter(text_lower))
        except StopIteration:
            return None

    def exists(self, text: str) -> bool:
        if not self._built:
            return False
        return self.first_match(text) is not None

    def iter_matches(self, text: str) -> List[Tuple[int, str]]:
        if not self._built:
            return []
        if self._auto is None:
            text_lower = text.lower()
            results: List[Tuple[int, str]] = []
            for start in range(len(text_lower)):
                node = self._fallback_trie
                for end in range(start, len(text_lower)):
                    node = node.get(text_lower[end])
                    if node is None:
                        break
                    keyword = node.get(None)
                    if keyword is not None:
                        results.append((end, keyword))
            # 与 pyahocorasick 一样按结束位置返回；同一位置按词典序稳定化。
            results.sort(key=lambda item: (item[0], item[1]))
            return results
        text_lower = text.lower()
        try:
            return list(self._auto.iter(text_lower))
        except StopIteration:
            return []

    @property
    def count(self) -> int:
        return self._count

    @property
    def available(self) -> bool:
        return ahocorasick is not None


class HybridMatcher:
    """混合匹配器：AC 快速过纯文本 + 正则回退补漏。

    用法：
        m = HybridMatcher()
        m.add_regex_patterns(["妈[的得]", "广告.*?联系方式"])
        m.build()
        m.is_match(text)  # AC 优先，回退正则
    """

    def __init__(self):
        self._ac = KeywordAutomaton()
        self._regex_fallback: List[re.Pattern] = []
        self._built = False

    def _add_ac_keywords(self, keywords: List[str]) -> None:
        """把纯文本关键词加入 AC；不可用时进入内置 Trie。"""
        if not keywords:
            return
        self._ac.add_keywords(keywords)

    def add_literal_keywords(self, keywords: List[str]) -> None:
        """添加纯文本字面量关键词（不做正则解析），供用户自定义违禁词等场景使用。"""
        cleaned = [str(k).strip() for k in (keywords or []) if str(k).strip()]
        self._add_ac_keywords(cleaned)
        self._built = False

    def add_regex_patterns(self, patterns: List[str]) -> None:
        """批量添加正则 pattern，逐个尝试拆成纯文本，拆不了的留作正则回退。"""
        ac_keywords: List[str] = []
        fallback_raw: List[str] = []
        for p in patterns:
            if not p or not p.strip():
                continue
            literals = regex_to_literals(p, min_len=1)
            if literals:
                ac_keywords.extend(literals)
            else:
                fallback_raw.append(p)
        self._add_ac_keywords(ac_keywords)
        for raw in fallback_raw:
            try:
                self._regex_fallback.append(re.compile(raw, re.IGNORECASE))
            except re.error as e:
                logger.warning(f"[GroupMgr] 规则正则编译失败已跳过: {raw!r}: {e}")
        self._built = False

    def build(self) -> None:
        self._ac.build()
        self._built = True

    def first_match(self, text: str) -> Optional[Tuple[int, str, str]]:
        """Return one AC/regex match without collecting all overlaps."""
        if not self._built:
            return None
        match = self._ac.first_match(text)
        if match is not None:
            return match[0], match[1], "ac"
        for p in self._regex_fallback:
            regex_match = p.search(text)
            if regex_match:
                return regex_match.end(), p.pattern, "regex"
        return None

    def is_match(self, text: str) -> bool:
        """文本是否匹配任意 pattern（AC 优先，退火正则回退）。"""
        return self.first_match(text) is not None

    def iter_matches(self, text: str) -> List[Tuple[int, str, str]]:
        """返回所有匹配：(end_pos, keyword_or_pattern, 'ac'|'regex')。"""
        results: List[Tuple[int, str, str]] = []
        if not self._built:
            return results
        for end_pos, kw in self._ac.iter_matches(text):
            results.append((end_pos, kw, "ac"))
        for p in self._regex_fallback:
            m = p.search(text)
            if m:
                results.append((m.end(), p.pattern, "regex"))
        return results

    @property
    def ac_count(self) -> int:
        return self._ac.count

    @property
    def regex_count(self) -> int:
        return len(self._regex_fallback)

    @property
    def available(self) -> bool:
        return self._ac.available
