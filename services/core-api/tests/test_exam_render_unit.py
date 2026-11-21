"""The renderer subprocess and its outputs, without a database (EXP-05/06/07/08/10/11/15/16, fonts, filenames).

EXP-06's audit-hook evidence is behavioural evidence for the tested inputs only; it is NOT an isolation
guarantee (the child shares UID, filesystem and network with core-api; docs/exam-export.md).
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.exams import render as rd
from app.exams import snapshot
from app.exams.export import AnswerKeyRenderModel, StudentRenderModel
from app.exams.filenames import content_disposition

LIMITS = rd.RenderLimits(30, 25, 512 * 1024 * 1024, 2 * 1024 * 1024, 16 * 1024 * 1024)
DOCX_LIMITS = rd.RenderLimits(30, 25, 512 * 1024 * 1024, 2 * 1024 * 1024, 8 * 1024 * 1024)
TS = "2026-11-21T10:00:00Z"
HOSTILE = ['<img src="file:///etc/passwd"/>', '<a href="javascript:alert(1)">x</a>', '<font name="Courier">f</font>',
           "&#0; &amp; <![CDATA[x]]>", "<script>alert(1)</script>", "**md** [link](http://example.com)",
           "RTL ‮evil‬ zero​width", "tab\there"]  # fmt: skip
UNICODE = "ğşıİçöü αβγΔΩ H₂O 10⁻³ → ⇌ ° ± ≤ ≥ µ"


def _header(n=3, title="Midterm 1 — Stoichiometry"):
    return {"course_code": "CHEM 101", "course_name": "General Chemistry", "title": title,
            "instructions": "Answer all.\nCalculators allowed.", "duration_minutes": 90, "question_count": n,
            "total_points": n}  # fmt: skip


def _questions():
    return [
        {"number": 1, "points": 1, "type": "mcq", "stem": "Pick one: " + UNICODE, "answer_lines": None,
         "options": [{"label": "A", "text": "Alpha CHOICE-A"}, {"label": "B", "text": "Beta CHOICE-B"}]},
        {"number": 2, "points": 1, "type": "true_false", "stem": "Hostile: " + " | ".join(HOSTILE), "answer_lines": None,
         "options": [{"label": None, "text": "True"}, {"label": None, "text": "False"}]},
        {"number": 3, "points": 1, "type": "short", "stem": "Explain.", "options": None, "answer_lines": 6},
    ]  # fmt: skip


def student_model(**overrides):
    return {"variant": "student", "header": _header(), "questions": _questions(), "draft_version": None,
            "content_sha12": None, **overrides}  # fmt: skip


def answer_model(**overrides):
    answers = [
        {"number": 1, "points": 1, "answer": "B", "answer_text": "Beta CHOICE-B", "model_answer": None, "rubric": None,
         "explanation": "ANSWERONLY-EXPL-1", "sources": ["ANSWERONLY-SRC-1.pdf, p. 6"]},
        {"number": 2, "points": 1, "answer": "True", "answer_text": None, "model_answer": None, "rubric": None,
         "explanation": None, "sources": []},
        {"number": 3, "points": 1, "answer": None, "answer_text": None, "model_answer": "ANSWERONLY-MODEL-3",
         "rubric": "ANSWERONLY-RUBRIC-3", "explanation": None, "sources": []},
    ]  # fmt: skip
    return {"variant": "answer_key", "header": _header(), "questions": _questions(), "answer_key": answers,
            "draft_version": None, "content_sha12": "0123456789ab", **overrides}  # fmt: skip


def _render(fmt, model, **kwargs):
    return rd.render(fmt, model, LIMITS if fmt == "pdf" else DOCX_LIMITS, timestamp=TS, **kwargs)


def pdf_text(data: bytes) -> str:
    import pypdf

    return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(io.BytesIO(data)).pages)


def pdf_raw_streams(data: bytes) -> bytes:
    """Every object of the file, streams decompressed (metadata and document info included)."""
    import pypdf
    from pypdf.generic import IndirectObject, StreamObject

    reader = pypdf.PdfReader(io.BytesIO(data))
    out = bytearray(data)
    for number in range(1, reader.trailer["/Size"]):
        try:
            obj = reader.get_object(IndirectObject(number, 0, reader))
        except Exception:  # noqa: BLE001 - free or compressed entries
            continue
        if isinstance(obj, StreamObject):
            out += obj.get_data()
        out += repr(obj).encode("utf-8", "replace")
    return bytes(out)


def pdf_active_content(data: bytes) -> set[str]:
    import pypdf
    from pypdf.generic import DictionaryObject, IndirectObject

    reader = pypdf.PdfReader(io.BytesIO(data))
    found = set()
    for number in range(1, reader.trailer["/Size"]):
        try:
            obj = reader.get_object(IndirectObject(number, 0, reader))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, DictionaryObject):
            for name in ("/JS", "/JavaScript", "/OpenAction", "/AA", "/Launch", "/URI", "/EmbeddedFile", "/EmbeddedFiles",
                         "/RichMedia", "/Annots", "/AcroForm", "/XFA", "/GoToR", "/SubmitForm"):  # fmt: skip
                if name in obj:
                    found.add(name)
            if obj.get("/Subtype") == "/Image":
                found.add("/Image")
    return found


def docx_parts(data: bytes) -> dict[str, bytes]:
    archive = zipfile.ZipFile(io.BytesIO(data))
    return {name: archive.read(name) for name in archive.namelist()}


ALLOWED_DOCX_PARTS = {
    "[Content_Types].xml", "_rels/.rels", "docProps/core.xml", "docProps/app.xml", "word/document.xml",
    "word/_rels/document.xml.rels", "word/styles.xml", "word/stylesWithEffects.xml", "word/settings.xml",
    "word/webSettings.xml", "word/fontTable.xml", "word/numbering.xml", "word/theme/theme1.xml", "word/header1.xml",
    "word/footer1.xml",
}  # fmt: skip


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
@pytest.mark.parametrize("variant", ["student", "answer_key"])
def test_exp05_no_active_or_external_content(fmt, variant):
    data = _render(fmt, student_model() if variant == "student" else answer_model()).data
    if fmt == "pdf":
        assert data.startswith(b"%PDF-") and pdf_active_content(data) == set()
        return
    parts = docx_parts(data)
    assert set(parts) <= ALLOWED_DOCX_PARTS, set(parts) - ALLOWED_DOCX_PARTS
    joined = b"".join(parts.values())
    # Elements, relationships and field codes (not mere words: the template's colour mapping says w:hyperlink="hyperlink"
    # and its latent styles name "Macro Text").
    for forbidden in (b"vbaProject", b'TargetMode="External"', b"altChunk", b"oleObject", b"OLEObject", b"<w:object",
                      b"INCLUDETEXT", b"INCLUDEPICTURE", b" HYPERLINK", b"<w:hyperlink", b"thumbnail", b"customXml",
                      b"macroEnabled", b"<w:drawing", b"<w:pict", b"<v:imagedata", b"r:link=", b"<w:instrText"):  # fmt: skip
        assert forbidden not in joined, forbidden
    instructions = re.findall(rb'w:instr="([^"]*)"', joined)
    assert sorted(set(instructions)) == [b"NUMPAGES", b"PAGE"]
    core = parts["docProps/core.xml"].decode()
    assert "<dc:creator></dc:creator>" in core or "<dc:creator/>" in core
    assert "@" not in core and "2026-11-21T10:00:00Z" in core
    assert parts["[Content_Types].xml"].count(b"macroEnabled") == 0
    assert zipfile.ZipFile(io.BytesIO(data)).infolist()[0].date_time == (1980, 1, 1, 0, 0, 0)


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_exp06_val05_markup_is_literal_and_no_unexpected_file_or_socket_use(fmt):
    rendered = _render(fmt, student_model(), audit=True)
    if fmt == "pdf":
        text = pdf_text(rendered.data)
        for payload in ('<img src="file:///etc/passwd"/>', '<a href="javascript:alert(1)">x</a>', '<font name="Courier">f</font>'):
            assert payload in text.replace("\n", " ")
        assert pdf_active_content(rendered.data) == set()
        assert b"/Courier" not in pdf_raw_streams(rendered.data)
    else:
        document = docx_parts(rendered.data)["word/document.xml"].decode()
        assert "&lt;img src=\"file:///etc/passwd\"/&gt;" in document and "<img" not in document
    fonts = {str(Path(p)) for p in rd.font_paths().values()}
    prefixes = (sys.prefix, sys.base_prefix, "/usr/local/lib/python3", "/usr/lib/python3")
    opened = [arg for event, arg in rendered.audit_events if event == "open"]
    unexpected = [p for p in opened if p not in fonts and not p.startswith(prefixes) and not re.fullmatch(r"\d+", p)]
    assert unexpected == [], unexpected
    assert not [e for e in rendered.audit_events if e[0].startswith("socket.")]
    assert all("/etc/" not in p and "/proc/" not in p and "/app/" not in p.replace(sys.prefix, "") or p in fonts
               for p in opened)  # fmt: skip


def test_exp07_unicode_survives_and_unsupported_characters_are_detected():
    text = pdf_text(_render("pdf", student_model()).data)
    for token in UNICODE.split():
        assert token in text, token
    document = docx_parts(_render("docx", student_model()).data)["word/document.xml"].decode()
    assert UNICODE in document
    assert not snapshot.unsupported(UNICODE)
    assert snapshot.unsupported("CJK 漢字") and snapshot.unsupported("Hangul 한") and not snapshot.unsupported("emoji \U0001F600")


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_exp08_same_model_same_bytes_in_separate_processes(fmt):
    first, second = _render(fmt, answer_model()), _render(fmt, answer_model())
    assert first.sha256 == second.sha256 and first.data == second.data
    other = _render(fmt, answer_model(content_sha12="ffffffffffff"))
    assert other.sha256 != first.sha256


def test_exp09_draft_watermark_on_every_page():
    long_questions = [{**q, "stem": q["stem"] + ("\nline" * 300)} for q in _questions()]
    data = _render("pdf", student_model(questions=long_questions, draft_version=7)).data
    import pypdf

    pages = pypdf.PdfReader(io.BytesIO(data)).pages
    assert len(pages) > 3
    for page in pages:
        assert "DRAFT — version 7 — not for distribution" in page.extract_text()
    header = docx_parts(_render("docx", student_model(draft_version=7)).data)["word/header1.xml"].decode()
    assert "DRAFT — version 7 — not for distribution" in header


def test_exp11_long_questions_split_and_unbroken_tokens_wrap():
    giant = [{"number": 1, "points": 1, "type": "short", "stem": "w" * 4000 + "\n" + "word " * 700, "options": None,
              "answer_lines": 6}]  # fmt: skip
    data = _render("pdf", student_model(questions=giant, header=_header(1))).data
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(data))
    assert len(reader.pages) >= 2
    assert pdf_text(data).replace("\n", "").count("w") >= 4000


def test_exp15_student_render_model_cannot_carry_answer_fields():
    StudentRenderModel.model_validate(student_model())
    for extra in ({"answer_key": []}, {"content_sha12": "abc"}):
        with pytest.raises(ValidationError):
            StudentRenderModel.model_validate(student_model(**extra))
    bad_question = {**_questions()[0], "answer": "B"}
    with pytest.raises(ValidationError):
        StudentRenderModel.model_validate(student_model(questions=[bad_question]))
    with pytest.raises(ValidationError):
        StudentRenderModel.model_validate(answer_model())
    AnswerKeyRenderModel.model_validate(answer_model())
    import inspect

    source = inspect.getsource(snapshot.student_print_rows)
    selected = re.findall(r"SELECT (.*?) FROM", source)
    assert selected and all("answer" not in s and "explanation" not in s and "source" not in s and "citation" not in s
                            for s in selected)  # fmt: skip


def test_exp10_wall_clock_kill_crash_cpu_and_output_caps():
    slow = [sys.executable, "-I", "-c", "import time; time.sleep(30)"]
    limits = rd.RenderLimits(1.0, 25, 512 * 1024 * 1024, 2 * 1024 * 1024, 1024)
    started = time.monotonic()
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", student_model(), limits, timestamp=TS, command=slow)
    assert raised.value.kind == "timeout" and time.monotonic() - started < 10
    crash = [sys.executable, "-I", "-c", "import sys; sys.stdin.read(); sys.exit(1)"]
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", student_model(), limits, timestamp=TS, command=crash)
    assert raised.value.kind == "failed" and raised.value.reason == "exit_1"
    flood = [sys.executable, "-I", "-c", "import sys; sys.stdin.read(); sys.stdout.write('x' * 100000)"]
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", student_model(), limits, timestamp=TS, command=flood)
    assert raised.value.reason == "output_too_large"
    spin = [sys.executable, "-I", "-c", "import resource,sys; sys.stdin.read(); resource.setrlimit(resource.RLIMIT_CPU, (1, 2))\nwhile True: pass"]
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", student_model(), rd.RenderLimits(20, 25, 512 * 1024 * 1024, 2 * 1024 * 1024, 1024),
                  timestamp=TS, command=spin)  # fmt: skip
    assert raised.value.kind == "timeout" and raised.value.reason == "cpu_limit"
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", student_model(), rd.RenderLimits(30, 25, 512 * 1024 * 1024, 100, 1024), timestamp=TS)
    assert raised.value.reason == "input_too_large"
    tiny = rd.RenderLimits(30, 25, 512 * 1024 * 1024, 2 * 1024 * 1024, 2000)
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", student_model(), tiny, timestamp=TS)
    assert raised.value.reason in ("too_large", "output_too_large")
    bad = student_model()
    bad["questions"][0]["answer"] = "B"  # the child re-checks the shape too
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", bad, LIMITS, timestamp=TS)
    assert raised.value.reason == "invalid_model"


def test_renderer_pins_fonts_coverage_and_licences():
    for name, digest in rd.FONT_SHA256.items():
        assert hashlib.sha256((rd.FONTS / name).read_bytes()).hexdigest() == digest, name
    licence = (rd.FONTS / "LICENSE").read_text()
    assert "Bitstream Vera" in licence and "DejaVu changes are in public domain" in licence
    for package, version in rd.LIBRARY_PINS.items():
        assert importlib.metadata.version(package) == version, package
    coverage = json.loads(Path(snapshot._COVERAGE).read_text())
    assert coverage["fonts"] == rd.FONT_SHA256
    from reportlab.pdfbase.ttfonts import TTFontFile

    common = None
    for name in rd.FONT_FILES.values():
        cps = {cp for cp, glyph in TTFontFile(str(rd.FONTS / name)).charToGlyph.items() if glyph}
        common = cps if common is None else common & cps
    covered = {cp for a, b in coverage["ranges"] for cp in range(a, b + 1)}
    assert covered == common
    assert re.fullmatch(r"exam-render/\d+", rd.RENDERER_VERSION)
    from app.exams import render_child

    assert render_child.RENDERER_VERSION == rd.RENDERER_VERSION


@pytest.mark.parametrize("course,title,version,variant,fmt,draft,ascii_name,encoded", [
    ("CHEM 101", "Midterm 1 — Stoichiometry and Gases", 9, "answer_key", "pdf", False,
     "CHEM-101-Midterm-1-Stoichiometry-and-Gases-v9-answer-key.pdf",
     "CHEM-101-Midterm-1-%E2%80%94-Stoichiometry-and-Gases-v9-answer-key.pdf"),
    ("KİM 101", "Sınav: ğ/ş\\\"x;y", 2, "student", "docx", True, "K-M-101-S-nav-x-y-draft-v2-student.docx",
     "K%C4%B0M-101-S%C4%B1nav%3A-%C4%9F%C5%9Fxy-draft-v2-student.docx"),
    ("", "../../etc/passwd", 3, "student", "pdf", False, "etc-passwd-v3-student.pdf", "etcpasswd-v3-student.pdf"),
    ("", "———", 1, "student", "pdf", False, "exam-v1-student.pdf", "———-v1-student.pdf".replace("———", "%E2%80%94%E2%80%94%E2%80%94")),
])  # fmt: skip
def test_exp01_content_disposition(course, title, version, variant, fmt, draft, ascii_name, encoded):
    value = content_disposition(course, title, version, variant, fmt, draft=draft)
    assert value == f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"
    assert "/" not in ascii_name and "\\" not in value.split("filename*")[0]
    long = content_disposition("C" * 32, "T" * 200, 12, "answer_key", "docx", draft=False)
    assert len(re.search(r'filename="([^"]+)"', long).group(1)) <= 120 and long.split('"')[1].endswith("-v12-answer-key.docx")


# --- EXP-16 (mandatory, release-blocking): maximum-boundary renders, recorded -------------------------------------

BUDGET = 300_000


def _boundary_models(shape: str):
    """Print models at exactly the export budget, in the shape the API produces (header 'C', 'N', 'T')."""
    header_chars = 3
    if shape == "byte_maximal":  # 4-byte (astral) code points: the largest render model
        per = (BUDGET - header_chars) // 200
        stems = ["\U0001D400" * per] * 200
        rest = BUDGET - header_chars - per * 200
        stems[0] += "\U0001D400" * rest
        items = [("short", s, None, None, None, None) for s in stems]
    elif shape == "layout_maximal":  # narrow ASCII, long unbroken tokens and line breaks: the most pages
        per = (BUDGET - header_chars) // 200
        half = per // 2
        stem = ("i" * half + "\n" + ("l\n" * ((per - half - 1) // 2)))[:per]
        items = [("short", stem.ljust(per, "i"), None, None, None, None)] * 200
    elif shape == "line_break_maximal":  # beyond the plan: one line break per two code points (the slowest PDF found)
        per = (BUDGET - header_chars) // 200
        stem = ("a\n" * (per // 2 + 1))[:per].rstrip("\n").ljust(per, "a")
        items = [("short", stem, None, None, None, None)] * 200
    else:  # short_answer_maximal: every field at its own limit, as many as fit, the rest minimal
        items, left = [], BUDGET - header_chars
        for index in range(200):
            remaining_items = 200 - index - 1
            room = left - remaining_items * 4  # keep 4 code points (stem 'q', model 'm', two source chars) per later item
            parts = []
            for limit in (4000, 4000, 4000, 4000):
                take = max(1, min(limit, room - sum(parts) - (4 - len(parts) - 1)))
                parts.append(take)
            stem_n, model_n, rubric_n, expl_n = parts
            items.append(("short", "S\n" * (stem_n // 2) + "S" * (stem_n % 2), "M" * model_n, "R" * rubric_n, "E" * expl_n, None))
            left -= sum(parts)
        assert left >= 0
    questions, answers, total = [], [], header_chars
    for number, (qtype, stem, model_answer, rubric, explanation, _) in enumerate(items, start=1):
        total += len(stem) + len(model_answer or "") + len(rubric or "") + len(explanation or "")
        questions.append({"number": number, "points": 1, "type": qtype, "stem": stem, "options": None, "answer_lines": 6})
        answers.append({"number": number, "points": 1, "answer": None, "answer_text": None,
                        "model_answer": model_answer or "m", "rubric": rubric, "explanation": explanation, "sources": []})  # fmt: skip
    header = {"course_code": "C", "course_name": "N", "title": "T", "instructions": None, "duration_minutes": 60,
              "question_count": len(questions), "total_points": len(questions)}  # fmt: skip
    student = {"variant": "student", "header": header, "questions": questions, "draft_version": None, "content_sha12": None}
    answer_key = {**student, "variant": "answer_key", "answer_key": answers, "content_sha12": "0" * 12}
    return student, answer_key, total


@pytest.mark.parametrize("shape", ["byte_maximal", "layout_maximal", "short_answer_maximal", "line_break_maximal"])
def test_exp16_maximum_boundary_renders_stay_below_half_of_every_limit(shape):
    student, answer_key, chars = _boundary_models(shape)
    assert BUDGET - 300 <= chars <= BUDGET, chars
    report = []
    for fmt in ("pdf", "docx"):
        for model in (student, answer_key):
            limits = rd.RenderLimits(30, 25, 512 * 1024 * 1024, 2 * 1024 * 1024,
                                     (16 if fmt == "pdf" else 8) * 1024 * 1024)  # fmt: skip
            payload = len(json.dumps(model, ensure_ascii=False).encode("utf-8"))
            assert payload <= 1_408_896  # the architecture's worst-case render-model bound (< 2 MiB input cap)
            rendered = rd.render(fmt, model, limits, timestamp=TS)
            pages = None
            if fmt == "pdf":
                import pypdf

                pages = len(pypdf.PdfReader(io.BytesIO(rendered.data)).pages)
            entry = {"shape": shape, "format": fmt, "variant": model["variant"], "content_chars": chars, "input_bytes": payload,
                     "output_bytes": len(rendered.data), "pages": pages, "wall_seconds": round(rendered.wall_seconds, 3),
                     "cpu_seconds": round(rendered.cpu_seconds, 3), "max_rss_mib": round(rendered.max_rss_kib / 1024, 1)}  # fmt: skip
            report.append(entry)
            print("EXP-16", json.dumps(entry, sort_keys=True))
            assert rendered.wall_seconds < 0.5 * limits.timeout_seconds, entry
            assert rendered.cpu_seconds < 0.5 * limits.cpu_seconds, entry
            assert rendered.max_rss_kib * 1024 < 0.5 * limits.memory_bytes, entry
            assert len(rendered.data) < 0.5 * limits.max_output_bytes, entry
    target = os.environ.get("EXP16_REPORT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            for entry in report:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
