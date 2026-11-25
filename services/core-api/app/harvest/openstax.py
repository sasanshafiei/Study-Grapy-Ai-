"""OpenStax connector (WP-6): one allowlisted textbook, read from OpenStax's own published book source.

Source. OpenStax publishes the source of each book in a public repository of
its own GitHub organization; for Chemistry 2e that is
github.com/openstax/osbooks-chemistry-bundle, whose collection file carries
the same book UUID that openstax.org lists for the book (CMS `cnx_id`). The
content is CNXML (OpenStax's XML format): one collection file per book
(metadata, licence, table of contents) and one file per module (book page).
It is read with GitHub's documented REST API (api.github.com), GET only,
unauthenticated, through the restricted transport (app/harvest/transport.py);
paths are built here, every git object id in a path is a validated 40-hex
id taken from an earlier answer of the same snapshot:

- head:  /repos/<repo>/branches/<branch>  -> commit SHA, its tree SHA, commit time
- tree:  /repos/<repo>/git/trees/<tree SHA>?recursive=1  -> blob SHAs of the
         book's collection file and of every modules/m<digits>/index.cnxml
         (refused if GitHub reports the listing as truncated)
- blob:  /repos/<repo>/git/blobs/<blob SHA>  -> base64 content; the git object
         id computed from the decoded bytes must equal the requested SHA, so
         the content is exactly that version (content-addressed)

No query text, course, user or document data is ever sent: the only
variable parts of a request are git object ids GitHub itself returned.

Version identity. OpenStax has no numeric revision ids (its publishing
pipeline versions books by git commit). A module's version is the blob SHA
of its file (`revision_key`); the commit of the snapshot is recorded in the
revision URL and the attribution. Nothing numeric is invented: `page_id`
holds the digits of OpenStax's module id (m68664 -> 68664), a lossless
encoding of the provider's own identifier.

Licence. Verified on every run from the pinned collection file itself, before
any module of the run is fetched: md:license must be CC BY-NC-SA 4.0 (URL and
text), and md:uuid, md:slug, md:title and md:language must match the
allowlist entry. Any other answer fails the run (non-retryable): a relicensed
or replaced book needs a code and licence review, never a silent switch. The
licence is NonCommercial and ShareAlike: see docs/external-harvesting.md for
the obligations this places on every deployment and derived output.

Excluded (not stored, not indexed): figures, media, captions (they carry
third-party image credits), tables, equations and all MathML, worked examples,
exercises/problems/solutions, footnotes, glossaries, block quotations,
"Link to Learning" boxes (external sites) and end-of-chapter exercise or
key-equation sections. Only modules inside a chapter are harvested (preface
and appendices are not).

Section locators are genuine: a CNXML <section> starts a section only if it
has both an id (the anchor OpenStax itself uses) and a title; its level is 2
for a top-level section and grows with nesting up to 6. The module text
before its first section is the lead (no anchor, no heading, level 0).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

from ..config import Settings
from .transport import (
    InvalidJson,
    ProviderEndpoint,
    ProviderResponse,
    ProviderTransport,
    TransportBounds,
    TransportError,
    strict_json_object,
)
from .wikipedia import (
    CandidateRefused,
    ExtractedSection,
    ProviderError,
    ProviderThrottled,
    content_sha256,
    normalize_text,
)

PROVIDER = "openstax"
DISPLAY_NAME = "OpenStax"
API_HOST = "api.github.com"
LICENCE = "CC BY-NC-SA 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
LICENCE_TEXT = "attribution-noncommercial-sharealike"
MAX_TITLE_CHARS = 255
MAX_ANCHOR_CHARS = 255
MAX_XML_DEPTH = 100
# Request budget written by `init-source` for an OpenStax source: GitHub allows 60 unauthenticated REST requests
# per hour per client address; this stays below it with spacing (docs/external-harvesting.md section 12).
BUDGET_WINDOW_SECONDS = 3600
BUDGET_MAX_REQUESTS = 50
BUDGET_MIN_INTERVAL_MS = 2000

_SHA = re.compile(r"[0-9a-f]{40}")
_MODULE_ID = re.compile(r"m[1-9][0-9]{0,15}")
_MODULE_PATH = re.compile(r"modules/(m[1-9][0-9]{0,15})/index\.cnxml")
_ANCHOR = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*")
_DOCTYPE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

CNXML = "{http://cnx.rice.edu/cnxml}"
COLLXML = "{http://cnx.rice.edu/collxml}"
MDML = "{http://cnx.rice.edu/mdml}"


@dataclass(frozen=True)
class Book:
    slug: str
    title: str
    uuid: str
    language: str
    repository: str
    branch: str
    authors: str
    year: int
    book_url: str


# The allowlist. Each entry was checked against openstax.org (CMS: title, cnx_id, licence) and the official
# repository (collection metadata and LICENSE) on 2026-11-25; see docs/external-harvesting.md section 12.
BOOKS = {
    "chemistry-2e": Book(
        slug="chemistry-2e",
        title="Chemistry 2e",
        uuid="7fccc9cf-9b71-44f6-800b-f9457fd64335",
        language="en",
        repository="openstax/osbooks-chemistry-bundle",
        branch="main",
        authors="Paul Flowers, Klaus Theopold, Richard Langley, William R. Robinson, PhD",
        year=2019,
        book_url="https://openstax.org/details/books/chemistry-2e",
    ),
}


def book(slug: str | None) -> Book:
    found = BOOKS.get(slug or "")
    if found is None:
        raise ProviderError("book_not_allowlisted", "the source names no allowlisted OpenStax book", retryable=False)
    return found


def canonical_url(b: Book, module_id: str) -> str:
    return f"https://github.com/{b.repository}/blob/{b.branch}/modules/{module_id}/index.cnxml"


def revision_url(b: Book, commit: str, module_id: str) -> str:
    return f"https://github.com/{b.repository}/blob/{commit}/modules/{module_id}/index.cnxml"


def document_title(b: Book, chapter: str, module_title: str) -> str:
    return f"{b.title}: {chapter} - {module_title}"[:MAX_TITLE_CHARS].strip()


def attribution(b: Book, *, module_id: str, module_title: str, chapter: str, commit: str) -> str:
    return (
        f'"{module_title}" ({chapter}), {b.title} by {b.authors}, OpenStax, Rice University ({b.year}), '
        f"{b.book_url}. Access for free at openstax.org. Source module {module_id} at commit {commit}: "
        f"{revision_url(b, commit, module_id)}. Licensed under {LICENCE} ({LICENCE_URL}). Changes: text only; "
        "figures, tables, mathematics, examples, exercises and media were excluded and the text was normalized."
    )


def git_blob_sha(data: bytes) -> str:
    """The git object id of a blob with these bytes (what GitHub names it by)."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data, usedforsecurity=False).hexdigest()


