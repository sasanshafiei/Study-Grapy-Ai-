"""Render one exam print model to PDF or DOCX. Runs as its own process; never imported by the app.

    python -I render_child.py --parent-pid <pid>     (render model as JSON on stdin, the file on stdout)

Started by render.py WITHOUT a Python preexec_fn (unsafe in the multithreaded API process: the child could
deadlock between fork and exec), so the Linux parent-death signal is set here, as the very first action
after exec: prctl(PR_SET_PDEATHSIG, SIGKILL), then os.getppid() is compared with --parent-pid. If the parent
died before the signal was armed, the child has already been re-parented and exits at once (code 122); if
it dies afterwards, the kernel kills the child. The window between exec and prctl is interpreter start-up
only (no input is read, nothing is imported but the standard library). Linux delivers the signal when the
parent THREAD that started the child ends; render.py's calling thread blocks until the child is reaped, so
that cannot happen while a render runs. On other platforms (no prctl) the check of the parent PID still
runs; the signal is not available there.

Started by render.py with an empty environment (plus PYTHONHASHSEED=0), no inherited descriptors, an
empty working directory and the input on stdin. Self-contained on purpose (standard library + ReportLab +
python-docx): with -I neither the app package, the working directory nor any environment variable is used.

Before it imports ReportLab or python-docx it limits itself: address space (RLIMIT_AS), CPU time
(RLIMIT_CPU; SIGXCPU then SIGKILL), no core files, no file creation (RLIMIT_FSIZE 0: it writes only to its
stdout pipe), few descriptors. These are RESOURCE limits and interpreter isolation, NOT an OS-level sandbox:
this process runs as the same user, in the same container, with the same filesystem view and network
namespace as core-api (docs/exam-export.md, "What the renderer isolates and what it does not").

What it never does with user text: every string is XML-escaped before it reaches a ReportLab Paragraph
(line breaks are inserted after escaping), and no user string becomes a font name, file path, URL, image,
link or style. ReportLab's URL reader is replaced by a function that refuses, and its trusted schemes and
hosts are emptied. No image, link, annotation, form field, attachment, script or open action is emitted.
DOCX output is the python-docx default template with the thumbnail and custom XML parts removed, neutral
properties, and ZIP entries rewritten with a fixed timestamp, order and compression level.

Exit codes: 0 the file on stdout; 2 a classified failure, {"status": "error", "code": "layout" |
"memory" | "too_large" | "invalid_model"} on stdout; 64 bad arguments; 121 the parent-death signal could
not be set (Linux); 122 the parent was already gone; anything else is a crash (the parent classifies it).
"""

import io
import json
import os
import resource
import signal
import sys

EXIT_OK = 0
EXIT_CLASSIFIED = 2
EXIT_CRASH = 3
EXIT_USAGE = 64
EXIT_NO_PDEATHSIG = 121
EXIT_PARENT_GONE = 122
_PR_SET_PDEATHSIG = 1
RENDERER_VERSION = "exam-render/1"
LINES_PER_PARAGRAPH = 24


