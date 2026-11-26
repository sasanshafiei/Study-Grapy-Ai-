# StudyGraph AI

A source-grounded adaptive study coach for university courses. Students ask
questions and get answers cited to their own class materials **and** to
automatically fetched external sources (hybrid RAG); the system tracks per-topic
mastery and recommends what to study next. Professors generate exam questions
from course materials; admins manage users and sources. Web + mobile.

MVP domain: General Chemistry.

## Architecture in one picture

```
            web (Next.js)      mobile (Expo)
                   \               /
                    \             /
                   core-api (FastAPI)  ──►  ai-service (FastAPI)
                   /        |      \              |
            Postgres     Redis    worker      (retrieval, LLM,
           (+ pgvector)         (Celery jobs)   mastery, recs)
```

The clients only ever talk to **core-api**. core-api talks to **ai-service**
over the internal contract in `contracts/ai-service.openapi.yaml`.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose
- That's it to run the skeleton. (Node and Python only needed once you build the apps.)

## Quickstart

```bash
git clone <this repo>
cd studygraph
docker compose up --build
```

Then, in another terminal, create the tables with the Alembic migrations and
check everything is wired:

```bash
docker compose run --rm migrate                  # alembic upgrade head, as the schema owner
docker compose run --rm db-init                  # runtime roles and grants: explicit, after every migration
docker compose up -d                             # restart the API and workers as studygraph_app
docker compose run --rm migrate alembic current  # expect: 0013 (head) for this version
docker compose exec core-api python -m app.ingestion.admin init-storage --database studygraph   # once

curl localhost:8000/health        # core-api process liveness
curl localhost:8000/health/db     # database connectivity and pgvector diagnostic
curl --fail localhost:8000/health/ready  # core-api readiness for traffic
curl localhost:8001/health        # mock ai-service up
```

Order matters: `migrate` (schema owner) first, then `db-init`, then the runtime services. Until
`db-init` has run on a volume, the API and workers cannot log in as `studygraph_app`; see
[docs/database-security.md](docs/database-security.md). An existing database that holds data is
upgraded as an explicit rollout: back it up first and follow the per-revision notes in
[docs/database-migrations.md](docs/database-migrations.md). Never delete the volume to upgrade.

| Core API probe | Meaning |
| --- | --- |
| `GET /health` | Returns 200 when the HTTP process can respond. It makes no dependency calls. Use it for liveness. |
| `GET /health/db` | Returns 200 when the database queries succeed, with `pgvector_installed` set to true or false. It can return 200 on older revisions. A database query failure returns the existing generic 500. |
| `GET /health/ready` | Returns 200 with `{"status":"ready"}` when the app's database connection uses the public schema, pgvector is installed there, and `public.alembic_version` contains exactly the required revision. Connection, query, extension and compatibility failures return 503 with code `not_ready`. Both responses use `Cache-Control: no-store`. |

Compose probes the core API through `/health/ready`. The PostgreSQL service uses `pg_isready`
for database startup. Core readiness checks the recorded migration revision on every request.
It performs no migrations or table-by-table drift audit. Redis delivery, upload storage and AI
providers have feature-specific checks. Their availability does not gate core traffic.
The API role needs usage on `public` and read access to `public.alembic_version`.
The probe sets a two-second SQL statement timeout. Connection and pool acquisition use the
application engine's settings. Configure deployment connection and probe timeouts accordingly.

The document workers (`worker`, `worker-maintenance`) exit until the schema
is at head; restart them after migrating (`docker compose up -d`). Redis is not
published on the host (the workers trust what it hands them); see
[docs/document-processing.md](docs/document-processing.md). The index worker
(`worker-index`) claims nothing while `INDEXING_ENABLED` is false (the default); the
proposed AI indexing interface is in
[docs/ai-indexing-integration.md](docs/ai-indexing-integration.md).

Interactive API docs: http://localhost:8000/docs and http://localhost:8001/docs

