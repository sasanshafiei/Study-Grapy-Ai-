"""Wikipedia connector (M6): fixed API requests, revision consistency, quality gates, section extraction.

Offline: a fake transport returns recorded-style MediaWiki responses and
records every request. No network access.
"""

from __future__ import annotations

import pytest

from app.harvest import wikipedia
from app.harvest.transport import TransportError
from app.harvest.wikipedia import (
    CandidateRefused,
    ProviderError,
    ProviderThrottled,
    WikipediaConnector,
    content_sha256,
    extract_sections,
    lexically_relevant,
)
from tests.harvest_support import harvest_settings, info, json_response

BODY = "Equilibrium means the forward and reverse reaction rates are equal. " * 5

HTML = f"""
<div class="mw-content-ltr mw-parser-output" lang="en" dir="ltr">
<div class="shortdescription nomobile noexcerpt noprint searchaux" style="display:none">Chemistry concept</div>
<div role="note" class="hatnote navigation-not-searchable">For other uses, see Equilibrium.</div>
<table class="infobox"><tr><td>Infobox text must not appear</td></tr></table>
<p><b>Chemical equilibrium</b> is a state.<sup id="cite_ref-1" class="reference"><a href="#cite_note-1">[1]</a></sup> {BODY}</p>
<figure typeof="mw:File/Thumb"><img src="x.png"/><figcaption>Caption must not appear</figcaption></figure>
<div class="mw-heading mw-heading2"><h2 id="History">History</h2><span class="mw-editsection">[edit]</span></div>
<p>Early work by Berthollet. {BODY}</p>
<blockquote>A quoted third-party passage must not appear.</blockquote>
<ul><li>First point</li><li>Second&nbsp;point</li></ul>
<h3><span class="mw-headline" id="Le_Chatelier">Le Chatelier's principle</span><span class="mw-editsection">edit</span></h3>
<p>Systems counteract changes. <span class="mwe-math-element">x^2 must not appear</span></p>
<h2 id="Injected">Not in the section list</h2>
<p>Still part of Le Chatelier.</p>
<div class="mw-heading mw-heading2"><h2 id="References">References</h2></div>
<div class="reflist"><ol class="references"><li>Reference text must not appear</li></ol></div>
<h3 id="Sub_of_references">Sub of references</h3><p>Excluded subsection text.</p>
<h2 id="Applications">Applications</h2><p>Haber process. {BODY}</p>
<div class="navbox"><p>Navbox text must not appear</p></div>
<table class="wikitable"><tr><td>Table text must not appear</td></tr></table>
</div>
"""

SECTIONS = [
    {"anchor": "History", "line": "History", "level": "2", "index": "1"},
    {"anchor": "Le_Chatelier", "line": "Le Chatelier's principle", "level": "3", "index": "2"},
    {"anchor": "References", "line": "References", "level": "2", "index": "3"},
    {"anchor": "Sub_of_references", "line": "Sub of references", "level": "3", "index": "4"},
    {"anchor": "Applications", "line": "Applications", "level": "2", "index": "5"},
]


def test_extraction_keeps_prose_with_genuine_section_locators_and_drops_unsupported_content():
    sections = extract_sections(HTML, frozenset(s["anchor"] for s in SECTIONS))
    assert [(s.anchor, s.heading, s.level) for s in sections] == [
        (None, None, 0),
        ("History", "History", 2),
        ("Le_Chatelier", "Le Chatelier's principle", 3),
        ("Applications", "Applications", 2),
    ]
    text = "\n".join(s.text for s in sections)
    for banned in ("Infobox", "Caption", "quoted third-party", "Reference text", "Excluded subsection", "Navbox",
                   "Table text", "[1]", "[edit]", "x^2", "Chemistry concept", "For other uses", "Not in the section list"):  # fmt: skip
        assert banned not in text, banned
    assert "Second point" in text  # &nbsp; normalized
    assert "Still part of Le Chatelier." in sections[2].text  # an unlisted heading never starts a section
    assert all(s.locator.keys() == {"anchor", "heading", "level"} for s in sections)
    assert "page" not in str([s.locator for s in sections])


def test_content_hash_depends_on_normalized_text_only():
    a = extract_sections(HTML, frozenset(s["anchor"] for s in SECTIONS))
    b = extract_sections(HTML.replace("<p>Early", "<p>   Early").replace("Haber", "Haber"), frozenset(s["anchor"] for s in SECTIONS))
    assert content_sha256(a) == content_sha256(b)
    c = extract_sections(HTML.replace("Haber process", "Ostwald process"), frozenset(s["anchor"] for s in SECTIONS))
    assert content_sha256(a) != content_sha256(c)