class Classified(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _limit(kind, soft, hard=None):
    hard = soft if hard is None else hard
    try:
        resource.setrlimit(kind, (soft, hard))
    except (ValueError, OSError):
        current_soft, current_hard = resource.getrlimit(kind)
        if current_hard == resource.RLIM_INFINITY or current_hard > hard:
            raise


def _apply_limits(cpu_seconds, memory_bytes):
    _limit(resource.RLIMIT_CORE, 0)
    _limit(resource.RLIMIT_FSIZE, 0)
    _limit(resource.RLIMIT_NOFILE, 64)
    _limit(resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1)
    _limit(resource.RLIMIT_AS, memory_bytes)


def _audit_hook(event, args):  # test mode only: behavioural evidence, never a security boundary
    if event == "open" or event.startswith("socket."):
        try:
            detail = str(args[0]) if args else ""
        except Exception:  # noqa: BLE001
            detail = "?"
        sys.stderr.write(json.dumps({"audit": event, "arg": detail[:300]}) + "\n")


# --- text helpers ------------------------------------------------------------------------------------------


def _markup(value):
    """User text as safe ReportLab paragraph markup: escaped first, then line breaks and tabs."""
    from xml.sax.saxutils import escape

    out = escape(value or "")
    out = out.replace("\t", "&#160;&#160;&#160;&#160;")
    return out.replace("\n", "<br/>")


def _lines(value):
    """User text split at its line breaks. Each line becomes its own flowable, so a long multi-line text is laid
    out in linear time (one huge paragraph would be re-wrapped at every page split)."""
    return (value or "").split("\n")


def _meta_line(header):
    parts = []
    if header["duration_minutes"] is not None:
        parts.append(f"Duration: {header['duration_minutes']} minutes")
    parts.append(f"Total points: {header['total_points']}")
    parts.append(f"Questions: {header['question_count']}")
    return "   ·   ".join(parts)


def _course_line(header):
    code, name = header["course_code"], header["course_name"]
    return f"{code} — {name}" if code else name


def _points(points):
    return f"{points} point" if points == 1 else f"{points} points"


def _option_texts(question):
    if question["options"] is None:
        return []
    return [(f"{o['label']}. " if o["label"] else "○ ") + o["text"] for o in question["options"]]


def _answer_lines(entry):
    lines = []
    if entry["answer"] is not None:
        answer = entry["answer"] if entry["answer_text"] is None else f"{entry['answer']} — {entry['answer_text']}"
        lines.append(("Answer", answer))
    if entry["model_answer"] is not None:
        lines.append(("Model answer", entry["model_answer"]))
    if entry["rubric"] is not None:
        lines.append(("Rubric", entry["rubric"]))
    if entry["explanation"] is not None:
        lines.append(("Explanation", entry["explanation"]))
    if entry["sources"]:
        lines.append(("Sources", "; ".join(entry["sources"])))
    return lines


def _banner(model):
    if model["variant"] == "answer_key":
        return "ANSWER KEY — CONFIDENTIAL"
    return None


def _draft_mark(model):
    if model["draft_version"] is None:
        return None
    return f"DRAFT — version {model['draft_version']} — not for distribution"


# --- PDF -----------------------------------------------------------------------------------------------------


def render_pdf(model, fonts):
    from reportlab import rl_config

    rl_config.invariant = 1  # fixed document id and dates: same model, same bytes
    rl_config.trustedSchemes = []
    rl_config.trustedHosts = []
    import reportlab.lib.utils as rl_utils

    def _refuse(*_args, **_kwargs):
        raise OSError("remote resources are disabled in the exam renderer")

    rl_utils.rlUrlRead = _refuse
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import BaseDocTemplate, Frame, HRFlowable, KeepTogether, PageTemplate, Paragraph, Spacer
    from reportlab.platypus.doctemplate import LayoutError

    pdfmetrics.registerFont(TTFont("SG-Regular", fonts["regular"]))
    pdfmetrics.registerFont(TTFont("SG-Bold", fonts["bold"]))
    pdfmetrics.registerFont(TTFont("SG-Oblique", fonts["oblique"]))
    pdfmetrics.registerFontFamily("SG-Regular", normal="SG-Regular", bold="SG-Bold", italic="SG-Oblique", boldItalic="SG-Bold")

    base = ParagraphStyle("base", fontName="SG-Regular", fontSize=10.5, leading=14, splitLongWords=1, spaceAfter=2)
    option = ParagraphStyle("option", parent=base, leftIndent=8 * mm)
    label = ParagraphStyle("label", parent=base, fontName="SG-Bold", spaceBefore=8)
    title = ParagraphStyle("title", parent=base, fontName="SG-Bold", fontSize=16, leading=20, spaceAfter=6)
    course = ParagraphStyle("course", parent=base, fontName="SG-Bold", fontSize=11)
    small = ParagraphStyle("small", parent=base, fontSize=9.5, leading=12)
    answer = ParagraphStyle("answer", parent=base, leftIndent=8 * mm)

    header = model["header"]
    footer_title = header["title"]
    banner = _banner(model)
    draft = _draft_mark(model)
    sha12 = model["content_sha12"]

    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):  # noqa: N802 - ReportLab API
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._decorate(total)
                rl_canvas.Canvas.showPage(self)
            rl_canvas.Canvas.save(self)

        def _fit(self, text, font, size, width):
            while text and pdfmetrics.stringWidth(text, font, size) > width:
                text = text[:-2] + "…" if len(text) > 2 else ""
            return text

        def _decorate(self, total):
            width, _ = A4
            self.setFont("SG-Regular", 8.5)
            self.drawRightString(width - 20 * mm, 12 * mm, f"Page {self._pageNumber} of {total}")
            left = footer_title if banner is None else f"Confidential answer key · content {sha12}"
            self.drawString(20 * mm, 12 * mm, self._fit(left, "SG-Regular", 8.5, width - 75 * mm))
            if banner is not None:
                self.setFont("SG-Bold", 9)
                self.drawCentredString(width / 2, A4[1] - 12 * mm, banner)
            if draft is not None:
                self.setFont("SG-Bold", 9)
                self.drawCentredString(width / 2, A4[1] - (17 if banner else 12) * mm, draft)

    line = ParagraphStyle("line", parent=base, spaceBefore=0, spaceAfter=0)
    option_line = ParagraphStyle("option_line", parent=option, spaceBefore=0, spaceAfter=0)
    answer_line = ParagraphStyle("answer_line", parent=answer, spaceBefore=0, spaceAfter=0)

    def text_block(value, style, *, prefix=None, after=2):
        """User text (escaped) in paragraphs of at most LINES_PER_PARAGRAPH source lines, so splitting a paragraph
        across pages stays cheap and the layout of a long multi-line text is linear; empty lines keep their height."""
        lines = [_markup(part) or "&#160;" for part in _lines(value)]
        if prefix is not None:
            lines[0] = prefix + lines[0]
        flowables = [Paragraph("<br/>".join(lines[i:i + LINES_PER_PARAGRAPH]), style)
                     for i in range(0, len(lines), LINES_PER_PARAGRAPH)]  # fmt: skip
        flowables.append(Spacer(1, after))
        return flowables

    story = []
    story.append(Paragraph(_markup(_course_line(header)), course))
    story.append(Paragraph(_markup(header["title"]), title))
    story.append(Paragraph(_markup(_meta_line(header)), small))
    if model["variant"] == "student":
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("Name: ______________________________   Student ID: ____________________", base))
    if header["instructions"]:
        story.append(Spacer(1, 3 * mm))
        story.extend(text_block(header["instructions"], line))
    story.append(HRFlowable(width="100%", thickness=0.6, spaceBefore=4 * mm, spaceAfter=3 * mm))

    answers = {e["number"]: e for e in model.get("answer_key") or []}
    for question in model["questions"]:
        block = [Paragraph(f"<b>{question['number']}.</b> ({_points(question['points'])})", label)]
        block.extend(text_block(question["stem"], line, after=4))
        for text in _option_texts(question):
            block.extend(text_block(text, option_line, after=1))
        if model["variant"] == "student" and question["answer_lines"]:
            for _ in range(question["answer_lines"]):
                block.append(HRFlowable(width="100%", thickness=0.4, spaceBefore=7 * mm, spaceAfter=0))
        entry = answers.get(question["number"])
        if entry is not None:
            for name, value in _answer_lines(entry):
                block.extend(text_block(value, answer_line, prefix=f"<b>{name}:</b> ", after=2))
        story.append(KeepTogether(block))

    out = io.BytesIO()
    doc = BaseDocTemplate(out, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=22 * mm,
                          bottomMargin=20 * mm, title="Exam" if banner is None else "Answer key", author="",
                          subject="", creator=RENDERER_VERSION, invariant=1)  # fmt: skip
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="page", frames=[frame])])
    try:
        doc.build(story, canvasmaker=NumberedCanvas)
    except LayoutError:
        raise Classified("layout") from None
    return out.getvalue()


