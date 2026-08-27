# -*- coding: utf-8 -*-
"""Bounded normalization for deliberately disguised external URLs.

The moderation rules must continue to see the original message.  This module
only derives small, explicit URL evidence for Unicode forms that QQ renders as
letters inside coloured boxes or with invisible separators.
"""

import json
import re
import unicodedata
from typing import List


_ENCLOSED_LATIN_RANGES = (
    (0x1F110, 0x1F129),  # PARENTHESIZED LATIN CAPITAL LETTER A..Z
    (0x1F130, 0x1F149),  # SQUARED LATIN CAPITAL LETTER A..Z
    (0x1F150, 0x1F169),  # NEGATIVE CIRCLED LATIN CAPITAL LETTER A..Z
    (0x1F170, 0x1F189),  # NEGATIVE SQUARED LATIN CAPITAL LETTER A..Z
)
_PARENTHESIZED_LATIN_RANGES = (
    (0x249C, 0x24B5),  # PARENTHESIZED LATIN SMALL LETTER A..Z
)
_REGIONAL_INDICATOR_RANGE = (0x1F1E6, 0x1F1FF)
_TAG_LATIN_RANGES = (
    (0xE0061, 0xE007A),  # TAG LATIN SMALL LETTER a..z
)
_ENCLOSED_NAME_PREFIXES = (
    "CIRCLED LATIN CAPITAL LETTER ",
    "CIRCLED LATIN SMALL LETTER ",
    "NEGATIVE CIRCLED LATIN CAPITAL LETTER ",
    "NEGATIVE CIRCLED LATIN SMALL LETTER ",
    "SQUARED LATIN CAPITAL LETTER ",
    "SQUARED LATIN SMALL LETTER ",
    "NEGATIVE SQUARED LATIN CAPITAL LETTER ",
    "NEGATIVE SQUARED LATIN SMALL LETTER ",
    "PARENTHESIZED LATIN CAPITAL LETTER ",
    "PARENTHESIZED LATIN SMALL LETTER ",
)

# QQ and other clients can render ordinary letters inside a box/circle by
# appending one of these combining enclosing marks.  They are ``Me`` marks,
# rather than the ``Mn`` variation selectors handled below.
_COMBINING_ENCLOSING_MARKS = frozenset({
    0x20DD,  # COMBINING ENCLOSING CIRCLE
    0x20DE,  # COMBINING ENCLOSING SQUARE
    0x20DF,  # COMBINING ENCLOSING DIAMOND
    0x20E2,  # COMBINING ENCLOSING SCREEN
    0x20E3,  # COMBINING ENCLOSING KEYCAP
})

# Common Latin-lookal Greek and Cyrillic code points.  They are only used to
# derive URL evidence, never to replace the original message body.
_ASCII_CONFUSABLES = {
    0x0391: "A", 0x0392: "B", 0x0395: "E", 0x0396: "Z",
    0x0397: "H", 0x0399: "I", 0x039A: "K", 0x039C: "M",
    0x039D: "N", 0x039F: "O", 0x03A1: "P", 0x03A4: "T",
    0x03A5: "Y", 0x03A7: "X", 0x03B1: "a", 0x03B5: "e",
    0x03B9: "i", 0x03BA: "k", 0x03BD: "v", 0x03BF: "o",
    0x03C1: "p", 0x03C4: "t", 0x03C5: "y", 0x03C7: "x",
    0x0410: "A", 0x0412: "B", 0x0415: "E", 0x041A: "K",
    0x0405: "S", 0x041C: "M", 0x041D: "H", 0x041E: "O", 0x0420: "P",
    0x0421: "C", 0x0422: "T", 0x0423: "Y", 0x0425: "X",
    0x0406: "I", 0x0408: "J", 0x0430: "a", 0x0432: "b",
    0x0435: "e", 0x043A: "k", 0x043C: "m", 0x043D: "h",
    0x043E: "o", 0x0440: "p", 0x0441: "c", 0x0442: "t",
    0x0443: "y", 0x0445: "x", 0x0456: "i", 0x0458: "j",
    0x0455: "s", 0x04BA: "H", 0x04BB: "h", 0x04C0: "I",
    0x04CF: "l",
}

