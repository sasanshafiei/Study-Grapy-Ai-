"""WP-10: DELETE /api/v1/documents/{id} withdraws a class upload (revision 0013).

Runs only against the disposable test database (tests/disposable_db.py guards).
"""

from __future__ import annotations

import importlib.util
import json
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app import schema_checks
from app.auth import get_current_user
from app.chat.protocol import ProposedAnswer, ProposedCitation
from app.chat.verify import VerificationFailed, verify_answer
from app.config import Settings
from app.dashboard import materials
from app.indexing.payload import request_for
from app.ingestion import jobs
from app.ingestion.normalize import Section
from app.main import create_app
from app.quiz.provenance import visible_citations
from app.services import documents
from tests.alembic_cli import head_revision
from tests.api_support import ApiHarness, RecordingPublisher
from tests.harvest_support import wikipedia_source
from tests.index_writer import COMMIT, parameters
from tests.pdf_samples import make_pdf
from tests.test_db_privileges import denied, roles  # noqa: F401  (fixture)
from tests.test_index_jobs import _parsed
from tests.worker_support import wait_for

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0013_document_withdrawal.py"
TEXTS = (
    "Le Chatelier's principle predicts how an equilibrium responds to a stress.",
    "The limiting reactant limits the amount of product that can form.",
)
CANCELLED = documents.WITHDRAWN_JOB_MESSAGE


def _path(document_id) -> str:
    return f"/api/v1/documents/{document_id}"


def _document(world, document_id):
    return world.rows("SELECT status, withdrawn_at, withdrawn_by, error_message FROM documents WHERE id = :d",
                      d=document_id)[0]  # fmt: skip


def _jobs(world, document_id) -> list:
    return world.rows("SELECT kind, status, error_message FROM ingestion_jobs WHERE document_id = :d "
                      "ORDER BY created_at, kind", d=document_id)  # fmt: skip


def _retrievable(world, course_id) -> set:
    with world.engine.connect() as c:
        return set(c.execute(text("SELECT document_id FROM public.retrieve_index_chunks(:c, ARRAY['class','external'])"),
                             {"c": course_id}).scalars())  # fmt: skip


def _claim_index(world, document_id, job_id):
    factory = sessionmaker(bind=world.engine)
    with factory.begin() as session:
        request = request_for(session, document_id)
        claim = jobs.claim_index(session, job_id, 600)
    return factory, claim, parameters(json.loads(request.body))


def _indexed(world, course, owner):
    document_id, job_id = _parsed(world, course, owner, TEXTS)
    factory, claim, data = _claim_index(world, document_id, job_id)
    with world.engine.begin() as c:
        c.execute(COMMIT, data)
    assert jobs.complete_index(factory, claim, data["digest"], data["sections"]) == jobs.CompletionStatus.COMPLETED
    return document_id


@pytest.fixture
def course(world):
    owner = world.user("professor", name="Owner")
    return owner, world.course(owner)


# --- contract and schema ------------------------------------------------------------------------------------


def test_migration_and_model_share_the_withdrawal_expressions_verbatim():
    spec = importlib.util.spec_from_file_location("migration_0013_under_test", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("0013", "0012")
    assert module.STATUS_CHECK == schema_checks.DOCUMENT_STATUS_CHECK
    assert module.WITHDRAWAL_CHECK == schema_checks.DOCUMENT_WITHDRAWAL_CHECK
    assert module.DEDUP_WHERE == schema_checks.DOCUMENT_DEDUP_WHERE
    assert head_revision() == "0013"


def test_openapi_declares_a_bodyless_204_and_structured_errors():
    operation = create_app(Settings()).openapi()["paths"]["/api/v1/documents/{document_id}"]["delete"]
    assert "content" not in operation["responses"]["204"]
    assert {"401", "403", "404", "409", "422", "503"} <= set(operation["responses"])
    # The published copy (contracts/core-api.v1.openapi.json) is checked by `scripts.export_openapi --check` in CI.


# --- access boundaries --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("caller", "status", "code"),
    [
        ("owner", 204, None),
        ("enrolled_student", 403, "withdraw_not_allowed"),
        ("enrolled_professor", 404, "document_not_found"),  # a student enrollment gives a professor no access
        ("disabled_student", 403, "account_disabled"),
        ("other_professor", 404, "document_not_found"),  # owner of another course: no cross-course access
        ("unenrolled_student", 404, "document_not_found"),
        ("admin", 404, "document_not_found"),  # no implicit course access (docs/courses-api.md)
        ("anonymous", 401, None),
    ],
)
def test_only_the_owning_professor_withdraws(world, api, course, caller, status, code):
    owner, course_id = course
    people = {
        "owner": owner,
        "enrolled_student": world.user("student"),
        "enrolled_professor": world.user("professor"),
        "disabled_student": world.user("student", status="disabled"),
        "other_professor": world.user("professor"),
        "unenrolled_student": world.user("student"),
        "admin": world.user("admin"),
        "anonymous": None,
    }
    for name in ("enrolled_student", "enrolled_professor", "disabled_student"):
        world.enroll(people[name], course_id)
    world.course(people["other_professor"])
    uploaded = api.as_user(owner).upload(course_id)
    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["id"]

    response = api.as_user(people[caller]).client.delete(_path(document_id))
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json()["code"] == code
    expected = "withdrawn" if status == 204 else "pending"
    assert _document(world, document_id)["status"] == expected
    if status == 204:
        assert response.content == b""
        assert _document(world, document_id)["withdrawn_by"] == owner.id