# --- DOCX ----------------------------------------------------------------------------------------------------


def _fixed_zip(data):
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            entry = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 0
            entry.external_attr = 0
            target.writestr(entry, source.read(info.filename), compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    return out.getvalue()


APP_XML = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
    'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
    f"<Application>{RENDERER_VERSION}</Application><DocSecurity>0</DocSecurity></Properties>"
)


def render_docx(model, timestamp):
    from datetime import datetime

    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    doc = Document()
    package = doc.part.package
    for rel_id, rel in list(package.rels.items()):  # no thumbnail image
        if rel.reltype.endswith("/thumbnail"):
            package.rels.pop(rel_id)
    for rel_id, rel in list(doc.part.rels.items()):  # no custom XML parts
        if rel.reltype.endswith("/customXml"):
            doc.part.rels.pop(rel_id)
    for part in package.iter_parts():
        if str(part.partname) == "/docProps/app.xml":
            part._blob = APP_XML.encode("utf-8")

    section = doc.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.left_margin = section.right_margin = Mm(20)
    section.top_margin, section.bottom_margin = Mm(22), Mm(20)
    normal = doc.styles["Normal"]
    normal.font.name = "DejaVu Sans"
    normal.font.size = Pt(10.5)

    def paragraph(text="", *, bold_prefix=None, bold=False, size=None, indent=False):
        p = doc.add_paragraph()
        if indent:
            p.paragraph_format.left_indent = Mm(8)
        if bold_prefix is not None:
            prefix = p.add_run(bold_prefix)
            prefix.bold = True
        run = p.add_run(text)  # python-docx sets text through lxml: escaped, \n -> <w:br/>, \t -> <w:tab/>
        run.bold = bold or None
        if size is not None:
            run.font.size = Pt(size)
        return p

    header = model["header"]
    banner, draft = _banner(model), _draft_mark(model)
    top = section.header.paragraphs[0]
    top.text = " · ".join(x for x in (banner, draft) if x) or ""
    bottom = section.footer.paragraphs[0]
    left = header["title"] if banner is None else f"Confidential answer key · content {model['content_sha12']}"
    bottom.add_run(f"{left} · Page ")
    for instruction, tail in (("PAGE", " of "), ("NUMPAGES", "")):
        field = OxmlElement("w:fldSimple")
        field.set(qn("w:instr"), instruction)
        bottom._p.append(field)
        if tail:
            bottom.add_run(tail)

    paragraph(_course_line(header), bold=True, size=11)
    paragraph(header["title"], bold=True, size=16)
    paragraph(_meta_line(header), size=9.5)
    if model["variant"] == "student":
        paragraph("Name: ______________________________   Student ID: ____________________")
    if header["instructions"]:
        paragraph(header["instructions"])
    answers = {e["number"]: e for e in model.get("answer_key") or []}
    for question in model["questions"]:
        paragraph(f" ({_points(question['points'])})", bold_prefix=f"{question['number']}.")
        paragraph(question["stem"])
        for text in _option_texts(question):
            paragraph(text, indent=True)
        if model["variant"] == "student" and question["answer_lines"]:
            for _ in range(question["answer_lines"]):
                paragraph("_" * 70)
        entry = answers.get(question["number"])
        if entry is not None:
            for name, value in _answer_lines(entry):
                paragraph(value, bold_prefix=f"{name}: ", indent=True)

    when = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    props = doc.core_properties
    props.author = ""
    props.last_modified_by = ""
    props.title = "Exam" if banner is None else "Answer key"
    props.subject = props.keywords = props.comments = props.category = ""
    props.created = props.modified = when
    props.last_printed = when
    props.revision = 1
    out = io.BytesIO()
    doc.save(out)
    return _fixed_zip(out.getvalue())


