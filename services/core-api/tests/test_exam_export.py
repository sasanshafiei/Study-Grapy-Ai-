"""Exports through the API (E22, E23) on real PostgreSQL: headers, content, confidentiality, archival stability,
drafts, limits and audit (EXP-01 ... EXP-04, EXP-08, EXP-09, EXP-10, EXP-12 ... EXP-15, EXP-17, VAL-05)."""

from __future__ import annotations

import io
import re
import uuid
import zipfile

import pytest

from app.exams import export, render
from tests import exam_support as ex
from tests.exam_support import ExamClient, match, mcq_body, short_body, tf_body
from tests.test_exam_render_unit import HOSTILE, docx_parts, pdf_active_content, pdf_raw_streams, pdf_text

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CANARIES = ("ANSWERONLY-EXPL-", "ANSWERONLY-MODEL-", "ANSWERONLY-RUBRIC-", "ANSWERONLY-SRC-")


@pytest.fixture
def exp(world, make_api):
    professor = world.user("professor")
    course = world.course(professor, name="General Chemistry", code="CHEM 101")
    document = world.document(course, title="ANSWERONLY-SRC-lecture.pdf", status="indexed", page_count=30)
    api = make_api(settings_overrides=ex.EXPORT_ENABLED).as_user(professor)
    return {"world": world, "api": api, "x": ExamClient(api, course), "c": api.client, "course": course,
            "document": document, "professor": professor, "make_api": make_api}  # fmt: skip


def _questions(x, document, tag="1", answers=("c", "true", "ANSWERONLY-MODEL-1")):
    ref = [{"document_id": str(document), "locator": {"page": 4}}]
    mcq = x.question(mcq_body(stem=f"Moles of oxygen {tag}?", explanation=f"ANSWERONLY-EXPL-{tag}", source_refs=ref,
                              answer_key={"choice_id": answers[0]}), approve=True)  # fmt: skip
    tf = x.question(tf_body(stem=f"Water boils at 100 °C {tag}.", answer_key={"choice_id": answers[1]},
                            explanation=f"ANSWERONLY-EXPL-tf{tag}"), approve=True)  # fmt: skip
    short = x.question(short_body(stem=f"Explain the limiting reactant {tag}.",
                                  answer_key={"model_answer": answers[2], "rubric": f"ANSWERONLY-RUBRIC-{tag}"},
                                  explanation=None), approve=True)  # fmt: skip
    return mcq, tf, short


def _final(x, document, title="Midterm 1 — Stoichiometry", **kwargs):
    exam = x.exam(title)
    for q, points in zip(_questions(x, document, **kwargs), (2, 1, 5)):
        exam = x.add(exam, q, points=points)
    return x.finalized(exam)


def _get(c, x, exam, variant, fmt, endpoint="/export"):
    return c.get(x.e_path(exam["id"], endpoint), params={"variant": variant, "format": fmt})


