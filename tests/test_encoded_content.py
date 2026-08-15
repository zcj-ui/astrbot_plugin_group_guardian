"""Base 系列编码规避内容的解码与资源边界测试。"""

import base64
import unittest

from encoded_content import (
    BASE_MAX_CANDIDATES,
    BASE_OUTPUT_MAX_CHARS,
    decode_base_evidence,
)


def _encode_integer_base(data: bytes, alphabet: str) -> str:
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, len(alphabet))
        encoded = alphabet[remainder] + encoded
    leading = len(data) - len(data.lstrip(b"\x00"))
    return (alphabet[0] * leading) + (encoded or alphabet[0])


class EncodedContentTests(unittest.TestCase):
    def setUp(self):
        self.message = "日抛plus /xxxxxx 加我微信 abc123"
        self.raw = self.message.encode("utf-8")

    def assert_decodes(self, encoded, prefix=""):
        evidence = decode_base_evidence(prefix + encoded)
        self.assertIn(self.message, evidence)
        return evidence

    def test_base16_base32_base64_and_urlsafe(self):
        self.assert_decodes(base64.b16encode(self.raw).decode("ascii"))
        self.assert_decodes(base64.b32encode(self.raw).decode("ascii"))
        self.assert_decodes(base64.b64encode(self.raw).decode("ascii"))
        self.assert_decodes(
            base64.urlsafe_b64encode(self.raw).decode("ascii"), "base64url:"
        )

    def test_base58_base62_and_base85_with_explicit_prefix(self):
        base58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        base62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        self.assert_decodes(_encode_integer_base(self.raw, base58), "base58:")
        self.assert_decodes(_encode_integer_base(self.raw, base62), "base62:")
        self.assert_decodes(base64.b85encode(self.raw).decode("ascii"), "base85:")
        self.assert_decodes(base64.a85encode(self.raw, adobe=True).decode("ascii"))

    def test_explicit_prefix_is_detected_next_to_chinese_text(self):
        encoded = base64.b85encode(self.raw).decode("ascii")

        evidence = decode_base_evidence("请看base85:" + encoded)

        self.assertIn(self.message, evidence)

    def test_double_base64_is_decoded_recursively(self):
        once = base64.b64encode(self.raw)
        twice = base64.b64encode(once).decode("ascii")

        evidence = self.assert_decodes(twice)

        self.assertGreaterEqual(evidence.count("解码]"), 2)

    def test_explicit_prefix_supports_gb18030_legacy_text(self):
        encoded = base64.b64encode(self.message.encode("gb18030")).decode("ascii")

        evidence = decode_base_evidence("base64:" + encoded)

        self.assertIn(self.message, evidence)

    def test_random_identifier_and_binary_payload_are_ignored(self):
        self.assertEqual("", decode_base_evidence("abcdefghijklmnop"))
        binary = base64.b64encode(bytes(range(64))).decode("ascii")
        self.assertEqual("", decode_base_evidence(binary))

    def test_candidate_and_output_budgets_are_bounded(self):
        token = base64.b64encode((self.message * 500).encode("utf-8")).decode("ascii")
        text = " ".join([token] * (BASE_MAX_CANDIDATES + 20))

        evidence = decode_base_evidence(text)

        self.assertLessEqual(len(evidence), BASE_OUTPUT_MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
