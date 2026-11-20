"""Wikipedia connector: official MediaWiki Action API only, revision-consistent, section locators.

Requests (all GET https://<language>.wikipedia.org/w/api.php, format=json,
formatversion=2, maxlag=HARVEST_MAXLAG_SECONDS; parameters built here only):

- search:  action=query&list=search&srsearch=<approved query>&srnamespace=0
           &srlimit=N&srprop=&srinfo=   -> page ids and titles (namespace 0)
- info:    action=query&prop=info|pageprops|revisions&pageids=<ids>
           &rvprop=ids|timestamp&ppprop=disambiguation
           -> per page: namespace, title, content model, page language,
              latest revision id (lastrevid and revisions[0].revid must agree),
              redirect / disambiguation / missing flags
- licence: action=query&meta=siteinfo&siprop=rightsinfo
           -> the site's declared licence (url and text), compared with the
              expected CC BY-SA 4.0 before any new content is accepted
              (check_rightsinfo); a mismatch or ambiguity fails closed
- content: action=parse&oldid=<revision id from info>&prop=text|sections|revid
           &disableeditsection=1&disabletoc=1&disablelimitreport=1
           -> the rendered HTML of exactly that revision

Revision consistency. The recorded revision id is never obtained separately
from the content: the content request names the revision (oldid) and the
response must echo the same revid and page id, else the candidate is refused
(fail closed). The page itself may have newer revisions by then; that is
detected on the next refresh. MediaWiki renders an old revision with the
templates current at fetch time, so the stored normalized text (and its hash)
is what this fetch produced; refresh compares normalized text, not revisions.

Licence verification. The site-wide licence the provider declares (rightsinfo)
must be Creative Commons Attribution-ShareAlike 4.0 before the first content
of a run is accepted; anything else, or an answer that is missing, malformed
or ambiguous, stops the run (non-retryable). This verifies the site's
declaration only: it does not prove that every sentence of an article is
CC BY-SA, that third-party material is absent from the prose that remains
after filtering, or that quotations are lawful to reuse; see
docs/external-harvesting.md for the documented limitations.

Excluded content (not reproduced, not indexed): tables, infoboxes, figures,
images, media, galleries, captions, maps, math markup, block quotations,
references and citation markers, navigation boxes, hatnotes, maintenance
boxes, edit links and the "See also / References / External links / Further
reading / Notes" sections. Article prose is CC BY-SA 4.0 (site-wide licence);
that does not prove every element is reusable, which is why everything with a
possibly different licence or third-party origin is dropped.

Section locators are genuine: a heading starts a new section only if its
anchor is in the section list the same parse response returned; the lead
(text before the first heading) has level 0 and no anchor or heading. Page
numbers are never produced.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote

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

PROVIDER = "wikipedia"
DISPLAY_NAME = "Wikipedia"
API_PATH = "/w/api.php"
LICENCE = "CC BY-SA 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
# Languages an operator may initialize. Hosts are derived from this list only.
LANGUAGES = frozenset({"en", "de", "fr", "es", "it", "nl", "pt", "pl", "sv", "tr"})
MAX_INFO_PAGES = 50
MAX_TITLE_CHARS = 255
MAX_ANCHOR_CHARS = 255
EXCLUDED_SECTION_TITLES = frozenset(
    {
        "references", "external links", "see also", "further reading", "notes", "bibliography",
        "sources", "citations", "footnotes", "notes and references", "works cited", "literature",
        "explanatory notes", "general and cited references", "gallery",
    }
)  # fmt: skip


def check_rightsinfo(info: object) -> None:
    """Fail closed unless the provider's declared site licence is CC BY-SA 4.0 (ProviderError, not retryable)."""
    if not isinstance(info, dict):
        raise ProviderError("licence_unverified", "rightsinfo missing or malformed", retryable=False)
    url, label = info.get("url"), info.get("text")
    if not isinstance(url, str) or not isinstance(label, str) or not url.strip() or not label.strip():
        raise ProviderError("licence_unverified", "rightsinfo url or text missing", retryable=False)
    if url.strip().rstrip("/").lower() != LICENCE_URL.rstrip("/"):
        raise ProviderError("licence_mismatch", "declared licence URL is not CC BY-SA 4.0", retryable=False)
    folded = label.casefold()
    if "attribution-sharealike" not in folded or "4.0" not in folded or any(
        marker in folded for marker in ("noncommercial", "non-commercial", "noderiv", "no-deriv", "3.0", "2.5", "gfdl")
    ):
        raise ProviderError("licence_mismatch", "declared licence text is not CC BY-SA 4.0", retryable=False)