def _text(fmt, data) -> str:
    if fmt == "pdf":
        return pdf_text(data)
    return b" ".join(docx_parts(data).values()).decode("utf-8", "replace")


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
@pytest.mark.parametrize("variant", ["student", "answer_key"])
def test_exp01_exp02_headers_and_content(exp, fmt, variant):
    x, c = exp["x"], exp["c"]
    final = _final(x, exp["document"])
    r = _get(c, x, final, variant, fmt)
    assert r.status_code == 200
    assert r.headers["content-type"] == ("application/pdf" if fmt == "pdf" else DOCX)
    assert int(r.headers["content-length"]) == len(r.content)
    kind = "answer-key" if variant == "answer_key" else "student"
    v = final["finalized_version"]
    assert r.headers["content-disposition"] == (
        f'attachment; filename="CHEM-101-Midterm-1-Stoichiometry-v{v}-{kind}.{fmt}"; '
        f"filename*=UTF-8''CHEM-101-Midterm-1-%E2%80%94-Stoichiometry-v{v}-{kind}.{fmt}")
    assert r.headers["cache-control"] == "no-store, private" and r.headers["pragma"] == "no-cache"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["x-exam-content-sha256"] == final["content_sha256"]
    assert r.headers["x-exam-finalized-version"] == str(v) and r.headers["x-exam-renderer-version"] == "exam-render/1"
    text = _text(fmt, r.content)
    for expected in ("CHEM 101", "General Chemistry", "Midterm 1 — Stoichiometry", "Total points: 8", "Questions: 3",
                     "Duration: 90 minutes", "Moles of oxygen 1?", "A. 1.0 mol", "B. 2.0 mol", "C. 4.0 mol", "D. 8.0 mol",
                     "○ True", "○ False", "Explain the limiting reactant 1.", "(2 points)", "(1 point)", "(5 points)"):  # fmt: skip
        assert expected in text, expected
    if fmt == "pdf":
        assert re.search(r"Page 1 of \d+", text)
        assert text.index("Moles of oxygen") < text.index("Water boils") < text.index("Explain the limiting")
    if variant == "student":
        assert "Name:" in text and "Student ID:" in text
    else:
        assert "ANSWER KEY — CONFIDENTIAL" in text and final["content_sha256"][:12] in text
        flat = text.replace("\n", " ")
        labelled = ("Answer: C — 4.0 mol", "Answer: True") if fmt == "pdf" else ("C — 4.0 mol", "Answer: ")  # DOCX: label run + value run
        for expected in (*labelled, "ANSWERONLY-MODEL-1", "ANSWERONLY-RUBRIC-1", "ANSWERONLY-EXPL-1",
                         "ANSWERONLY-SRC-lecture.pdf, p. 4"):  # fmt: skip
            assert expected in flat, expected


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_exp03_exp15_student_file_has_no_answer_only_content_anywhere(exp, fmt):
    x, c = exp["x"], exp["c"]
    final = _final(x, exp["document"])
    data = _get(c, x, final, "student", fmt).content
    everything = pdf_raw_streams(data) if fmt == "pdf" else b"".join(docx_parts(data).values())
    for canary in CANARIES:
        assert canary.encode() not in everything, canary
    text = _text(fmt, data)
    for marker in ("ANSWER KEY", "Answer:", "Model answer", "Rubric", "Explanation", "Sources", "correct", "✓", "✔"):
        assert marker not in text, marker
    for choice in ("1.0 mol", "2.0 mol", "4.0 mol", "8.0 mol"):  # every choice text, the correct one included (by design)
        assert choice in text
    if fmt == "pdf":
        assert pdf_active_content(data) == set()


def test_exp04_choices_share_one_style(exp):
    import pypdf

    x, c = exp["x"], exp["c"]
    final = _final(x, exp["document"])
    pdf = _get(c, x, final, "student", "pdf").content
    seen = {}

    def visit(text, cm, tm, font, size):
        match_ = re.match(r"^([A-D])\. \d\.0 mol", text.strip())
        if match_:
            seen[match_.group(1)] = (font.get("/BaseFont") if font else None, round(size, 2))

    for page in pypdf.PdfReader(io.BytesIO(pdf)).pages:
        page.extract_text(visitor_text=visit)
    assert set(seen) == {"A", "B", "C", "D"} and len(set(seen.values())) == 1
    document = docx_parts(_get(c, x, final, "student", "docx").content)["word/document.xml"].decode()
    paragraphs = re.findall(r"<w:p>(?:(?!</w:p>).)*?[A-D]\. \d\.0 mol(?:(?!</w:p>).)*</w:p>", document, re.S)
    assert len(paragraphs) == 4
    shapes = {re.sub(r"<w:t[^>]*>[^<]*</w:t>", "", p) for p in paragraphs}
    assert len(shapes) == 1, shapes