# --- results --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    commit: str
    tree: str
    committed_at: datetime | None


@dataclass(frozen=True)
class ModuleRef:
    module_id: str
    blob: str
    chapter: str

    @property
    def page_id(self) -> int:
        return int(self.module_id[1:])


@dataclass(frozen=True)
class FetchedModule:
    page_id: int
    module_id: str
    revision_key: str
    commit: str
    committed_at: datetime | None
    chapter: str
    module_title: str
    title: str
    sections: tuple[ExtractedSection, ...]
    content_sha256: str
    char_count: int


@dataclass(frozen=True)
class GoneModule:
    """Evidence for a missing check: the module is no longer part of the book at the snapshot."""

    page_id: int
    commit: str
    gone_reason: str = "not_in_book"
    namespace: None = None


# --- CNXML ----------------------------------------------------------------------------------------


def _xml(data: bytes, refuse) -> ET.Element:
    """Parse provider XML; documents declaring a DOCTYPE or entities are refused (no expansion, no XXE)."""
    if _DOCTYPE.search(data):
        raise refuse("xml_doctype")
    try:
        return ET.fromstring(data, parser=ET.XMLParser())
    except (ET.ParseError, ValueError):
        raise refuse("invalid_xml") from None


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return normalize_text("".join(element.itertext())).replace("\n", " ").strip()


def check_licence(url: object, label: object) -> None:
    """Fail closed unless the declared licence is CC BY-NC-SA 4.0 (ProviderError, not retryable)."""
    if not isinstance(url, str) or not isinstance(label, str) or not url.strip() or not label.strip():
        raise ProviderError("licence_unverified", "md:license url or text missing", retryable=False)
    normalized = re.sub(r"^http://", "https://", url.strip().lower()).rstrip("/")
    if normalized != LICENCE_URL.rstrip("/"):
        raise ProviderError("licence_mismatch", "declared licence URL is not CC BY-NC-SA 4.0", retryable=False)
    folded = " ".join(label.casefold().split())
    if LICENCE_TEXT not in folded or "4.0" not in folded or any(
        marker in folded for marker in ("noderiv", "no-deriv", "3.0", "2.5", "2.0")
    ):
        raise ProviderError("licence_mismatch", "declared licence text is not CC BY-NC-SA 4.0", retryable=False)


