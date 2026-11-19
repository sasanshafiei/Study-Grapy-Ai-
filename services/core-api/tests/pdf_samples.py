"""Small, deterministic PDF files generated for tests (no real documents).

make_pdf() builds a structurally valid one-page PDF (catalog, page tree, page,
content stream, font, cross-reference table, trailer). Different labels give
different bytes and therefore different SHA-256 hashes.

build_pdf() (M3) builds multi-page documents for the parser tests: pages with
text, including arbitrary Unicode (a simple font whose ToUnicode CMap maps one
byte code per character, which is what text extraction reads), blank pages,
image-only pages, and a page whose content stream is a compression bomb.
encrypt_pdf() encrypts one with pypdf (RC4, no extra dependency). All output
is deterministic except encryption, whose file identifier is random by design.
"""

from __future__ import annotations

import io
import zlib
from dataclasses import dataclass


def make_pdf(label: str = "StudyGraph test document", *, padding: int = 0) -> bytes:
    if not all(ch.isalnum() or ch in " -_." for ch in label):
        raise ValueError("keep test labels to letters, digits, spaces and - _ .")
    content = f"BT /F1 12 Tf 20 100 Td ({label}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    if padding:
        out += b"%" + b"p" * max(padding - 2, 0) + b"\n"  # a comment line, ignored by readers
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


# --- M3: multi-page documents ---------------------------------------------------------------


@dataclass(frozen=True)
class Text:
    """A page with text; `lines` are drawn top to bottom."""

    lines: tuple[str, ...]


@dataclass(frozen=True)
class Blank:
    """A page with no content at all."""


@dataclass(frozen=True)
class ImageOnly:
    """A page that only paints an image (like a scanned page): no extractable text."""


@dataclass(frozen=True)
class Bomb:
    """A page whose content stream inflates to `inflated_bytes` of spaces."""

    inflated_bytes: int


def text_page(*lines: str) -> Text:
    return Text(tuple(lines))


class _Builder:
    def __init__(self) -> None:
        self.objects: list[bytes | None] = []

    def reserve(self) -> int:
        self.objects.append(None)
        return len(self.objects)

    def set(self, number: int, body: bytes) -> None:
        self.objects[number - 1] = body

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)

    def stream(self, data: bytes, extra: bytes = b"") -> int:
        return self.add(b"<< /Length %d %s>>\nstream\n" % (len(data), extra) + data + b"\nendstream")

    def render(self, root: int) -> bytes:
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(self.objects, start=1):
            assert body is not None, f"object {number} was reserved but never set"
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(self.objects) + 1)
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(self.objects) + 1, root, xref)
        return bytes(out)


def _to_unicode_cmap(mapping: dict[int, str]) -> bytes:
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /StudyGraph-Test-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<00> <FF>",
        "endcodespacerange",
    ]
    items = sorted(mapping.items())
    for start in range(0, len(items), 100):
        chunk = items[start : start + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for code, char in chunk:
            lines.append(f"<{code:02X}> <{char.encode('utf-16-be').hex().upper()}>")
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("ascii")


def build_pdf(pages: list[Text | Blank | ImageOnly | Bomb]) -> bytes:
    """A PDF with the given pages, in order. Text pages share one font.

    Every distinct character of the text pages gets a one-byte code (at most
    255 distinct characters) mapped to its Unicode value by a ToUnicode CMap,
    so any Unicode text, including characters outside the BMP, round-trips
    through text extraction.
    """
    chars = sorted({ch for page in pages if isinstance(page, Text) for line in page.lines for ch in line})
    if len(chars) > 255:
        raise ValueError("at most 255 distinct characters per document")
    codes = {ch: index + 1 for index, ch in enumerate(chars)}

    b = _Builder()
    catalog = b.reserve()
    tree = b.reserve()
    font = None
    if chars:
        cmap = b.stream(_to_unicode_cmap({code: ch for ch, code in codes.items()}))
        widths = " ".join(["500"] * 255).encode()
        font = b.add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /FirstChar 1 /LastChar 255 "
            b"/Widths [" + widths + b"] /ToUnicode %d 0 R >>" % cmap
        )
    image = b.stream(
        b"\x80", b"/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 "
    )

    kids = []
    for page in pages:
        resources = b"<< "
        if font is not None:
            resources += b"/Font << /F1 %d 0 R >> " % font
        resources += b"/XObject << /Im1 %d 0 R >> >>" % image
        if isinstance(page, Text):
            ops = [b"BT /F1 12 Tf 14 TL 20 760 Td"]
            for index, line in enumerate(page.lines):
                hexed = "".join(f"{codes[ch]:02X}" for ch in line).encode()
                ops.append((b"" if index == 0 else b"T* ") + b"<" + hexed + b"> Tj")
            ops.append(b"ET")
            contents = b.stream(b"\n".join(ops))
        elif isinstance(page, ImageOnly):
            contents = b.stream(b"q 300 0 0 300 50 400 cm /Im1 Do Q")
        elif isinstance(page, Bomb):
            contents = b.stream(_deflated_spaces(page.inflated_bytes), b"/Filter /FlateDecode ")
        else:
            contents = b.stream(b"")
        kids.append(
            b.add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R /Resources %s >>"
                % (tree, contents, resources)
            )
        )
    b.set(catalog, b"<< /Type /Catalog /Pages %d 0 R >>" % tree)
    b.set(tree, b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids)))
    return b.render(catalog)


def _deflated_spaces(size: int) -> bytes:
    compressor = zlib.compressobj(9)
    chunk = b" " * (1024 * 1024)
    out = bytearray()
    remaining = size
    while remaining > 0:
        piece = chunk[: min(remaining, len(chunk))]
        out += compressor.compress(piece)
        remaining -= len(piece)
    out += compressor.flush()
    return bytes(out)


def encrypt_pdf(data: bytes, *, user_password: str = "", owner_password: str = "owner-secret") -> bytes:
    """The same document, encrypted (RC4-128, which pypdf writes without extra dependencies)."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(user_password=user_password, owner_password=owner_password, algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def malformed_pdf() -> bytes:
    """Starts and ends like a PDF (passes the upload checks) but has no readable structure."""
    return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog /Pages 9 0 R\nthis is not a pdf body\n%%EOF\n"