_PUNCTUATION_MAP = {
    0x005C: "/",  # REVERSE SOLIDUS (including NFKC fullwidth reverse solidus)
    0x3002: ".",  # IDEOGRAPHIC FULL STOP
    0xFF0E: ".",  # FULLWIDTH FULL STOP
    0xFF1A: ":",  # FULLWIDTH COLON
    0xFF0F: "/",  # FULLWIDTH SOLIDUS
    0x2044: "/",  # FRACTION SLASH
    0x2215: "/",  # DIVISION SLASH
    0x29F8: "/",  # BIG SOLIDUS
    0x2024: ".",  # ONE DOT LEADER
    0x2027: ".",  # HYPHENATION POINT
    0xFE52: ".",  # SMALL FULL STOP
    0xFF61: ".",  # HALFWIDTH IDEOGRAPHIC FULL STOP
    0x2212: "-",  # MINUS SIGN
}

_URL_RE = re.compile(
    r"(?i)(?<![a-z0-9])"
    r"(?:https?|hxxps?)://"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z](?:[a-z0-9-]{0,61}[a-z0-9]))"
    r"(?::\d{2,5})?"
    r"(?:/[a-z0-9._~!$&'()*+,;=:@%/?#-]*)?"
)
# A protocol is often omitted in QQ names/cards.  Keep this deliberately
# stricter than a general hostname parser: at least two labels and a two-
# character alphabetic TLD are required, and callers only accept a match when
# its source contains an actual obfuscation.
_BARE_DOMAIN_RE = re.compile(
    r"(?i)(?<![a-z0-9@])"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])"
    r"(?:/[a-z0-9._~!$&'()*+,;=:@%/?#-]*)?"
)
_URL_SCHEME_WITH_GAPS_RE = re.compile(
    r"(?i)(?<![a-z0-9])"
    r"(?:h\s*t\s*t\s*p(?:\s*s)?|h\s*x\s*x\s*p(?:\s*s)?)"
    r"\s*:\s*/\s*/"
)
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}"
_URL_CONTINUATION_CHARS = set("./?#:&=~!$'()*+,;@%_-\\")
_JSON_UNICODE_ESCAPES_RE = re.compile(r"(?:\\u[0-9a-fA-F]{4}){1,128}")


def _enclosed_latin(codepoint: int) -> str:
    for start, end in _ENCLOSED_LATIN_RANGES:
        if start <= codepoint <= end:
            return chr(ord("A") + codepoint - start)
    for start, end in _PARENTHESIZED_LATIN_RANGES:
        if start <= codepoint <= end:
            return chr(ord("a") + codepoint - start)
    regional_start, regional_end = _REGIONAL_INDICATOR_RANGE
    if regional_start <= codepoint <= regional_end:
        return chr(ord("A") + codepoint - regional_start)
    for start, end in _TAG_LATIN_RANGES:
        if start <= codepoint <= end:
            return chr(ord("a") + codepoint - start)
    name = unicodedata.name(chr(codepoint), "")
    for prefix in _ENCLOSED_NAME_PREFIXES:
        if name.startswith(prefix):
            suffix = name[len(prefix):]
            if len(suffix) == 1 and "A" <= suffix <= "Z":
                return suffix if "CAPITAL" in prefix else suffix.lower()
    return ""