def test_exp14_student_files_do_not_depend_on_answers(exp):
    x, c = exp["x"], exp["c"]
    one = _final(x, exp["document"], tag="1", answers=("c", "true", "ANSWERONLY-MODEL-1"))
    # identical student-visible content, different answer keys/explanations/model answers/rubrics/sources
    world = exp["world"]
    other_doc = world.document(exp["course"], title="ANSWERONLY-SRC-other.pdf", status="indexed", page_count=30)
    exam = x.exam("Midterm 1 — Stoichiometry")
    mcq = x.question(mcq_body(stem="Moles of oxygen 1?", explanation="ANSWERONLY-EXPL-other", answer_key={"choice_id": "a"},
                              source_refs=[{"document_id": str(other_doc), "locator": {"page": 9}}]), approve=True)  # fmt: skip
    tf = x.question(tf_body(stem="Water boils at 100 °C 1.", answer_key={"choice_id": "false"}, explanation=None), approve=True)
    short = x.question(short_body(stem="Explain the limiting reactant 1.", explanation="ANSWERONLY-EXPL-x",
                                  answer_key={"model_answer": "ANSWERONLY-MODEL-other", "rubric": None}), approve=True)  # fmt: skip
    for q, points in zip((mcq, tf, short), (2, 1, 5)):
        exam = x.add(exam, q, points=points)
    two = x.finalized(exam)
    assert one["content_sha256"] != two["content_sha256"]
    assert _get(c, x, one, "student", "pdf").content == _get(c, x, two, "student", "pdf").content
    a, b = (docx_parts(_get(c, x, e, "student", "docx").content) for e in (one, two))
    assert {k: v for k, v in a.items() if k != "docProps/core.xml"} == {k: v for k, v in b.items() if k != "docProps/core.xml"}
    strip = re.compile(rb"<dcterms:(created|modified)[^<]*</dcterms:\1>|<cp:lastPrinted>[^<]*</cp:lastPrinted>")
    assert strip.sub(b"", a["docProps/core.xml"]) == strip.sub(b"", b["docProps/core.xml"])  # only the finalization time differs
    settings, factory = exp["api"].settings, exp["api"].session_factory
    models = [export.prepare(factory, settings, exp["api"].principal, exp["course"], uuid.UUID(e["id"]), "student", "docx",
                             preview_file=False)["model"] for e in (one, two)]  # fmt: skip
    assert models[0] == models[1]
    rendered = [render.render("docx", m, export.limits_for(settings, "docx"), timestamp="2026-11-21T10:00:00Z") for m in models]
    assert rendered[0].data == rendered[1].data


def test_exp17_exp08_archival_and_later_bank_changes_change_nothing(exp):
    x, c, world = exp["x"], exp["c"], exp["world"]
    final = _final(x, exp["document"])
    before = {(v, f): _get(c, x, final, v, f) for v in ("student", "answer_key") for f in ("pdf", "docx")}
    for item in final["items"]:  # edit and re-approve every bank question, rename the document, rename the course
        q = c.get(x.q_path(item["question_id"])).json()
        body = {"type": q["type"], "difficulty": q["difficulty"], "bloom_level": q["bloom_level"], "topic_id": None,
                "stem": q["stem"] + " (edited)", "choices": q["choices"], "answer_key": q["answer_key"],
                "explanation": q["explanation"], "source_refs": [{"document_id": r["document_id"], "locator": r["locator"]}
                                                                  for r in q["source_refs"]]}  # fmt: skip
        x.approve(x.edit(q, body))
    world.execute("UPDATE documents SET title = 'Renamed.pdf' WHERE id = :d", d=exp["document"])
    world.execute("UPDATE courses SET name = 'Renamed', code = 'NEW 1' WHERE id = :c", c=exp["course"])
    archived = c.post(x.e_path(final["id"], "/archive"), headers=match(final["version"])).json()
    assert archived["version"] == final["version"] + 1
    for (variant, fmt), old in before.items():
        new = _get(c, x, archived, variant, fmt)
        assert new.status_code == 200 and new.content == old.content, (variant, fmt)
        for header in ("content-disposition", "x-exam-finalized-version", "x-exam-content-sha256", "x-exam-renderer-version"):
            assert new.headers[header] == old.headers[header]
    assert x.get(archived)["content_sha256"] == final["content_sha256"]
    from app.exams import snapshot

    with exp["api"].session_factory() as session:
        course = session.execute(__import__("sqlalchemy").text("SELECT id, owner_id, code, name FROM courses WHERE id = :c"),
                                 {"c": exp["course"]}).one()  # fmt: skip
        state = snapshot.load_exam(session, exp["course"], uuid.UUID(final["id"]), course)
        assert snapshot.content_sha256(state) == final["content_sha256"]


