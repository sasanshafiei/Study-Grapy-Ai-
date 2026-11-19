"""Extract the text of a PDF, page by page. Runs as its own process; never imported by the app.

    python -I extract_child.py --fd N --max-pages P --max-chars C --cpu-seconds S --memory-bytes M

Started by extraction.py with the PDF open on descriptor N (already verified
against its stored size and SHA-256), an empty environment and no other
inherited descriptors. Self-contained on purpose (standard library + pypdf):
with -I, neither the app package nor the working directory is on sys.path.

Before it imports pypdf, it limits itself: address space (RLIMIT_AS), CPU
time (RLIMIT_CPU; SIGXCPU then SIGKILL), no core files, no file creation
(RLIMIT_FSIZE 0: it writes only to its stdout pipe) and few descriptors.

Output, on stdout, one JSON document (UTF-8, ensure_ascii=False):
    exit 0: {"status": "ok", "page_count": n, "pages": [{"page": p, "text": "..."}, ...]}
            only pages with non-blank text are listed, in page order
    exit 2: {"status": "error", "code": "<code>", "detail": "<short text>"}
Any other exit is a crash; the parent classifies it.

pypdf only reads: it does not run JavaScript, follow launch actions or fetch
anything over the network, and this process has nothing to fetch with but
does not rely on that being impossible.
"""

import argparse
import json
import logging
import os
import re
import resource
import sys
import warnings

EXIT_OK = 0
EXIT_CLASSIFIED = 2
EXIT_USAGE = 64
_SURROGATES = re.compile("[\ud800-\udfff]")


class Classified(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail[:300]


def _limit(kind, soft, hard=None):
    hard = soft if hard is None else hard
    try:
        resource.setrlimit(kind, (soft, hard))
    except (ValueError, OSError):
        # Cannot lower below the current usage or raise above the hard limit;
        # keep whatever stricter limit is already in place.
        current_soft, current_hard = resource.getrlimit(kind)
        if current_hard == resource.RLIM_INFINITY or current_hard > hard:
            raise


def _apply_limits(cpu_seconds, memory_bytes):
    _limit(resource.RLIMIT_CORE, 0)
    _limit(resource.RLIMIT_FSIZE, 0)
    _limit(resource.RLIMIT_NOFILE, 64)
    _limit(resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1)
    _limit(resource.RLIMIT_AS, memory_bytes)


def _emit(document, code):
    data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    out = sys.stdout.buffer
    out.write(data)
    out.flush()
    return code


def _extract(fd, max_pages, max_chars):
    import pypdf
    from pypdf.errors import LimitReachedError

    os.lseek(fd, 0, os.SEEK_SET)
    stream = os.fdopen(fd, "rb", closefd=True)
    try:
        try:
            reader = pypdf.PdfReader(stream, strict=False)
            if reader.is_encrypted:
                raise Classified("encrypted_pdf", "the document is encrypted")
            page_count = len(reader.pages)
            if page_count == 0:
                raise Classified("invalid_pdf", "the document has no pages")
            if page_count > max_pages:
                raise Classified("too_many_pages", f"{page_count} pages")
            pages = []
            total = 0
            for index in range(page_count):
                text = reader.pages[index].extract_text() or ""
                text = _SURROGATES.sub("", text)
                if not text.strip():
                    continue
                total += len(text)
                if total > max_chars:
                    raise Classified("too_much_text", f"more than {max_chars} characters")
                pages.append({"page": index + 1, "text": text})
        except Classified:
            raise
        except MemoryError:
            raise Classified("processing_limit", "memory limit reached") from None
        except LimitReachedError as exc:
            raise Classified("processing_limit", f"parser limit: {type(exc).__name__}") from None
        except RecursionError:
            raise Classified("invalid_pdf", "structure nested too deeply") from None
        except Exception as exc:  # noqa: BLE001 - malformed input raises arbitrary errors inside pypdf
            raise Classified("invalid_pdf", f"{type(exc).__name__}") from None
    finally:
        stream.close()
    return {"status": "ok", "page_count": page_count, "pages": pages}


def main(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--max-chars", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    parser.add_argument("--memory-bytes", type=int, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    if min(args.fd, args.max_pages, args.max_chars, args.cpu_seconds, args.memory_bytes) < 0:
        return EXIT_USAGE

    _apply_limits(args.cpu_seconds, args.memory_bytes)
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    try:
        result = _extract(args.fd, args.max_pages, args.max_chars)
    except Classified as exc:
        return _emit({"status": "error", "code": exc.code, "detail": exc.detail}, EXIT_CLASSIFIED)
    except MemoryError:
        return _emit({"status": "error", "code": "processing_limit", "detail": "memory limit reached"}, EXIT_CLASSIFIED)
    return _emit(result, EXIT_OK)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