def parse_collection(data: bytes, b: Book) -> list[tuple[str, str]]:
    """(module id, chapter title) of every module inside a chapter, in book order; verifies licence and identity."""

    def refuse(code: str) -> ProviderError:
        return ProviderError(code, "collection file", retryable=False)

    root = _xml(data, refuse)
    if root.tag != f"{COLLXML}collection":
        raise refuse("invalid_collection")
    metadata = root.find(f"{COLLXML}metadata")
    if metadata is None:
        raise refuse("invalid_collection")
    licence = metadata.findall(f"{MDML}license")
    if len(licence) != 1:
        raise ProviderError("licence_unverified", "exactly one md:license expected", retryable=False)
    check_licence(licence[0].get("url"), "".join(licence[0].itertext()))
    expected = {"uuid": b.uuid, "slug": b.slug, "title": b.title, "language": b.language}
    for name, value in expected.items():
        found = metadata.findall(f"{MDML}{name}")
        if len(found) != 1 or _text(found[0]) != value:
            raise ProviderError("book_mismatch", f"md:{name} does not match the allowlisted book", retryable=False)
    content = root.find(f"{COLLXML}content")
    if content is None:
        raise refuse("invalid_collection")
    modules: list[tuple[str, str]] = []
    for chapter in content.findall(f"{COLLXML}subcollection"):  # top-level modules (preface, appendices) skipped
        title = _text(chapter.find(f"{MDML}title"))
        if not title:
            raise refuse("invalid_collection")
        for module in chapter.iter(f"{COLLXML}module"):
            module_id = module.get("document") or ""
            if not _MODULE_ID.fullmatch(module_id):
                raise refuse("invalid_collection")
            modules.append((module_id, title[:MAX_TITLE_CHARS]))
    if not modules or len({m for m, _ in modules}) != len(modules):
        raise refuse("invalid_collection")
    return modules


_SKIP_TAGS = frozenset(
    {"figure", "media", "image", "caption", "table", "equation", "example", "exercise", "problem", "solution",
     "footnote", "glossary", "iframe", "object", "embed", "code", "preformat"}
)  # fmt: skip
_SKIP_CLASSES = frozenset({"exercises", "key-equations", "link-to-learning", "references"})
_BLOCK_TAGS = frozenset({"para", "item", "list", "note", "title", "div"})


class _ModuleExtractor:
    """Text blocks per genuine section. `_open` holds the indexes (into `sections`) of the enclosing open sections.

    Text that follows a nested section inside its parent continues the parent under the parent's own locator.
    Text outside every titled section after the module's first section has no genuine locator (only the first
    section may be the lead) and is not kept.
    """

    def __init__(self) -> None:
        self.sections: list[tuple[str | None, str | None, int, list[str]]] = [(None, None, 0, [])]
        self._open = [0]
        self._buffer: list[str] = []
        self._anchors: set[str] = set()

    def _flush(self) -> None:
        if self._buffer:
            text = normalize_text("".join(self._buffer)).replace("\n", " ").strip()
            self._buffer = []
            if not text:
                return
            current = self._open[-1]
            if current != len(self.sections) - 1:  # back in an enclosing section after a nested one
                anchor, heading, level, _ = self.sections[current]
                if level == 0:
                    return
                self.sections.append((anchor, heading, level, []))
                self._open[-1] = len(self.sections) - 1
            self.sections[-1][3].append(text)

    def _data(self, value: str | None) -> None:
        if value:
            self._buffer.append(value)

    def walk(self, element: ET.Element, depth: int, level: int) -> None:
        if depth > MAX_XML_DEPTH:
            raise CandidateRefused("xml_too_deep")
        if not isinstance(element.tag, str) or not element.tag.startswith(CNXML):
            return  # MathML and every other namespace: excluded (its tail is handled by the parent)
        tag = element.tag[len(CNXML):]
        classes = set((element.get("class") or "").split())
        if tag in _SKIP_TAGS or classes & _SKIP_CLASSES or (tag == "quote" and element.get("display") == "block"):
            return
        if tag == "newline":
            self._data(" ")
            return
        if tag == "section":
            self._section(element, depth, level)
            return
        block = tag in _BLOCK_TAGS
        if block:
            self._flush()
        self._data(element.text)
        for child in element:
            self.walk(child, depth + 1, level)
            self._data(child.tail)
        if block:
            self._flush()

    def _section(self, element: ET.Element, depth: int, level: int) -> None:
        self._flush()
        title_element = element.find(f"{CNXML}title")
        heading = _text(title_element)[:MAX_TITLE_CHARS].strip()
        anchor = element.get("id") or ""
        nested = min(level + 1, 6) if level >= 2 else 2
        if (
            heading
            and level < 6
            and len(anchor) <= MAX_ANCHOR_CHARS
            and _ANCHOR.fullmatch(anchor)
            and anchor not in self._anchors
        ):
            self._anchors.add(anchor)
            self.sections.append((anchor, heading, nested, []))
            self._open.append(len(self.sections) - 1)
            opened = True
        else:
            opened = False
        for child in element:
            if child is not title_element:
                self.walk(child, depth + 1, nested)
                self._data(child.tail)
        self._flush()
        if opened:
            self._open.pop()


