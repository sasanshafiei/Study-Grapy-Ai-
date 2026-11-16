# 0001 — Tech stack and repo shape

Status: accepted · Date: 2026-11-16

## Context

Capstone 2 adds automated external sources, hybrid RAG, three roles, a web and
a mobile UI, and a deployed system. We needed to lock the stack before writing
features so five people can work in parallel.

## Decision

- Monorepo: pnpm workspaces (JS) + uv/pip (Python).
- Backend: FastAPI (Python 3.11), SQLAlchemy 2, Alembic migrations.
- AI service: separate FastAPI service behind an internal HTTP contract.
- Database: PostgreSQL + pgvector (one DB for relational + dense + sparse).
- Sparse retrieval: Postgres full-text search first; a BM25 extension only if an
  ablation shows a clear gain.
- Jobs: Celery + Redis (Celery Beat for schedules).
- Web: Next.js + TypeScript. Mobile: Expo (React Native) + TypeScript.
- Auth: JWT access + refresh, three roles checked server-side.
- LLM: one hosted API with JSON-schema output, behind a single provider interface.
- Hosting: one small VM, Docker Compose, HTTPS.

## Consequences

- Web and mobile share one generated API client and one set of types.
- The AI team ships on its own schedule against a mock service.
- Embedding dimension (384) is fixed at indexing time; changing models means
  re-embedding everything, so the model is chosen before indexing real content.