def test_unknown_and_courseless_documents_are_not_found(world, api, course):
    owner, _ = course
    orphan = world.document(None, origin="external", source_id=world.source())
    for document_id in (uuid.uuid4(), orphan):
        response = api.as_user(owner).client.delete(_path(document_id))
        assert (response.status_code, response.json()["code"]) == (404, "document_not_found")
    assert api.as_user(owner).client.delete("/api/v1/documents/not-a-uuid").status_code == 422


def test_external_material_is_refused_and_left_untouched(world, api, course):
    owner, course_id = course
    harvested = world.document(course_id, origin="external", source_id=wikipedia_source(world), status="indexed",
                               title="Chemical equilibrium", url="https://en.wikipedia.org/wiki/Equilibrium")  # fmt: skip
    cached = world.document(course_id, origin="web_cache", status="indexed", title="Cached page")
    student = world.user("student")
    world.enroll(student, course_id)
    for document_id in (harvested, cached):
        response = api.as_user(owner).client.delete(_path(document_id))
        assert (response.status_code, response.json()["code"]) == (409, "document_not_withdrawable")
        assert _document(world, document_id)["status"] == "indexed"
        # A student is refused for lack of permission before anything about the material is revealed.
        assert api.as_user(student).client.delete(_path(document_id)).json()["code"] == "withdraw_not_allowed"
    with pytest.raises(DBAPIError, match="documents_withdrawal_check"):  # the database refuses it too
        world.execute("UPDATE documents SET status = 'withdrawn', withdrawn_at = now(), withdrawn_by = :u "
                      "WHERE id = :d", u=owner.id, d=harvested)  # fmt: skip


# --- states, listing, deduplication, idempotency ------------------------------------------------------------


@pytest.mark.parametrize("state", ["queued", "running", "failed", "indexed"])
def test_withdrawal_in_every_processing_state(world, api, course, state):
    owner, course_id = course
    if state == "indexed":
        document_id = _indexed(world, course_id, owner)
        assert _retrievable(world, course_id) == {document_id}
    else:
        uploaded = api.as_user(owner).upload(course_id).json()
        document_id, job_id = uploaded["id"], uploaded["job_id"]
        if state == "running":
            with sessionmaker(bind=world.engine).begin() as session:
                assert jobs.claim_parse(session, job_id, 600) is not None
        elif state == "failed":
            world.execute("UPDATE ingestion_jobs SET status = 'failed', finished_at = now() WHERE id = :j", j=job_id)
            world.execute("UPDATE documents SET status = 'failed', error_message = 'Unreadable.' WHERE id = :d",
                          d=document_id)  # fmt: skip
    assert str(document_id) in {item["id"] for item in api.as_user(owner).list(course_id).json()}

    assert api.as_user(owner).client.delete(_path(document_id)).status_code == 204
    row = _document(world, document_id)
    assert row["status"] == "withdrawn" and row["withdrawn_at"] is not None
    assert all(job["status"] in ("succeeded", "failed") for job in _jobs(world, document_id))
    if state in ("queued", "running"):
        assert _jobs(world, document_id)[-1]["error_message"] == CANCELLED
    if state == "failed":
        assert _jobs(world, document_id)[-1]["error_message"] is None  # history kept as it was
    assert str(document_id) not in {item["id"] for item in api.as_user(owner).list(course_id).json()}
    assert uuid.UUID(str(document_id)) not in _retrievable(world, course_id)


def test_repeated_and_concurrent_deletes_answer_204_and_withdraw_once(world, api, course):
    owner, course_id = course
    document_id = api.as_user(owner).upload(course_id).json()["id"]
    factory = sessionmaker(bind=world.engine, autoflush=False)
    results = []
    threads = [threading.Thread(target=lambda: results.append(documents.withdraw_document(factory, owner, document_id)))
               for _ in range(2)]  # fmt: skip
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(results) == [False, True]
    first = _document(world, document_id)
    for _ in range(2):
        assert api.as_user(owner).client.delete(_path(document_id)).status_code == 204
    assert _document(world, document_id) == first  # the withdrawal record never changes


