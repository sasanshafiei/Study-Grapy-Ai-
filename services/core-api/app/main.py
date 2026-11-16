from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db

app = FastAPI(title="StudyGraph Core API", version="0.1.0")

# Wide-open CORS for development so web + mobile can call freely.
# Tighten this before any public deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# TODO (CE-1): add routers under app/routers/ for auth, courses, documents,
# chat (proxy to ai-service), quizzes, exams, admin. Keep them thin — they
# validate input, call the AI service or the DB, and shape the response.