def parse_module(data: bytes, module_id: str) -> tuple[str, list[ExtractedSection]]:
    """(module title, sections) of one CNXML module; CandidateRefused if it is not exactly that module."""
    root = _xml(data, CandidateRefused)
    if root.tag != f"{CNXML}document":
        raise CandidateRefused("not_cnxml")
    metadata = root.find(f"{CNXML}metadata")
    content_ids = [] if metadata is None else metadata.findall(f"{MDML}content-id")
    if len(content_ids) != 1 or _text(content_ids[0]) != module_id:
        raise CandidateRefused("module_mismatch", "the file does not declare the requested module id")
    title = _text(root.find(f"{CNXML}title"))[:MAX_TITLE_CHARS].strip()
    content = root.find(f"{CNXML}content")
    if not title or content is None:
        raise CandidateRefused("invalid_module")
    extractor = _ModuleExtractor()
    extractor._data(content.text)
    for child in content:
        extractor.walk(child, 1, 0)
        extractor._data(child.tail)
    extractor._flush()
    sections = []
    for anchor, heading, level, blocks in extractor.sections:
        text = "\n".join(blocks).strip()
        if text:
            sections.append(ExtractedSection(anchor=anchor, heading=heading, level=level, text=text))
    return title, sections


# --- connector ------------------------------------------------------------------------------------