def _normalize_url_with_sources(text: str):
    """Return normalized text plus the source index for every output char."""
    result = []
    source_indexes = []
    for source_index, original_char in enumerate(str(text or "")):
        # This lookup must happen before NFKC.  Parenthesized letters otherwise
        # become "(A)", which no longer carries their original code point.
        enclosed = _enclosed_latin(ord(original_char))
        if enclosed:
            result.append(enclosed)
            source_indexes.append(source_index)
            continue
        for char in unicodedata.normalize("NFKC", original_char):
            codepoint = ord(char)
            mapped = _ASCII_CONFUSABLES.get(codepoint)
            if mapped:
                result.append(mapped)
                source_indexes.append(source_index)
                continue
            mapped = _PUNCTUATION_MAP.get(codepoint)
            if mapped:
                result.append(mapped)
                source_indexes.append(source_index)
                continue
            if codepoint in _COMBINING_ENCLOSING_MARKS:
                continue
            category = unicodedata.category(char)
            if category in {"Cf", "Mn"}:
                continue
            result.append(char)
            source_indexes.append(source_index)
    return "".join(result), source_indexes


def normalize_url_obfuscation(text: str) -> str:
    """Normalize only URL-relevant Unicode transformations.

    NFKC handles fullwidth and mathematical alphabets, but intentionally does
    not fold QQ's negative-square/circle emoji letters.  Format controls,
    variation selectors, and combining enclosure marks are removed so they
    cannot split a URL token.
    """
    return _normalize_url_with_sources(text)[0]


def _canonical_url(value: str) -> str:
    value = str(value or "").rstrip(_URL_TRAILING_PUNCTUATION)
    lower = value.lower()
    if lower.startswith("hxxps://"):
        return "https://" + value[8:]
    if lower.startswith("hxxp://"):
        return "http://" + value[7:]
    return value


def _decode_json_unicode_escapes(text: str):
    """Decode bounded JSON Unicode runs and retain their source positions."""
    pieces = []
    escaped_flags = []
    cursor = 0

    def append(value: str, escaped: bool) -> None:
        pieces.append(value)
        escaped_flags.extend([escaped] * len(value))

    for match in _JSON_UNICODE_ESCAPES_RE.finditer(text):
        append(text[cursor:match.start()], False)
        escaped = match.group(0)
        value = escaped
        try:
            decoded = json.loads(f'"{escaped}"')
            if isinstance(decoded, str):
                value = decoded
        except (TypeError, ValueError):
            pass
        append(value, value != escaped)
        cursor = match.end()
    append(text[cursor:], False)
    return "".join(pieces), escaped_flags


def _source_has_json_escape(match, source_indexes, escaped_flags) -> bool:
    if not escaped_flags or match.start() >= len(source_indexes):
        return False
    end = min(match.end(), len(source_indexes))
    return any(
        0 <= source_indexes[index] < len(escaped_flags)
        and escaped_flags[source_indexes[index]]
        for index in range(match.start(), end)
    )


def _extract_spaced_url_candidates(
        normalized: str, limit: int, source_text: str = "", source_indexes=None):
    """Parse visible spaces near a URL without joining adjacent prose.

    Once a complete host is visible, ordinary words after a whitespace
    boundary end the candidate. A slash/query separator or a sequence of
    one-character runs keeps the parser inside a deliberately spaced path.
    """
    for scheme_match in _URL_SCHEME_WITH_GAPS_RE.finditer(normalized):
        segment = normalized[scheme_match.start():scheme_match.start() + 1024]
        compact = []
        removed_inside = False
        spaced_host = False
        spaced_path = False
        pos = 0
        while pos < len(segment):
            char = segment[pos]
            if char in "\r\n":
                break
            if char.isspace():
                end = pos + 1
                while end < len(segment) and segment[end].isspace():
                    end += 1
                current = "".join(compact)
                current_match = _URL_RE.match(current)
                next_pos = end
                if current_match:
                    if next_pos >= len(segment):
                        break
                    next_char = segment[next_pos]
                    if next_char in _URL_CONTINUATION_CHARS:
                        source_position = scheme_match.start() + next_pos
                        if (source_text and source_indexes
                                and source_position < len(source_indexes)):
                            source_index = source_indexes[source_position]
                            escape_start = source_text[source_index:source_index + 6]
                            if re.fullmatch(r"\\u[0-9a-fA-F]{4}", escape_start):
                                break
                        spaced_path = spaced_path or next_char in "/?#"
                        pos = end
                        removed_inside = True
                        continue
                    run_end = next_pos
                    while (run_end < len(segment)
                           and not segment[run_end].isspace()
                           and segment[run_end] not in "\r\n"):
                        run_end += 1
                    run = segment[next_pos:run_end]
                    if ((spaced_host or spaced_path) and len(run) == 1
                            and run[0].isalnum()
                            and (run_end == len(segment)
                                 or segment[run_end].isspace())):
                        pos = end
                        removed_inside = True
                        continue
                    break
                # The host is not complete yet, so this gap is part of the
                # obfuscated token (for example: "c a t f k . c o m").
                if "://" in current:
                    spaced_host = True
                pos = end
                removed_inside = True
                continue
            compact.append(char)
            pos += 1

        if not removed_inside:
            continue
        candidate_match = _URL_RE.match("".join(compact))
        if candidate_match:
            yield candidate_match.group(0)
            limit -= 1
            if limit <= 0:
                return


