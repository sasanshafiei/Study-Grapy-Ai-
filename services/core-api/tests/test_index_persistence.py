"""F03 on the disposable PostgreSQL instance, using independent provider and CE transactions."""

import json
import multiprocessing
import threading
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.indexing.payload import idempotency_key, request_for
from app.ingestion import jobs
from tests.alembic_cli import run_alembic
from tests.disposable_db import verify_connection
from tests.index_writer import COMMIT, parameters
from tests.test_index_jobs import _parsed

VISIBLE = text("SELECT id FROM public.retrieve_index_chunks(:course, ARRAY['class','external'])")
GRANTS = Path(__file__).resolve().parents[1] / "scripts/index_privileges.sql"


@pytest.fixture
def batch(world):
    owner = world.user("professor")
    course = world.course(owner)
    doc, job = _parsed(world, course, owner)
    factory = sessionmaker(bind=world.engine)
    with factory.begin() as session:
        request = request_for(session, doc)
        claim = jobs.claim_index(session, job, 600)
    return factory, claim, parameters(json.loads(request.body))


def finish(batch, count=None):
    factory, claim, data = batch
    return jobs.complete_index(factory, claim, data["digest"], data["sections"] if count is None else count)


def commit(world, data):
    with world.engine.begin() as c:
        return c.execute(COMMIT, data).one()


def visible(world, course):
    with world.engine.connect() as c:
        return list(c.execute(VISIBLE, {"course": course}).scalars())


def _provider_process(target, data):
    engine = create_engine(target.url)
    try:
        with engine.begin() as connection:
            verify_connection(connection, target)
            return tuple(connection.execute(COMMIT, data).one())
    finally:
        engine.dispose()


def test_restarted_provider_replays_without_chunks_or_process_memory(disposable_target, batch, world):
    data = batch[2]
    lookup = {**data, "chunks": None, "model": None, "version": None}
    for request, expected in ((data, (2, False)), (lookup, (2, True))):
        # Exit the first interpreter before starting its replacement.
        with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as process:
            assert process.submit(_provider_process, disposable_target, request).result(timeout=30) == expected
    assert world.scalar("SELECT count(*) FROM chunks WHERE document_id=:d", d=data["document"]) == 2
    assert finish(batch) == jobs.CompletionStatus.COMPLETED


def test_atomic_visibility_rollback_then_commit_publish_and_fresh_connection_replay(world, batch):
    _, _, data = batch
    with world.engine.connect() as writer:
        assert writer.execute(COMMIT, data).one() == (2, False)
        assert world.scalar("SELECT count(*) FROM index_generations") == 0
        assert visible(world, data["course"]) == []
        writer.rollback()
    assert world.scalar("SELECT count(*) FROM chunks WHERE document_id=:d", d=data["document"]) == 0
    assert commit(world, data) == (2, False)
    assert visible(world, data["course"]) == []
    assert commit(world, {**data, "chunks": None, "model": None, "version": None}) == (2, True)
    assert finish(batch) == jobs.CompletionStatus.COMPLETED
    assert len(visible(world, data["course"])) == 2
    assert visible(world, uuid.uuid4()) == []
    world.execute("UPDATE documents SET status='stale' WHERE id=:d", d=data["document"])
    assert visible(world, data["course"]) == []


@pytest.mark.parametrize("damage", [
    "DELETE FROM chunks WHERE document_id=:d",  # well-formed receipt, empty generation
    "DELETE FROM chunks WHERE document_id=:d AND section_ordinal=0",
    "UPDATE chunks SET locator='{}' WHERE document_id=:d",
    "UPDATE chunks SET text='Invented passage' WHERE document_id=:d",
    "UPDATE chunks SET section_ordinal=0 WHERE document_id=:d",
    "UPDATE index_generations SET section_count=1 WHERE document_id=:d",
    "UPDATE index_generations SET content_digest=repeat('0',64) WHERE document_id=:d",
])
def test_even_privileged_inconsistent_evidence_cannot_publish(world, batch, damage):
    data = batch[2]
    commit(world, data)
    world.execute(damage, d=data["document"])
    assert finish(batch) == jobs.CompletionStatus.PROOF_MISSING
    assert visible(world, data["course"]) == []
    assert world.scalar("SELECT status FROM documents WHERE id=:d", d=data["document"]) == "pending"