def endpoint(language: str) -> ProviderEndpoint:
    if language not in LANGUAGES:
        raise ValueError("language is not on the Wikipedia allowlist")
    return ProviderEndpoint(host=f"{language}.wikipedia.org", path=API_PATH)


def canonical_url(language: str, title: str) -> str:
    return f"https://{language}.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="()_,!:'*-.~")


def revision_url(language: str, revision_id: int) -> str:
    return f"https://{language}.wikipedia.org/w/index.php?oldid={revision_id}"


def attribution(language: str, title: str, page_id: int, revision_id: int) -> str:
    history = f"https://{language}.wikipedia.org/w/index.php?curid={page_id}&action=history"
    return (
        f'"{title}", Wikipedia ({language}), by Wikipedia contributors (authors: {history}), '
        f"revision {revision_id}, {revision_url(language, revision_id)}, licensed under {LICENCE} "
        f"({LICENCE_URL}). Article prose only; tables, images, media, quotations and references were excluded."
    )


# --- errors ---------------------------------------------------------------------------------------


class ProviderError(Exception):
    def __init__(self, code: str, detail: str = "", *, retryable: bool) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.retryable = retryable


class ProviderThrottled(ProviderError):
    """The provider asked us to slow down (429/503, maxlag, ratelimited). Not a failed attempt."""

    def __init__(self, code: str, retry_after_seconds: float) -> None:
        super().__init__(code, f"retry after {retry_after_seconds:.0f}s", retryable=True)
        self.retry_after_seconds = retry_after_seconds


