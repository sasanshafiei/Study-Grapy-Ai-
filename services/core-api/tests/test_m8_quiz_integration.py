"""The M7 grading path with revision 0007: one mastery event per graded attempt, in the same commit;
idempotent replays and conflicting answers create none, and the M7 responses are unchanged."""

from __future__ import annotations

from tests.test_quiz_api import answer, fake, key_of, quiz, quiz_api, setup, wrong_of  # noqa: F401 (fixtures)



def test_graded_attempts_enqueue_exactly_one_event_and_replays_none(world, quiz_api, setup, quiz):  # noqa: F811
    api = quiz_api()
    question = quiz["questions"][0]
    choice = wrong_of(world, question)
    first = answer(api, setup.course, quiz["quiz_id"], question["id"], choice)
    assert first.status_code == 201
    events = world.rows("SELECT event_id, topic_seq, origin, status FROM mastery_outbox WHERE user_id = :u",
                        u=setup.student.id)  # fmt: skip
    assert [(str(e["event_id"]), e["topic_seq"], e["origin"], e["status"]) for e in events] == [
        (first.json()["attempt_id"], 1, "trigger", "pending")
    ]
    again = answer(api, setup.course, quiz["quiz_id"], question["id"], choice)
    conflict = answer(api, setup.course, quiz["quiz_id"], question["id"], key_of(world, question["id"]))
    assert (again.status_code, conflict.status_code) == (200, 409)
    assert set(again.json()) == set(first.json())  # the M7 response schema is unchanged
    assert {k: v for k, v in first.json().items() if k != "replayed"} == {
        k: v for k, v in again.json().items() if k != "replayed"
    }
    assert world.scalar("SELECT count(*) FROM mastery_outbox WHERE user_id = :u", u=setup.student.id) == 1
    second = quiz["questions"][1]
    assert answer(api, setup.course, quiz["quiz_id"], second["id"], key_of(world, second["id"])).status_code == 201
    seqs = world.rows("SELECT o.topic_seq, a.stats_attempts FROM mastery_outbox o JOIN attempts a ON a.id = o.event_id "
                      "WHERE o.user_id = :u ORDER BY o.created_at", u=setup.student.id)  # fmt: skip
    assert all(r["topic_seq"] == r["stats_attempts"] for r in seqs) and len(seqs) == 2
    assert first.json()["mastery"]["method"] == "cumulative_accuracy/1"  # M7 never shows model output
