"""Database selection and engine ownership at the application factory boundary."""

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import SessionLocal
from app.main import app as runtime_app
from app.main import create_app
from tests.disposable_db import verify_connection


def test_explicit_settings_select_independent_databases_without_connecting(monkeypatch):
    def unexpected_connection(*args, **kwargs):
        pytest.fail("Building or starting the app opened a database connection")

    monkeypatch.setattr(psycopg, "connect", unexpected_connection)
    first_url = "postgresql+psycopg://unused.invalid:5432/first"
    second_url = "postgresql+psycopg://unused.invalid:5432/second"
    first = create_app(settings=Settings(database_url=first_url))
    second = create_app(settings=Settings(database_url=second_url))

    with TestClient(first), TestClient(second):
        with first.state.session_factory() as first_session, second.state.session_factory() as second_session:
            assert first_session.get_bind().url == make_url(first_url)
            assert second_session.get_bind().url == make_url(second_url)
            assert first_session.get_bind() is not second_session.get_bind()


def test_owned_engine_releases_real_connections_at_shutdown(disposable_target, head_database):
    app = create_app(settings=Settings(database_url=disposable_target.url.render_as_string(hide_password=False)))
    with app.state.session_factory() as session:
        engine = session.get_bind()
    assert engine.url == disposable_target.url  # Verify the target before opening a connection.
    connections = []

    @event.listens_for(engine, "connect")
    def track_connection(dbapi_connection, _record):
        connections.append(dbapi_connection)

    @event.listens_for(engine, "engine_connect")
    def verify_target(connection):
        verify_connection(connection, disposable_target)
        connection.rollback()

    with TestClient(app) as client:
        assert not connections
        response = client.get("/health/db")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "pgvector_installed": True}
        assert len(connections) == 1 and not connections[0].closed

    assert connections[0].closed


def test_injected_factory_takes_precedence_and_remains_caller_owned(api_engine, head_database):
    factory = sessionmaker(bind=api_engine)
    original_pool = api_engine.pool
    # Even an unusable URL must be irrelevant when the caller supplies the binding.
    app = create_app(settings=Settings(database_url="unused"), session_factory=factory)
    assert app.state.session_factory is factory
    with TestClient(app) as client:
        assert client.get("/health/db").json() == {"status": "ok", "pgvector_installed": True}
    assert api_engine.pool is original_pool


def test_default_app_keeps_the_process_session_factory():
    app = create_app()
    assert app.state.session_factory is runtime_app.state.session_factory is SessionLocal
    with SessionLocal() as session:
        engine = session.get_bind()
        assert engine.url == make_url(app.state.settings.database_url.get_secret_value())
    original_pool = engine.pool
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert engine.pool is original_pool
