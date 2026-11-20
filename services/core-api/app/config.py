from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024

QUEUE_NAME_PATTERN = r"^[A-Za-z0-9._-]{1,100}$"


class Settings(BaseSettings):
    """All config comes from environment variables (see .env.example).

    Field names map to env vars case-insensitively, so `database_url`
    is read from DATABASE_URL.

    The database and broker URLs can carry passwords, so they are SecretStr:
    repr(), str() and JSON dumps of the settings show `**********`, and
    validation errors never echo input values (hide_input_in_errors). Code
    that needs the real URL calls .get_secret_value() at the point of use.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr = SecretStr("postgresql+psycopg://studygraph:studygraph@db:5432/studygraph")
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")
    ai_service_url: str = "http://ai-service:8000"
    environment: str = "development"

    # Root of the uploaded-file store. docker-compose mounts the persistent
    # `uploads` volume here; nothing creates or touches it until the first upload.
    upload_dir: Path = Path("/data/uploads")
    # Largest accepted upload, in bytes (25 MiB). Lecture PDFs are normally far
    # below this; raise it for image-heavy slide exports.
    max_upload_bytes: int = Field(default=25 * MIB, gt=0, le=1024 * MIB)

    # --- asynchronous processing (M3, see docs/document-processing.md) -----------------
    # Broker queues; configurable so that tests can use queues nobody else consumes.
    parse_queue: str = Field(default="ingestion.parse", pattern=QUEUE_NAME_PATTERN)
    maintenance_queue: str = Field(default="ingestion.maintenance", pattern=QUEUE_NAME_PATTERN)
    # Publish the parse job from the API right after an upload is committed.
    # Off by default: socket timeouts bound each broker operation's inactivity,
    # not its total wall-clock time, so a slowly answering broker could hold an
    # API worker thread. The dispatch sweep (every DISPATCH_INTERVAL_SECONDS, on
    # the maintenance worker, under a Celery hard time limit) publishes new jobs.
    dispatch_on_upload: bool = False
    # Broker I/O bounds for every publish: at most broker_publish_attempts
    # attempts; each broker read/write is bounded by the socket timeout.
    broker_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    broker_socket_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    broker_publish_attempts: int = Field(default=2, ge=1, le=5)
    # Redis visibility timeout for unacknowledged messages (task_acks_late).
    broker_visibility_timeout_seconds: int = Field(default=3600, ge=60)

    # Extraction subprocess limits, per attempt.
    parse_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    parse_memory_limit_mb: int = Field(default=512, ge=128, le=16384)
    parse_max_pages: int = Field(default=1000, ge=1, le=100_000)
    parse_max_text_chars: int = Field(default=5_000_000, ge=1000, le=50_000_000)
    # Celery limits of the parse task; the job lease is derived from them.
    parse_task_soft_time_limit_seconds: int = Field(default=150, ge=2)
    parse_task_time_limit_seconds: int = Field(default=180, ge=3)
    job_lease_margin_seconds: int = Field(default=120, ge=1)
    # Backoff after a transient failure: base * 2**(attempt - 1), +-20 %, capped.
    retry_backoff_base_seconds: int = Field(default=30, ge=0)
    retry_backoff_max_seconds: int = Field(default=600, ge=0)

    # Dispatch sweep (the durable outbox is the ingestion_jobs table). With
    # dispatch_on_upload off, this interval is the main pick-up latency of uploads.
    dispatch_interval_seconds: float = Field(default=10, gt=0)
    dispatch_batch_size: int = Field(default=100, ge=1, le=10_000)
    # A published job still queued after this long is published again (lost message).
    redispatch_after_seconds: int = Field(default=300, ge=1)
    # After a failed publish the job is not published again before this delay.
    publish_retry_delay_seconds: int = Field(default=30, ge=0)
    # Advisory: the sweep publishes nothing while the parse queue holds this many messages.
    dispatch_max_queue_depth: int = Field(default=100, ge=1)
    # Recovery of running jobs whose lease expired.
    recovery_interval_seconds: float = Field(default=60, gt=0)
    recovery_batch_size: int = Field(default=100, ge=1, le=10_000)
    # File reconciliation.
    reconcile_interval_seconds: float = Field(default=3600, gt=0)
    reconcile_temp_grace_seconds: int = Field(default=3600, ge=0)
    reconcile_orphan_grace_seconds: int = Field(default=86400, ge=0)
    reconcile_batch_size: int = Field(default=1000, ge=1)
    maintenance_task_soft_time_limit_seconds: int = Field(default=100, ge=1)
    maintenance_task_time_limit_seconds: int = Field(default=120, ge=2)

    # --- AI indexing (M4, see docs/ai-indexing-integration.md) -------------------------
    # Off by default. While off, index jobs stay queued: they are not published,
    # not claimed and consume no attempt; documents stay `processing`.
    indexing_enabled: bool = False
    index_queue: str = Field(default="ingestion.index", pattern=QUEUE_NAME_PATTERN)
    # Bearer credential the AI service expects (proposal, docs/ai-indexing-integration.md).
    # Required by the index worker while indexing is enabled; never logged or shown.
    ai_service_token: SecretStr | None = None
    # HTTP bounds of one index request. Connect and read are *inactivity* timeouts
    # (per socket operation, restarted by every byte that arrives). The request
    # deadline is only checked between body reads, after the headers arrived; it
    # does not bound the connect, send or header phases. The only absolute bound
    # is the index task's Celery limits (soft raises, hard kills the pool process).
    ai_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    ai_read_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    ai_request_deadline_seconds: float = Field(default=150.0, gt=0, le=3600)
    # Largest request body sent (canonical JSON, bytes) and response body accepted.
    index_max_payload_bytes: int = Field(default=16 * MIB, ge=1024, le=256 * MIB)
    index_max_response_bytes: int = Field(default=64 * 1024, ge=1024, le=MIB)
    # Readiness probe (GET capabilities, made inside the index task, never at worker
    # start-up): short inactivity timeouts, not a hard deadline; a result is reused
    # for this long per worker process, so an unavailable service is not asked again
    # for every job.
    ai_probe_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    ai_probe_cache_seconds: float = Field(default=30.0, ge=0, le=3600)
    index_task_soft_time_limit_seconds: int = Field(default=240, ge=2)
    index_task_time_limit_seconds: int = Field(default=270, ge=3)

    # --- courses and enrollment (M5, see docs/courses-api.md) ----------------------------
    # Join attempts per user per window. Enforced in this process only: with more
    # than one API process or replica the effective limit is multiplied, so a
    # shared (e.g. Redis) limiter is required before a multi-replica deployment.
    join_rate_limit_attempts: int = Field(default=10, ge=1, le=1000)
    join_rate_limit_window_seconds: float = Field(default=300.0, gt=0, le=86400)
    # Most users tracked at once; the least recently seen is forgotten beyond it.
    join_rate_limit_max_users: int = Field(default=10_000, ge=100, le=1_000_000)

    # --- course chat (M5, see docs/ai-chat-integration.md) ------------------------------
    # Off by default. While off, POST /courses/{id}/chat answers 503 after the
    # caller's course access was checked, and nothing is sent to the AI service.
    chat_enabled: bool = False
    # Answer scopes callers may request, comma-separated. Only class_only by
    # default; class_plus_web is refused here until live web search has its own
    # milestone. A scope that is not enabled is refused, never replaced.
    chat_enabled_scopes: str = "class_only"
    # Request body: bytes actually received (not only Content-Length) and the time to receive it.
    chat_max_body_bytes: int = Field(default=16 * 1024, ge=256, le=MIB)
    chat_body_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    chat_max_question_chars: int = Field(default=2000, ge=1, le=20_000)
    # Concurrent chat requests in this process, in total and per user. A request
    # that finds no free slot is refused at once (429), it does not wait.
    chat_max_concurrent: int = Field(default=8, ge=1, le=1000)
    chat_max_concurrent_per_user: int = Field(default=1, ge=1, le=100)
    # Upstream HTTP bounds. Connect/read/write are inactivity timeouts; the
    # deadlines are absolute wall-clock bounds over the whole call.
    chat_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    chat_read_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    chat_probe_deadline_seconds: float = Field(default=5.0, gt=0, le=60)
    chat_answer_deadline_seconds: float = Field(default=60.0, gt=0, le=600)
    # Absolute bound on handing the opening, and later the verified frames, to
    # the ASGI server (a connected client that stops reading cannot keep a slot).
    chat_delivery_deadline_seconds: float = Field(default=15.0, gt=0, le=120)
    chat_max_response_bytes: int = Field(default=256 * 1024, ge=1024, le=8 * MIB)
    # Capability probe results are reused for this long (at most 5 minutes).
    chat_capabilities_cache_seconds: float = Field(default=30.0, ge=0, le=300)
    chat_max_answer_chars: int = Field(default=8000, ge=100, le=100_000)
    chat_max_citations: int = Field(default=16, ge=1, le=64)
    chat_max_topics: int = Field(default=16, ge=0, le=256)

    @model_validator(mode="after")
    def _ordered_limits(self) -> "Settings":
        """parse timeout < soft limit < hard limit (< lease), visibility timeout above both."""
        if not self.parse_timeout_seconds < self.parse_task_soft_time_limit_seconds < self.parse_task_time_limit_seconds:
            raise ValueError(
                "PARSE_TIMEOUT_SECONDS < PARSE_TASK_SOFT_TIME_LIMIT_SECONDS < PARSE_TASK_TIME_LIMIT_SECONDS is required"
            )
        if self.broker_visibility_timeout_seconds <= 2 * self.parse_task_time_limit_seconds:
            raise ValueError("BROKER_VISIBILITY_TIMEOUT_SECONDS must exceed twice PARSE_TASK_TIME_LIMIT_SECONDS")
        if not self.maintenance_task_soft_time_limit_seconds < self.maintenance_task_time_limit_seconds:
            raise ValueError("MAINTENANCE_TASK_SOFT_TIME_LIMIT_SECONDS must be below the hard limit")
        if self.retry_backoff_max_seconds < self.retry_backoff_base_seconds:
            raise ValueError("RETRY_BACKOFF_MAX_SECONDS must not be below RETRY_BACKOFF_BASE_SECONDS")
        if self.parse_queue == self.maintenance_queue:
            raise ValueError("PARSE_QUEUE and MAINTENANCE_QUEUE must differ")
        if self.index_queue in (self.parse_queue, self.maintenance_queue):
            raise ValueError("INDEX_QUEUE must differ from PARSE_QUEUE and MAINTENANCE_QUEUE")
        # Sanity relation for well-behaved peers (probe + one request); a slow peer is
        # bounded only by the Celery limits, see AiIndexClient's module docstring.
        if not (
            self.ai_probe_timeout_seconds + self.ai_request_deadline_seconds + self.ai_read_timeout_seconds
            < self.index_task_soft_time_limit_seconds
            < self.index_task_time_limit_seconds
        ):
            raise ValueError(
                "AI_PROBE_TIMEOUT_SECONDS + AI_REQUEST_DEADLINE_SECONDS + AI_READ_TIMEOUT_SECONDS "
                "< INDEX_TASK_SOFT_TIME_LIMIT_SECONDS < INDEX_TASK_TIME_LIMIT_SECONDS is required"
            )
        if self.broker_visibility_timeout_seconds <= 2 * self.index_task_time_limit_seconds:
            raise ValueError("BROKER_VISIBILITY_TIMEOUT_SECONDS must exceed twice INDEX_TASK_TIME_LIMIT_SECONDS")
        from .chat.scopes import parse_enabled_scopes  # local: chat imports Settings

        parse_enabled_scopes(self.chat_enabled_scopes)  # raises ValueError for an unknown or unavailable scope
        if self.chat_max_concurrent_per_user > self.chat_max_concurrent:
            raise ValueError("CHAT_MAX_CONCURRENT_PER_USER must not exceed CHAT_MAX_CONCURRENT")
        if self.chat_probe_deadline_seconds >= self.chat_answer_deadline_seconds:
            raise ValueError("CHAT_PROBE_DEADLINE_SECONDS must be below CHAT_ANSWER_DEADLINE_SECONDS")
        return self

    @property
    def index_lease_seconds(self) -> int:
        """Lease of a claimed index attempt: the index task's hard limit plus the same margin."""
        return self.index_task_time_limit_seconds + self.job_lease_margin_seconds

    @property
    def job_lease_seconds(self) -> int:
        """How long a claimed attempt may hold its job before recovery treats it as dead.

        Longer than the Celery hard time limit, by which a live worker has
        already killed the attempt; the margin covers persisting the result.
        """
        return self.parse_task_time_limit_seconds + self.job_lease_margin_seconds


settings = Settings()
