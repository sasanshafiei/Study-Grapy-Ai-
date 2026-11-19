"""A real HTTP round trip: Uvicorn on a loopback port, real sockets, real request framing.

The application is the test harness's (create_app with the verified disposable
database, a temporary upload directory and an injected principal), served by
a temporary Uvicorn server in a background thread inside the test container.
Only the behaviour that differs from in-process testing is checked here:
multipart over the wire, a refusal before the body is read, and a chunked
body cut off at the size limit.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from app.uploads import MULTIPART_OVERHEAD_BYTES
from tests.api_support import ApiHarness, multipart_body
from tests.pdf_samples import make_pdf

LIMIT = 4096


@pytest.fixture
def live(make_api) -> Iterator[tuple[ApiHarness, httpx.Client]]:
    harness = make_api(max_upload_bytes=LIMIT)
    server = uvicorn.Server(
        uvicorn.Config(harness.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off", ws="none")
    )
    thread = threading.Thread(target=server.run, name="uvicorn-smoke", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise AssertionError("uvicorn did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20)
    try:
        yield harness, client
    finally:
        client.close()
        server.should_exit = True
        thread.join(15)


def test_upload_and_list_over_real_http(live, world):
    harness, client = live
    owner = world.user("professor", name="Owner")
    course = world.course(owner)
    harness.as_user(owner)
    pdf = make_pdf("over the wire")

    upload = client.post(harness.path(course), files={"file": ("Lecture.pdf", pdf, "application/pdf")})
    listing = client.get(harness.path(course))

    assert upload.status_code == 202, upload.text
    assert upload.json()["status"] == "queued"
    assert [item["id"] for item in listing.json()] == [upload.json()["id"]]
    [stored] = harness.stored_files()
    assert stored.read_bytes() == pdf


@pytest.mark.parametrize("caller", [None, "student"])
def test_refusals_over_real_http(live, world, caller):
    harness, client = live
    owner = world.user("professor")
    course = world.course(owner)
    if caller == "student":
        student = world.user("student")
        world.enroll(student, course)
        harness.as_user(student)
    else:
        harness.as_user(None)

    response = client.post(harness.path(course), files={"file": ("a.pdf", make_pdf(), "application/pdf")})

    assert response.status_code == (401 if caller is None else 403)
    assert response.json()["code"] == ("not_authenticated" if caller is None else "upload_not_allowed")
    assert harness.stored_files() == [] and harness.incoming_files() == []


def test_chunked_body_over_the_limit_gets_413_over_real_http(live, world):
    harness, client = live
    owner = world.user("professor")
    course = world.course(owner)
    harness.as_user(owner)
    # Just over the body limit, so the server has read all of it when it answers.
    payload = b"%PDF-1.4\n" + b"x" * (LIMIT + MULTIPART_OVERHEAD_BYTES)
    body, content_type = multipart_body([("file", "big.pdf", payload, "application/pdf")])

    def chunks():  # a generator body is sent with Transfer-Encoding: chunked, without Content-Length
        for start in range(0, len(body), 8192):
            yield body[start : start + 8192]

    response = client.post(harness.path(course), content=chunks(), headers={"content-type": content_type})

    assert response.status_code == 413, response.text
    assert response.json()["code"] == "file_too_large"
    assert harness.stored_files() == [] and harness.incoming_files() == []
    assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 0
