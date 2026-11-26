# worker (CE-1)

The Celery workers live in the core-api codebase and image, not here: they share the
models, storage, configuration and dependency set of `services/core-api`, so there is
one image to build and no second copy of the schema code. Entry point:
`services/core-api/app/worker.py`; logic in `services/core-api/app/ingestion/`.

- `ingestion.parse`: parse uploaded PDFs into page-aware sections (M3)
- `ingestion.maintenance`: dispatch sweep, lease recovery, file reconciliation (M3), on
  their own worker so long parses cannot starve them
- `ingestion.index`: indexing through the AI service (M4; off by default, `INDEXING_ENABLED`)
- `harvest.run`: the external-source harvester (M6; off by default, `HARVEST_ENABLED`)
- `mastery.deliver`: mastery event delivery (M8; off by default, `MASTERY_DELIVERY_ENABLED`)
- Celery Beat: the maintenance schedule (one instance)

Broker: the Redis instance from `docker-compose.yml` (`REDIS_URL`). There is no result
backend: job state lives in PostgreSQL (`ingestion_jobs`). See
[docs/document-processing.md](../../docs/document-processing.md).
