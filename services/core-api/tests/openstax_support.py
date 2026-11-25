"""Offline OpenStax fixtures (WP-6): CNXML shaped like OpenStax's book source, and a fake GitHub REST API.

The CNXML structure (collection metadata, chapters as subcollections, module
metadata, sections with ids, figures, MathML, notes, exercises) follows the
official openstax/osbooks-chemistry-bundle repository; the prose is written
for these tests. Nothing here performs a network request: FakeGitHub answers
from in-memory git objects and records every request.
"""

from __future__ import annotations

import base64
import json
import uuid

from app.harvest import openstax
from app.harvest.openstax import git_blob_sha
from app.harvest.transport import ProviderResponse

REPO = "/repos/openstax/osbooks-chemistry-bundle"
BY_NC_SA = ("http://creativecommons.org/licenses/by-nc-sa/4.0/",
            "Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International")  # fmt: skip


def collection_xml(*, licence=BY_NC_SA, uuid_="7fccc9cf-9b71-44f6-800b-f9457fd64335", slug="chemistry-2e",
                   chapters=(("Essential Ideas", ("m68663", "m68664")), ("Fundamental Equilibrium Concepts", ("m68797",))),
                   prolog: str = "") -> bytes:  # fmt: skip
    licence_xml = "" if licence is None else f'<md:license url="{licence[0]}">{licence[1]}</md:license>'
    body = "".join(
        f"<col:subcollection><md:title>{title}</md:title><col:content>"
        + "".join(f'<col:module document="{m}"/>' for m in modules)
        + "</col:content></col:subcollection>"
        for title, modules in chapters
    )
    return (
        f'{prolog}<col:collection xmlns="http://cnx.rice.edu/collxml" xmlns:md="http://cnx.rice.edu/mdml" '
        f'xmlns:col="http://cnx.rice.edu/collxml" xml:lang="en">\n'
        f'<metadata xmlns:md="http://cnx.rice.edu/mdml" mdml-version="0.5"><md:title>Chemistry 2e</md:title>'
        f"{licence_xml}<md:language>en</md:language><md:uuid>{uuid_}</md:uuid><md:slug>{slug}</md:slug></metadata>\n"
        f'<col:content><col:module document="m68662"/>{body}<col:module document="m68859"/></col:content>'
        f"</col:collection>"
    ).encode()


def module_xml(module_id: str, title: str, content: str, *, doc_class: str = "", prolog: str = "") -> bytes:
    return (
        f'{prolog}<document xmlns="http://cnx.rice.edu/cnxml" xmlns:m="http://www.w3.org/1998/Math/MathML" '
        f'class="{doc_class}"><title>{title}</title><metadata xmlns:md="http://cnx.rice.edu/mdml">'
        f"<md:content-id>{module_id}</md:content-id><md:title>{title}</md:title>"
        f"<md:uuid>{uuid.uuid5(uuid.NAMESPACE_URL, module_id)}</md:uuid></metadata>"
        f"<content>{content}</content></document>"
    ).encode()


LEAD = "Matter is anything that occupies space and has mass, and chemistry studies how it changes. " * 3
CENTRAL = "Chemistry connects physics, biology and geology, so it is often called a central science. " * 3
CONTEXT_CONTENT = f"""
<figure id="CNX_Chem_01_01_Alchemist"><media id="m1" alt="An alchemist at work"><image src="../../media/a.jpg"/></media>
  <caption>An alchemist at work. (credit: modification of a photograph by a third party)</caption></figure>
<para id="fs-lead">{LEAD}</para>
<section id="fs-central"><title>Chemistry: The Central Science</title>
  <para id="fs-p1">{CENTRAL} Water, H<sub>2</sub>O, is a <term id="t1">compound</term> made of
  <emphasis effect="italics">two</emphasis> elements; its energy is <m:math><m:mi>E</m:mi></m:math> joules.</para>
  <table id="tbl"><tgroup cols="1"><tbody><row><entry>TABLE CELL TEXT</entry></row></tbody></tgroup></table>
  <equation id="eq1"><m:math><m:mn>42</m:mn></m:math></equation>
  <note id="ltl" class="link-to-learning"><para>LINK TO LEARNING TEXT visit an external site.</para></note>
  <note id="everyday" class="chemistry-everyday-life"><title>Chemistry in Everyday Life</title>
    <para>Cooking an egg changes its proteins in ways that cannot be reversed by cooling.</para></note>
  <example id="ex1"><para>WORKED EXAMPLE TEXT</para></example>
  <quote id="q1" display="block">BLOCK QUOTATION TEXT</quote>
  <section id="fs-branches"><title>Branches of Chemistry</title>
    <list id="l1"><item>Analytical chemistry measures what matter contains.</item>
    <item>Physical chemistry explains why reactions happen.</item></list></section>
  <para id="fs-after">After its subsections the central science section continues here.</para>
</section>
<section id="fs-untitled"><para>UNTITLED TOP LEVEL TEXT has no genuine locator.</para></section>
<section id="fs-summary" class="summary"><title>Key Concepts and Summary</title>
  <para>Chemistry studies matter, its properties and its changes at several scales.</para></section>
<section id="fs-exercises" class="exercises"><title>Chemistry End of Chapter Exercises</title>
  <exercise id="e1"><problem><para>EXERCISE PROBLEM TEXT</para></problem><solution><para>SOLUTION TEXT</para></solution></exercise>
</section>
<glossary><definition id="g1"><term>matter</term><meaning>GLOSSARY MEANING TEXT</meaning></definition></glossary>
"""
EXCLUDED_MARKERS = ("alchemist", "TABLE CELL", "42", "LINK TO LEARNING", "WORKED EXAMPLE", "BLOCK QUOTATION",
                    "EXERCISE PROBLEM", "SOLUTION TEXT", "GLOSSARY MEANING", "is E joules", "UNTITLED")  # fmt: skip
