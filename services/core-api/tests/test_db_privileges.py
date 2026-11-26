"""F05 least-privilege database roles, on the disposable PostgreSQL server (docs/database-security.md).

The shipped scripts (scripts/db_roles.sql, scripts/db_privileges.sql) are applied as they would be in a
deployment, real login roles connect over the network, and real application code runs through them.
Every role created here is dropped again, and the database's PUBLIC privileges are restored.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, pool, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.auth import get_current_user
from app.config import Settings
from app.database_compatibility import IncompatibleDatabase, check_database_compatibility, runtime_identity_problems
from app.indexing.client import AiIndexClient
from app.indexing.pipeline import IndexContext, run_index_job
from app.ingestion import runtime
from app.main import create_app
from tests.alembic_cli import run_alembic
from tests.api_support import ApiHarness, RecordingPublisher
from tests.disposable_db import DisposableTarget, verify_connection
from tests.fake_ai_service import FakeAiService
from tests.index_writer import persist
from tests.m8_support import graded_attempt
from tests.pdf_samples import make_pdf
from tests.test_index_jobs import _parsed, index_settings

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
GROUPS = ("studygraph_ce_runtime", "studygraph_ai_indexer", "studygraph_ai_retriever")
COMMIT = "public.commit_index_generation(uuid,uuid,text,text,integer,text,text,jsonb)"
RETRIEVE = "public.retrieve_index_chunks(uuid,text[])"
VERIFY = "public.verified_index_generation(uuid,uuid,text,text,integer,integer)"
# CE runtime tables that are not plain row DML; every other table must be.
READ_ONLY = {"alembic_version", "index_generations", "chunk_topics", "chunks"}
APPEND_ONLY = {"exam_audit_events", "admin_audit_events"}
DENIED = r"permission denied|must be owner|must be superuser|must have"


def run_script(connection: Connection, name: str) -> None:
    """Execute a shipped script verbatim, as psql would: no parameters, so `%` reaches the server untouched."""
    with connection.connection.cursor() as cursor:
        cursor.execute((SCRIPTS / name).read_text())


@dataclass
class Identities:
    target: DisposableTarget
    names: dict[str, str]
    passwords: dict[str, str]
    engines: list[Engine] = field(default_factory=list)

    def url(self, kind: str) -> URL:
        return self.target.url.set(username=self.names[kind], password=self.passwords[kind])

    def runtime_target(self, kind: str = "app") -> DisposableTarget:
        return DisposableTarget(url=self.url(kind), token=self.target.token)

    def engine(self, kind: str) -> Engine:
        """A NullPool engine for one login whose every connection re-verifies the disposable database."""
        identity = self.runtime_target(kind)
        engine = create_engine(identity.url, poolclass=pool.NullPool)

        @event.listens_for(engine, "engine_connect")
        def _verify(connection: Connection) -> None:
            verify_connection(connection, identity)
            connection.rollback()

        self.engines.append(engine)
        return engine


@pytest.fixture
def roles(head_database, disposable_engine: Engine, disposable_target: DisposableTarget):
    """The three groups, one login per group plus one without membership, and the grants at head."""
    suffix = secrets.token_hex(4)
    members = {"app": "studygraph_ce_runtime", "indexer": "studygraph_ai_indexer",
               "retriever": "studygraph_ai_retriever", "stranger": None}  # fmt: skip
    ids = Identities(disposable_target, {k: f"sg_f05_{k}_{suffix}" for k in members},
                     {k: secrets.token_hex(16) for k in members})  # fmt: skip
    try:
        with disposable_engine.begin() as c:
            verify_connection(c, disposable_target)
            run_script(c, "db_roles.sql")
            for kind, group in members.items():
                c.exec_driver_sql(f"CREATE ROLE {ids.names[kind]} LOGIN PASSWORD '{ids.passwords[kind]}'")
                if group:
                    c.exec_driver_sql(f"GRANT {group} TO {ids.names[kind]}")
            run_script(c, "db_privileges.sql")
        yield ids
    finally:
        for engine in ids.engines:
            engine.dispose()
        with disposable_engine.begin() as c:
            verify_connection(c, disposable_target)
            for name in (*ids.names.values(), *GROUPS):
                if c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": name}).first():
                    c.exec_driver_sql(f"DROP OWNED BY {name}")
                    c.exec_driver_sql(f"DROP ROLE {name}")
            c.exec_driver_sql(f'GRANT CONNECT, TEMPORARY ON DATABASE "{disposable_target.url.database}" TO PUBLIC')


def denied(engine: Engine, sql: str, params: dict | None = None, pattern: str = DENIED) -> None:
    with engine.connect() as c, c.begin():
        with pytest.raises(DBAPIError, match=pattern):
            c.execute(text(sql), params or {})


# --- catalog: role attributes, ownership, PUBLIC, exact grants ------------------------------------------


def test_groups_are_unprivileged_own_nothing_and_grants_are_exact(roles, disposable_engine):
    with disposable_engine.begin() as c:
        run_script(c, "db_roles.sql")  # both scripts are idempotent
        run_script(c, "db_privileges.sql")
        attrs = c.execute(text(
            "SELECT rolname, rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
            "FROM pg_roles WHERE rolname = ANY(:g)"), {"g": list(GROUPS)}).all()  # fmt: skip
        assert sorted(attrs) == sorted((g, False) for g in GROUPS)
        owners = c.execute(text(
            "SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "UNION SELECT DISTINCT pg_get_userbyid(proowner) FROM pg_proc WHERE pronamespace = 'public'::regnamespace"
        )).scalars().all()  # fmt: skip
        assert not set(owners) & {*GROUPS, *roles.names.values()}
        # No PostgreSQL RLS exists: course isolation is enforced by the application, not by policies.
        assert c.execute(text("SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                              "AND (relrowsecurity OR relforcerowsecurity)")).scalar_one() == 0  # fmt: skip

        tables = c.execute(text("SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                                "AND relkind IN ('r', 'p', 'v', 'm')")).scalars().all()  # fmt: skip
        privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
        for table in tables:
            for role in (*GROUPS, "public"):
                held = {p for p in privileges if c.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"), {"r": role, "t": f"public.{table}", "p": p}
                ).scalar_one()}  # fmt: skip
                if role != "studygraph_ce_runtime":
                    expected = set()
                elif table in READ_ONLY:
                    expected = {"SELECT"}
                elif table in APPEND_ONLY:
                    expected = {"SELECT", "INSERT"}
                else:
                    expected = {"SELECT", "INSERT", "UPDATE", "DELETE"}
                assert held == expected, (role, table, held)
        assert c.execute(text("SELECT has_column_privilege('studygraph_ce_runtime', 'public.chunks', 'generation_key', "
                              "'UPDATE')")).scalar_one()  # fmt: skip

        routines = {COMMIT: "studygraph_ai_indexer", RETRIEVE: "studygraph_ai_retriever", VERIFY: "studygraph_ce_runtime"}
        for routine, owner_group in routines.items():
            for role in (*GROUPS, "public"):
                allowed = c.execute(text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                                    {"r": role, "f": routine}).scalar_one()  # fmt: skip
                assert allowed == (role == owner_group), (role, routine)
        # Definer-rights routines keep their fixed search_path.
        definers = c.execute(text("SELECT proname, proconfig FROM pg_proc WHERE prosecdef "
                                  "AND pronamespace = 'public'::regnamespace")).all()  # fmt: skip
        assert {name for name, _ in definers} == {"commit_index_generation", "retrieve_index_chunks"}
        assert all(config == ["search_path=pg_catalog, pg_temp"] for _, config in definers)

        db = c.execute(text("SELECT current_database()")).scalar_one()
        for privilege in ("CONNECT", "TEMPORARY", "CREATE"):
            assert not c.execute(text("SELECT has_database_privilege('public', :d, :p)"),
                                 {"d": db, "p": privilege}).scalar_one()  # fmt: skip
        assert not c.execute(text("SELECT has_schema_privilege('public', 'public', 'CREATE')")).scalar_one()


def test_scripts_refuse_or_repair_unsafe_role_state(roles, disposable_engine):
    with disposable_engine.connect() as c:
        with c.begin() as tx:  # a pre-existing group with an attribute is normalized
            c.exec_driver_sql("ALTER ROLE studygraph_ai_retriever CREATEDB BYPASSRLS")
            run_script(c, "db_roles.sql")
            assert c.execute(text("SELECT rolcreatedb OR rolbypassrls FROM pg_roles "
                                  "WHERE rolname = 'studygraph_ai_retriever'")).scalar_one() is False  # fmt: skip
            tx.rollback()
        with c.begin() as tx:  # a group nested in another role is refused
            c.exec_driver_sql(f"GRANT {roles.names['stranger']} TO studygraph_ce_runtime")
            with pytest.raises(psycopg.Error, match="is a member of another role"):
                run_script(c, "db_roles.sql")
            tx.rollback()
        for unsafe in (f"ALTER ROLE {roles.names['app']} CREATEROLE",  # a privileged member of a group
                       f"ALTER TABLE public.users OWNER TO {roles.names['app']}",  # a member that owns a table
                       f"GRANT pg_read_server_files TO {roles.names['app']}"):  # a predefined role
            with c.begin() as tx:
                c.exec_driver_sql(unsafe)
                with pytest.raises(psycopg.Error, match="refusing to grant"):
                    run_script(c, "db_privileges.sql")
                tx.rollback()


# --- positive: the real application paths through the runtime and AI logins ----------------------------


def test_runtime_and_ai_logins_run_the_application(roles, world, tmp_path):
    app_engine, indexer_engine, retriever_engine = roles.engine("app"), roles.engine("indexer"), roles.engine("retriever")
    owner = world.user("professor", name="Owner")
    course = world.course(owner)

    # API as the runtime login, with production identity enforcement: readiness, upload, listing.
    harness = ApiHarness(world, tmp_path)
    settings = Settings(upload_dir=harness.upload_root, environment="production")
    app = create_app(settings, session_factory=sessionmaker(bind=app_engine, autoflush=False),
                     storage=harness.storage, publisher=RecordingPublisher())  # fmt: skip
    app.dependency_overrides[get_current_user] = lambda: owner
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        uploaded = client.post(ApiHarness.path(course), files={"file": ("Lecture.pdf", make_pdf(), "application/pdf")})
        assert uploaded.status_code == 202, uploaded.text
        assert uploaded.json()["id"] in {item["id"] for item in client.get(ApiHarness.path(course)).json()}
    harness.client.close()

    # Worker preflight as the runtime login under production enforcement.
    runtime.check_database(runtime.DatabaseTarget(roles.url("app"), roles.target.marker), environment="production")

    # Triggers firing as the invoker: graded attempt guards and the mastery outbox enqueue.
    topic = world.topic(course, "Equilibrium")
    student = world.user("student", name="Student")
    world.enroll(student, course)
    attempt = graded_attempt(app_engine, student.id, course, topic)
    assert world.scalar("SELECT count(*) FROM mastery_outbox WHERE event_id = :a", a=attempt) == 1

    # The F03 index boundary end to end: CE claims and publishes as the runtime login, the provider commits
    # through the definer routine as the indexer login, the retriever reads through its routine only.
    document_id, job_id = _parsed(world, course, owner)
    fake = FakeAiService(persist=lambda body: persist(indexer_engine, body)).start()
    try:
        index = index_settings(fake)
        ctx = IndexContext(session_factory=sessionmaker(bind=app_engine), settings=index, client=AiIndexClient(index))
        assert run_index_job(job_id, ctx).outcome == "succeeded"
    finally:
        fake.stop()
    assert world.scalar("SELECT status FROM documents WHERE id = :d", d=document_id) == "indexed"
    with retriever_engine.connect() as c:
        rows = c.execute(text(f"SELECT document_id FROM {RETRIEVE.split('(')[0]}(:c, ARRAY['class'])"),
                         {"c": course}).scalars().all()  # fmt: skip
        assert rows and set(rows) == {document_id}
        assert not c.execute(text("SELECT * FROM public.retrieve_index_chunks(:c, ARRAY['class'])"),
                             {"c": uuid.uuid4()}).all()  # fmt: skip
    with app_engine.connect() as c, c.begin():  # quiz provenance locks cited chunks
        assert c.execute(text("SELECT id FROM chunks WHERE document_id = :d ORDER BY id FOR SHARE"),
                         {"d": document_id}).all()  # fmt: skip
        c.execute(text("UPDATE chunks SET generation_key = generation_key WHERE document_id = :d"), {"d": document_id})
    for change in ("UPDATE chunks SET generation_key = repeat('0', 64) WHERE document_id = :d",
                   "UPDATE chunks SET generation_key = NULL WHERE document_id = :d"):  # fmt: skip
        denied(app_engine, change, {"d": document_id}, r"violates (foreign key|check) constraint")


# --- negative: everything else is refused ----------------------------------------------------------


def test_runtime_login_cannot_change_structure_roles_or_protected_rows(roles, disposable_target):
    app = roles.engine("app")
    for sql in (
        "CREATE TABLE public.f05_probe (id int)",
        "CREATE FUNCTION public.f05_probe() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        "CREATE SCHEMA f05_probe",
        "CREATE TEMPORARY TABLE f05_probe (id int)",
        "ALTER TABLE documents ADD COLUMN f05_probe int",
        "ALTER TABLE attempts DISABLE TRIGGER attempts_guard",
        "DROP TABLE exam_audit_events",
        "TRUNCATE ingestion_jobs",
        "SET session_replication_role = replica",
        "CREATE ROLE f05_probe",
        f"ALTER ROLE {roles.names['app']} BYPASSRLS",
        f"SET ROLE {disposable_target.url.username}",
        "UPDATE alembic_version SET version_num = '0001'",
        "INSERT INTO index_generations DEFAULT VALUES",
        "UPDATE chunks SET text = 'poisoned'",
        "UPDATE chunks SET section_ordinal = 0",
        "DELETE FROM chunks",
        "DELETE FROM chunk_topics",
        "DELETE FROM index_generations",
        "UPDATE exam_audit_events SET id = id",
        "DELETE FROM exam_audit_events",
        f"SELECT {COMMIT.split('(')[0]}(NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
        "SELECT * FROM public.retrieve_index_chunks(NULL, NULL)",
        "SELECT pg_read_file('postgresql.conf')",
        "COPY (SELECT 1) TO PROGRAM 'true'",
    ):
        denied(app, sql)
    with app.connect() as c, c.begin():  # no grant option: GRANT is a no-op, never an escalation
        c.exec_driver_sql("GRANT SELECT ON users TO PUBLIC")
        assert not c.execute(text("SELECT has_table_privilege('public', 'public.users', 'SELECT')")).scalar_one()
    with pytest.raises(DBAPIError, match="permission denied to create database"):
        with app.connect() as c:
            c.execution_options(isolation_level="AUTOCOMMIT").exec_driver_sql("CREATE DATABASE f05_probe")


def test_ai_logins_reach_only_their_routine_and_strangers_cannot_connect(roles):
    indexer, retriever = roles.engine("indexer"), roles.engine("retriever")
    commit = f"SELECT {COMMIT.split('(')[0]}(NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)"
    verify = "SELECT public.verified_index_generation(NULL, NULL, NULL, NULL, NULL, NULL)"
    retrieve = "SELECT * FROM public.retrieve_index_chunks(NULL, NULL)"
    for engine, refused in ((indexer, (retrieve, verify)), (retriever, (commit, verify))):
        for sql in (*refused, "SELECT 1 FROM users", "SELECT 1 FROM chunks", "SELECT 1 FROM index_generations",
                    "INSERT INTO chunks DEFAULT VALUES", "UPDATE documents SET status = 'indexed'",
                    "CREATE TABLE public.f05_probe (id int)", "CREATE TEMPORARY TABLE f05_probe (id int)"):  # fmt: skip
            denied(engine, sql)
    with indexer.connect() as c:  # its routine runs (with the owner's rights) and rejects the bogus call itself
        with pytest.raises(DBAPIError, match="returned no rows"):
            c.execute(text(commit.replace("NULL", "gen_random_uuid()", 1)))
    stranger = create_engine(roles.url("stranger"), poolclass=pool.NullPool)
    try:
        with pytest.raises(OperationalError, match="permission denied for database"):
            stranger.connect()
    finally:
        stranger.dispose()


def test_api_and_workers_refuse_a_privileged_role_outside_development(roles, api_engine, disposable_target):
    with api_engine.connect() as c:  # the disposable superuser
        assert "superuser" in runtime_identity_problems(c)
        check_database_compatibility(c, unprivileged=False)
        with pytest.raises(IncompatibleDatabase, match="too privileged"):
            check_database_compatibility(c, unprivileged=True)
    with roles.engine("app").connect() as c:
        assert runtime_identity_problems(c) == []
    with api_engine.connect() as c, c.begin() as tx:  # power through a predefined role is refused as well
        c.exec_driver_sql(f"GRANT pg_write_all_data TO {roles.names['app']}")
        c.exec_driver_sql(f"SET LOCAL ROLE {roles.names['app']}")
        assert runtime_identity_problems(c) == ["predefined role"]
        tx.rollback()
    superuser = runtime.DatabaseTarget(disposable_target.url, disposable_target.marker)
    runtime.check_database(superuser, environment="test")
    for environment in ("production", "staging", "unknown"):
        with pytest.raises(runtime.PreflightError, match="too privileged"):
            runtime.check_database(superuser, environment=environment)
    factory = sessionmaker(bind=api_engine)
    for environment, status in (("development", 200), ("production", 503)):
        with TestClient(create_app(Settings(environment=environment), session_factory=factory)) as client:
            assert client.get("/health/ready").status_code == status


# --- migrations with a dedicated, non-superuser owner --------------------------------------------


def test_migrations_and_grants_run_as_a_dedicated_non_superuser_owner(
    roles, disposable_engine, disposable_target, alembic
):
    database, superuser = disposable_target.url.database, disposable_target.url.username
    owner, password = f"sg_f05_owner_{secrets.token_hex(4)}", secrets.token_hex(16)
    alembic("downgrade", "base")
    try:
        with disposable_engine.begin() as c:
            verify_connection(c, disposable_target)
            c.exec_driver_sql("DROP TABLE alembic_version")  # empty; recreated by the owner below
            c.exec_driver_sql(f"CREATE ROLE {owner} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{password}'")
            c.exec_driver_sql(f'ALTER DATABASE "{database}" OWNER TO {owner}')
        owner_url = disposable_target.url.set(username=owner, password=password)
        result = run_alembic("upgrade", "head", env={
            "DATABASE_URL": owner_url.render_as_string(hide_password=False),
            "MIGRATIONS_REQUIRE_DATABASE_COMMENT": disposable_target.marker})  # fmt: skip
        assert result.returncode == 0, result.stderr
        owner_engine = create_engine(owner_url, poolclass=pool.NullPool)
        try:
            with owner_engine.begin() as c:
                verify_connection(c, DisposableTarget(url=owner_url, token=disposable_target.token))
                run_script(c, "db_privileges.sql")
                assert {"table owner", "schema create", "database create"} <= set(runtime_identity_problems(c))
                foreign = c.execute(text(
                    "SELECT count(*) FROM pg_class c WHERE relnamespace = 'public'::regnamespace "
                    "AND relowner <> current_user::regrole AND NOT EXISTS (SELECT 1 FROM pg_depend d "
                    "WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
                )).scalar_one()  # fmt: skip
                assert foreign == 0
        finally:
            owner_engine.dispose()
        # Definer-rights routines owned by a non-superuser still reach the tables they need.
        with roles.engine("retriever").connect() as c:
            assert c.execute(text("SELECT count(*) FROM public.retrieve_index_chunks(:c, ARRAY['class'])"),
                             {"c": uuid.uuid4()}).scalar_one() == 0  # fmt: skip
        with roles.engine("indexer").connect() as c:
            with pytest.raises(DBAPIError, match="returned no rows"):
                c.execute(text(f"SELECT {COMMIT.split('(')[0]}(gen_random_uuid(), NULL, NULL, NULL, NULL, NULL, NULL, NULL)"))
    finally:
        with disposable_engine.begin() as c:
            verify_connection(c, disposable_target)
            c.exec_driver_sql(f'ALTER DATABASE "{database}" OWNER TO {superuser}')
            if c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": owner}).first():
                c.exec_driver_sql(f"DROP OWNED BY {owner}")  # the disposable schema it created
                c.exec_driver_sql(f"DROP ROLE {owner}")
        alembic("upgrade", "head")