def extract_obfuscated_url_evidence(text: str, limit: int = 4) -> List[str]:
    """Return reconstructed URLs that were hidden by Unicode or spacing.

    A normal URL is deliberately not returned: existing URL rules already see
    it.  At most a few reconstructed URLs are emitted so hostile long messages
    cannot inflate the downstream audit prompt.
    """
    original = str(text or "")
    if not original:
        return []
    results = []
    seen = set()
    max_results = max(1, int(limit or 1))

    def add_candidate(raw_candidate: str) -> bool:
        candidate = _canonical_url(raw_candidate)
        key = candidate.casefold()
        if not candidate or key in seen:
            return False
        seen.add(key)
        results.append(candidate)
        return len(results) >= max_results

    sources = [(original, [False] * len(original))]
    decoded_escapes, escaped_flags = _decode_json_unicode_escapes(original)
    if decoded_escapes != original:
        # A literal JSON escape is itself the obfuscation, even where decoding
        # yields an otherwise normal ASCII URL. Keep the source flags narrow so
        # a separate ordinary URL in the same message stays ordinary.
        sources.append((decoded_escapes, escaped_flags))

    for source_text, escaped_flags in sources:
        normalized, source_indexes = _normalize_url_with_sources(source_text)

        def source_fragment_for(match) -> str:
            if match.start() >= len(source_indexes) or match.end() <= match.start():
                return ""
            source_start = source_indexes[match.start()]
            source_end = source_indexes[match.end() - 1] + 1
            return source_text[source_start:source_end]

        def is_obfuscated_source(raw_candidate: str, match) -> bool:
            source_fragment = source_fragment_for(match)
            raw_is_hxxp = raw_candidate.casefold().startswith(("hxxp://", "hxxps://"))
            return (_source_has_json_escape(match, source_indexes, escaped_flags)
                    or raw_is_hxxp
                    or source_fragment.casefold() != raw_candidate.casefold())

        for match in _URL_RE.finditer(normalized):
            raw_candidate = match.group(0)
            # A normal ASCII URL must remain invisible to this detector. Compare
            # the source span for this particular match instead of checking whether
            # the URL appears anywhere in the message: a normal copy must not mask
            # a second, deliberately disguised copy of the same URL.
            if not is_obfuscated_source(raw_candidate, match):
                continue
            if add_candidate(raw_candidate):
                return results
        for raw_candidate in _extract_spaced_url_candidates(
                normalized, max_results - len(results), source_text, source_indexes):
            if add_candidate(raw_candidate):
                return results

        for match in _BARE_DOMAIN_RE.finditer(normalized):
            raw_candidate = match.group(0)
            # Do not re-emit the host portion of a protocol URL. The protocol
            # form above carries better evidence and should remain the sole result.
            prefix = normalized[max(0, match.start() - 8):match.start()].casefold()
            if prefix.endswith("://"):
                continue
            if not is_obfuscated_source(raw_candidate, match):
                continue
            if add_candidate(raw_candidate):
                return results
    return results