Browsers may call core-api cross-origin only from the exact origins in `CORS_ALLOWED_ORIGINS`
(Compose allows the web dev server, `http://localhost:3000`; the default is same-origin only).
Staging and production must list their https origins; see
[docs/frontend-exam-adaptation.md](docs/frontend-exam-adaptation.md#browser-origins-and-readable-headers).

The old `python init_db.py` step is deprecated and no longer creates tables. If
your database was created with it, adopt it instead of re-running anything; see
[docs/database-migrations.md](docs/database-migrations.md). That document also
covers writing migrations and running the migration tests
(`services/core-api/scripts/test_migrations.sh`).

## Repo layout

```
apps/
  web/            Next.js app .......................... CE-2
  mobile/         Expo app ............................. CE-3
services/
  core-api/       FastAPI: auth, courses, docs, chat
                  proxy, quizzes, exams, admin ......... CE-1 (auth: CE-3)
  worker/         notes only; Celery workers run from
                  the core-api code and image .......... CE-1
  ai-service/     retrieval, generation, mastery,
                  recommendations (MOCK for now) ....... AI-1, AI-2
packages/
  api-client/     generated from the core API spec ..... CE-2/CE-3
  ui/             shared components + tokens ........... CE-2
contracts/        ai-service.openapi.yaml (the boundary)
eval/             gold sets, ablation scripts, results . AI-1, AI-2
infra/            docker-compose bits, deploy, CI ...... CE-3
docs/decisions/   one short file per locked decision
```

## Who does what next

| Person | Role | First task |
| --- | --- | --- |
| You | CE-1 backend + ingestion | this scaffold, then course document upload + parse job |
| Friend A | CE-2 web | `create-next-app` in `apps/web`, login + chat page |
| Friend B | CE-3 mobile + auth + devops | auth endpoints, CI, staging deploy, `create-expo-app` |
| AI friend 1 | AI-1 retrieval + generation | replace mock `/index` + `/chat/answer` with the real thing |
| AI friend 2 | AI-2 learner models | topic extraction, mastery on ASSISTments, recommendations |

## Next steps for the backend (CE-1)

1. ~~Swap `init_db.py` for **Alembic** migrations~~ Done: see [docs/database-migrations.md](docs/database-migrations.md).
2. Add routers under `app/routers/`: ~~`documents` (upload, list)~~ done, see
   [docs/document-ingestion.md](docs/document-ingestion.md); ~~`courses`, `chat`~~ done (M5), see
   [docs/courses-api.md](docs/courses-api.md) and [docs/ai-chat-integration.md](docs/ai-chat-integration.md)
   (chat is off by default and needs the AI service's studygraph.chat/1); ~~`quizzes`, `attempts`~~ done (M7),
   see [docs/quiz-api.md](docs/quiz-api.md) (off by default; generation needs the proposed
   studygraph.quiz/1, [docs/ai-quiz-integration.md](docs/ai-quiz-integration.md)); ~~`dashboard`,
   `analytics`, `study-events`~~ done (M8), see [docs/dashboard-api.md](docs/dashboard-api.md),
   [docs/analytics-privacy.md](docs/analytics-privacy.md) (off by default),
   [docs/study-events.md](docs/study-events.md) (off by default) and
   [docs/ai-mastery-integration.md](docs/ai-mastery-integration.md) (delivery off by default); ~~`exam-questions`,
   `exams` (manual professor exam builder, PDF/DOCX export)~~ done (M9-A1), see [docs/exam-api.md](docs/exam-api.md),
   [docs/exam-export.md](docs/exam-export.md) and [docs/frontend-exam-adaptation.md](docs/frontend-exam-adaptation.md)
   (off by default; AI-generated exam candidates are M9-A2 and wait for AI-1's protocol approval).
3. ~~Stand up the Celery worker and a first parse job~~ Done (M3), see
   [docs/document-processing.md](docs/document-processing.md). The index worker (M4) is off by default.
4. ~~Build the external-source connectors (OpenStax, Wikipedia) and the harvester~~ Done (M6, WP-6), see
   [docs/external-harvesting.md](docs/external-harvesting.md) (off by default; activation prerequisites,
   licence terms and egress restrictions are listed there). Admin observability and audited mutations
   (WP-4, WP-9): [docs/admin-api.md](docs/admin-api.md).

See `CONTRIBUTING.md` for the branch/PR rules.