def test_exp09_drafts_preview_files_and_archived_drafts(exp):
    x, c, world = exp["x"], exp["c"], exp["world"]
    draft = x.exam("Draft exam")
    for q in _questions(x, exp["document"]):
        draft = x.add(draft, q)
    r = _get(c, x, draft, "student", "pdf")
    assert r.status_code == 409 and r.json()["code"] == "exam_not_finalized"
    preview = _get(c, x, draft, "student", "pdf", "/preview-file")
    assert preview.status_code == 200 and preview.headers["x-exam-version"] == str(draft["version"])
    assert preview.headers["x-exam-preview-fingerprint"] == x.preview(draft)["preview_fingerprint"]
    assert "x-exam-finalized-version" not in preview.headers
    assert preview.headers["content-disposition"].startswith(f'attachment; filename="CHEM-101-Draft-exam-draft-v{draft["version"]}-student.pdf"')
    import pypdf

    for page in pypdf.PdfReader(io.BytesIO(preview.content)).pages:
        assert f"DRAFT — version {draft['version']} — not for distribution" in page.extract_text()
    docx = _get(c, x, draft, "answer_key", "docx", "/preview-file")
    assert docx.status_code == 200 and b"DRAFT" in docx_parts(docx.content)["word/header1.xml"]
    assert world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action = 'preview_file_downloaded'",
                        e=uuid.UUID(draft["id"])) == 2  # fmt: skip
    final = x.finalized(draft)
    official = _get(c, x, final, "student", "pdf")
    same = _get(c, x, final, "student", "pdf", "/preview-file")
    assert same.content == official.content and same.headers["x-exam-finalized-version"] == str(final["finalized_version"])
    archived_draft = c.post(x.e_path(x.exam("Old")["id"], "/archive"), headers=match(1)).json()
    for endpoint in ("/export", "/preview-file"):
        r = _get(c, x, archived_draft, "student", "pdf", endpoint)
        assert r.status_code == 409 and r.json()["code"] == "exam_not_finalized"
    for params in ({"variant": "student"}, {"format": "pdf"}, {"variant": "teacher", "format": "pdf"},
                   {"variant": "student", "format": "odt"}):  # fmt: skip
        r = c.get(x.e_path(final["id"], "/export"), params=params)
        assert r.status_code == 422 and r.json()["code"] == "invalid_export_option"
    r = c.get(x.e_path(final["id"], "/export") + "?variant=student&format=pdf&x=1")
    assert r.status_code == 422 and r.json()["code"] == "invalid_query"


def test_exp09_draft_above_the_budget_cannot_be_previewed(exp):
    api = exp["make_api"](settings_overrides={**ex.EXPORT_ENABLED, "exam_export_max_content_chars": 1000}).as_user(exp["professor"])
    x = ExamClient(api, exp["course"])
    q = x.question(mcq_body(stem="s" * 900, explanation=None))
    exam = x.add(x.exam("T", instructions=None), q)
    x.edit(q, mcq_body(stem="s" * 990, explanation=None))  # a bank edit may push a draft above the budget
    r = api.client.get(x.e_path(exam["id"], "/preview-file"), params={"variant": "student", "format": "pdf"})
    assert r.status_code == 409 and r.json()["code"] == "export_budget_exceeded"


