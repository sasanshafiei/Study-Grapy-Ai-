"""PDF extraction in its subprocess: results, classification, limits and lifecycle (no database).

The subprocess tests need Linux (/proc, RLIMIT_AS, PR_SET_PDEATHSIG): they run
in the test container. Fake extractors (tests/fake_extractor.py) are passed
with `command=`, a test-only argument; production code has no way to choose them.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.ingestion import extraction
from app.ingestion.extraction import (
    EXTRACTOR_COMMAND,
    STDERR_CAP_BYTES,
    Extraction,
    ExtractionFailed,
    ExtractionLimits,
    extract_pdf,
    parse_output,
)
from tests.pdf_samples import Blank, Bomb, ImageOnly, build_pdf, encrypt_pdf, make_pdf, malformed_pdf, text_page
from tests.worker_support import pid_alive, wait_for

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs Linux process controls")
FAKE = Path(__file__).with_name("fake_extractor.py")
LIMITS = ExtractionLimits(max_pages=50, max_text_chars=100_000, timeout_seconds=30, memory_bytes=512 * 1024 * 1024)


@pytest.fixture
def pdf_fd(tmp_path):
    opened: list[int] = []

    def open_pdf(data: bytes) -> int:
        path = tmp_path / f"doc{len(opened)}.pdf"
        path.write_bytes(data)
        fd = os.open(path, os.O_RDONLY)
        opened.append(fd)
        return fd

    yield open_pdf
    for fd in opened:
        try:
            os.close(fd)
        except OSError:
            pass


def _fake(mode: str, signal_dir: Path) -> tuple[str, ...]:
    return (sys.executable, "-I", str(FAKE), mode, str(signal_dir))


def _failure(fd: int, limits: ExtractionLimits = LIMITS, **kwargs) -> ExtractionFailed:
    with pytest.raises(ExtractionFailed) as caught:
        extract_pdf(fd, limits, **kwargs)
    return caught.value


# --- results ----------------------------------------------------------------------------------


@linux_only
def test_multipage_text_with_blank_and_image_pages_keeps_source_page_numbers(pdf_fd):
    data = build_pdf([text_page("Chapter 1", "Atoms"), Blank(), ImageOnly(), text_page("Chapter 2"), Blank()])
    result = extract_pdf(pdf_fd(data), LIMITS)
    assert result.page_count == 5
    assert [page for page, _ in result.pages] == [1, 4]
    assert "Chapter 1" in result.pages[0][1] and "Atoms" in result.pages[0][1]
    assert result.pages[1][1] == "Chapter 2"


@linux_only
def test_unicode_and_scientific_symbols_survive_extraction(pdf_fd):
    line = "H₂O → 2H₂ + O₂; ΔH = −92 kJ/mol; x² µ Çğş \U0001d6fc"
    result = extract_pdf(pdf_fd(build_pdf([text_page(line)])), LIMITS)
    assert result.pages == ((1, line),)


@linux_only
def test_the_original_one_page_sample_extracts(pdf_fd):
    assert extract_pdf(pdf_fd(make_pdf("Hello chemistry")), LIMITS).pages == ((1, "Hello chemistry"),)


@linux_only
def test_a_document_without_text_is_a_result_with_no_pages(pdf_fd):
    result = extract_pdf(pdf_fd(build_pdf([ImageOnly(), Blank()])), LIMITS)
    assert result == Extraction(page_count=2, pages=())  # the pipeline turns this into `no_text`


# --- document failures (permanent) ------------------------------------------------------------


@linux_only
@pytest.mark.parametrize("user_password", ["", "secret"], ids=["owner-password-only", "user-password"])
def test_encrypted_pdfs_are_rejected(pdf_fd, user_password):
    failure = _failure(pdf_fd(encrypt_pdf(build_pdf([text_page("secret text")]), user_password=user_password)))
    assert (failure.code, failure.permanent) == ("encrypted_pdf", True)


@linux_only
def test_malformed_pdf_is_invalid(pdf_fd):
    failure = _failure(pdf_fd(malformed_pdf()))
    assert (failure.code, failure.permanent) == ("invalid_pdf", True)


@linux_only
def test_too_many_pages(pdf_fd):
    limits = ExtractionLimits(max_pages=3, max_text_chars=100_000, timeout_seconds=30, memory_bytes=512 * 1024 * 1024)
    failure = _failure(pdf_fd(build_pdf([text_page("p")] * 4)), limits)
    assert (failure.code, failure.permanent) == ("too_many_pages", True)


@linux_only
def test_too_much_text(pdf_fd):
    limits = ExtractionLimits(max_pages=50, max_text_chars=1000, timeout_seconds=30, memory_bytes=512 * 1024 * 1024)
    failure = _failure(pdf_fd(build_pdf([text_page("x" * 200)] * 6)), limits)
    assert (failure.code, failure.permanent) == ("too_much_text", True)


@linux_only
def test_decompression_bomb_is_stopped_by_the_parser_limit(pdf_fd):
    data = build_pdf([Bomb(200 * 1024 * 1024)])
    assert len(data) < 1024 * 1024  # a small file...
    failure = _failure(pdf_fd(data))  # ...that would inflate to 200 MiB
    assert (failure.code, failure.permanent) == ("processing_limit", True)


@linux_only
def test_memory_limit_stops_a_document_under_the_parser_limit(pdf_fd):
    limits = ExtractionLimits(max_pages=50, max_text_chars=100_000, timeout_seconds=30, memory_bytes=160 * 1024 * 1024)
    failure = _failure(pdf_fd(build_pdf([Bomb(60 * 1024 * 1024)])), limits)
    assert (failure.code, failure.permanent) == ("processing_limit", True)


# --- lifecycle and output handling ----------------------------------------------------------


@linux_only
def test_timeout_kills_and_reaps_the_extractor(pdf_fd, tmp_path):
    limits = ExtractionLimits(max_pages=50, max_text_chars=100_000, timeout_seconds=1, memory_bytes=512 * 1024 * 1024)
    started = time.monotonic()
    failure = _failure(pdf_fd(make_pdf()), limits, command=_fake("hang", tmp_path))
    assert time.monotonic() - started < 10
    assert (failure.code, failure.permanent) == ("processing_limit", True)
    [(pid, _)] = [tuple(map(int, p.read_text().split())) for p in tmp_path.glob("extractor-*.pid")]
    assert not pid_alive(pid)  # killed and reaped: neither running nor a zombie


@linux_only
def test_oversized_output_is_cut_off_without_reading_it_all(pdf_fd, tmp_path):
    limits = ExtractionLimits(max_pages=1, max_text_chars=1000, timeout_seconds=30, memory_bytes=512 * 1024 * 1024)
    assert limits.stdout_cap_bytes < 1024 * 1024  # the fake writes 512 MiB
    failure = _failure(pdf_fd(make_pdf()), limits, command=_fake("huge", tmp_path))
    assert (failure.code, failure.permanent) == ("processing_limit", True)


@linux_only
def test_malformed_output_is_a_transient_failure(pdf_fd, tmp_path):
    failure = _failure(pdf_fd(make_pdf()), command=_fake("garbage", tmp_path))
    assert (failure.code, failure.permanent) == ("extractor_output_invalid", False)


@linux_only
def test_abnormal_exit_is_a_transient_failure(pdf_fd, tmp_path):
    failure = _failure(pdf_fd(make_pdf()), command=_fake("crash:1", tmp_path))
    assert (failure.code, failure.permanent) == ("extractor_crashed", False)


@linux_only
def test_child_gets_only_the_pdf_descriptor_and_an_empty_environment(pdf_fd, tmp_path):
    fd = pdf_fd(make_pdf())
    result = extract_pdf(fd, LIMITS, command=_fake("inspect", tmp_path))
    seen = json.loads(result.pages[0][1])
    assert seen["env"] == {} or set(seen["env"]) <= {"LC_CTYPE"}  # the interpreter may set LC_CTYPE itself
    assert seen["ppid"] == os.getpid()
    # 0-2 plus the PDF; listing /proc/self/fd itself holds one more descriptor
    assert fd in seen["fds"] and {0, 1, 2} <= set(seen["fds"])
    assert len(set(seen["fds"]) - {0, 1, 2, fd}) <= 1


@linux_only
def test_extractor_limits_are_applied(pdf_fd, tmp_path):
    result = extract_pdf(pdf_fd(make_pdf()), LIMITS, command=_fake("limits", tmp_path))
    limits = json.loads(result.pages[0][1])
    assert limits["RLIMIT_AS"] == [LIMITS.memory_bytes, LIMITS.memory_bytes]
    assert limits["RLIMIT_CPU"] == [LIMITS.cpu_seconds, LIMITS.cpu_seconds + 1]
    assert limits["RLIMIT_CORE"] == [0, 0]
    assert limits["RLIMIT_FSIZE"] == [0, 0]
    assert limits["RLIMIT_NOFILE"] == [64, 64]


@linux_only
def test_extractor_dies_with_the_process_that_started_it(pdf_fd, tmp_path):
    """The owner (like a Celery pool process) is SIGKILLed mid-extraction: its extractor must go too."""
    data_path = tmp_path / "doc.pdf"
    data_path.write_bytes(make_pdf())
    owner_script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from app.ingestion.extraction import ExtractionLimits, extract_pdf
        fd = os.open({str(data_path)!r}, os.O_RDONLY)
        extract_pdf(fd, ExtractionLimits(50, 100000, 600, 512 * 1024 * 1024),
                    command=({sys.executable!r}, "-I", {str(FAKE)!r}, "hang", {str(tmp_path)!r}))
        """
    )
    owner = subprocess.Popen([sys.executable, "-c", owner_script])
    try:
        [(pid, ppid)] = wait_for(
            lambda: [tuple(map(int, p.read_text().split())) for p in tmp_path.glob("extractor-*.pid")] or None,
            30,
            what="the extractor to start",
        )
        assert ppid == owner.pid and pid_alive(pid)
        owner.send_signal(signal.SIGKILL)
        owner.wait(10)
        wait_for(lambda: not pid_alive(pid), 10, what="the orphaned extractor to be killed")
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()