def test_the_same_file_can_be_uploaded_again_after_withdrawal(world, api, course):
    owner, course_id = course
    pdf = make_pdf()
    first = api.as_user(owner).upload(course_id, pdf).json()["id"]
    duplicate = api.as_user(owner).upload(course_id, pdf)
    assert (duplicate.status_code, duplicate.json()["document_id"]) == (409, first)
    assert api.as_user(owner).client.delete(_path(first)).status_code == 204
    again = api.as_user(owner).upload(course_id, pdf)
    assert again.status_code == 202, again.text
    assert [item["id"] for item in api.as_user(owner).list(course_id).json()] == [again.json()["id"]]
    assert api.as_user(owner).upload(course_id, pdf).json()["document_id"] == again.json()["id"]  # dedup still on
    assert len(api.stored_files()) == 2  # the withdrawn original is retained (deferred purge)


# --- workers, retries and the index boundary cannot resurrect it --------------------------------------------


def test_a_running_parse_attempt_is_fenced_and_retries_are_refused(world, api, course):
    owner, course_id = course
    uploaded = api.as_user(owner).upload(course_id).json()
    factory = sessionmaker(bind=world.engine, autoflush=False)
    with factory.begin() as session:
        claim = jobs.claim_parse(session, uploaded["job_id"], 600)
    assert api.as_user(owner).client.delete(_path(uploaded["id"])).status_code == 204

    assert jobs.complete_parse(factory, claim, 1, [Section(0, 1, TEXTS[0])]) == jobs.CompletionStatus.STALE
    assert jobs.record_failure(factory, claim, jobs.Failure("invalid_pdf", "unreadable", True), Settings()) \
        == jobs.FailureStatus.STALE  # fmt: skip
    assert world.scalar("SELECT count(*) FROM document_sections WHERE document_id = :d", d=uploaded["id"]) == 0
    with pytest.raises(jobs.RetryRefused, match="withdrawn, not failed"):
        jobs.retry_parse(factory, uploaded["id"])
    admin = world.user("admin")
    retried = api.as_user(admin).client.post(f"/api/v1/admin/jobs/{uploaded['job_id']}/retry")
    assert (retried.status_code, retried.json()["code"]) == (409, "retry_refused")
    assert _document(world, uploaded["id"])["status"] == "withdrawn"
    overview = api.as_user(admin).client.get("/api/v1/admin/overview")
    assert overview.status_code == 200 and overview.json()["documents"]["withdrawn"] >= 1


def test_withdrawal_waits_for_a_parse_completion_and_cancels_the_index_job_it_created(world, api, course):
    owner, course_id = course
    uploaded = api.as_user(owner).upload(course_id).json()
    factory = sessionmaker(bind=world.engine, autoflush=False)
    with factory.begin() as session:
        claim = jobs.claim_parse(session, uploaded["job_id"], 600)
    completing = factory()
    result = []
    try:
        assert jobs._write_completion(completing, claim, 1, [Section(0, 1, TEXTS[0])]) == jobs.CompletionStatus.COMPLETED
        withdraw = lambda: result.append(documents.withdraw_document(factory, owner, uploaded["id"]))  # noqa: E731
        thread = threading.Thread(target=withdraw)
        thread.start()
        waiting = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
        wait_for(lambda: world.scalar(waiting), what="withdrawal waiting on the job lock")
        completing.commit()
    finally:
        completing.close()
    thread.join(30)
    assert result == [True]
    assert [(j["kind"], j["status"], j["error_message"]) for j in _jobs(world, uploaded["id"])] == [
        ("parse", "succeeded", None), ("index", "failed", CANCELLED)]  # fmt: skip
    assert _document(world, uploaded["id"])["status"] == "withdrawn"


def test_an_index_attempt_racing_the_withdrawal_never_publishes(world, api, course):
    owner, course_id = course
    # Provider committed its generation before the withdrawal: chunks exist, publication is fenced.
    document_id, job_id = _parsed(world, course_id, owner, TEXTS, title="Committed.pdf")
    factory, claim, data = _claim_index(world, document_id, job_id)
    with world.engine.begin() as c:
        c.execute(COMMIT, data)
    assert api.as_user(owner).client.delete(_path(document_id)).status_code == 204
    assert jobs.complete_index(factory, claim, data["digest"], data["sections"]) == jobs.CompletionStatus.STALE
    assert world.scalar("SELECT count(*) FROM chunks WHERE document_id = :d", d=document_id) == 2  # retained
    with pytest.raises(jobs.RetryRefused, match="withdrawn, not failed"):
        jobs.retry_index(factory, document_id)

    # Withdrawal before the provider commits: the definer routine refuses to write.
    late, late_job = _parsed(world, course_id, owner, TEXTS, title="Late.pdf")
    _, _, late_data = _claim_index(world, late, late_job)
    assert api.as_user(owner).client.delete(_path(late)).status_code == 204
    with pytest.raises(DBAPIError, match="document_not_writable"):
        with world.engine.begin() as c:
            c.execute(COMMIT, late_data)
    assert _retrievable(world, course_id) == set()
    with pytest.raises(DBAPIError, match="cannot change"):  # not even by hand (trigger documents_withdrawal_final)
        world.execute("UPDATE documents SET status = 'indexed', withdrawn_at = NULL, withdrawn_by = NULL "
                      "WHERE id = :d", d=document_id)  # fmt: skip


