"""Text normalization and page -> section mapping (no database)."""

from __future__ import annotations

import pytest

from app.ingestion.normalize import build_sections, normalize_text


@pytest.mark.parametrize(
    "raw, expected",
    [
        # scientific notation, sub/superscripts and symbols survive (NFC, never NFKC)
        ("H₂O → 2H₂ + O₂", "H₂O → 2H₂ + O₂"),
        ("ΔH = −92 kJ/mol, x² + y³, ½, µm, 25 °C", "ΔH = −92 kJ/mol, x² + y³, ½, µm, 25 °C"),
        ("CH₃COO⁻ + H⁺ ⇌ CH₃COOH", "CH₃COO⁻ + H⁺ ⇌ CH₃COOH"),
        ("∫ f(x) dx ≈ ∑ aᵢ, α ≤ β", "∫ f(x) dx ≈ ∑ aᵢ, α ≤ β"),
        ("\U0001d6fc + \U0001d6fd", "\U0001d6fc + \U0001d6fd"),  # outside the BMP
        # non-English text, including joiners that matter
        ("Çift bağlı şeker; naïve résumé", "Çift bağlı şeker; naïve résumé"),
        ("می‌خواهم", "می‌خواهم"),  # ZWNJ kept
        # decomposed accents are composed (NFC)
        ("é", "é"),
        # ligatures spelled out
        ("eﬃcient ﬁlter ﬂow", "efficient filter flow"),
        # line endings
        ("a\r\nb\rc d e\x0cf\x0bg\x85h", "a\nb\nc\nd\ne\nf\ng\nh"),
        # control characters, BOM, zero-width space and soft hyphen removed
        ("a\x00b\x07c\x1bd\x7fe\x9ff﻿g​h", "abcdefgh"),
        ("elec­tron", "electron"),
        # horizontal whitespace collapsed, line ends trimmed, paragraphs kept
        ("a \t  b c   \nnext   \n\n\n\n\npara", "a b c\nnext\n\npara"),
        ("  \n\n  padded  \n\n ", "padded"),
        # punctuation and hyphens are content
        ("1-propanol; trans-2-butene (cis/trans) [a, b] {x} \"q\" 'r' — …", "1-propanol; trans-2-butene (cis/trans) [a, b] {x} \"q\" 'r' — …"),
        ("hyphen-\nated words stay as extracted", "hyphen-\nated words stay as extracted"),
        ("", ""),
        (" \t\n ​ ", ""),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


def test_lone_surrogates_are_removed():
    assert normalize_text("a\ud800b\udfffc") == "abc"


def test_removing_a_character_does_not_leave_a_decomposed_sequence():
    assert normalize_text("e​́") == "é"


def test_normalization_is_idempotent():
    raw = "H₂O →\r\n\r\n\r\nﬁne  text\x00 é"
    once = normalize_text(raw)
    assert normalize_text(once) == once


def test_sections_skip_empty_pages_without_renumbering_pages():
    sections = build_sections([(1, "First"), (2, "   \n "), (3, ""), (4, "Fourth\x00"), (7, "Seventh")])
    assert [(s.ordinal, s.page, s.text) for s in sections] == [(0, 1, "First"), (1, 4, "Fourth"), (2, 7, "Seventh")]
    assert [s.locator for s in sections] == [{"page": 1}, {"page": 4}, {"page": 7}]


def test_no_text_gives_no_sections():
    assert build_sections([(1, ""), (2, "​")]) == []
