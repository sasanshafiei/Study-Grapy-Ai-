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
    # Browser origins allowed to call the API cross-origin (F12, app/cors.py): a JSON array of exact
    # origins such as ["https://app.example.edu"]. Empty (default): same-origin only, no CORS headers.
    # Plain http and loopback origins are refused unless ENVIRONMENT is development or test.
    cors_allowed_origins: list[str] = []
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

    # --- external harvesting (M6, see docs/external-harvesting.md) -----------------------
    # Off by default. While off nothing is scheduled, dispatched or claimed and no
    # provider request is made; lease recovery and history cleanup still run.
    # Course opt-in and provider activation are separate, explicit operator steps.
    harvest_enabled: bool = False
    harvest_queue: str = Field(default="harvest.run", pattern=QUEUE_NAME_PATTERN)
    # Wikimedia User-Agent policy: "<client>/<version> (<contact URL or e-mail>) ...".
    # No default: required (and validated) by the harvest worker while harvesting is on.
    harvest_user_agent: str | None = None
    # HTTP bounds of one provider request (inactivity timeouts plus a deadline checked
    # between body reads; the absolute bound is the harvest task's Celery limits).
    harvest_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    harvest_read_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    harvest_request_deadline_seconds: float = Field(default=30.0, gt=0, le=300)
    harvest_max_response_bytes: int = Field(default=4 * MIB, ge=64 * 1024, le=32 * MIB)
    harvest_maxlag_seconds: int = Field(default=5, ge=1, le=60)
    # Retry-After values are honoured up to this; a longer one blocks the source this long.
    harvest_retry_after_max_seconds: int = Field(default=86400, ge=60, le=7 * 86400)
    harvest_throttle_default_seconds: int = Field(default=300, ge=1, le=86400)
    # Celery limits of the harvest task; the run lease is derived from them.
    harvest_task_soft_time_limit_seconds: int = Field(default=240, ge=30)
    harvest_task_time_limit_seconds: int = Field(default=270, ge=31)
    # Finite work: per claim (slice), per run, per course.
    harvest_slice_max_requests: int = Field(default=20, ge=2, le=500)  # >= one info + one content request
    harvest_run_max_requests: int = Field(default=300, ge=1, le=10_000)
    harvest_run_max_claims: int = Field(default=40, ge=1, le=1000)
    harvest_run_max_failures: int = Field(default=3, ge=1, le=20)
    harvest_max_queries_per_course: int = Field(default=20, ge=1, le=200)
    harvest_candidates_per_query: int = Field(default=5, ge=1, le=20)
    harvest_max_candidates_per_run: int = Field(default=60, ge=1, le=1000)
    harvest_max_accepted_per_run: int = Field(default=20, ge=1, le=500)
    harvest_refresh_batch_size: int = Field(default=20, ge=1, le=50)
    harvest_default_max_active_documents: int = Field(default=20, ge=1, le=500)
    # Quality gates on normalized article text.
    harvest_min_document_chars: int = Field(default=1500, ge=100, le=100_000)
    harvest_max_document_chars: int = Field(default=300_000, ge=1000, le=5_000_000)
    harvest_max_sections: int = Field(default=200, ge=1, le=5000)
    # A page reported missing is marked stale only after a second observation this much later.
    harvest_missing_confirm_seconds: int = Field(default=86400, ge=60, le=30 * 86400)
    # Run backoff after a failed attempt, and target backoff after a failed run.
    harvest_retry_backoff_seconds: int = Field(default=120, ge=1, le=86400)
    harvest_failed_run_backoff_seconds: int = Field(default=3600, ge=60, le=7 * 86400)
    # Defaults written by `init-source` into the provider budget (operators may lower them).
    harvest_budget_window_seconds: int = Field(default=60, ge=1, le=86400)
    harvest_budget_max_requests: int = Field(default=30, ge=1, le=100_000)
    harvest_budget_min_interval_ms: int = Field(default=1000, ge=0, le=600_000)
    # A budget wait up to this long is slept inside the slice; a longer one yields the run.
    harvest_budget_max_wait_seconds: float = Field(default=3.0, ge=0, le=30)
    harvest_maintenance_interval_seconds: float = Field(default=60, gt=0)
    harvest_dispatch_batch_size: int = Field(default=10, ge=1, le=1000)
    harvest_redispatch_after_seconds: int = Field(default=600, ge=1)
    harvest_history_retention_days: int = Field(default=30, ge=1, le=3650)
    harvest_history_cleanup_batch: int = Field(default=500, ge=1, le=10_000)
    # External index jobs the AI service cannot take yet (no advertised section
    # locator support, source disabled, course opted out) are deferred this long,
    # without being claimed and without consuming an attempt.
    external_index_defer_seconds: int = Field(default=900, ge=10, le=86400)

    # --- quizzes and graded practice (M7, see docs/quiz-api.md) ----------------------------
    # Master switch, off by default: while off every quiz endpoint answers 503 after the
    # caller's course access was checked. Generation and attempts have their own switches
    # (effective only while QUIZ_ENABLED is on): turning generation off, or losing the AI
    # service, never stops access to quizzes already issued or their local grading.
    quiz_enabled: bool = False
    quiz_generation_enabled: bool = True
    quiz_attempts_enabled: bool = True
    # Only class_only exists in M7; any other scope is refused at start-up.
    quiz_enabled_scopes: str = "class_only"
    quiz_max_body_bytes: int = Field(default=4096, ge=256, le=64 * 1024)
    quiz_body_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    quiz_max_questions: int = Field(default=10, ge=1, le=10)
    quiz_max_topics: int = Field(default=5, ge=1, le=5)
    # Concurrent generations in this process (per user: one, also enforced by the database).
    quiz_max_concurrent: int = Field(default=4, ge=1, le=1000)
    # Generations a student may start per UTC day (database counter, reserved before any upstream call).
    quiz_daily_generation_quota: int = Field(default=20, ge=1, le=10_000)
    quiz_quota_retention_days: int = Field(default=14, ge=2, le=3650)
    # Upstream HTTP bounds (inactivity timeouts) and absolute deadlines. The pipeline deadline
    # covers the probe, the generation request, validation and the persistence checkpoint.
    quiz_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    quiz_read_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    quiz_probe_deadline_seconds: float = Field(default=5.0, gt=0, le=60)
    quiz_generate_deadline_seconds: float = Field(default=45.0, gt=0, le=600)
    quiz_pipeline_deadline_seconds: float = Field(default=60.0, gt=0, le=900)
    # The generation lease (database time) is the pipeline deadline plus this margin.
    quiz_lease_margin_seconds: int = Field(default=30, ge=5, le=600)
    quiz_max_response_bytes: int = Field(default=256 * 1024, ge=4096, le=4 * MIB)
    quiz_capabilities_cache_seconds: float = Field(default=30.0, ge=0, le=300)

    # --- dashboard, analytics, study events, mastery delivery (M8, docs/dashboard-api.md) ------
    # Deterministic, read-only student dashboard (no AI involved): on by default.
    dashboard_enabled: bool = True
    # Professor analytics: OFF until the privacy review approves it (docs/analytics-privacy.md).
    # While off the endpoint answers 503 after authorization and no release is generated.
    analytics_enabled: bool = False
    # Minimum distinct contributing students per released cell. Never below 5.
    analytics_min_group_size: int = Field(default=5, ge=5, le=100)
    # A completed UTC ISO week is released no earlier than this long after it ended (late commits).
    analytics_publication_delay_seconds: int = Field(default=6 * 3600, ge=3600, le=7 * 86400)
    # Completed weeks shown (and released) per course, and releases generated per maintenance tick.
    analytics_weeks: int = Field(default=12, ge=1, le=26)
    analytics_release_batch_size: int = Field(default=50, ge=1, le=1000)
    analytics_maintenance_interval_seconds: float = Field(default=900, gt=0)
    # Client-reported study telemetry: off until approved (docs/study-events.md).
    study_events_enabled: bool = False
    study_events_max_body_bytes: int = Field(default=16 * 1024, ge=1024, le=256 * 1024)
    study_events_body_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    study_events_max_batch: int = Field(default=25, ge=1, le=100)
    # Database-backed, per student and UTC day, across every API process.
    study_events_daily_quota: int = Field(default=2000, ge=1, le=100_000)
    study_events_max_interval_seconds: int = Field(default=1800, ge=60, le=3600)
    study_events_future_skew_seconds: int = Field(default=120, ge=0, le=600)
    # Events older than this are deleted by maintenance. At least 2 days: longer than the
    # 24-hour acceptance window, so an acceptable replay always finds its original.
    study_events_retention_days: int = Field(default=180, ge=2, le=3650)
    study_events_cleanup_batch: int = Field(default=1000, ge=1, le=100_000)
    # Proposed AI recommendation ranking (docs/ai-recommendations-contract.md): not implemented in M8.
    ai_recommendations_enabled: bool = False
    # Delivery of graded attempts to the AI-2 mastery model. Outbox rows are written whatever
    # this says; while off nothing is dispatched, claimed or sent (lease recovery still runs).
    mastery_delivery_enabled: bool = False
    mastery_queue: str = Field(default="mastery.deliver", pattern=QUEUE_NAME_PATTERN)
    # The approved model, pinned. A capability probe or response that differs blocks delivery.
    mastery_model_name: str | None = None
    mastery_model_version: str | None = None
    mastery_state_epoch: str | None = None  # a UUID agreed with AI-2
    # Pseudonyms: HMAC-SHA256(key, namespace | user | course). The namespace separates deployments;
    # the key is required while delivery is enabled and is never logged.
    mastery_pseudonym_namespace: str | None = None
    mastery_pseudonym_key: SecretStr | None = None
    mastery_max_batch_events: int = Field(default=100, ge=1, le=500)
    mastery_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    mastery_read_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    mastery_probe_deadline_seconds: float = Field(default=5.0, gt=0, le=60)
    mastery_update_deadline_seconds: float = Field(default=20.0, gt=0, le=300)
    mastery_max_request_bytes: int = Field(default=256 * 1024, ge=4096, le=4 * MIB)
    mastery_max_response_bytes: int = Field(default=64 * 1024, ge=1024, le=MIB)
    mastery_capabilities_cache_seconds: float = Field(default=30.0, ge=0, le=300)
    mastery_task_soft_time_limit_seconds: int = Field(default=60, ge=10)
    mastery_task_time_limit_seconds: int = Field(default=75, ge=11)
    # Delivery failures of one batch before the stream is blocked (terminal until an operator retries).
    mastery_max_failures: int = Field(default=10, ge=1, le=100)
    # Claims of one batch (failures, pauses, unconfirmed-model releases) before it is blocked.
    mastery_max_batch_claims: int = Field(default=50, ge=1, le=1000)
    mastery_backoff_base_seconds: int = Field(default=30, ge=1, le=3600)
    mastery_backoff_max_seconds: int = Field(default=3600, ge=1, le=86400)
    mastery_dispatch_batch_size: int = Field(default=50, ge=1, le=1000)
    mastery_redispatch_after_seconds: int = Field(default=300, ge=1)
    mastery_maintenance_interval_seconds: float = Field(default=30, gt=0)
    mastery_dispatch_max_queue_depth: int = Field(default=200, ge=1)
    # Predictions older than this are not shown, and a review time is shown only for a fresh,
    # current prediction that covers every committed attempt of the topic.
    mastery_prediction_max_age_days: int = Field(default=30, ge=1, le=365)

    # --- professor exams (M9-A1, docs/exam-api.md, docs/exam-export.md) ------------------------------
    # Every switch is OFF by default and fails closed: while off, every exam endpoint answers 503 after
    # authorization (EXAM_EXPORT_ENABLED: only the file endpoints). EXAM_GENERATION_ENABLED belongs to
    # M9-A2 and is refused below until an approved AI-1 integration exists.
    exams_enabled: bool = False
    exam_export_enabled: bool = False
    exam_generation_enabled: bool = False
    exam_max_body_bytes: int = Field(default=64 * 1024, ge=4096, le=MIB)
    exam_body_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    exam_max_items: int = Field(default=200, ge=1, le=200)
    exam_max_bank_questions: int = Field(default=5000, ge=1, le=100_000)
    exam_max_exams: int = Field(default=500, ge=1, le=10_000)
    # Total printable code points per exam over every text the answer-key variant prints (architecture
    # §9.10, calibrated by the EXP-16 maximum-boundary renders). The database CHECK caps it at 300,000.
    exam_export_max_content_chars: int = Field(default=300_000, ge=1000, le=300_000)
    # Renderer subprocess (resource limits, not an OS sandbox) and download limits.
    exam_export_max_concurrent: int = Field(default=2, ge=1, le=16)
    exam_export_rate_limit: int = Field(default=30, ge=1, le=1000)
    exam_export_rate_window_seconds: int = Field(default=600, ge=10, le=86400)
    exam_export_rate_max_users: int = Field(default=10_000, ge=10, le=1_000_000)
    exam_export_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    exam_export_cpu_seconds: int = Field(default=25, ge=1, le=120)
    exam_export_memory_mb: int = Field(default=512, ge=256, le=4096)
    exam_export_max_input_bytes: int = Field(default=2 * MIB, ge=64 * 1024, le=16 * MIB)
    exam_export_max_pdf_bytes: int = Field(default=16 * MIB, ge=MIB, le=64 * MIB)
    exam_export_max_docx_bytes: int = Field(default=8 * MIB, ge=MIB, le=64 * MIB)

    @model_validator(mode="after")
    def _ordered_limits(self) -> "Settings":
        """parse timeout < soft limit < hard limit (< lease), visibility timeout above both."""
        from .cors import validate_allowed_origins  # local: dependency-free

        validate_allowed_origins(self.cors_allowed_origins, self.environment)  # raises ValueError
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
        if self.harvest_queue in (self.parse_queue, self.maintenance_queue, self.index_queue):
            raise ValueError("HARVEST_QUEUE must differ from PARSE_QUEUE, MAINTENANCE_QUEUE and INDEX_QUEUE")
        if not (
            self.harvest_request_deadline_seconds + self.harvest_read_timeout_seconds
            < self.harvest_task_soft_time_limit_seconds
            < self.harvest_task_time_limit_seconds
        ):
            raise ValueError(
                "HARVEST_REQUEST_DEADLINE_SECONDS + HARVEST_READ_TIMEOUT_SECONDS "
                "< HARVEST_TASK_SOFT_TIME_LIMIT_SECONDS < HARVEST_TASK_TIME_LIMIT_SECONDS is required"
            )
        if self.broker_visibility_timeout_seconds <= 2 * self.harvest_task_time_limit_seconds:
            raise ValueError("BROKER_VISIBILITY_TIMEOUT_SECONDS must exceed twice HARVEST_TASK_TIME_LIMIT_SECONDS")
        if self.harvest_min_document_chars >= self.harvest_max_document_chars:
            raise ValueError("HARVEST_MIN_DOCUMENT_CHARS must be below HARVEST_MAX_DOCUMENT_CHARS")
        if self.harvest_user_agent is not None:
            from .harvest.transport import validate_user_agent  # local: harvest imports Settings

            validate_user_agent(self.harvest_user_agent)  # raises ValueError
        from .chat.scopes import parse_enabled_scopes  # local: chat imports Settings

        parse_enabled_scopes(self.chat_enabled_scopes)  # raises ValueError for an unknown or unavailable scope
        if self.chat_max_concurrent_per_user > self.chat_max_concurrent:
            raise ValueError("CHAT_MAX_CONCURRENT_PER_USER must not exceed CHAT_MAX_CONCURRENT")
        if self.chat_probe_deadline_seconds >= self.chat_answer_deadline_seconds:
            raise ValueError("CHAT_PROBE_DEADLINE_SECONDS must be below CHAT_ANSWER_DEADLINE_SECONDS")
        from .quiz.scopes import parse_quiz_scopes  # local, dependency-free

        parse_quiz_scopes(self.quiz_enabled_scopes)  # raises ValueError: only class_only exists in M7
        if not (
            self.quiz_probe_deadline_seconds + self.quiz_generate_deadline_seconds
            < self.quiz_pipeline_deadline_seconds
        ):
            raise ValueError(
                "QUIZ_PROBE_DEADLINE_SECONDS + QUIZ_GENERATE_DEADLINE_SECONDS < QUIZ_PIPELINE_DEADLINE_SECONDS is required"
            )
        if self.mastery_queue in (self.parse_queue, self.maintenance_queue, self.index_queue, self.harvest_queue):
            raise ValueError("MASTERY_QUEUE must differ from every other queue")
        if not (
            self.mastery_probe_deadline_seconds + self.mastery_update_deadline_seconds
            < self.mastery_task_soft_time_limit_seconds
            < self.mastery_task_time_limit_seconds
        ):
            raise ValueError(
                "MASTERY_PROBE_DEADLINE_SECONDS + MASTERY_UPDATE_DEADLINE_SECONDS "
                "< MASTERY_TASK_SOFT_TIME_LIMIT_SECONDS < MASTERY_TASK_TIME_LIMIT_SECONDS is required"
            )
        if self.broker_visibility_timeout_seconds <= 2 * self.mastery_task_time_limit_seconds:
            raise ValueError("BROKER_VISIBILITY_TIMEOUT_SECONDS must exceed twice MASTERY_TASK_TIME_LIMIT_SECONDS")
        if self.mastery_backoff_max_seconds < self.mastery_backoff_base_seconds:
            raise ValueError("MASTERY_BACKOFF_MAX_SECONDS must not be below MASTERY_BACKOFF_BASE_SECONDS")
        if self.ai_recommendations_enabled:
            raise ValueError("AI_RECOMMENDATIONS_ENABLED cannot be true: no AI recommendation integration exists in M8")
        if self.exam_generation_enabled:
            raise ValueError(
                "EXAM_GENERATION_ENABLED cannot be true: AI exam generation (M9-A2) needs an approved AI-1 protocol first"
            )
        if self.exam_export_enabled and not self.exams_enabled:
            raise ValueError("EXAM_EXPORT_ENABLED requires EXAMS_ENABLED")
        if not self.exam_export_cpu_seconds < self.exam_export_timeout_seconds:
            raise ValueError("EXAM_EXPORT_CPU_SECONDS must be below EXAM_EXPORT_TIMEOUT_SECONDS")
        from .mastery.pins import validate_pins  # local: dependency-free

        validate_pins(self)  # raises ValueError for malformed pins (and missing ones while delivery is enabled)
        return self

    @property
    def mastery_lease_seconds(self) -> int:
        """Lease of a claimed mastery batch: the task's hard limit plus the shared margin (database time)."""
        return self.mastery_task_time_limit_seconds + self.job_lease_margin_seconds

    @property
    def quiz_lease_seconds(self) -> int:
        """Lease of a generating quiz session: the pipeline deadline plus a margin (database time)."""
        return int(self.quiz_pipeline_deadline_seconds + 0.999) + self.quiz_lease_margin_seconds

    @property
    def index_lease_seconds(self) -> int:
        """Lease of a claimed index attempt: the index task's hard limit plus the same margin."""
        return self.index_task_time_limit_seconds + self.job_lease_margin_seconds

    @property
    def harvest_lease_seconds(self) -> int:
        """Lease of a claimed harvest slice: the harvest task's hard limit plus the same margin."""
        return self.harvest_task_time_limit_seconds + self.job_lease_margin_seconds

    @property
    def harvest_slice_seconds(self) -> float:
        """No new provider request is started after this much of a slice (soft limit minus one request)."""
        return max(
            1.0,
            self.harvest_task_soft_time_limit_seconds
            - self.harvest_request_deadline_seconds
            - self.harvest_read_timeout_seconds,
        )

    @property
    def job_lease_seconds(self) -> int:
        """How long a claimed attempt may hold its job before recovery treats it as dead.

        Longer than the Celery hard time limit, by which a live worker has
        already killed the attempt; the margin covers persisting the result.
        """
        return self.parse_task_time_limit_seconds + self.job_lease_margin_seconds


settings = Settings()
