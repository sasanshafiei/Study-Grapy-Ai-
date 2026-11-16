# worker (CE-1)

Celery workers for jobs that can't run inside a web request:

- parse uploaded PDF/PPTX into clean sections
- call the AI service to index documents
- the external-source harvester (Lane A)
- scheduled jobs via Celery Beat (weekly re-harvest)

Not built yet. Skeleton lands in week 2. Broker + result backend: the Redis
instance from `docker-compose.yml` (`REDIS_URL`).
