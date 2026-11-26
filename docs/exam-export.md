# Exam export: PDF and DOCX (M9-A1)

Official files of a finalized exam (`GET …/exams/{id}/export`, E22) and review files of a draft
(`GET …/exams/{id}/preview-file`, E23), each as a **student version** or a separate **answer key**, in
**PDF** (ReportLab) or **DOCX** (python-docx). Both endpoints need `EXAMS_ENABLED` and
`EXAM_EXPORT_ENABLED` (otherwise 503 `exam_export_unavailable` after authorization); the JSON preview (E18)
works without the export switch. Only the course owner can download anything; answer keys are
confidential.

## Request and response

Query: `variant=student|answer_key` and `format=pdf|docx`, both required (422 `invalid_export_option`;
other parameters 422 `invalid_query`). E22 only for `finalized` or archived-finalized exams (409
`exam_not_finalized`); E23 for drafts (watermarked) and finalized exams (identical bytes to E22, audited as
`preview_file_downloaded`). A draft above the export budget: 409 `export_budget_exceeded`.

```
HTTP/1.1 200 OK
Content-Type: application/pdf | application/vnd.openxmlformats-officedocument.wordprocessingml.document
Content-Length: 48213
Content-Disposition: attachment; filename="CHEM-101-Midterm-1-Stoichiometry-v9-answer-key.pdf"; filename*=UTF-8''CHEM-101-Midterm-1-%E2%80%94-Stoichiometry-v9-answer-key.pdf
Cache-Control: no-store, private
Pragma: no-cache
X-Content-Type-Options: nosniff
Content-Security-Policy: default-src 'none'; sandbox
Referrer-Policy: no-referrer
X-Exam-Content-SHA256: <64 hex>         (finalized)
X-Exam-Finalized-Version: 9             (finalized; frozen, unchanged by archival)
X-Exam-Version: 7                       (draft preview files)
X-Exam-Preview-Fingerprint: pf1:<hex>   (draft preview files; usable with E19)
X-Exam-Renderer-Version: exam-render/1
```