def test_count_echo_and_canonical_key_must_match_durable_identity(world, batch):
    data = batch[2]
    commit(world, data)
    assert finish(batch, 3) == jobs.CompletionStatus.PROOF_MISSING
    # Same source, but an operator restored chunks under a different well-formed key.
    # Preserve the FK by removing the chunks before editing the generation.
    with world.engine.begin() as c:
        c.execute(text("CREATE TEMP TABLE saved ON COMMIT DROP AS SELECT * FROM chunks WHERE document_id=:d"),
                  {"d": data["document"]})
        c.execute(text("DELETE FROM chunks WHERE document_id=:d"), {"d": data["document"]})
        c.execute(text("UPDATE index_generations SET idempotency_key=repeat('1',64) WHERE document_id=:d"),
                  {"d": data["document"]})
        c.execute(text("UPDATE saved SET generation_key=repeat('1',64)"))
        c.execute(text("INSERT INTO chunks SELECT * FROM saved"))
    assert finish(batch) == jobs.CompletionStatus.PROOF_MISSING


@pytest.mark.parametrize("invalid", ["missing_vector", "zero_vector", "wrong_dimension", "nonfinite",
                                     "wrong_topic_course", "missing_section", "invalid_model", "wrong_locator", "wrong_text"])
def test_invalid_writer_input_rolls_back_all_rows(world, batch, invalid):
    data = batch[2].copy()
    chunks = json.loads(data["chunks"])
    if invalid == "wrong_topic_course":
        other = world.course(world.user("professor"))
        chunks[-1]["topic_ids"] = [str(world.topic(other, "Foreign topic"))]
    elif invalid == "missing_section":
        chunks[-1]["section_ordinal"] = 0
    elif invalid == "wrong_locator":
        chunks[-1]["locator"] = {"page": 98}
    elif invalid == "wrong_text":
        chunks[-1]["text"] = "Unrelated content"
    elif invalid == "invalid_model":
        data["model"] = ""
    else:
        chunks[-1]["embedding"] = {"missing_vector": None, "zero_vector": [0] * 384,
                                    "wrong_dimension": [1], "nonfinite": ["NaN"] + [0] * 383}[invalid]
    data["chunks"] = json.dumps(chunks)
    with pytest.raises(DBAPIError):
        commit(world, data)
    assert world.scalar("SELECT count(*) FROM index_generations WHERE document_id=:d", d=data["document"]) == 0
    assert world.scalar("SELECT count(*) FROM chunks WHERE document_id=:d", d=data["document"]) == 0


def test_concurrent_duplicate_and_conflict_do_not_replace_original(world, batch):
    data = batch[2]
    barrier = threading.Barrier(4)
    def writer():
        barrier.wait(timeout=10)
        return commit(world, data)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: writer(), range(4)))
    assert sorted(results) == [(2, False)] + [(2, True)] * 3
    digest = "1" * 64
    for changes, error in [
        ({"course": uuid.uuid4()}, "course_mismatch"),
        ({"document": uuid.uuid4()}, "no rows"),
        ({"key": "2" * 64}, "inconsistent_request"),
        ({"digest": digest, "key": idempotency_key(data["document"], data["course"], digest)}, "generation_conflict"),
    ]:
        with pytest.raises(DBAPIError, match=error):
            commit(world, {**data, **changes})
    assert finish(batch) == jobs.CompletionStatus.COMPLETED


def test_expired_unrecovered_claim_cannot_publish(world, batch):
    data = batch[2]
    commit(world, data)
    world.execute("UPDATE ingestion_jobs SET started_at=now()-interval '1 hour', lease_expires_at=now()-interval '1 second' "
                  "WHERE id=:j", j=batch[1].job_id)
    assert finish(batch) == jobs.CompletionStatus.STALE
    assert visible(world, data["course"]) == []


