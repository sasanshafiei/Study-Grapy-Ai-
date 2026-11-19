"""Who may list and upload course documents, and that nothing bypasses authentication."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, get_current_user
from app.main import app as runtime_app
from app.main import create_app
from tests.api_support import asgi_call, multipart_body
from tests.pdf_samples import make_pdf


@pytest.fixture
def setup(world):
    """A course, its owner, and every other kind of caller."""
    owner = world.user("professor", name="Owner")
    course = world.course(owner)
    people = {
        "owner": owner,
        "other_professor": world.user("professor"),
        "enrolled_student": world.user("student"),
        "unenrolled_student": world.user("student"),
        "admin": world.user("admin"),
        "enrolled_admin": world.user("admin"),
        "enrolled_professor": world.user("professor"),  # a professor holding a student enrollment
        "unknown_role": world.user("teaching_assistant"),
        "enrolled_as_ta": world.user("student"),  # an enrollment with another role
        "disabled_owner": None,
        "disabled_student": world.user("student", status="disabled"),
    }
    for name in ("enrolled_student", "enrolled_admin", "enrolled_professor", "unknown_role", "disabled_student"):
        world.enroll(people[name], course)
    world.enroll(people["enrolled_as_ta"], course, role="ta")
    disabled_owner = world.user("professor", status="disabled")
    disabled_course = world.course(disabled_owner)
    people["disabled_owner"] = disabled_owner
    return {"course": course, "disabled_course": disabled_course, **people}


# (caller, list status, upload status, error code for a refused upload)
MATRIX = [
    ("owner", 200, 202, None),
    ("other_professor", 404, 404, "course_not_found"),
    ("enrolled_student", 200, 403, "upload_not_allowed"),
    ("unenrolled_student", 404, 404, "course_not_found"),
    ("admin", 404, 404, "course_not_found"),
    ("enrolled_admin", 404, 404, "course_not_found"),
    ("enrolled_professor", 404, 404, "course_not_found"),
    ("unknown_role", 404, 404, "course_not_found"),
    ("enrolled_as_ta", 404, 404, "course_not_found"),
    ("disabled_student", 403, 403, "account_disabled"),
]


@pytest.mark.parametrize("caller, list_status, upload_status, code", MATRIX)
def test_authorization_matrix(api, world, setup, caller, list_status, upload_status, code):
    api.as_user(setup[caller])
    course = setup["course"]

    listed = api.list(course)
    uploaded = api.upload(course, make_pdf(f"matrix {caller}"))

    assert listed.status_code == list_status, listed.text
    assert uploaded.status_code == upload_status, uploaded.text
    if code is not None:
        assert uploaded.json()["code"] == code
        assert api.stored_files() == [] and api.incoming_files() == []
        assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 0


def test_disabled_owner_is_refused_even_on_their_own_course(api, world, setup):
    api.as_user(setup["disabled_owner"])
    course = setup["disabled_course"]
    assert api.list(course).json()["code"] == "account_disabled"
    assert api.upload(course).status_code == 403


def test_account_disabled_in_the_database_is_refused_even_with_an_active_principal(api, world, setup):
    """The principal says active (stale token); the users row says disabled."""
    owner = setup["owner"]
    world.execute("UPDATE users SET status = 'disabled' WHERE id = :u", u=owner.id)
    api.as_user(CurrentUser(id=owner.id, role="professor", status="active"))
    assert api.list(setup["course"]).status_code == 403
    assert api.upload(setup["course"]).status_code == 403


def test_unknown_and_inaccessible_courses_look_the_same(api, world, setup):
    import uuid

    api.as_user(setup["other_professor"])
    unknown = api.list(uuid.uuid4())
    inaccessible = api.list(setup["course"])
    assert (unknown.status_code, unknown.json()) == (inaccessible.status_code, inaccessible.json())
    assert unknown.json() == {"detail": "Course not found.", "code": "course_not_found"}


def test_unauthenticated_requests_get_401_with_a_bearer_challenge(api, setup):
    api.as_user(None)
    for response in (api.list(setup["course"]), api.upload(setup["course"])):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json() == {"detail": "Sign in to continue.", "code": "not_authenticated"}
    assert api.stored_files() == [] and api.incoming_files() == []


@pytest.mark.parametrize("caller", [None, "other_professor", "enrolled_student", "unenrolled_student"])
def test_refused_uploads_never_read_the_body_or_touch_storage(api, setup, caller, monkeypatch):
    api.as_user(setup[caller] if caller else None)
    stage_calls = []
    monkeypatch.setattr(api.storage, "stage", lambda: stage_calls.append(1))
    body, content_type = multipart_body([("file", "a.pdf", make_pdf(padding=50_000), "application/pdf")])

    result = asgi_call(
        api.app,
        "POST",
        api.path(setup["course"]),
        {"content-type": content_type, "content-length": str(len(body))},
        [body],
    )

    assert result.status in (401, 403, 404)
    assert result.receive_calls == 0  # the multipart body was never read, let alone spooled
    assert stage_calls == []


def test_runtime_application_fails_closed():
    """The real app has no override and no bypass: tokens, user headers and cookies all get 401."""
    assert runtime_app.dependency_overrides == {}
    client = TestClient(runtime_app)
    path = "/api/v1/courses/00000000-0000-4000-8000-000000000000/documents"
    attempts = [
        {},
        {"Authorization": "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJhZG1pbiJ9."},
        {"Authorization": "Bearer dev"},
        {"X-User-Id": "00000000-0000-4000-8000-000000000001", "X-Role": "professor"},
        {"Cookie": "session=admin"},
    ]
    for headers in attempts:
        assert client.get(path, headers=headers).status_code == 401
        response = client.post(path, headers=headers, files={"file": ("a.pdf", make_pdf(), "application/pdf")})
        assert response.status_code == 401
    client.close()


def test_overrides_stay_on_the_test_application(api):
    assert get_current_user in api.app.dependency_overrides
    assert runtime_app.dependency_overrides == {}
    assert create_app().dependency_overrides == {}


def test_test_application_never_uses_the_default_engine(api, world, disposable_target):
    """Its session factory is bound to the verified disposable database, also for /health/db."""
    from app.db import SessionLocal

    assert api.app.state.session_factory is not SessionLocal
    bound = api.app.state.session_factory.kw["bind"].url
    assert (bound.host, bound.database) == (disposable_target.url.host, disposable_target.url.database)
    assert api.client.get("/health/db").json() == {"status": "ok", "pgvector_installed": True}
    assert api.app.state.storage is api.storage and api.settings.upload_dir == api.upload_root