def _sha(value: object, what: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ProviderError("invalid_response", f"{what} is not a git object id", retryable=True)
    return value


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class OpenStaxConnector:
    def __init__(self, transport: ProviderTransport, settings: Settings, book_slug: str | None) -> None:
        self._transport = transport
        self._settings = settings
        self.book = book(book_slug)
        self.language = self.book.language

    def _bounds(self) -> TransportBounds:
        s = self._settings
        return TransportBounds(
            connect_timeout=s.harvest_connect_timeout_seconds,
            read_timeout=s.harvest_read_timeout_seconds,
            deadline=s.harvest_request_deadline_seconds,
            max_response_bytes=s.harvest_max_response_bytes,
        )

    def _get(self, path: str, params: dict[str, str]) -> dict:
        endpoint = ProviderEndpoint(host=API_HOST, path=f"/repos/{self.book.repository}/{path}")
        try:
            response = self._transport.get(endpoint, params, self._bounds())
        except TransportError as exc:
            if exc.throttle_status is not None and exc.retry_after_seconds is not None:
                raise ProviderThrottled(f"http_{exc.throttle_status}", exc.retry_after_seconds) from None
            raise ProviderError(f"transport_{exc.code}", exc.detail, retryable=exc.retryable) from None
        return self._classify(response)

    def _classify(self, response: ProviderResponse) -> dict:
        s = self._settings
        status = response.status
        waits = [w for w in (response.retry_after_seconds, response.rate_limit_reset_seconds) if w is not None]
        if status in (403, 429) and waits:
            # GitHub's primary (x-ratelimit-*) and secondary (Retry-After) limits: wait the longer one.
            raise ProviderThrottled(f"http_{status}", max(waits))
        if status in (429, 503):
            raise ProviderThrottled(f"http_{status}", max(waits) if waits else s.harvest_throttle_default_seconds)
        if status in (408, 500, 502, 504):
            raise ProviderError(f"http_{status}", retryable=True)
        if status != 200:
            raise ProviderError(f"http_{status}", "provider refused the request", retryable=False)
        try:
            return strict_json_object(response)
        except InvalidJson as exc:
            raise ProviderError("invalid_json", str(exc), retryable=True) from None

    def _blob(self, sha: str, refuse) -> bytes:
        body = self._get(f"git/blobs/{_sha(sha, 'blob')}", {})
        content, size = body.get("content"), body.get("size")
        if body.get("sha") != sha or body.get("encoding") != "base64" or type(size) is not int or not isinstance(content, str):
            raise refuse("blob_invalid")
        try:
            data = base64.b64decode("".join(content.split()), validate=True)
        except (binascii.Error, ValueError):
            raise refuse("blob_invalid") from None
        if len(data) != size or git_blob_sha(data) != sha:
            raise refuse("blob_mismatch")
        return data

    # --- operations (one request each) ---

    def head(self) -> Snapshot:
        body = self._get(f"branches/{self.book.branch}", {})
        commit = body.get("commit")
        inner = commit.get("commit") if isinstance(commit, dict) else None
        if body.get("name") != self.book.branch or not isinstance(inner, dict):
            raise ProviderError("invalid_response", "branch answer malformed", retryable=True)
        tree = inner.get("tree")
        committer = inner.get("committer")
        return Snapshot(
            commit=_sha(commit.get("sha"), "commit"),
            tree=_sha(tree.get("sha") if isinstance(tree, dict) else None, "tree"),
            committed_at=_timestamp(committer.get("date") if isinstance(committer, dict) else None),
        )

    def tree(self, tree_sha: str) -> tuple[str, dict[str, str]]:
        """(collection blob SHA, {module id: blob SHA}) of the snapshot's tree."""
        body = self._get(f"git/trees/{_sha(tree_sha, 'tree')}", {"recursive": "1"})
        entries = body.get("tree")
        if body.get("sha") != tree_sha or not isinstance(entries, list):
            raise ProviderError("invalid_response", "tree answer malformed", retryable=True)
        if body.get("truncated") is not False:
            raise ProviderError("tree_truncated", "GitHub truncated the tree listing", retryable=False)
        collection_path = f"collections/{self.book.slug}.collection.xml"
        collection: str | None = None
        modules: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "blob" or not isinstance(entry.get("path"), str):
                continue
            path = entry["path"]
            if path == collection_path:
                collection = _sha(entry.get("sha"), "collection")
            elif match := _MODULE_PATH.fullmatch(path):
                modules[match.group(1)] = _sha(entry.get("sha"), "module")
        if collection is None:
            raise ProviderError("collection_missing", "the book's collection file is not in the tree", retryable=False)
        return collection, modules

    def collection(self, collection_sha: str) -> list[tuple[str, str]]:
        """Verified licence and book identity; then (module id, chapter) in book order."""
        data = self._blob(collection_sha, lambda code: ProviderError(code, "collection file", retryable=False))
        return parse_collection(data, self.book)

    def fetch_module(self, ref: ModuleRef, commit: str, committed_at: datetime | None) -> FetchedModule:
        """The normalized sections of exactly the module blob `ref.blob` (verified), or CandidateRefused."""
        data = self._blob(ref.blob, CandidateRefused)
        module_title, sections = parse_module(data, ref.module_id)
        s = self._settings
        if not sections:
            raise CandidateRefused("no_text")
        if len(sections) > s.harvest_max_sections:
            raise CandidateRefused("too_many_sections")
        chars = sum(len(section.text) for section in sections)
        if chars < s.harvest_min_document_chars:
            raise CandidateRefused("too_short")
        if chars > s.harvest_max_document_chars:
            raise CandidateRefused("too_long")
        return FetchedModule(
            page_id=ref.page_id,
            module_id=ref.module_id,
            revision_key=ref.blob,
            commit=commit,
            committed_at=committed_at,
            chapter=ref.chapter,
            module_title=module_title,
            title=document_title(self.book, ref.chapter, module_title),
            sections=tuple(sections),
            content_sha256=content_sha256(sections),
            char_count=chars,
        )
