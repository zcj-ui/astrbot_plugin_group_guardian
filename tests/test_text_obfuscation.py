"""Regression coverage for Unicode-disguised external URLs."""

import json
import unittest

from text_obfuscation import (
    extract_obfuscated_url_evidence,
    normalize_url_obfuscation,
)


def _enclosed(text, start=0x1F170):
    result = []
    for char in text.upper():
        if "A" <= char <= "Z":
            result.append(chr(start + ord(char) - ord("A")))
        else:
            result.append(char)
    return "".join(result)


class TextObfuscationTests(unittest.TestCase):
    url = "https://catfk.com/shop/bugbugteam"

    def test_negative_squared_letters_match_the_screenshot_style(self):
        value = _enclosed(self.url)

        self.assertEqual(
            extract_obfuscated_url_evidence(value),
            [self.url.upper()],
        )

    def test_mixed_square_and_colored_emoji_letters_match_the_screenshot_style(self):
        white = 0x1F130
        red = 0x1F170
        blue_p = 0x1F17F
        red_o = 0x1F17E
        value = []
        for index, char in enumerate(self.url.upper()):
            if char == "P":
                value.append(chr(blue_p))
            elif char == "O":
                value.append(chr(red_o))
            elif "A" <= char <= "Z":
                start = red if index % 3 == 0 else white
                value.append(chr(start + ord(char) - ord("A")))
            else:
                value.append(char)

        self.assertEqual(
            extract_obfuscated_url_evidence("".join(value)),
            [self.url.upper()],
        )

    def test_other_enclosed_letter_styles_are_folded(self):
        for start in (0x1F110, 0x1F130, 0x1F150):
            with self.subTest(start=hex(start)):
                self.assertEqual(
                    extract_obfuscated_url_evidence(_enclosed(self.url, start)),
                    [self.url.upper()],
                )

    def test_mixed_enclosed_styles_and_variation_selectors_are_folded(self):
        starts = (0x1F110, 0x1F130, 0x1F150, 0x1F170)
        value = []
        letter_index = 0
        for char in self.url.upper():
            if "A" <= char <= "Z":
                start = starts[letter_index % len(starts)]
                value.append(chr(start + ord(char) - ord("A")) + "\ufe0f")
                letter_index += 1
            else:
                value.append(char)
        self.assertEqual(
            extract_obfuscated_url_evidence("".join(value)),
            [self.url.upper()],
        )

    def test_circled_compatibility_letters_and_tag_letters_are_folded(self):
        circled = "".join(
            chr(0x24B6 + ord(char) - ord("A"))
            if "A" <= char <= "Z" else char
            for char in self.url.upper()
        )
        tagged = "".join(
            chr(0xE0061 + ord(char.lower()) - ord("a"))
            if char.isalpha() and char.isascii() else char
            for char in self.url
        )
        self.assertEqual(
            extract_obfuscated_url_evidence(circled),
            [self.url.upper()],
        )
        self.assertEqual(
            extract_obfuscated_url_evidence(tagged),
            [self.url],
        )

    def test_parenthesized_and_regional_indicator_letters_are_folded(self):
        parenthesized = "".join(
            chr(0x249C + ord(char) - ord("A")).lower()
            if "A" <= char <= "Z" else char
            for char in self.url.upper()
        )
        regional = "".join(
            chr(0x1F1E6 + ord(char) - ord("A"))
            if "A" <= char <= "Z" else char
            for char in self.url.upper()
        )
        self.assertEqual(
            extract_obfuscated_url_evidence(parenthesized),
            [self.url],
        )
        self.assertEqual(
            extract_obfuscated_url_evidence(regional),
            [self.url.upper()],
        )

    def test_zero_width_and_spaced_url_are_reconstructed(self):
        zero_width = "h\u200bt\u200bt\u200bp\u200bs://catfk.com/shop/bugbugteam"
        spaced = "h t t p s : / / c a t f k . c o m / s h o p / b u g b u g t e a m"

        self.assertEqual(extract_obfuscated_url_evidence(zero_width), [self.url])
        self.assertEqual(extract_obfuscated_url_evidence(spaced), [self.url])

    def test_combining_enclosure_marks_are_removed_from_boxed_letters(self):
        for mark in ("\u20dd", "\u20de", "\u20df", "\u20e2", "\u20e3"):
            with self.subTest(mark=hex(ord(mark))):
                value = "".join(
                    char + mark if char.isascii() and char.isalpha() else char
                    for char in self.url
                )
                self.assertEqual(
                    extract_obfuscated_url_evidence(value),
                    [self.url],
                )

    def test_spaced_url_does_not_consume_following_prose(self):
        spaced = (
            "h t t p s : / / c a t f k . c o m / s h o p / "
            "b u g b u g t e a m please"
        )

        self.assertEqual(
            extract_obfuscated_url_evidence(spaced),
            [self.url],
        )

    def test_fullwidth_reverse_solidus_is_treated_as_a_url_separator(self):
        value = self.url.replace("//", "\uff3c\uff3c", 1)

        self.assertEqual(extract_obfuscated_url_evidence(value), [self.url])

    def test_big_solidus_and_duplicate_plain_url_do_not_hide_the_obfuscated_copy(self):
        value = self.url.replace("//", "\u29f8\u29f8", 1)
        boxed = _enclosed(self.url)

        self.assertEqual(extract_obfuscated_url_evidence(value), [self.url])
        self.assertEqual(
            extract_obfuscated_url_evidence(boxed + " " + self.url),
            [self.url.upper()],
        )
        self.assertEqual(
            extract_obfuscated_url_evidence(self.url + " " + boxed),
            [self.url.upper()],
        )

    def test_hxxp_scheme_is_kept_as_obfuscation_evidence(self):
        self.assertEqual(
            extract_obfuscated_url_evidence(self.url.replace("https", "hxxps", 1)),
            [self.url],
        )

    def test_obfuscated_bare_domains_are_reconstructed(self):
        circled = "".join(
            chr(0x24B6 + ord(char.upper()) - ord("A"))
            if char.isascii() and char.isalpha() else char
            for char in "catfk.com/shop"
        )
        boxed = _enclosed("catfk.com/shop")

        self.assertEqual(
            extract_obfuscated_url_evidence(circled),
            ["CATFK.COM/SHOP"],
        )
        self.assertEqual(
            extract_obfuscated_url_evidence(boxed),
            ["CATFK.COM/SHOP"],
        )
        self.assertEqual(extract_obfuscated_url_evidence("catfk.com/shop"), [])

    def test_bare_domain_detection_does_not_duplicate_protocol_url(self):
        boxed = _enclosed(self.url)
        self.assertEqual(
            extract_obfuscated_url_evidence(boxed),
            [self.url.upper()],
        )

    def test_common_cyrillic_lookal_letters_are_folded(self):
        # Cyrillic H/T/P/C/A/K/O/M and DZE S mimic the ASCII URL glyphs.
        letters = {
            "H": 0x041D, "T": 0x0422, "P": 0x0420, "S": 0x0405,
            "C": 0x0421, "A": 0x0410, "K": 0x041A, "O": 0x041E,
            "M": 0x041C,
        }
        value = "".join(
            chr(letters[char]) if char in letters else char
            for char in self.url.upper()
        )

        self.assertEqual(extract_obfuscated_url_evidence(value), [self.url.upper()])

    def test_lowercase_cyrillic_lookal_letters_are_folded(self):
        letters = {
            "h": 0x043D, "t": 0x0442, "p": 0x0440, "s": 0x0455,
            "c": 0x0441, "a": 0x0430, "k": 0x043A, "o": 0x043E,
            "m": 0x043C, "b": 0x0432,
        }
        value = "".join(
            chr(letters[char]) if char in letters else char
            for char in self.url
        )

        self.assertEqual(extract_obfuscated_url_evidence(value), [self.url])

    def test_literal_json_unicode_escapes_are_checked_as_obfuscation(self):
        boxed = _enclosed(self.url)
        escaped_boxed = json.dumps(boxed, ensure_ascii=True)[1:-1]
        escaped_ascii = "".join(f"\\u{ord(char):04x}" for char in self.url)

        self.assertEqual(
            extract_obfuscated_url_evidence(escaped_boxed),
            [self.url.upper()],
        )
        self.assertEqual(
            extract_obfuscated_url_evidence(escaped_ascii),
            [self.url],
        )
        self.assertEqual(
            extract_obfuscated_url_evidence(
                self.url + " " + escaped_ascii
            ),
            [self.url],
        )

    def test_normal_url_and_normal_chat_do_not_add_evidence(self):
        self.assertEqual(extract_obfuscated_url_evidence(self.url), [])
        self.assertEqual(
            extract_obfuscated_url_evidence("https://example.com hello"),
            [],
        )
        self.assertEqual(
            extract_obfuscated_url_evidence(
                "https://example.com and https://other.com"
            ),
            [],
        )
        self.assertEqual(extract_obfuscated_url_evidence("normal chat message"), [])
        self.assertEqual(
            normalize_url_obfuscation("ＡＢＣ\u200b"),
            "ABC",
        )


if __name__ == "__main__":
    unittest.main()
