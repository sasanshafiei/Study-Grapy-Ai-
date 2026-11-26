"""WP-12 (A03): admission to the exam file endpoints (E22, E23) happens before any snapshot or render work.

The rate limit and the render slot are checked after authentication, owner authorization, the feature gates
and the query, and before `export.prepare`; the slot is released exactly once on every path. The unit tests
drive the shared `_file` handler with fakes (no database); the last two run on real PostgreSQL.
"""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace

import anyio
import pytest
from starlette.requests import Request

from app.errors import ApiError
from app.exams import export, render
from app.limits import ConcurrencyLimiter, RateLimiter
from app.routers import exams as router
from tests import exam_support as ex
from tests.exam_support import ExamClient, mcq_body

QUERY = "variant=student&format=pdf"
COURSE = uuid.uuid4()


class Harness:
    """`_file` with the expensive steps replaced by counting fakes and in-memory app state."""

    def __init__(self, monkeypatch, max_concurrent: int = 2, rate_limit: int = 30) -> None:
        self.calls = {"prepare": 0, "render": 0, "record": 0}
        self.on_prepare = self.on_render = lambda: None
        self.professor = SimpleNamespace(id=uuid.uuid4())
        self.state = SimpleNamespace(
            settings=SimpleNamespace(exam_export_enabled=True), session_factory=None,
            exam_export_limiter=ConcurrencyLimiter(max_concurrent, 1),
            exam_export_rate_limiter=RateLimiter(rate_limit, 600, 1000),
        )  # fmt: skip

        async def authorize(request, user, course_id):
            return COURSE

        monkeypatch.setattr(router, "authorize", authorize)
        monkeypatch.setattr(export, "prepare", self._prepare)
        monkeypatch.setattr(export, "render", self._render)
        monkeypatch.setattr(export, "record", self._record)
        monkeypatch.setattr(export, "limits_for", lambda settings, fmt: None)
        monkeypatch.setattr(export, "file_response", lambda meta, rendered, variant, fmt: "file")

    def _prepare(self, *args, **kwargs):
        self.calls["prepare"] += 1
        self.on_prepare()
        return {"model": {}, "timestamp": "2026-11-26T10:00:00Z", "finalized": True, "version": 3}

    def _render(self, *args, **kwargs):
        self.calls["render"] += 1
        self.on_render()
        return SimpleNamespace(data=b"%PDF-1.7", sha256="0" * 64)

    def _record(self, *args):
        self.calls["record"] += 1

    def call(self, user=None, query: str = QUERY, exam_id: str | None = None, preview_file: bool = False):
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": query.encode(),
                           "app": SimpleNamespace(state=self.state)})  # fmt: skip
        return router._file(request, user or self.professor, str(COURSE), exam_id or str(uuid.uuid4()),
                            preview_file=preview_file)  # fmt: skip

    def run(self, **kwargs):
        async def go():
            return await self.call(**kwargs)

        return anyio.run(go)

    def refused(self, **kwargs) -> ApiError:
        with pytest.raises(ApiError) as info:
            self.run(**kwargs)
        return info.value

    def in_use(self) -> tuple[int, int]:
        return self.state.exam_export_limiter.in_use()


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


def _raiser(make):
    def hook():
        raise make()

    return hook


@pytest.mark.parametrize("preview_file", [False, True])
def test_refused_requests_never_prepare_render_or_audit(monkeypatch, preview_file):
    h = Harness(monkeypatch, rate_limit=1)
    held = [h.state.exam_export_limiter.try_acquire(uuid.uuid4()) for _ in range(2)]  # every slot taken by others
    busy = h.refused(preview_file=preview_file)
    assert (busy.status_code, busy.code, busy.headers["Retry-After"]) == (429, "export_busy", "2")
    limited = h.refused(preview_file=preview_file)  # the busy attempt used the only allowance
    assert (limited.status_code, limited.code) == (429, "too_many_exports") and int(limited.headers["Retry-After"]) >= 1
    assert h.calls == {"prepare": 0, "render": 0, "record": 0}
    assert h.in_use() == (2, 2)  # a refusal neither takes nor frees a slot
    for lease in held:
        lease.release()
    assert h.in_use() == (0, 0)