class CandidateRefused(Exception):
    """This page/revision is not acceptable or the response about it was ambiguous. Skip it, never stale it."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# --- results --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    page_id: int
    title: str


@dataclass(frozen=True)
class PageInfo:
    page_id: int
    present: bool  # False: the API reported the page id missing
    namespace: int | None = None
    title: str | None = None
    redirect: bool = False
    disambiguation: bool = False
    content_model: str | None = None
    page_language: str | None = None
    revision_id: int | None = None
    revision_timestamp: datetime | None = None

    @property
    def gone_reason(self) -> str | None:
        """Why this page no longer holds an article we can keep (evidence for a missing check)."""
        if not self.present:
            return "missing"
        if self.redirect:
            return "redirect"
        if self.namespace != 0:
            return "namespace"
        return None


@dataclass(frozen=True)
class ExtractedSection:
    anchor: str | None
    heading: str | None
    level: int
    text: str

    @property
    def locator(self) -> dict:
        return {"anchor": self.anchor, "heading": self.heading, "level": self.level}


@dataclass(frozen=True)
class FetchedRevision:
    page_id: int
    revision_id: int
    title: str
    sections: tuple[ExtractedSection, ...]
    content_sha256: str
    char_count: int


def content_sha256(sections) -> str:
    """Normalized content hash: canonical JSON of the sections (locator + text), in order."""
    material = [{"anchor": s.anchor, "heading": s.heading, "level": s.level, "text": s.text} for s in sections]
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --- HTML -> sections ------------------------------------------------------------------------------

_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_SKIP_TAGS = frozenset(
    {
        "table", "figure", "figcaption", "style", "script", "noscript", "math", "img", "video", "audio",
        "svg", "blockquote", "pre", "map", "object", "iframe", "button", "form", "input", "select",
        "textarea", "caption", "gallery", "template", "picture", "canvas", "annotation", "semantics",
    }
)  # fmt: skip
_SKIP_CLASSES = frozenset(
    {
        "reference", "references", "reflist", "mw-references-wrap", "navbox", "navbox-styles", "vertical-navbox",
        "infobox", "sidebar", "hatnote", "dablink", "metadata", "noprint", "mw-editsection", "thumb", "thumbinner",
        "gallery", "mw-empty-elt", "shortdescription", "toc", "portalbox", "sistersitebox", "side-box",
        "mw-cite-backlink", "mbox-small", "ambox", "ombox", "tmbox", "cmbox", "fmbox", "imbox", "quotebox",
        "rquote", "pullquote", "mwe-math-element", "authority-control", "catlinks", "printfooter",
        "mw-kartographer-maplink", "geo-nondefault", "mw-file-element", "mw-halign-right", "mw-halign-left",
        "plainlist", "wikitable", "mw-collapsible", "reference-text", "citation", "cs1-maint",
    }
)  # fmt: skip
_SKIP_CLASS_PREFIXES = ("box-", "navbox", "infobox", "mw-ext-", "mbox", "metadata")
_SKIP_ROLES = frozenset({"navigation", "note", "presentation", "figure", "math", "img"})
_BLOCK = frozenset({"p", "li", "dd", "dt", "div", "ul", "ol", "dl", "section", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr"})
_HEADINGS = {"h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤﻿]")
_SPACES = re.compile(r"[ \t\r\f\v   -     　]+")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = _INVISIBLE.sub("", value)
    value = "".join(ch if ch == "\n" or not unicodedata.category(ch).startswith("C") else " " for ch in value)
    lines = (_SPACES.sub(" ", line).strip() for line in value.split("\n"))
    return "\n".join(line for line in lines if line)


def _skipped(tag: str, attrs: dict[str, str | None]) -> bool:
    if tag in _SKIP_TAGS:
        return True
    classes = (attrs.get("class") or "").split()
    if any(c in _SKIP_CLASSES or c.startswith(_SKIP_CLASS_PREFIXES) for c in classes):
        return True
    if (attrs.get("role") or "").strip().lower() in _SKIP_ROLES:
        return True
    style = (attrs.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


class _SectionExtractor(HTMLParser):
    def __init__(self, anchors: frozenset[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._anchors = anchors
        self._stack: list[tuple[str, bool]] = []
        self._skip_depth = 0
        self._heading: dict | None = None  # {"level", "anchor", "parts"} while inside h2..h6
        self._excluded_level: int | None = None  # inside an excluded section of this level
        self._buffer: list[str] = []
        self._blocks: list[str] = []
        self.sections: list[tuple[str | None, str | None, int, list[str]]] = [(None, None, 0, self._blocks)]

    # structure

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attributes = {k.lower(): v for k, v in attrs}
        if tag in _VOID:
            if tag in ("br", "hr"):
                self._flush()
            return
        skip = self._skip_depth > 0 or _skipped(tag, attributes)
        self._stack.append((tag, skip))
        if skip:
            self._skip_depth += 1
            return
        if tag in _HEADINGS:
            self._flush()
            self._heading = {"level": _HEADINGS[tag], "anchor": attributes.get("id"), "parts": []}
        elif self._heading is not None and tag == "span" and "mw-headline" in (attributes.get("class") or "").split():
            self._heading["anchor"] = self._heading["anchor"] or attributes.get("id")
        elif tag in _BLOCK:
            self._flush()

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag.lower() in ("br", "hr"):
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID or not any(open_tag == tag for open_tag, _ in self._stack):
            return
        while self._stack:
            open_tag, skip = self._stack.pop()
            if skip:
                self._skip_depth -= 1
            elif open_tag in _HEADINGS and self._heading is not None:
                self._close_heading()
            elif open_tag in _BLOCK:
                self._flush()
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._heading is not None:
            self._heading["parts"].append(data)
        elif self._excluded_level is None:
            self._buffer.append(data)

    # helpers

    def _flush(self) -> None:
        if self._buffer:
            text = normalize_text("".join(self._buffer))
            self._buffer = []
            if text:
                self._blocks.append(text)

    def _close_heading(self) -> None:
        heading, self._heading = self._heading, None
        anchor = heading["anchor"]
        title = normalize_text("".join(heading["parts"])).replace("\n", " ")
        level = heading["level"]
        if not anchor or anchor not in self._anchors or not title:
            return  # not a heading the provider listed: never invent a section
        if self._excluded_level is not None and level > self._excluded_level:
            return  # a subsection of an excluded section stays excluded
        self._excluded_level = None
        if title.casefold() in EXCLUDED_SECTION_TITLES:
            self._excluded_level = level
            return
        self._flush()
        self._blocks = []
        self.sections.append((anchor, title[:MAX_TITLE_CHARS].strip(), level, self._blocks))

    def close(self) -> None:
        super().close()
        self._flush()


def extract_sections(html: str, anchors: frozenset[str]) -> list[ExtractedSection]:
    parser = _SectionExtractor(anchors)
    parser.feed(html)
    parser.close()
    sections = []
    for anchor, heading, level, blocks in parser.sections:
        text = "\n".join(blocks).strip()
        if text:
            sections.append(ExtractedSection(anchor=anchor, heading=heading, level=level, text=text))
    return sections


# --- connector -------------------------------------------------------------------------------------


def _int(value, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProviderError("invalid_response", f"{name} is not an integer >= {minimum}", retryable=True)
    return value


def _title(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TITLE_CHARS or any(
        unicodedata.category(ch).startswith("C") for ch in value
    ):
        raise ProviderError("invalid_response", "title is not a printable string", retryable=True)
    return value


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class WikipediaConnector:
    def __init__(self, transport: ProviderTransport, settings: Settings, language: str) -> None:
        self._transport = transport
        self._settings = settings
        self.language = language
        self._endpoint = endpoint(language)

    def _bounds(self) -> TransportBounds:
        s = self._settings
        return TransportBounds(
            connect_timeout=s.harvest_connect_timeout_seconds,
            read_timeout=s.harvest_read_timeout_seconds,
            deadline=s.harvest_request_deadline_seconds,
            max_response_bytes=s.harvest_max_response_bytes,
        )

    def _call(self, params: dict[str, str]) -> dict:
        query = {**params, "format": "json", "formatversion": "2", "maxlag": str(self._settings.harvest_maxlag_seconds)}
        try:
            response = self._transport.get(self._endpoint, query, self._bounds())
        except TransportError as exc:
            if exc.throttle_status is not None and exc.retry_after_seconds is not None:
                raise ProviderThrottled(f"http_{exc.throttle_status}", exc.retry_after_seconds) from None
            raise ProviderError(f"transport_{exc.code}", exc.detail, retryable=exc.retryable) from None
        return self._classify(response)

    def _classify(self, response: ProviderResponse) -> dict:
        s = self._settings
        retry_after = response.retry_after_seconds
        if response.status in (429, 503):
            raise ProviderThrottled(f"http_{response.status}", retry_after if retry_after is not None else s.harvest_throttle_default_seconds)
        if response.status in (408, 500, 502, 504):
            raise ProviderError(f"http_{response.status}", retryable=True)
        if response.status != 200:
            # 403 is what Wikimedia answers to a policy-violating client: stop, do not hammer.
            raise ProviderError(f"http_{response.status}", "provider refused the request", retryable=False)
        try:
            body = strict_json_object(response)
        except InvalidJson as exc:
            raise ProviderError("invalid_json", str(exc), retryable=True) from None
        error = body.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            if code == "maxlag":
                raise ProviderThrottled("maxlag", retry_after if retry_after is not None else s.harvest_maxlag_seconds)
            if code == "ratelimited":
                raise ProviderThrottled("ratelimited", retry_after if retry_after is not None else s.harvest_throttle_default_seconds)
            raise ProviderError("api_error", str(code)[:64] if code else "unknown", retryable=False)
        return body

    # --- operations ---

    def verify_licence(self) -> None:
        """One request: the provider's declared site licence must be the expected CC BY-SA 4.0 (else ProviderError)."""
        body = self._call({"action": "query", "meta": "siteinfo", "siprop": "rightsinfo"})
        query = body.get("query")
        check_rightsinfo(query.get("rightsinfo") if isinstance(query, dict) else None)

    def search(self, query: str, limit: int) -> list[SearchHit]:
        body = self._call(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srnamespace": "0",
                "srlimit": str(limit),
                "srprop": "",
                "srinfo": "",
            }
        )
        results = body.get("query", {}).get("search") if isinstance(body.get("query"), dict) else None
        if not isinstance(results, list) or len(results) > limit:
            raise ProviderError("invalid_response", "search results missing or over the limit", retryable=True)
        hits: list[SearchHit] = []
        seen: set[int] = set()
        for item in results:
            if not isinstance(item, dict) or item.get("ns") != 0:
                continue  # only articles; never another namespace
            page_id = _int(item.get("pageid"), "pageid", 1)
            if page_id not in seen:
                seen.add(page_id)
                hits.append(SearchHit(page_id=page_id, title=_title(item.get("title"))))
        return hits

    def page_info(self, page_ids: list[int]) -> dict[int, PageInfo]:
        if not 1 <= len(page_ids) <= MAX_INFO_PAGES or len(set(page_ids)) != len(page_ids):
            raise ValueError("page_info takes 1..50 distinct page ids")
        body = self._call(
            {
                "action": "query",
                "prop": "info|pageprops|revisions",
                "pageids": "|".join(str(p) for p in page_ids),
                "rvprop": "ids|timestamp",
                "ppprop": "disambiguation",
            }
        )
        pages = body.get("query", {}).get("pages") if isinstance(body.get("query"), dict) else None
        if not isinstance(pages, list):
            raise ProviderError("invalid_response", "query.pages missing", retryable=True)
        result: dict[int, PageInfo] = {}
        for page in pages:
            if not isinstance(page, dict):
                raise ProviderError("invalid_response", "page entry is not an object", retryable=True)
            page_id = _int(page.get("pageid"), "pageid", 1)
            if page_id not in page_ids or page_id in result:
                raise ProviderError("invalid_response", "unexpected or repeated page id", retryable=True)
            if page.get("missing") is True:
                result[page_id] = PageInfo(page_id=page_id, present=False)
                continue
            revisions = page.get("revisions")
            revision = revisions[0] if isinstance(revisions, list) and len(revisions) == 1 else None
            last = page.get("lastrevid")
            revision_id = None
            if isinstance(revision, dict) and type(revision.get("revid")) is int and revision["revid"] == last:
                revision_id = _int(last, "lastrevid", 1)
            pageprops = page.get("pageprops")
            result[page_id] = PageInfo(
                page_id=page_id,
                present=True,
                namespace=_int(page.get("ns"), "ns"),
                title=_title(page.get("title")),
                redirect=page.get("redirect") is True,
                disambiguation=isinstance(pageprops, dict) and "disambiguation" in pageprops,
                content_model=page.get("contentmodel") if isinstance(page.get("contentmodel"), str) else None,
                page_language=page.get("pagelanguage") if isinstance(page.get("pagelanguage"), str) else None,
                revision_id=revision_id,  # None: lastrevid and the revision disagree (ambiguous)
                revision_timestamp=_timestamp(revision.get("timestamp")) if isinstance(revision, dict) else None,
            )
        if set(result) != set(page_ids):
            raise ProviderError("invalid_response", "not every requested page id was answered", retryable=True)
        return result

    def check_article(self, info: PageInfo) -> None:
        """Quality gate on page metadata; CandidateRefused if it is not an acceptable article."""
        if info.gone_reason is not None:
            raise CandidateRefused(f"page_{info.gone_reason}")
        if info.disambiguation:
            raise CandidateRefused("disambiguation_page")
        if info.content_model != "wikitext":
            raise CandidateRefused("content_model")
        if info.page_language != self.language:
            raise CandidateRefused("page_language")
        if info.revision_id is None:
            raise CandidateRefused("revision_ambiguous")

    def fetch_revision(self, info: PageInfo) -> FetchedRevision:
        """The normalized sections of exactly info.revision_id (verified), or CandidateRefused."""
        self.check_article(info)
        body = self._call(
            {
                "action": "parse",
                "oldid": str(info.revision_id),
                "prop": "text|sections|revid",
                "disableeditsection": "1",
                "disabletoc": "1",
                "disablelimitreport": "1",
            }
        )
        parse = body.get("parse")
        if not isinstance(parse, dict):
            raise ProviderError("invalid_response", "parse missing", retryable=True)
        if parse.get("revid") != info.revision_id or type(parse.get("revid")) is not int:
            raise CandidateRefused("revision_mismatch", "parse did not return the requested revision")
        if parse.get("pageid") != info.page_id or type(parse.get("pageid")) is not int:
            raise CandidateRefused("page_mismatch", "the revision belongs to another page")
        title = _title(parse.get("title"))
        html = parse.get("text")
        listed = parse.get("sections")
        if not isinstance(html, str) or not isinstance(listed, list):
            raise ProviderError("invalid_response", "text or sections missing", retryable=True)
        anchors = frozenset(
            item["anchor"]
            for item in listed
            if isinstance(item, dict)
            and isinstance(item.get("anchor"), str)
            and 0 < len(item["anchor"]) <= MAX_ANCHOR_CHARS
            and not re.search(r"[\s\x00-\x1f\x7f]", item["anchor"])
        )
        sections = extract_sections(html, anchors)
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
        return FetchedRevision(
            page_id=info.page_id,
            revision_id=info.revision_id,
            title=title,
            sections=tuple(sections),
            content_sha256=content_sha256(sections),
            char_count=chars,
        )


def lexically_relevant(query: str, fetched: FetchedRevision) -> bool:
    """A lexical gate, NOT semantic relevance: some query word (>= 3 letters) occurs in the title or lead.

    Embedding-based relevance is deferred until the AI service offers it
    (docs/external-harvesting.md); nothing here claims similarity.
    """
    words = {w for w in re.findall(r"\w{3,}", query.casefold())}
    if not words:
        return False
    lead = fetched.sections[0].text if fetched.sections and fetched.sections[0].level == 0 else ""
    haystack = set(re.findall(r"\w{3,}", (fetched.title + " " + lead[:5000]).casefold()))
    return bool(words & haystack)
