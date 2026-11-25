"""Liveness, database diagnostics and traffic readiness on the disposable PostgreSQL server."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.ingestion import runtime
from app.main import create_app


def assert_unready(response):
    assert response.status_code == 503
    assert response.json() == {"code": "not_ready", "detail": "The database is not ready."}
    assert response.headers["Cache-Control"] == "no-store"


def test_diagnostic_succeeds_on_0008_but_readiness_and_worker_refuse(
    head_database, alembic, disposable_target, api_engine
):
    target = runtime.DatabaseTarget(disposable_target.url, disposable_target.marker)
    app = create_app(settings=Settings(database_url="unused"), session_factory=sessionmaker(bind=api_engine))
    with TestClient(app) as client:
        alembic("downgrade", "0008")
        try:
            with pytest.raises(runtime.PreflightError, match="0008"):
                runtime.check_database(target)
            diagnostic = client.get("/health/db")
            assert diagnostic.status_code == 200
            assert diagnostic.json() == {"status": "ok", "pgvector_installed": True}
            assert client.get("/health").status_code == 200
            assert_unready(client.get("/health/ready"))
        finally:
            alembic("upgrade", "head")
        runtime.check_database(target)
        assert client.get("/health/ready").status_code == 200


def test_readiness_uses_each_apps_database_without_requiring_ai(head_database, disposable_target):
    good = disposable_target.url
    unavailable = good.set(port=1, password="private-probe-password", query={"connect_timeout": "2"})

    def settings(url):
        return Settings(database_url=url.render_as_string(hide_password=False), environment="test",
                        ai_service_url="http://127.0.0.1:1",
                        indexing_enabled=False, quiz_enabled=False, chat_enabled=False, mastery_delivery_enabled=False)

    with TestClient(create_app(settings=settings(good))) as ready, TestClient(
        create_app(settings=settings(unavailable)), raise_server_exceptions=False
    ) as down:
        response = ready.get("/health/ready")
        assert response.status_code == 200 and response.json() == {"status": "ready"}
        assert response.headers["Cache-Control"] == "no-store"
        assert_unready(down.get("/health/ready"))
        live = down.get("/health")
        assert live.status_code == 200 and live.json() == {"status": "ok", "service": "core-api", "env": "test"}
        diagnostic = down.get("/health/db")
        assert diagnostic.status_code == 500 and diagnostic.json()["code"] == "server_error"
        assert "private-probe-password" not in diagnostic.text


@pytest.mark.parametrize("statement,vector_installed", [
    ("DROP EXTENSION vector CASCADE", False),
    ("ALTER EXTENSION vector SET SCHEMA probe_other", True),
    ("SET LOCAL search_path TO probe_other", True),
    ("DROP TABLE public.alembic_version", True),
    ("DELETE FROM public.alembic_version", True),
    ("UPDATE public.alembic_version SET version_num='future'", True),
    ("INSERT INTO public.alembic_version VALUES ('other_branch')", True),
])
def test_incompatible_database_metadata_fails_closed(head_database, rolled_back_transaction, statement, vector_installed):
    # The injected session observes the test's transaction. All DDL/data changes are rolled back on exit.
    with rolled_back_transaction() as connection:
        connection.execute(text("CREATE SCHEMA probe_other"))
        connection.execute(text(statement))
        app = create_app(session_factory=sessionmaker(bind=connection))
        with TestClient(app) as client:
            diagnostic = client.get("/health/db")
            assert diagnostic.status_code == 200
            assert diagnostic.json() == {"status": "ok", "pgvector_installed": vector_installed}
            assert_unready(client.get("/health/ready"))


def test_readiness_recovers_after_migration_lock(head_database, rolled_back_transaction, api_engine):
    app = create_app(session_factory=sessionmaker(bind=api_engine))
    with TestClient(app) as client:
        with rolled_back_transaction() as migration:
            # Bound the test even if the API ever loses its own statement timeout.
            migration.execute(text("SET LOCAL idle_in_transaction_session_timeout = '10s'"))
            migration.execute(text("LOCK TABLE public.alembic_version IN ACCESS EXCLUSIVE MODE"))
            assert_unready(client.get("/health/ready"))
        assert client.get("/health/ready").status_code == 200