def test_authorization_gates_and_query_come_before_admission(h, monkeypatch):
    cases = [({"exam_id": "not-a-uuid"}, (404, "exam_not_found")),
             ({"query": "variant=teacher&format=pdf"}, (422, "invalid_export_option")),
             ({"query": QUERY + "&x=1"}, (422, "invalid_query"))]  # fmt: skip
    for kwargs, expected in cases:
        error = h.refused(**kwargs)
        assert (error.status_code, error.code) == expected
    h.state.settings.exam_export_enabled = False
    assert h.refused().code == "exam_export_unavailable"
    h.state.settings.exam_export_enabled = True

    async def not_owner(request, user, course_id):
        raise ApiError(404, "course_not_found", "Course not found.")

    monkeypatch.setattr(router, "authorize", not_owner)
    assert h.refused().code == "course_not_found"
    # nothing was counted against the professor, so a non-owner can never see a 429 for someone else's exam
    assert h.state.exam_export_rate_limiter.tracked_users() == 0 and h.in_use() == (0, 0)
    assert h.calls == {"prepare": 0, "render": 0, "record": 0}


@pytest.mark.parametrize("step, make, expected", [
    ("prepare", export.not_finalized, (409, "exam_not_finalized")),
    ("prepare", lambda: RuntimeError("snapshot failed"), RuntimeError),
    ("render", lambda: render.RenderFailed("timeout", "injected"), (504, "export_timeout")),
    ("render", lambda: render.RenderFailed("failed", "injected"), (500, "export_failed")),
    ("render", lambda: RuntimeError("renderer bug"), RuntimeError),
])  # fmt: skip
def test_every_failure_releases_the_slot_exactly_once(h, step, make, expected):
    setattr(h, f"on_{step}", _raiser(make))
    with pytest.raises(ApiError if isinstance(expected, tuple) else expected) as info:
        h.run()
    if isinstance(expected, tuple):
        assert (info.value.status_code, info.value.code) == expected
    assert h.in_use() == (0, 0)  # released, and not twice (that would go negative)
    assert h.calls == {"prepare": 1, "render": int(step == "render"), "record": 0}  # a failure writes no audit event
    setattr(h, f"on_{step}", lambda: None)
    assert h.run() == "file" and h.in_use() == (0, 0)  # the professor's one slot is usable again


@pytest.mark.parametrize("how", ["cancel_scope", "task_cancel"])
def test_cancellation_releases_the_slot_exactly_once(h, how):
    entered, gate = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert gate.wait(10)

    h.on_prepare = block
    held_while_running = []

    async def scenario():
        import asyncio

        scope = anyio.CancelScope()

        async def request():
            with scope:
                await h.call()

        task = asyncio.ensure_future(request())
        with anyio.fail_after(5):
            while not entered.is_set():
                await anyio.sleep(0.005)
        scope.cancel() if how == "cancel_scope" else task.cancel()
        await anyio.sleep(0.05)
        held_while_running.append(h.in_use())
        gate.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        with anyio.fail_after(5):  # an abandoned worker thread finishes on its own
            while h.calls["prepare"] and h.in_use() != (0, 0):
                await anyio.sleep(0.005)

    anyio.run(scenario)
    # anyio cancellation waits for the worker thread, so the slot stays taken meanwhile. A native
    # task.cancel() makes anyio abandon the thread: the slot is then free before the thread ends, but it is
    # still released exactly once.
    if how == "cancel_scope":
        assert held_while_running == [(1, 1)]
    assert h.in_use() == (0, 0)
    assert h.calls == {"prepare": 1, "render": 0, "record": 0}  # a cancelled request never renders or audits