# --- output validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        {"status": "ok", "page_count": 0, "pages": []},
        {"status": "ok", "page_count": 2, "pages": [{"page": 2, "text": "a"}, {"page": 1, "text": "b"}]},
        {"status": "ok", "page_count": 2, "pages": [{"page": 3, "text": "a"}]},
        {"status": "ok", "page_count": 1, "pages": [{"page": 1, "text": 5}]},
        {"status": "ok", "page_count": 1, "pages": [{"page": True, "text": "a"}]},
        {"status": "ok", "page_count": 1, "pages": [{"page": 1, "text": "a", "extra": 1}]},
        {"status": "ok", "page_count": 1, "pages": [], "extra": 1},
        {"status": "ok", "page_count": 51, "pages": []},
        {"status": "ok", "page_count": 1, "pages": [{"page": 1, "text": "x" * 100_001}]},
        {"status": "error", "page_count": 1, "pages": []},
        ["not", "an", "object"],
    ],
)
def test_output_schema_is_strict(document):
    with pytest.raises(ExtractionFailed) as caught:
        parse_output(json.dumps(document).encode(), LIMITS)
    assert (caught.value.code, caught.value.permanent) == ("extractor_output_invalid", False)


def test_output_must_be_utf8_json():
    for raw in (b"\xff\xfe", b"{", b""):
        with pytest.raises(ExtractionFailed):
            parse_output(raw, LIMITS)


