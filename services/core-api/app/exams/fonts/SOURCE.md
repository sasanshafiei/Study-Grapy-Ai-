# Vendored fonts: provenance and integrity

| File | SHA-256 | Size (bytes) |
|---|---|---|
| DejaVuSans.ttf | 57f73e11f51999432bf7ab22ce55b6f945d5eca1bf824404cfa9ec2e3718c84e | 759720 |
| DejaVuSans-Bold.ttf | a4c5bc453ca281d90ea079e596da7ae0dfeb5777497c29ec254e76d97ff6f890 | 708920 |
| DejaVuSans-Oblique.ttf | e2f09289f4276309a36b9a93e5a0ac64957ef3eb7158151b243d41f667151ee4 | 637648 |

Obtained on 2026-11-21 from the Debian 13 (trixie) packages `fonts-dejavu-core` and `fonts-dejavu-extra`,
version 2.37-8 (source package `fonts-dejavu`), installed with apt in a disposable `python:3.11-slim`
container. `dpkg --verify` passed for both packages and the MD5 sums of the three files equal the
package manifests (`/var/lib/dpkg/info/fonts-dejavu-{core,extra}.md5sums`):
4f29244f3b64c6978e77a4baffd6b6b5 (Bold), 94d8e21951bb4eb050c779a4318f3dc0 (Regular),
27e2d8c5a620798a34fcfe1c2496ff56 (Oblique).

Licence: Bitstream Vera Fonts licence with DejaVu changes in the public domain (see LICENSE). It permits
redistribution, also inside a larger software package; modified fonts must not use the names "Bitstream"
or "Vera". These files are not modified.

The SHA-256 values are pinned in app/exams/render.py (FONT_SHA256) and verified by
tests/test_exam_render_unit.py; `glyph_coverage.json` (code points every face maps) is derived from these
exact files and verified against them by the same test.