def test_role_capabilities_on_real_database(world, batch):
    data = batch[2].copy()
    topic = world.topic(uuid.UUID(data["course"]), "Catalysts")
    chunks = json.loads(data["chunks"])
    chunks[0]["topic_ids"] = [str(topic)]
    data["chunks"] = json.dumps(chunks)
    # Transactional provisioning exercises the shipped grants, scoped to this disposable session.
    with world.engine.connect() as c, c.begin() as tx:
        for role in ("studygraph_ai_indexer", "studygraph_ai_retriever", "studygraph_ce_runtime"):
            c.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        c.exec_driver_sql("GRANT SELECT ON documents, chunks, document_sections TO studygraph_ce_runtime")
        c.exec_driver_sql(GRANTS.read_text())
        c.exec_driver_sql("SET LOCAL ROLE studygraph_ai_indexer")
        assert c.execute(COMMIT, data).one() == (2, False)
        for sql in ("SELECT * FROM users LIMIT 0", "SELECT answer_key FROM questions LIMIT 0",
                    "SELECT * FROM alembic_version", "DELETE FROM chunks WHERE false",
                    "UPDATE documents SET status='indexed' WHERE false", "UPDATE ingestion_jobs SET status='succeeded' WHERE false",
                    "UPDATE index_generations SET chunk_count=1 WHERE false", str(VISIBLE)):
            with pytest.raises(DBAPIError, match="permission denied"), c.begin_nested():
                c.execute(text(sql), {"course": data["course"]})
        c.exec_driver_sql("RESET ROLE")
        c.exec_driver_sql("SET LOCAL ROLE studygraph_ai_retriever")
        assert not c.execute(VISIBLE, {"course": data["course"]}).all()
        for stmt in (text("SELECT embedding FROM chunks"), COMMIT):
            with pytest.raises(DBAPIError, match="permission denied"), c.begin_nested():
                c.execute(stmt, data)
        c.exec_driver_sql("RESET ROLE")
        # Emulate CE publication inside this isolated permissions test. Normal publication is tested above.
        c.execute(text("UPDATE documents SET status='indexed' WHERE id=:document"), data)
        c.exec_driver_sql("SET LOCAL ROLE studygraph_ai_retriever")
        assert len(c.execute(VISIBLE, data).all()) == 2
        assert c.execute(text("SELECT count(*) FROM retrieve_index_chunks(:course, ARRAY['external'])"), data).scalar_one() == 0
        assert not c.execute(VISIBLE, {"course": uuid.uuid4()}).all()
        c.exec_driver_sql("RESET ROLE")
        c.exec_driver_sql("SET LOCAL ROLE studygraph_ce_runtime")
        assert c.execute(text("SELECT verified_index_generation(:document,:course,:key,:digest,:sections,2)"), data).scalar_one()
        assert c.execute(text("SELECT chunk_count FROM index_generations WHERE document_id=:document"), data).scalar_one() == 2
        with pytest.raises(DBAPIError, match="permission denied"), c.begin_nested():
            c.execute(COMMIT, data)
        tx.rollback()


def test_previous_revision_quarantines_legacy_data_and_downgrade_refuses_durable_data(world, batch, alembic, alembic_env):
    _, _, data = batch
    alembic("downgrade", "0008")
    world.execute("UPDATE documents SET status='indexed' WHERE id=:d", d=data["document"])
    old = world.chunk(uuid.UUID(data["document"]), None, "legacy unscoped passage")
    topic = world.topic(uuid.UUID(data["course"]), "Old topic")
    world.execute("INSERT INTO chunk_topics(chunk_id,topic_id) VALUES (:id,:topic)", id=old, topic=topic)
    alembic("upgrade", "0009")
    assert world.scalar("SELECT status FROM documents WHERE id=:d", d=data["document"]) == "failed"
    assert world.scalar("SELECT error_message FROM documents WHERE id=:d", d=data["document"]) is None
    assert world.scalar("SELECT text FROM chunks WHERE id=:id AND generation_key IS NULL", id=old) == "legacy unscoped passage"
    assert world.scalar("SELECT count(*) FROM chunk_topics WHERE chunk_id=:id AND course_id IS NULL", id=old) == 1
    assert visible(world, data["course"]) == []
    world.execute("UPDATE documents SET status='pending' WHERE id=:d", d=data["document"])
    with pytest.raises(DBAPIError, match="document_not_writable"):
        commit(world, data)
    # Deliberate cleanup on the disposable copy only.
    world.execute("DELETE FROM chunk_topics WHERE chunk_id=:id", id=old)
    world.execute("DELETE FROM chunks WHERE id=:id", id=old)
    commit(world, data)
    refused = run_alembic("downgrade", "0008", env=alembic_env)
    assert refused.returncode and "Downgrade below 0009 refused" in refused.stderr
    assert world.scalar("SELECT version_num FROM alembic_version") == "0009"


def test_multiple_distinct_chunks_per_section_and_cross_course_fks(world, batch):
    data = batch[2].copy()
    chunks = json.loads(data["chunks"])
    chunks.append({**chunks[0], "text": "Chatelier"})
    data["chunks"] = json.dumps(chunks)
    assert commit(world, data) == (3, False)
    other = world.course(world.user("professor"))
    with pytest.raises(DBAPIError, match="chunks_generation_fkey"):
        world.execute("UPDATE chunks SET course_id=:other WHERE document_id=:id", other=other, id=data["document"])
    other_doc = world.document(other)
    with pytest.raises(DBAPIError, match="chunks_generation_fkey"):
        world.execute("UPDATE chunks SET document_id=:other WHERE document_id=:id", other=other_doc, id=data["document"])
    assert finish(batch, 3) == jobs.CompletionStatus.COMPLETED
    assert len(visible(world, data["course"])) == 3


def test_new_generation_requires_live_ce_work(world, batch):
    data = batch[2]
    world.execute("UPDATE ingestion_jobs SET started_at=now()-interval '1 hour', lease_expires_at=now()-interval '1 second' "
                  "WHERE id=:j", j=batch[1].job_id)
    with pytest.raises(DBAPIError, match="no_live_index_claim"):
        commit(world, data)
    assert not visible(world, data["course"])
