from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .celery_app import make_celery
from .chat.client import AiChatClient
from .config import Settings
from .config import settings as default_settings
from .database_compatibility import IncompatibleDatabase, check_database_compatibility
from .db import SessionLocal, create_engine_and_session_factory, get_db
from .errors import ApiError, api_error_handler, unhandled_error_handler
from .indexing.client import InvalidServiceUrl
from .ingestion.dispatch import CeleryPublisher, Publisher
from .limits import ConcurrencyLimiter, RateLimiter
from .quiz.client import AiQuizClient
from .routers import chat, courses, dashboard, documents, exam_questions, exams, quizzes
from .schemas.documents import ErrorBody
from .schemas.openapi import STRING, enum, exact
from .storage import LocalFileStorage

_DEFAULT = object()


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    storage: LocalFileStorage | None = None,
    publisher: Publisher | None | object = _DEFAULT,
    chat_client: AiChatClient | None | object = _DEFAULT,
    quiz_client: AiQuizClient | None | object = _DEFAULT,
) -> FastAPI:
    """Build the application.

    Everything that touches the database, the file store or the broker goes
    through app.state, so an application built with its own session factory,
    storage and publisher (as the tests do) has no path to the default engine,
    the real upload directory or the real broker. Pass publisher=None to
    publish nothing after uploads (the dispatch sweep still does). Building
    the app opens no connections. An injected session factory is caller-owned.
    Explicit settings without a factory give this app its own engine. The
    no-argument app uses the process-wide SessionLocal.

    M5: the chat client (AI_SERVICE_URL; None when that URL is invalid, which
    makes chat answer 503) and the in-process limiters live on app.state too,
    so every application instance has its own.

    M7: the quiz client (None when AI_SERVICE_URL is invalid: generation then
    answers 503, already issued quizzes and local grading keep working) and the
    generation limiter live on app.state as well.

    M9: the exam routers (all answer 503 after authorization while EXAMS_ENABLED is
    off) and the export render/rate limiters.
    """
    owned_engine = None
    if session_factory is None:
        if settings is None:
            session_factory = SessionLocal
        else:
            owned_engine, session_factory = create_engine_and_session_factory(settings)
    settings = settings or default_settings

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if owned_engine is not None:
                owned_engine.dispose()

    app = FastAPI(
        title="StudyGraph Core API", version="0.1.0", lifespan=lifespan,
        description="CE-1 public HTTP API. Protected routes require a bearer token. The CE-3 authentication seam "
                    "currently fails closed (401). Exam routes require the active owning professor and EXAMS_ENABLED; "
                    "file downloads additionally require EXAM_EXPORT_ENABLED. Both print variants are professor-only. "
                    "All exam responses carry Cache-Control: no-store.",
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.storage = storage or LocalFileStorage(settings.upload_dir)
    if publisher is _DEFAULT:
        publisher = CeleryPublisher(make_celery(settings), settings) if settings.dispatch_on_upload else None
    app.state.publisher = publisher
    if chat_client is _DEFAULT:
        try:
            chat_client = AiChatClient(settings)
        except InvalidServiceUrl:
            chat_client = None
    app.state.chat_client = chat_client
    if quiz_client is _DEFAULT:
        try:
            quiz_client = AiQuizClient(settings)
        except InvalidServiceUrl:
            quiz_client = None
    app.state.quiz_client = quiz_client
    app.state.quiz_limiter = ConcurrencyLimiter(settings.quiz_max_concurrent, 1)
    app.state.chat_limiter = ConcurrencyLimiter(settings.chat_max_concurrent, settings.chat_max_concurrent_per_user)
    app.state.join_rate_limiter = RateLimiter(
        settings.join_rate_limit_attempts, settings.join_rate_limit_window_seconds, settings.join_rate_limit_max_users
    )
    # M9: renderer slots (global and one per professor) and the per-professor download rate (single process).
    app.state.exam_export_limiter = ConcurrencyLimiter(settings.exam_export_max_concurrent, 1)
    app.state.exam_export_rate_limiter = RateLimiter(
        settings.exam_export_rate_limit, settings.exam_export_rate_window_seconds, settings.exam_export_rate_max_users
    )

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
    app.include_router(courses.router)
    app.include_router(chat.router)
    app.include_router(quizzes.router)
    app.include_router(dashboard.router)
    app.include_router(exam_questions.router)
    app.include_router(exams.router)

    @app.get("/health", responses={200: {"content": {"application/json": {"schema":
        exact(status=enum("ok"), service=enum("core-api"), env=STRING)}}}})
    def health():
        """Process liveness. This check makes no dependency calls."""
        return {"status": "ok", "service": "core-api", "env": settings.environment}

    @app.get("/health/db", responses={200: {"content": {"application/json": {"schema":
        exact(status=enum("ok"), pgvector_installed={"type": "boolean"})}}}, 500: {"model": ErrorBody}})
    def health_db(db: Session = Depends(get_db)):
        """Database diagnostic. Reports connectivity and whether pgvector is installed at any schema revision."""
        db.execute(text("SELECT 1"))
        row = db.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).first()
        return {"status": "ok", "pgvector_installed": row is not None}

    @app.get("/health/ready", responses={
        200: {"description": "The core API database is ready.", "content": {"application/json": {
            "schema": exact(status=enum("ready"))}}},
        503: {"description": "The database is unreachable or incompatible.", "model": ErrorBody},
    })
    def readiness(response: Response, db: Session = Depends(get_db)):
        """Core API readiness. Requires the application's PostgreSQL schema revision and pgvector in public."""
        try:
            db.execute(text("SET LOCAL statement_timeout = '2s'"))
            check_database_compatibility(db.connection())
        except (IncompatibleDatabase, SQLAlchemyError):
            raise ApiError(503, "not_ready", "The database is not ready.", headers={"Cache-Control": "no-store"}) from None
        response.headers["Cache-Control"] = "no-store"
        return {"status": "ready"}

    return app


app = create_app()

# TODO (CE-1): add routers under app/routers/ for admin. Keep them thin — they validate input,
# call the AI service or the DB, and shape the response. Authentication is
# CE-3's app.auth.get_current_user.