EQUILIBRIUM = "At equilibrium the forward and reverse reaction rates are equal, so concentrations stay constant. " * 3


def book_files(*, collection: bytes | None = None, context: str = CONTEXT_CONTENT, equilibrium: str = EQUILIBRIUM,
               drop: tuple[str, ...] = ()) -> dict[str, bytes]:  # fmt: skip
    files = {
        "collections/chemistry-2e.collection.xml": collection or collection_xml(),
        "collections/chemistry-atoms-first-2e.collection.xml": b"<other/>",
        "modules/m68662/index.cnxml": module_xml("m68662", "Preface", f"<para>{LEAD}</para>"),
        "modules/m68663/index.cnxml": module_xml("m68663", "Introduction", "<para>A short opening.</para>",
                                                 doc_class="introduction"),  # fmt: skip
        "modules/m68664/index.cnxml": module_xml("m68664", "Chemistry in Context", context),
        "modules/m68797/index.cnxml": module_xml("m68797", "Chemical Equilibria", f"<para id='eq-lead'>{equilibrium}</para>"),
        "modules/m68859/index.cnxml": module_xml("m68859", "The Periodic Table", f"<para>{LEAD}</para>"),
        "media/a.jpg": b"\xff\xd8 not really a jpeg",
    }
    for path in drop:
        files.pop(path)
    return files


def response(body: object, status: int = 200, *, retry_after: float | None = None,
             reset: float | None = None) -> ProviderResponse:  # fmt: skip
    return ProviderResponse(status, "application/json; charset=utf-8", json.dumps(body).encode(), retry_after, reset)


class FakeGitHub:
    """Stands in for ProviderTransport; answers the three GitHub REST requests the connector makes."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.objects: dict[str, bytes] = {}
        self.paths: dict[str, str] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.overrides: dict[str, object] = {}  # path suffix -> ProviderResponse or exception (every time)
        self.generation = 0
        self.publish(files)

    def publish(self, files: dict[str, bytes]) -> None:
        """A new commit of the repository with exactly these files."""
        self.generation += 1
        self.files = dict(files)
        self.commit = format(0xC0FFEE00 + self.generation, "040x")
        self.tree_sha = format(0x7EEE0000 + self.generation, "040x")
        for path, data in files.items():
            self.objects[git_blob_sha(data)] = data
            self.paths[git_blob_sha(data)] = path

    def blob_of(self, path: str) -> str:
        return git_blob_sha(self.files[path])

    def get(self, endpoint, params, bounds):
        assert endpoint.host == openstax.API_HOST and endpoint.port == 443
        self.requests.append((endpoint.host, endpoint.path, dict(params)))
        for suffix, answer in self.overrides.items():
            if endpoint.path.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        path = endpoint.path.removeprefix(REPO)
        if path == "/branches/main" and params == {}:
            return response({"name": "main", "commit": {"sha": self.commit, "commit": {
                "tree": {"sha": self.tree_sha}, "committer": {"date": "2026-11-12T19:45:50Z"}}}})  # fmt: skip
        if path == f"/git/trees/{self.tree_sha}" and params == {"recursive": "1"}:
            dirs = sorted({p.rsplit("/", 1)[0] for p in self.files})
            entries = [{"path": d, "type": "tree", "mode": "040000", "sha": "e" * 40} for d in dirs]
            entries += [{"path": p, "type": "blob", "mode": "100644", "sha": git_blob_sha(b), "size": len(b)}
                        for p, b in sorted(self.files.items())]  # fmt: skip
            return response({"sha": self.tree_sha, "tree": entries, "truncated": False})
        if path.startswith("/git/blobs/") and params == {}:
            sha = path.rsplit("/", 1)[-1]
            if sha in self.objects:
                data = self.objects[sha]
                encoded = base64.encodebytes(data).decode()  # GitHub wraps base64 lines, as here
                return response({"sha": sha, "size": len(data), "encoding": "base64", "content": encoded})
        return response({"message": "Not Found"}, 404)

    def blob_requests(self) -> list[str]:
        """What each blob request fetched, in order: a module id, or "collection"."""
        names = []
        for _, path, _ in self.requests:
            if "/git/blobs/" in path:
                where = self.paths.get(path.rsplit("/", 1)[-1], "unknown/unknown")
                names.append(where.split("/")[1] if where.startswith("modules/") else "collection")
        return names