def test_withdrawn_material_is_never_cited_or_shown_again(world, api, course):
    owner, course_id = course
    student = world.user("student")
    world.enroll(student, course_id)
    document_id = _indexed(world, course_id, owner)
    chunk = world.rows("SELECT id, locator FROM chunks WHERE document_id = :d ORDER BY section_ordinal", d=document_id)[0]
    proposed = ProposedAnswer(True, "It shifts. [1]", (ProposedCitation(1, chunk["id"], document_id, "class", TEXTS[0]),), ())
    evidence = [{"chunk_id": str(chunk["id"]), "document_id": str(document_id), "locator": chunk["locator"],
                 "quote": TEXTS[0]}]  # fmt: skip
    factory = sessionmaker(bind=world.engine, autoflush=False)

    def check():
        with factory() as session:
            visible = visible_citations(session, course_id, evidence)
            shown = materials.visible_document(session, course_id, document_id)
        with factory() as session:
            try:
                cited = len(verify_answer(session, student, course_id, "class_only", proposed).citations)
            except VerificationFailed as exc:
                cited = str(exc)
        return len(visible.citations), visible.omitted, bool(shown), cited

    assert check() == (1, 0, True, 1)
    assert api.as_user(owner).client.delete(_path(document_id)).status_code == 204
    assert check() == (0, 1, False, "document_not_indexed")
    assert _retrievable(world, course_id) == set()


# --- least-privileged runtime role and migration ------------------------------------------------------------


def test_the_runtime_role_withdraws_and_cannot_undo_it(roles, world, tmp_path):  # noqa: F811
    owner = world.user("professor")
    course_id = world.course(owner)
    harness = ApiHarness(world, tmp_path)
    document_id = harness.as_user(owner).upload(course_id).json()["id"]
    app_engine = roles.engine("app")
    app = create_app(Settings(upload_dir=harness.upload_root, environment="production"),
                     session_factory=sessionmaker(bind=app_engine, autoflush=False), storage=harness.storage,
                     publisher=RecordingPublisher())  # fmt: skip
    app.dependency_overrides[get_current_user] = lambda: owner
    with TestClient(app) as client:
        assert client.delete(_path(document_id)).status_code == 204
        assert client.delete(_path(document_id)).status_code == 204
        assert client.get(ApiHarness.path(course_id)).json() == []
    harness.client.close()
    undo = "UPDATE documents SET status = 'failed', withdrawn_at = NULL, withdrawn_by = NULL WHERE id = :d"
    denied(app_engine, undo, {"d": document_id}, "cannot change")


def _objects(world) -> tuple:
    return (
        world.scalar("SELECT count(*) FROM information_schema.columns WHERE table_name = 'documents' "
                     "AND column_name IN ('withdrawn_at', 'withdrawn_by')"),  # fmt: skip
        world.scalar("SELECT count(*) FROM pg_trigger WHERE tgname = 'documents_withdrawal_final'"),
        world.scalar("SELECT pg_get_indexdef('documents_course_id_content_hash_idx'::regclass) LIKE '%withdrawn%'"),
    )


def test_downgrade_refuses_while_withdrawn_documents_exist_and_round_trips_without_them(world, api, course, alembic):
    owner, course_id = course
    document_id = api.as_user(owner).upload(course_id).json()["id"]
    assert api.as_user(owner).client.delete(_path(document_id)).status_code == 204
    try:
        with pytest.raises(pytest.fail.Exception, match="downgrade to 0012 refused"):
            alembic("downgrade", "0012")
        assert world.scalar("SELECT version_num FROM alembic_version") == head_revision()
        world.execute("DELETE FROM documents WHERE id = :d", d=document_id)  # deliberate removal (as the owner)
        alembic("downgrade", "0012")
        assert _objects(world) == (0, 0, False)
        assert world.scalar("SELECT count(*) FROM pg_proc WHERE proname = 'studygraph_documents_withdrawal_final'") == 0
    finally:
        alembic("upgrade", "head")
    assert _objects(world) == (2, 1, True)