class FakeTransport:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, endpoint, params, bounds):
        self.calls.append((endpoint.host + endpoint.path, dict(params)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _connector(*responses, **settings):
    transport = FakeTransport(*responses)
    return WikipediaConnector(transport, harvest_settings(**settings), "en"), transport


def _parse(revid=42, pageid=7, html=HTML, sections=SECTIONS, title="Chemical equilibrium"):
    return json_response({"parse": {"title": title, "pageid": pageid, "revid": revid, "text": html, "sections": sections}})


def test_fetch_requests_exactly_the_verified_revision_and_records_it():
    connector, transport = _connector(_parse())
    fetched = connector.fetch_revision(info(7, 42))
    assert fetched.revision_id == 42 and fetched.page_id == 7 and fetched.title == "Chemical equilibrium"
    (host_path, params), = transport.calls
    assert host_path == "en.wikipedia.org/w/api.php"
    assert params == {
        "action": "parse", "oldid": "42", "prop": "text|sections|revid", "disableeditsection": "1",
        "disabletoc": "1", "disablelimitreport": "1", "format": "json", "formatversion": "2", "maxlag": "5",
    }  # fmt: skip


@pytest.mark.parametrize("response, code", [(dict(revid=43), "revision_mismatch"), (dict(pageid=8), "page_mismatch")])
def test_a_response_for_another_revision_or_page_fails_closed(response, code):
    connector, _ = _connector(_parse(**response))
    with pytest.raises(CandidateRefused) as raised:
        connector.fetch_revision(info(7, 42))
    assert raised.value.code == code


@pytest.mark.parametrize(
    "page, code",
    [(info(7, 42, namespace=118), "page_namespace"), (info(7, 42, redirect=True), "page_redirect"),
     (info(7, 42, disambiguation=True), "disambiguation_page"), (info(7, 42, page_language="de"), "page_language"),
     (info(7, 42, content_model="css"), "content_model"), (info(7, None), "revision_ambiguous")],
)  # fmt: skip
def test_quality_gates_refuse_before_any_content_request(page, code):
    connector, transport = _connector()
    with pytest.raises(CandidateRefused) as raised:
        connector.fetch_revision(page)
    assert raised.value.code == code and transport.calls == []


def test_short_articles_are_refused():
    connector, _ = _connector(_parse(html="<p>Tiny stub.</p>", sections=[]))
    with pytest.raises(CandidateRefused, match="too_short"):
        connector.fetch_revision(info(7, 42))


def test_page_info_marks_missing_redirect_disambiguation_and_inconsistent_revisions():
    body = {"batchcomplete": True, "query": {"pages": [
        {"pageid": 1, "missing": True},
        {"pageid": 2, "ns": 0, "title": "A", "contentmodel": "wikitext", "pagelanguage": "en", "lastrevid": 20,
         "redirect": True, "revisions": [{"revid": 20, "parentid": 19, "timestamp": "2026-01-01T00:00:00Z"}]},
        {"pageid": 3, "ns": 0, "title": "B", "contentmodel": "wikitext", "pagelanguage": "en", "lastrevid": 31,
         "pageprops": {"disambiguation": ""}, "revisions": [{"revid": 30, "timestamp": "2026-01-01T00:00:00Z"}]},
    ]}}  # fmt: skip
    connector, transport = _connector(json_response(body))
    pages = connector.page_info([1, 2, 3])
    assert pages[1].gone_reason == "missing"
    assert pages[2].gone_reason == "redirect" and pages[2].revision_id == 20
    assert pages[3].disambiguation and pages[3].revision_id is None  # lastrevid and revision disagree
    assert transport.calls[0][1]["pageids"] == "1|2|3" and "redirects" not in transport.calls[0][1]


def test_page_info_refuses_answers_for_pages_not_asked_for():
    body = {"query": {"pages": [{"pageid": 9, "missing": True}]}}
    connector, _ = _connector(json_response(body))
    with pytest.raises(ProviderError):
        connector.page_info([1])


def test_search_keeps_only_articles_within_the_limit():
    body = {"query": {"search": [{"ns": 0, "title": "A", "pageid": 1}, {"ns": 4, "title": "Wikipedia:X", "pageid": 2}]}}
    connector, transport = _connector(json_response(body))
    assert [h.page_id for h in connector.search("chemical equilibrium", 5)] == [1]
    params = transport.calls[0][1]
    assert params["srsearch"] == "chemical equilibrium" and params["srnamespace"] == "0" and params["srlimit"] == "5"
    too_many = {"query": {"search": [{"ns": 0, "title": str(i), "pageid": i + 1} for i in range(3)]}}
    connector, _ = _connector(json_response(too_many))
    with pytest.raises(ProviderError):
        connector.search("x y", 2)


def test_throttling_is_distinguished_from_failures():
    connector, _ = _connector(json_response({}, status=429, retry_after=90))
    with pytest.raises(ProviderThrottled) as raised:
        connector.search("x y", 1)
    assert raised.value.retry_after_seconds == 90
    connector, _ = _connector(json_response({"error": {"code": "maxlag", "info": "lagged"}}, retry_after=7))
    with pytest.raises(ProviderThrottled) as raised:
        connector.search("x y", 1)
    assert raised.value.code == "maxlag" and raised.value.retry_after_seconds == 7
    connector, _ = _connector(json_response({}, status=503))
    with pytest.raises(ProviderThrottled) as raised:
        connector.search("x y", 1)
    assert raised.value.retry_after_seconds == 300  # default when no Retry-After is given
    connector, _ = _connector(json_response({}, status=403))
    with pytest.raises(ProviderError) as raised:
        connector.search("x y", 1)
    assert not raised.value.retryable
    connector, _ = _connector(TransportError("timeout", retryable=True))
    with pytest.raises(ProviderError) as raised:
        connector.search("x y", 1)
    assert raised.value.retryable and raised.value.code == "transport_timeout"


def test_urls_and_attribution_are_built_from_verified_identities():
    assert wikipedia.canonical_url("en", "Le Chatelier's principle") == "https://en.wikipedia.org/wiki/Le_Chatelier's_principle"
    assert wikipedia.revision_url("en", 42) == "https://en.wikipedia.org/w/index.php?oldid=42"
    text = wikipedia.attribution("en", "Chemical equilibrium", 7, 42)
    assert "CC BY-SA 4.0" in text and "curid=7&action=history" in text and "oldid=42" in text
    with pytest.raises(ValueError):
        wikipedia.endpoint("xx")


def test_lexical_gate_is_lexical():
    connector, _ = _connector(_parse())
    fetched = connector.fetch_revision(info(7, 42))
    assert lexically_relevant("Chemical equilibrium", fetched)
    assert not lexically_relevant("photosynthesis", fetched)


# --- M3: declared licence verification -------------------------------------------------------------------------


GOOD_RIGHTS = {"url": "https://creativecommons.org/licenses/by-sa/4.0/", "text": "Creative Commons Attribution-ShareAlike 4.0 International License"}


def test_the_declared_licence_is_requested_and_accepted_when_it_matches():
    for url in ("https://creativecommons.org/licenses/by-sa/4.0", "https://creativecommons.org/licenses/by-sa/4.0/"):
        body = {"query": {"rightsinfo": {**GOOD_RIGHTS, "url": url}}}
        connector, transport = _connector(json_response(body))
        connector.verify_licence()
        params = transport.calls[0][1]
        assert (params["action"], params["meta"], params["siprop"]) == ("query", "siteinfo", "rightsinfo")


@pytest.mark.parametrize(
    "rights, code",
    [
        ({"url": "https://creativecommons.org/licenses/by-nc-sa/4.0/", "text": GOOD_RIGHTS["text"]}, "licence_mismatch"),
        ({"url": "https://creativecommons.org/licenses/by-sa/3.0/", "text": GOOD_RIGHTS["text"]}, "licence_mismatch"),
        ({"url": "http://creativecommons.org.evil.example/licenses/by-sa/4.0/", "text": GOOD_RIGHTS["text"]}, "licence_mismatch"),
        ({"url": GOOD_RIGHTS["url"], "text": "Creative Commons Attribution-NonCommercial-ShareAlike 4.0"}, "licence_mismatch"),
        ({"url": GOOD_RIGHTS["url"], "text": "GNU Free Documentation License"}, "licence_mismatch"),
        ({"url": GOOD_RIGHTS["url"], "text": ""}, "licence_unverified"),
        ({"url": GOOD_RIGHTS["url"]}, "licence_unverified"),
        ({"text": GOOD_RIGHTS["text"]}, "licence_unverified"),
        ({"url": 4, "text": 5}, "licence_unverified"),
        ("CC BY-SA 4.0", "licence_unverified"),
        (None, "licence_unverified"),
    ],
)
def test_incompatible_or_ambiguous_licence_metadata_fails_closed_without_retry(rights, code):
    connector, _ = _connector(json_response({"query": {"rightsinfo": rights}}))
    with pytest.raises(ProviderError) as raised:
        connector.verify_licence()
    assert raised.value.code == code and raised.value.retryable is False


def test_a_missing_rightsinfo_block_is_refused():
    connector, _ = _connector(json_response({"query": {}}))
    with pytest.raises(ProviderError) as raised:
        connector.verify_licence()
    assert raised.value.code == "licence_unverified"


def test_a_throttle_whose_body_was_rejected_keeps_the_provider_delay():
    from app.harvest.transport import TransportError

    error = TransportError("encoded_response", "Content-Encoding is not identity", retryable=False)
    error.throttle_status, error.retry_after_seconds = 429, 172800.0
    connector, _ = _connector(error)
    with pytest.raises(ProviderThrottled) as raised:
        connector.search("x y", 1)
    assert raised.value.code == "http_429" and raised.value.retry_after_seconds == 172800.0
    connector, _ = _connector(TransportError("encoded_response", "", retryable=False))
    with pytest.raises(ProviderError) as raised:
        connector.search("x y", 1)
    assert not isinstance(raised.value, ProviderThrottled)
