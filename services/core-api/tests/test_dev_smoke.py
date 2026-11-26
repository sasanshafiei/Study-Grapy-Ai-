"""WP-13: the read-only checks behind infra/dev/smoke.sh (scripts/dev_smoke.py).

The verdicts are tested on plain rows; the SQL runs on the disposable server at head, as the runtime
login, against the real groups and grants (db_roles.sql, db_privileges.sql), inside READ ONLY
transactions.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.storage_identity import StorageIdentityError
from scripts import dev_smoke
from tests.api_support import bind_storage
from tests.disposable_db import verify_connection
from tests.test_db_privileges import roles  # noqa: F401 - fixture: the groups, one login each, grants at head


def by_check(results):
    return {result[1]: result for result in results}


def read_only_results(engine, expected_user):
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        results = dev_smoke.database_results(conn, expected_user)
        conn.rollback()
    return by_check(results)


# --- verdicts on plain rows ---------------------------------------------------------------------


def test_identity_and_schema_verdicts():
    assert dev_smoke.identity_result("studygraph_app", "studygraph_app", [])[0] == "PASS"
    assert dev_smoke.identity_result("studygraph", "studygraph_app", [])[0] == "FAIL"
    assert dev_smoke.identity_result("studygraph_app", "studygraph_app", ["superuser"])[0] == "FAIL"
    assert dev_smoke.schema_result(["0013"], "0013")[0] == "PASS"
    behind = dev_smoke.schema_result(["0009"], "0013")
    assert behind[0] == "FAIL" and "0009" in behind[2] and "migrate" in behind[2]
    assert dev_smoke.schema_result([], "0013")[0] == "FAIL"


GOOD_GROUPS = [
    ("studygraph_ai_indexer", "studygraph_ai_indexer", "commit", 0, "x, y", 2),
    ("studygraph_ai_retriever", "studygraph_ai_retriever", "retrieve", 0, "x, y", 2),
    ("studygraph_ce_runtime", "studygraph_ce_runtime", "verify", 2, "", 2),
]


def test_grant_verdicts():
    assert dev_smoke.grant_results(GOOD_GROUPS)[0][0] == "PASS"
    assert "studygraph_ai_retriever" in dev_smoke.grant_results(GOOD_GROUPS[:1] + GOOD_GROUPS[2:])[0][2]
    unreadable = GOOD_GROUPS[:2] + [("studygraph_ce_runtime", "studygraph_ce_runtime", "verify", 2, "late", 3)]
    assert dev_smoke.grant_results(unreadable)[0][0] == "FAIL" and "late" in dev_smoke.grant_results(unreadable)[0][2]
    reads = [("studygraph_ai_indexer", "studygraph_ai_indexer", "commit", 1, "y", 2)] + GOOD_GROUPS[1:]
    assert dev_smoke.grant_results(reads)[0][0] == "FAIL"
    executes = [("studygraph_ai_indexer", "studygraph_ai_indexer", "commit,retrieve", 0, "x, y", 2)] + GOOD_GROUPS[1:]
    assert dev_smoke.grant_results(executes)[0][0] == "FAIL"


def test_ai_login_and_superuser_verdicts():
    assert dev_smoke.ai_login_results([], GOOD_GROUPS)[0][0] == "SKIP"
    login = ("ix", "studygraph_ai_indexer", "commit", 0, "x, y", 2)
    assert dev_smoke.ai_login_results([("ix", "studygraph_ai_indexer", False, "")], GOOD_GROUPS + [login])[0][0] == "PASS"
    for attributes, privilege in (
        (("ix", "studygraph_ai_indexer", True, ""), login),
        (("ix", "studygraph_ai_indexer", False, "studygraph_ce_runtime"), login),
        (("ix", "studygraph_ai_indexer", False, ""), ("ix", "studygraph_ai_indexer", "commit", 1, "y", 2)),
        (("ix", "studygraph_ai_indexer", False, ""), ("ix", "studygraph_ai_indexer", "", 0, "x, y", 2)),
    ):
        assert dev_smoke.ai_login_results([attributes], GOOD_GROUPS + [privilege])[0][0] == "FAIL"
    assert dev_smoke.superuser_result(0, "")[0] == "PASS"
    assert dev_smoke.superuser_result(2, "psql, unnamed")[0] == "FAIL"


def test_flags_note_lists_only_switches_in_effect():
    class FakeSettings:
        def __init__(self, **values):
            self.values = values

        def model_dump(self):
            return self.values

    defaults = FakeSettings(dashboard_enabled=True, quiz_enabled=False, quiz_generation_enabled=True,
                            quiz_attempts_enabled=True, exams_enabled=False, chat_scopes="class_only")
    assert dev_smoke.flags_result(defaults)[2] == "in effect in core-api: DASHBOARD_ENABLED"
    quiz_on = FakeSettings(quiz_enabled=True, quiz_generation_enabled=False, quiz_attempts_enabled=True)
    assert dev_smoke.flags_result(quiz_on)[2] == "in effect in core-api: QUIZ_ATTEMPTS_ENABLED, QUIZ_ENABLED"
    assert dev_smoke.flags_result(FakeSettings(dashboard_enabled=False))[2] == "in effect in core-api: none"


def test_unknown_command_prints_usage(capsys):
    assert dev_smoke.main(["nope"]) == 2
    assert "python -m scripts.dev_smoke api" in capsys.readouterr().err


# --- the SQL as the runtime login, on the disposable server at head -----------------------------


def test_provisioned_roles_pass(roles):  # noqa: F811 - the imported fixture
    results = read_only_results(roles.engine("app"), roles.names["app"])
    for check in ("identity", "schema", "grants", "ai-logins"):
        assert results[check][0] == "PASS", results[check]
    listed = results["ai-logins"][2]
    assert roles.names["indexer"] in listed and roles.names["retriever"] in listed
    assert roles.names["stranger"] not in listed
    # Another login is not the one the stack must use.
    assert read_only_results(roles.engine("indexer"), roles.names["app"])["identity"][0] == "FAIL"


def test_drift_and_superuser_sessions_are_reported(roles, disposable_engine, disposable_target):  # noqa: F811
    indexer = roles.names["indexer"]
    with disposable_engine.begin() as c:
        verify_connection(c, disposable_target)
        c.exec_driver_sql("CREATE TABLE public.wp13_smoke_probe (id integer)")  # a migration without db-init
        c.exec_driver_sql(f"GRANT studygraph_ce_runtime TO {indexer}")
    try:
        with disposable_engine.connect() as held:  # stays open while the runtime login looks
            verify_connection(held, disposable_target)
            owner_is_superuser = held.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar_one()
            results = read_only_results(roles.engine("app"), roles.names["app"])
            held.rollback()
        assert results["grants"][0] == "FAIL" and "wp13_smoke_probe" in results["grants"][2]
        assert results["ai-logins"][0] == "FAIL"
        assert f"{indexer} is also a member of studygraph_ce_runtime" in results["ai-logins"][2]
        if owner_is_superuser:
            assert results["superuser"][0] == "FAIL", results["superuser"]
    finally:
        with disposable_engine.begin() as c:
            verify_connection(c, disposable_target)
            c.exec_driver_sql("DROP TABLE IF EXISTS public.wp13_smoke_probe")
            c.exec_driver_sql(f"REVOKE studygraph_ce_runtime FROM {indexer}")


def test_upload_store_is_verified_read_only(roles, world, tmp_path):  # noqa: F811
    storage_id = bind_storage(world.engine, tmp_path / "store")
    (tmp_path / "empty").mkdir()
    with roles.engine("app").connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        assert dev_smoke.verify_upload_store(conn, tmp_path / "store") == storage_id
        with pytest.raises(StorageIdentityError):
            with conn.begin_nested():
                dev_smoke.verify_upload_store(conn, tmp_path / "empty")
        conn.rollback()