def test_concurrent_requests_never_exceed_render_capacity(h):
    gate, lock = threading.Event(), threading.Lock()
    active, peak = [0], [0]

    def track():
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            assert gate.wait(10)
        finally:
            with lock:
                active[0] -= 1

    h.on_prepare = track
    results: list[str] = []

    async def one(user):
        try:
            results.append(await h.call(user=user))
        except ApiError as exc:
            results.append(exc.code)

    async def scenario():
        async with anyio.create_task_group() as tg:
            for _ in range(6):
                tg.start_soon(one, SimpleNamespace(id=uuid.uuid4()))
            with anyio.fail_after(5):
                while results.count("export_busy") < 4 or active[0] < 2:
                    await anyio.sleep(0.005)
            assert h.in_use() == (2, 2)
            gate.set()

    anyio.run(scenario)
    assert sorted(results) == ["export_busy"] * 4 + ["file"] * 2
    assert peak[0] == 2 and h.calls == {"prepare": 2, "render": 2, "record": 2}
    assert h.in_use() == (0, 0)


# --- real PostgreSQL ------------------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint, action", [("/export", "export_downloaded"), ("/preview-file", "preview_file_downloaded")])
def test_admission_order_on_the_real_stack(world, make_api, monkeypatch, endpoint, action):
    professor = world.user("professor")
    api = make_api(settings_overrides={**ex.EXPORT_ENABLED, "exam_export_rate_limit": 2}).as_user(professor)
    x = ExamClient(api, world.course(professor, code="CHEM 101"))
    final = x.finalized(x.add(x.exam("Guards"), x.question(mcq_body(), approve=True)))
    calls = {"prepare": 0, "render": 0}

    def spy(name, real):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(export, "prepare", spy("prepare", export.prepare))
    monkeypatch.setattr(export, "render", spy("render", export.render))
    limiter, path = api.app.state.exam_export_limiter, x.e_path(final["id"], endpoint)
    params = {"variant": "student", "format": "pdf"}

    held = [limiter.try_acquire(uuid.uuid4()) for _ in range(api.settings.exam_export_max_concurrent)]
    busy = api.client.get(path, params=params)
    for lease in held:
        lease.release()
    ok = api.client.get(path, params=params)
    limited = api.client.get(path, params=params)
    assert busy.status_code == 429 and busy.json()["code"] == "export_busy" and busy.headers["retry-after"] == "2"
    assert ok.status_code == 200 and ok.headers["content-type"] == "application/pdf" and ok.content.startswith(b"%PDF")
    assert ok.headers["x-exam-finalized-version"] == str(final["finalized_version"])
    assert limited.status_code == 429 and limited.json()["code"] == "too_many_exports"
    assert int(limited.headers["retry-after"]) >= 1 and limited.headers["cache-control"] == "no-store"
    assert calls == {"prepare": 1, "render": 1}  # only the admitted request did any work
    assert limiter.in_use() == (0, 0)
    assert world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action = :a",
                        e=uuid.UUID(final["id"]), a=action) == 1  # fmt: skip


def test_real_refusals_return_or_never_take_a_slot(world, make_api):
    professor, other = world.user("professor"), world.user("professor")
    api = make_api(settings_overrides=ex.EXPORT_ENABLED).as_user(professor)
    x = ExamClient(api, world.course(professor, code="CHEM 101"))
    draft = x.exam("Draft")
    params = {"variant": "student", "format": "pdf"}
    r = api.client.get(x.e_path(draft["id"], "/export"), params=params)
    assert r.status_code == 409 and r.json()["code"] == "exam_not_finalized"
    assert api.app.state.exam_export_limiter.in_use() == (0, 0)  # refused inside the snapshot: the slot is back
    outsider = make_api(settings_overrides=ex.EXPORT_ENABLED).as_user(other)
    for endpoint in ("/export", "/preview-file"):
        assert outsider.client.get(x.e_path(draft["id"], endpoint), params=params).status_code == 404
    assert outsider.app.state.exam_export_rate_limiter.tracked_users() == 0  # authorization first: nothing counted
    assert world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action LIKE '%downloaded'",
                        e=uuid.UUID(draft["id"])) == 0  # fmt: skip