File name: `<course code>-<title>-v<finalized_version>-<student|answer-key>.<pdf|docx>` (drafts:
`…-draft-v<version>-…`). ASCII `filename`: characters outside `[A-Za-z0-9._-]` become `-`, runs
collapsed, trimmed, ≤ 120 characters with the suffix kept, fallback `exam`. `filename*`: Unicode kept
(NFC), whitespace as `-`, controls, quotes, `/`, `\`, `;` removed, RFC 5987 encoded. No path component
can appear.

Errors: 429 `export_busy` (render slot taken: `EXAM_EXPORT_MAX_CONCURRENT` = 2 per process, one per
professor) and 429 `too_many_exports` (`EXAM_EXPORT_RATE_LIMIT` = 30 per 10 minutes per professor), both
with `Retry-After`; 504 `export_timeout`; 500 `export_failed`. The limiters are in-process (per API
process).

Guard order (both file endpoints): authentication → owner authorization → `EXAMS_ENABLED` →
`EXAM_EXPORT_ENABLED` → strict query (`variant`, `format`, exam id) → rate limit → render slot → the
read-only snapshot (`404`/`409 exam_not_finalized`/`409 export_budget_exceeded`) → renderer → slot
released → audit event. A request refused with 429 therefore never reads the exam or starts the
renderer. Every admitted attempt counts against the rate limit, including one the snapshot then refuses
with 404/409. That is deliberate: the over-budget draft check already loads the whole exam, and a separate
eligibility query would add database work to every request. Only the course owner reaches the limiters,
so a 429 reveals nothing to anyone else. The slot covers the snapshot and the render. It is released
exactly once on every path (snapshot or render error, timeout, unexpected exception, cancellation),
before the audit transaction.

Every successful download commits one audit event (`export_downloaded` / `preview_file_downloaded`:
variant, format, bytes, file SHA-256, renderer version; drafts also the draft version) **before** the
first byte is sent, in a transaction that re-checks the owner with `FOR SHARE` locks. A failed render
writes no event. Nothing is stored on disk; files are rendered on demand.

## Layout

A4 portrait, DejaVu Sans (embedded subset in PDF; DOCX names "DejaVu Sans" and relies on the word
processor's fallback).

- **Student version:** course code — name, title, "Duration · Total points · Questions", Name / Student ID
  lines, instructions; questions numbered 1..n in item order with points; MCQ options `A.`–`F.` in stored
  order with identical styling; true/false as "○ True" / "○ False"; six answer lines after a short-answer
  question; footer "Page x of y" and the title. No answer, explanation, model answer, rubric, source,
  topic, id, Bloom level or approval data, and no designation of the correct option. It necessarily
  contains every option text, the correct one included.
- **Answer key:** header line "ANSWER KEY — CONFIDENTIAL"; every question with its options, then
  "Answer: C — 4.0 mol" / "Answer: True" or "Model answer" + "Rubric", "Explanation", "Sources" (frozen
  titles and page labels); footer "Confidential answer key · content <first 12 hex of the content hash>".
- **Drafts (E23):** "DRAFT — version n — not for distribution" on every page (DOCX: in the header).
- Question blocks are kept together when they fit and split otherwise; long tokens are broken; text is laid
  out in paragraphs of at most 24 source lines, so very long multi-line texts render in linear time.

## Sanitized, variant-specific render models

The parent builds two different models from two different queries. The **student** model comes from a
query that selects only position, points, type, stem and choice texts (`snapshot.student_print_rows`);
answer keys, explanations, model answers, rubrics and sources are never loaded for it, and its schema
(`export.StudentRenderModel`, `extra="forbid"`) has no field that could carry them. The answer-key model
adds them. The child receives exactly one variant's model and re-checks its shape.

## The renderer process — what it isolates and what it does not

`app/exams/render.py` starts `python -I app/exams/render_child.py --parent-pid <pid>` with an empty
environment (plus `PYTHONHASHSEED=0`), `close_fds`, a fresh empty working directory and its own session
and process group (`start_new_session`); the render model as JSON on stdin (≤ 2 MiB).

**Launch safety.** The export runs in the API's thread pool, so the launch uses **no Python
`preexec_fn`**: Python code between `fork` and `exec` can deadlock in a multithreaded process, and the
wall-clock deadline would not bound that because `Popen` has not returned yet. Only `subprocess`'s C code
runs between `fork` and `exec` (descriptor closing, `cwd`, environment, `setsid`). On Linux the child arms
the parent-death signal itself as its first action after `exec` — `prctl(PR_SET_PDEATHSIG, SIGKILL)` —
and then compares `getppid()` with `--parent-pid`: a parent that died before the signal was armed is
detected (exit 122, "parent_gone"), a later death kills the child; if `prctl` fails the child exits (121).
The unprotected window is interpreter start-up only (no input read, no third-party import). Linux sends
the signal when the parent *thread* ends; the calling thread blocks until the child is reaped, so that
cannot happen during a render. Where `prctl` does not exist (non-Linux development machines) only the
parent-PID check runs. Before importing ReportLab or
python-docx the child sets `RLIMIT_AS` (512 MiB), `RLIMIT_CPU` (25 s), `RLIMIT_FSIZE 0`, `RLIMIT_NOFILE`
64 and no core files. The parent writes stdin and reads stdout/stderr with byte caps (PDF 16 MiB, DOCX
8 MiB), a 30 s wall-clock deadline, kills and reaps on overrun (`wait4`, which also yields the child's
own CPU time and peak RSS). The whole process group is killed on every exit path, also when the leader
already exited and a descendant still holds the pipes or lingers: the leader is observed with
`waitid(WNOWAIT)` and reaped only after the group was signalled, so the group ID (= the unreaped leader's
PID) cannot have been reused by an unrelated process; after reaping it is never signalled again. The parent then classifies the result (ok, timeout → 504, layout / memory / too large /
crash / invalid output → 500).

**This is interpreter isolation plus resource limits, not an OS-level sandbox.** The child runs as the same
OS user, in the same container, with the same filesystem view and network namespace as core-api: it could
technically read any file core-api can read (including `/proc/<parent>/environ`) and open sockets to `db`
or `redis`. `RLIMIT_FSIZE` prevents writes, not reads; nothing prevents connections.

The controls that address the real residual risk (a defect in ReportLab, python-docx, lxml or Pillow
reached through renderer input):

1. only validated, parent-generated input (bounds, no control characters, no surrogates; finalized
   content is frozen);
2. every user string is XML-escaped before it reaches a ReportLab `Paragraph` (line breaks inserted after
   escaping); no user string becomes a font name, path, URL, image, link or style; ReportLab's URL reader
   is replaced by a function that refuses and its trusted schemes/hosts are emptied; python-docx sets text
   through lxml; fonts are fixed vendored files whose hashes are pinned;
3. bounded execution (the limits above, one variant per process, no retry loop);
4. output verification in the tests: no `/JS`, `/JavaScript`, `/OpenAction`, `/AA`, `/Launch`, `/URI`,
   `/EmbeddedFile(s)`, `/RichMedia`, `/Annots`, `/AcroForm`, `/XFA`, image XObjects in PDFs; DOCX contains
   only allow-listed parts (the template's thumbnail and custom XML parts are removed), no `vbaProject`,
   external relationships, `altChunk`, OLE objects, drawings, hyperlinks or `INCLUDE*`/`HYPERLINK` fields
   (only `PAGE`/`NUMPAGES`), neutral core properties (empty author; created/modified = finalization time).

A test-mode audit hook (`sys.addaudithook` for `open` and `socket.*`) shows that the tested inputs open
only the fonts and Python's own files and use no socket. It is behavioural evidence for those inputs and
**not** a security boundary. Stronger isolation (separate UID without access to the app tree and `/proc`,
or a network-less, read-only render container) is a deployment option owned by CE-3 (decision D-30).

## Content stability and byte determinism

- **Content stability (guaranteed by the database):** exports of a finalized exam read only frozen exam
  columns and immutable revisions; questions, order, points, answers and frozen source titles can never
  change. `exams.content_sha256` = SHA-256 of the canonical answer-key print model, independent of the
  renderer, sent as `X-Exam-Content-SHA256`.
- **Byte determinism (one pinned renderer):** for a fixed `X-Exam-Renderer-Version` the same model gives
  the same bytes (ReportLab invariant mode, fixed fonts and subsets, `PYTHONHASHSEED=0`; DOCX core
  properties fixed to the finalization time and ZIP entries rewritten with a fixed timestamp, order and
  compression level). Tested by rendering in separate processes and across archival. A library, font or
  renderer change may change bytes, never content; each audit event records the file hash and renderer
  version.

`exam-render/1` pins: `render_child.py`, ReportLab 5.1.0, python-docx 1.2.0, lxml 6.1.3, Pillow 13.0.0
and the three font files below (asserted by `tests/test_exam_render_unit.py`).

## Dependencies and fonts

| Package | Version | Licence | Use |
|---|---|---|---|
| reportlab | 5.1.0 (2026-10-22) | BSD | PDF |
| python-docx | 1.2.0 (2025-06-16) | MIT | DOCX |
| pillow | 13.0.0 (2026-10-16) | MIT-CMU (HPND) | ReportLab dependency |
| charset-normalizer | 3.5.2 (2026-09-30) | MIT | ReportLab dependency |
| lxml | 6.1.3 (2026-09-02) | BSD-3-Clause | python-docx dependency |

Checked on 2026-11-21 in a disposable `python:3.11-slim` container with the project's requirements:
`pip check` clean, `pip-audit`: no known vulnerabilities. All releases are older than two weeks.
`pypdf` (already pinned) is used by the tests to read PDFs.

Fonts (`services/core-api/app/exams/fonts/`, see `SOURCE.md` and `LICENSE`): DejaVu Sans 2.37 Regular,
Bold, Oblique from the Debian 13 packages `fonts-dejavu-core` / `fonts-dejavu-extra` 2.37-8 (`dpkg
--verify` clean, MD5 equal to the package manifests), Bitstream Vera licence with DejaVu changes in the
public domain:

| File | SHA-256 |
|---|---|
| DejaVuSans.ttf | `57f73e11f51999432bf7ab22ce55b6f945d5eca1bf824404cfa9ec2e3718c84e` |
| DejaVuSans-Bold.ttf | `a4c5bc453ca281d90ea079e596da7ae0dfeb5777497c29ec254e76d97ff6f890` |
| DejaVuSans-Oblique.ttf | `e2f09289f4276309a36b9a93e5a0ac64957ef3eb7158151b243d41f667151ee4` |

`glyph_coverage.json` lists the code points all three faces map (5,226); characters outside it produce
the readiness warning `unsupported_characters` (CJK, Hangul and most astral characters, for example).
LaTeX/MathML are not rendered.

## Export content budget and its calibration (EXP-16)

Budget: **300,000 code points** per exam over everything the answer key prints ([exam-api.md](exam-api.md)
§4). The worst-case render model is ≈ 1.25 MB (< the 1,408,896-byte bound and the 2 MiB input cap). Rule:
every maximum-boundary render must stay below 50 % of every hard limit (30 s wall, 25 s CPU, 512 MiB,
16 MiB PDF / 8 MiB DOCX); otherwise the budget is lowered, not the limits raised.

EXP-16 renders, for both formats and both variants, four shapes at the budget: byte-maximal (4-byte
characters), layout-maximal (narrow ASCII, long unbroken tokens and line breaks), short-answer-maximal
(every field at its limit as long as the budget allows, the rest minimal, 200 items) and — added during
implementation — line-break-maximal (a line break every second code point, the slowest PDF found). The
measured maxima (development machine, Docker on Apple Silicon, test image `python:3.11-slim`):

| Shape | Worst PDF (pages, wall / CPU, peak RSS, size) | Worst DOCX (wall, peak RSS, size) |
|---|---|---|
| byte-maximal (input 1.25 MB) | 200 pages, 0.6 s / 0.6 s, 48 MiB, 0.2 MB | 0.17 s, 41 MiB, 39 KB |
| layout-maximal | 1,601 pages, 3.1 s / 3.1 s, 92 MiB, 1.2 MB | 0.53 s, 69 MiB, 43 KB |
| short-answer-maximal | 881 pages, 2.3 s / 2.3 s, 73 MiB, 0.7 MB | 0.38 s, 53 MiB, 41 KB |
| line-break-maximal | 3,201 pages, 5.4 s / 5.4 s, 141 MiB, 2.3 MB | 0.91 s, 101 MiB, 46 KB |

Peak RSS is the child's own high-water mark after `exec` (`VmHWM`, reported by the child; that is the
address space RLIMIT_AS governs). `ru_maxrss` from `wait4` is not used for this evidence because it also
counts the parent's pages that `fork` copied before `exec` (≈ 650 MiB inside the full pytest run).

The largest shares of the limits are 21.5 % CPU, 17.9 % wall clock, 27.4 % memory and 13.9 % PDF output (all line-break-maximal). **Calibration note:** the first
renderer version laid out each multi-line text as one paragraph; ReportLab re-wraps the remainder of a
split paragraph on every page, so the line-break-maximal PDF took ≈ 16 s CPU (64 % of the limit) and failed
the rule. The renderer now lays out at most 24 source lines per paragraph (linear layout); the budget and
the limits were not changed. These numbers come from one development machine; CI hardware must be
re-measured before release (EXP-16 asserts the 50 % rule wherever it runs and prints its measurements).
