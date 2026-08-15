# -*- coding: utf-8 -*-
"""受限 Base 系列文本解码，用于把编码规避内容注入现有审核管线。"""

import base64
import binascii
import re
from typing import Callable, Iterable, Tuple


BASE_INPUT_MAX_CHARS = 100_000
BASE_TOKEN_MAX_CHARS = 4096
BASE_OUTPUT_MAX_CHARS = 20_000
BASE_DECODED_ITEM_MAX_CHARS = 8000
BASE_MAX_CANDIDATES = 32
BASE_MAX_DEPTH = 2

_PREFIX_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(base64url|base64|b64|base32|b32|base16|b16|hex|"
    r"base58|b58|base62|b62|base85|b85|ascii85|a85)\s*[:：=]\s*(\S{4,4096})"
)
_GENERIC_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z0-9+/_-]{12,4096}={0,2})(?![A-Za-z0-9])"
)
_ADOBE_ASCII85_RE = re.compile(r"<~[^\s]{5,4096}~>")
_TEXT_SIGNAL_RE = re.compile(
    r"(?i)(?:https?://|www\.|[A-Za-z]{3,}|[\u3400-\u4dbf\u4e00-\u9fff]{2,})"
)

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _restore_padding(value: str, block_size: int) -> str:
    return value + ("=" * ((-len(value)) % block_size))


def _decode_integer_base(value: str, alphabet: str) -> bytes:
    lookup = {char: index for index, char in enumerate(alphabet)}
    number = 0
    for char in value:
        if char not in lookup:
            raise ValueError("字符不属于目标编码字母表")
        number = number * len(alphabet) + lookup[char]
    body = b"" if number == 0 else number.to_bytes((number.bit_length() + 7) // 8, "big")
    zero_char = alphabet[0]
    leading = len(value) - len(value.lstrip(zero_char))
    return (b"\x00" * leading) + body


def _decode_bytes_as_text(data: bytes, allow_legacy: bool = False) -> str:
    if not data or len(data) > BASE_DECODED_ITEM_MAX_CHARS * 4:
        return ""
    decoded = ""
    encodings = ("utf-8", "gb18030") if allow_legacy else ("utf-8",)
    for encoding in encodings:
        try:
            decoded = data.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not decoded:
        return ""
    decoded = decoded.strip("\x00\ufeff \t\r\n")
    if len(decoded) < 4:
        return ""
    sample = decoded[:BASE_DECODED_ITEM_MAX_CHARS]
    printable = sum(char.isprintable() or char in "\r\n\t" for char in sample)
    if printable / max(1, len(sample)) < 0.9:
        return ""
    if not _TEXT_SIGNAL_RE.search(sample):
        return ""
    return sample


def _decode_with(label: str, value: str, allow_legacy: bool = False) -> str:
    compact = "".join(str(value or "").strip().split())
    compact = compact.strip("'\"`，。；;、")
    if not compact or len(compact) > BASE_TOKEN_MAX_CHARS:
        return ""
    normalized = label.lower()
    try:
        if normalized in {"base16", "b16", "hex"}:
            raw = base64.b16decode(compact.upper(), casefold=True)
        elif normalized in {"base32", "b32"}:
            raw = base64.b32decode(_restore_padding(compact.upper(), 8), casefold=True)
        elif normalized in {"base64", "b64"}:
            raw = base64.b64decode(_restore_padding(compact, 4), validate=True)
        elif normalized == "base64url":
            raw = base64.b64decode(
                _restore_padding(compact.replace("-", "+").replace("_", "/"), 4),
                validate=True,
            )
        elif normalized in {"base58", "b58"}:
            raw = _decode_integer_base(compact, _BASE58_ALPHABET)
        elif normalized in {"base62", "b62"}:
            raw = _decode_integer_base(compact, _BASE62_ALPHABET)
        elif normalized in {"ascii85", "a85"}:
            raw = base64.a85decode(compact, adobe=compact.startswith("<~"))
        elif normalized in {"base85", "b85"}:
            raw = base64.b85decode(compact)
        else:
            return ""
    except (ValueError, TypeError, binascii.Error, OverflowError):
        return ""
    return _decode_bytes_as_text(raw, allow_legacy=allow_legacy)


def _generic_decoder_candidates(value: str) -> Iterable[Tuple[str, Callable[[str], str]]]:
    compact = value.rstrip("=")
    if len(value) % 2 == 0 and re.fullmatch(r"[0-9A-Fa-f]+", value):
        yield "Base16", lambda token: _decode_with("base16", token)
    if re.fullmatch(r"[A-Z2-7]+=*", value, re.IGNORECASE):
        yield "Base32", lambda token: _decode_with("base32", token)
    if len(compact) % 4 != 1 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value):
        yield "Base64", lambda token: _decode_with("base64", token)
    if len(compact) % 4 != 1 and re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        yield "Base64URL", lambda token: _decode_with("base64url", token)
    if len(value) >= 16 and all(char in _BASE58_ALPHABET for char in value):
        yield "Base58", lambda token: _decode_with("base58", token)
    if len(value) >= 16 and all(char in _BASE62_ALPHABET for char in value):
        yield "Base62", lambda token: _decode_with("base62", token)


def _iter_candidates(text: str) -> Iterable[Tuple[str, str, bool]]:
    occupied = []
    for match in _PREFIX_RE.finditer(text):
        occupied.append(match.span())
        yield match.group(1), match.group(2), True
    for match in _ADOBE_ASCII85_RE.finditer(text):
        occupied.append(match.span())
        yield "ascii85", match.group(0), True
    for match in _GENERIC_TOKEN_RE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        yield "", match.group(1), False


def decode_base_evidence(text: str) -> str:
    """返回带编码类型标签的可读解码证据；没有可靠证据时返回空串。"""
    root = str(text or "")[:BASE_INPUT_MAX_CHARS]
    if not root:
        return ""
    queue = [(root, 0)]
    seen_tokens = set()
    seen_outputs = set()
    results = []
    candidates = 0
    output_chars = 0

    while queue and candidates < BASE_MAX_CANDIDATES:
        current, depth = queue.pop(0)
        for label, token, explicit in _iter_candidates(current):
            token_key = (label.lower(), token)
            if token_key in seen_tokens:
                continue
            seen_tokens.add(token_key)
            candidates += 1
            if candidates > BASE_MAX_CANDIDATES:
                break

            decoded_items = []
            if explicit:
                decoded = _decode_with(label, token, allow_legacy=True)
                if decoded:
                    decoded_items.append((label.upper(), decoded))
            else:
                for detected_label, decoder in _generic_decoder_candidates(token):
                    decoded = decoder(token)
                    if decoded:
                        decoded_items.append((detected_label, decoded))

            for detected_label, decoded in decoded_items:
                normalized = decoded.strip()
                if not normalized or normalized in seen_outputs:
                    continue
                seen_outputs.add(normalized)
                prefix = f"[{detected_label}解码] "
                separator_chars = 1 if results else 0
                remaining = (
                    BASE_OUTPUT_MAX_CHARS
                    - output_chars
                    - separator_chars
                    - len(prefix)
                )
                if remaining <= 0:
                    return "\n".join(results)
                clipped = normalized[:remaining]
                result = f"{prefix}{clipped}"
                results.append(result)
                output_chars += separator_chars + len(result)
                if depth + 1 < BASE_MAX_DEPTH:
                    queue.append((normalized, depth + 1))

    return "\n".join(results)
