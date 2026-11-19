from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from .celery_app import make_celery
from .config import Settings
from .config import settings as default_settings
from .db import SessionLocal, get_db
from .errors import ApiError, api_error_handler, unhandled_error_handler
from .ingestion.dispatch import CeleryPublisher, Publisher
from .routers import documents
from .storage import LocalFileStorage

_DEFAULT = object()


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    storage: LocalFileStorage | None = None,
    publisher: Publisher | None | object = _DEFAULT,
) -> FastAPI:
    """Build the application.

    Everything that touches the database, the file store or the broker goes
    through app.state, so an application built with its own session factory,
    storage and publisher (as the tests do) has no path to the default engine,
    the real upload directory or the real broker. Pass publisher=None to
    publish nothing after uploads (the dispatch sweep still does). Building
    the app connects to nothing and creates nothing.
    """
    settings = settings or default_settings
    app = FastAPI(title="StudyGraph Core API", version="0.1.0")
    app.state.settings = settings
    app.state.session_factory = session_factory or SessionLocal
    app.state.storage = storage or LocalFileStorage(settings.upload_dir)
    if publisher is _DEFAULT:
        publisher = CeleryPublisher(make_celery(settings), settings) if settings.dispatch_on_upload else None
    app.state.publisher = publisher

    # Wide-open CORS for development so web + mobile can call freely.
    # Tighten this before any public deployment.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)  # JSON 500, details only in the log
    app.include_router(documents.router)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "core-api", "env": settings.environment}

    @app.get("/health/db")
    def health_db(db: Session = Depends(get_db)):
        """Proves the database is reachable and pgvector is installed."""
        db.execute(text("SELECT 1"))
        row = db.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).first()
        return {"status": "ok", "pgvector_installed": row is not None}

    return app


app = create_app()

# TODO (CE-1): add routers under app/routers/ for courses, chat (proxy to
# ai-service), quizzes, exams, admin. Keep them thin — they validate input,
# call the AI service or the DB, and shape the response. Authentication is
# CE-3's app.auth.get_current_user.
