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
docker compose exec core-api alembic upgrade head
docker compose exec core-api alembic current   # expect: 0003 (head)
docker compose exec core-api python -m app.ingestion.admin init-storage --database studygraph   # once

curl localhost:8000/health        # core-api up
curl localhost:8000/health/db     # db reachable + pgvector installed
curl localhost:8001/health        # mock ai-service up
```

The document workers (`worker`, `worker-maintenance`, `beat`) exit until the schema
is at head; restart them after migrating (`docker compose up -d`). Redis is not
published on the host (the workers trust what it hands them); see
[docs/document-processing.md](docs/document-processing.md).

Interactive API docs: http://localhost:8000/docs and http://localhost:8001/docs

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
  worker/         Celery: parse, harvest, schedules .... CE-1
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
   [docs/document-ingestion.md](docs/document-ingestion.md); `courses`, `chat` (proxy to ai-service).
3. Stand up the Celery worker and a first parse job.
4. Build the external-source connectors (OpenStax, Wikipedia) and the harvester.

See `CONTRIBUTING.md` for the branch/PR rules.