# --- main -------------------------------------------------------------------------------------------------------


_HEADER_KEYS = {"course_code", "course_name", "title", "instructions", "duration_minutes", "question_count", "total_points"}


def _check_model(model):
    """Structural re-check of the parent's (already schema-validated) model; anything else is invalid_model."""
    keys = {"variant", "header", "questions", "draft_version", "content_sha12"}
    if model.get("variant") == "answer_key":
        keys = keys | {"answer_key"}
    if not isinstance(model, dict) or set(model) != keys or set(model["header"]) != _HEADER_KEYS:
        raise Classified("invalid_model")
    if model["variant"] == "student" and any(set(q) != {"number", "points", "type", "stem", "options", "answer_lines"}
                                             for q in model["questions"]):  # fmt: skip
        raise Classified("invalid_model")


_STATUS_FD = None


def _open_status():
    """Opened before any audit hook: reading it at the end is then not an `open` of the rendering phase."""
    global _STATUS_FD
    import os

    try:
        _STATUS_FD = os.open("/proc/self/status", os.O_RDONLY)
    except OSError:
        _STATUS_FD = None


def _report_peak_memory():
    """This process's own peak resident set (VmHWM of the address space created by exec), for the parent's
    evidence. ru_maxrss would also count the parent's pages that fork copied before exec."""
    import os

    if _STATUS_FD is None:
        return
    try:
        os.lseek(_STATUS_FD, 0, os.SEEK_SET)
        text = os.read(_STATUS_FD, 16384).decode("ascii", "replace")
    except OSError:
        return
    for line in text.splitlines():
        if line.startswith("VmHWM:"):
            sys.stderr.write(json.dumps({"vmhwm_kib": int(line.split()[1])}) + "\n")
            return


