"""Conservative normalization of extracted page text, and pages -> sections.

The goal is clean, faithful text for indexing and citation, not prettier text.
Applied per page, in this order:

1. Unicode NFC. Never NFKC: compatibility folding would turn H₂O into H2O,
   x² into x2, ½ into 1⁄2 and µ into μ.
2. The Latin ligatures U+FB00..U+FB06 (ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ) are spelled out; PDF fonts
   produce them for ordinary words, which would otherwise not match searches.
3. Line breaks: CR LF, CR, VT, FF, NEL, U+2028 and U+2029 become LF.
4. Removed: NUL and the other C0/C1 control characters except TAB and LF
   (PostgreSQL text cannot hold NUL), lone surrogates, BOM / zero-width no-break
   space (U+FEFF), zero-width space (U+200B) and soft hyphen (U+00AD). Zero-width
   joiner and non-joiner are kept: they matter in Persian, Arabic and Indic text.
5. Horizontal whitespace (TAB and every Unicode space separator, including
   NO-BREAK SPACE) collapses to one SPACE; spaces at line ends are removed.
6. Three or more line breaks collapse to two (paragraphs are kept), and the
   page is trimmed. A page that becomes empty yields no section.

Not done, on purpose: case folding, punctuation or symbol removal, joining
lines or hyphenated words (1-propanol, trans-2-butene), header/footer
removal, spelling correction, any rewriting by a model, chunking.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

_LIGATURES = str.maketrans(
    {
        "ﬀ": "ff",
        "ﬁ": "fi",
        "ﬂ": "fl",
        "ﬃ": "ffi",
        "ﬄ": "ffl",
        "ﬅ": "st",
        "ﬆ": "st",
    }
)
_LINE_BREAKS = re.compile("\r\n|[\r\x0b\x0c\x85  ]")
_REMOVED = re.compile("[\x00-\x08\x0e-\x1f\x7f-\x84\x86-\x9f\ud800-\udfff﻿​­]")
_HORIZONTAL_SPACE = re.compile("[\t    -   　]+")
_BLANK_LINES = re.compile("\n{3,}")


def normalize_text(raw: str) -> str:
    """Normalize one page of extracted text (see the module docstring)."""
    text = unicodedata.normalize("NFC", raw)
    text = text.translate(_LIGATURES)
    text = _LINE_BREAKS.sub("\n", text)
    text = _REMOVED.sub("", text)
    text = _HORIZONTAL_SPACE.sub(" ", text)
    text = "\n".join(line.strip(" ") for line in text.split("\n"))
    text = _BLANK_LINES.sub("\n\n", text)
    # Removing characters can bring a combining mark next to a new base character.
    return unicodedata.normalize("NFC", text).strip()


@dataclass(frozen=True)
class Section:
    ordinal: int  # 0-based, contiguous within the document
    page: int  # 1-based physical page number
    text: str

    @property
    def locator(self) -> dict[str, int]:
        return {"page": self.page}


def build_sections(pages: Iterable[tuple[int, str]]) -> list[Section]:
    """One section per page whose normalized text is not empty, in page order.

    `pages` are (page number, raw text) pairs in increasing page order. Pages
    without text are skipped without renumbering the pages that follow.
    """
    sections: list[Section] = []
    for page, raw in pages:
        text = normalize_text(raw)
        if text:
            sections.append(Section(ordinal=len(sections), page=page, text=text))
    return sections