def test_valid_output_is_accepted():
    raw = json.dumps(
        {"status": "ok", "page_count": 3, "pages": [{"page": 1, "text": "a"}, {"page": 3, "text": "ç"}]},
        ensure_ascii=False,
    ).encode()
    assert parse_output(raw, LIMITS) == Extraction(3, ((1, "a"), (3, "ç")))


def test_output_cap_covers_the_worst_case_json_encoding():
    limits = ExtractionLimits(max_pages=2, max_text_chars=10, timeout_seconds=1, memory_bytes=1)
    worst = json.dumps(
        {"status": "ok", "page_count": 2, "pages": [{"page": 1, "text": "\x00" * 5}, {"page": 2, "text": "\x01" * 5}]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    assert len(worst) <= limits.stdout_cap_bytes
    assert STDERR_CAP_BYTES <= 64 * 1024


def test_production_command_is_the_real_extractor():
    assert EXTRACTOR_COMMAND[1:] == ("-I", str(Path(__file__).resolve().parents[1] / "app" / "ingestion" / "extract_child.py"))


# --- M3 review: no extractor without its parent-death protection -------------------------------


@linux_only
def test_extractor_is_not_started_when_the_parent_death_signal_cannot_be_set(pdf_fd, tmp_path, monkeypatch):
    monkeypatch.setattr(extraction, "_prctl", lambda option, signum: -1)  # prctl reports failure
    failure = _failure(pdf_fd(make_pdf()), command=_fake("hang", tmp_path))
    assert (failure.code, failure.permanent) == ("extractor_unprotected", False)
    assert list(tmp_path.glob("extractor-*.pid")) == []  # the extractor program never ran


def test_no_extractor_where_the_parent_death_signal_is_unavailable(pdf_fd, tmp_path, monkeypatch):
    monkeypatch.setattr(extraction, "_prctl", None)  # e.g. not Linux
    failure = _failure(pdf_fd(make_pdf()), command=_fake("hang", tmp_path))
    assert (failure.code, failure.permanent) == ("extractor_unprotected", False)
    assert list(tmp_path.glob("extractor-*.pid")) == []


@linux_only
def test_the_real_parent_death_signal_is_set(pdf_fd, tmp_path):
    """prctl(PR_GET_PDEATHSIG) inside the started program reports SIGKILL."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import ctypes, json, signal, sys\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "value = ctypes.c_int(0)\n"
        "libc.prctl(2, ctypes.byref(value))  # PR_GET_PDEATHSIG\n"
        "doc = {'status': 'ok', 'page_count': 1, 'pages': [{'page': 1, 'text': str(value.value)}]}\n"
        "sys.stdout.write(json.dumps(doc))\n"
    )
    result = extract_pdf(pdf_fd(make_pdf()), LIMITS, command=(sys.executable, "-I", str(probe)))
    assert result.pages == ((1, str(int(signal.SIGKILL))),)