def _expected_parent(argv):
    if len(argv) != 2 or argv[0] != "--parent-pid" or not argv[1].isdigit() or int(argv[1]) < 1:
        return None
    return int(argv[1])


def _arm_parent_death(expected_parent):
    """Set the Linux parent-death signal, then close the race with an explicit check of the actual parent."""
    if sys.platform.startswith("linux"):
        import ctypes

        try:
            libc = ctypes.CDLL(None, use_errno=True)
            armed = libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) == 0
        except (OSError, AttributeError):
            armed = False
        if not armed:
            return EXIT_NO_PDEATHSIG
    if os.getppid() != expected_parent:
        return EXIT_PARENT_GONE
    return None


def main():
    expected_parent = _expected_parent(sys.argv[1:])
    if expected_parent is None:
        return EXIT_USAGE
    refused = _arm_parent_death(expected_parent)
    if refused is not None:
        return refused
    _open_status()
    try:
        return _main()
    finally:
        _report_peak_memory()


def _main():
    raw = sys.stdin.buffer.read()
    try:
        request = json.loads(raw.decode("utf-8"))
        limits = request["limits"]
        _apply_limits(int(limits["cpu_seconds"]), int(limits["memory_bytes"]))
        fmt, model = request["format"], request["model"]
        _check_model(model)
        if fmt == "pdf":
            import reportlab  # noqa: F401  (imported before the audit hook: module loading is not under test)
            import reportlab.platypus  # noqa: F401
        else:
            import docx  # noqa: F401
        if request.get("audit"):
            sys.addaudithook(_audit_hook)
        data = render_pdf(model, request["fonts"]) if fmt == "pdf" else render_docx(model, request["timestamp"])
        if len(data) > int(limits["max_output_bytes"]):
            raise Classified("too_large")
    except Classified as exc:
        sys.stdout.buffer.write(json.dumps({"status": "error", "code": exc.code}).encode())
        return EXIT_CLASSIFIED
    except MemoryError:
        sys.stdout.buffer.write(b'{"status": "error", "code": "memory"}')
        return EXIT_CLASSIFIED
    except Exception as exc:  # noqa: BLE001 - the class only, never the message (it may echo content)
        sys.stderr.write(f"render_failed class={type(exc).__name__}\n")
        return EXIT_CRASH
    out = sys.stdout.buffer
    out.write(data)
    out.flush()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