def test_exp10_exp12_limits_failures_and_audit(exp, monkeypatch):
    x, c, world = exp["x"], exp["c"], exp["world"]
    final = _final(x, exp["document"])

    def audits():
        return world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action = 'export_downloaded'",
                            e=uuid.UUID(final["id"]))  # fmt: skip

    ok = _get(c, x, final, "answer_key", "pdf")
    assert ok.status_code == 200 and audits() == 1
    event = world.rows("SELECT details, entity_version FROM exam_audit_events WHERE entity_id = :e AND action = 'export_downloaded'",
                       e=uuid.UUID(final["id"]))[0]  # fmt: skip
    import hashlib

    assert event["details"] == {"variant": "answer_key", "format": "pdf", "bytes": len(ok.content),
                                "file_sha256": hashlib.sha256(ok.content).hexdigest(), "renderer_version": "exam-render/1"}  # fmt: skip
    for kind, status, code in (("timeout", 504, "export_timeout"), ("failed", 500, "export_failed")):
        def fail(*args, kind=kind, **kwargs):
            raise render.RenderFailed(kind, "injected")

        monkeypatch.setattr(export, "render", fail)
        r = _get(c, x, final, "student", "pdf")
        assert r.status_code == status and r.json()["code"] == code and r.headers["cache-control"] == "no-store"
    monkeypatch.undo()
    assert audits() == 1  # a failed render writes no event
    lease = exp["api"].app.state.exam_export_limiter.try_acquire(exp["professor"].id)
    busy = _get(c, x, final, "student", "pdf")
    lease.release()
    assert busy.status_code == 429 and busy.json()["code"] == "export_busy" and busy.headers["retry-after"] == "2"
    limited = exp["make_api"](settings_overrides={**ex.EXPORT_ENABLED, "exam_export_rate_limit": 1}).as_user(exp["professor"])
    lx = ExamClient(limited, exp["course"])
    assert _get(limited.client, lx, final, "student", "pdf").status_code == 200
    r = _get(limited.client, lx, final, "student", "pdf")
    assert r.status_code == 429 and r.json()["code"] == "too_many_exports" and int(r.headers["retry-after"]) >= 1


def test_exp13_export_switch_off_keeps_json_preview(exp):
    api = exp["make_api"](settings_overrides=ex.ENABLED).as_user(exp["professor"])
    x = ExamClient(api, exp["course"])
    final = _final(ExamClient(exp["api"], exp["course"]), exp["document"])
    r = api.client.get(x.e_path(final["id"], "/export"), params={"variant": "student", "format": "pdf"})
    assert r.status_code == 503 and r.json()["code"] == "exam_export_unavailable"
    assert api.client.get(x.e_path(final["id"], "/preview")).status_code == 200


def test_val05_hostile_text_is_stored_and_rendered_literally(exp):
    x, c = exp["x"], exp["c"]
    stem = "Hostile? " + " ".join(HOSTILE[:4]) + " " + "x" * 3000
    q = x.question(mcq_body(stem=stem, choices=[{"id": "a", "text": "<b>bold?</b>"}, {"id": "b", "text": "&amp;"}],
                            answer_key={"choice_id": "a"}, explanation="<img src=x onerror=alert(1)>"), approve=True)  # fmt: skip
    assert q["stem"] == stem
    final = x.finalized(x.add(x.exam("<script>T</script>"), q))
    for fmt in ("pdf", "docx"):
        data = _get(c, x, final, "answer_key", fmt).content
        if fmt == "pdf":
            assert pdf_active_content(data) == set()
            text = pdf_text(data).replace("\n", "")
            assert '<img src="file:///etc/passwd"/>' in text and "<b>bold?</b>" in text and "<script>T</script>" in text
        else:
            document = docx_parts(data)["word/document.xml"]
            assert b"&lt;b&gt;bold?&lt;/b&gt;" in document and b"<script>" not in document
            assert zipfile.ZipFile(io.BytesIO(data)).testzip() is None
