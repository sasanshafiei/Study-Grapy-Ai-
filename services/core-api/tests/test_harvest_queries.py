"""Approved public queries (M6): deterministic derivation, screens, exact-text approval binding."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app.harvest import queries
from app.harvest.queries import CourseContext, QueryRefused, derive_query, screen
from tests.harvest_support import opt_in, wikipedia_source

CONTEXT = CourseContext(uuid.UUID(int=7), "CHEM 101", "AB12CD34EF56", ("Ada Lovelace", "Bo"))


def test_derivation_is_deterministic_and_normalized():
    assert derive_query("  Chemical\tequilibrium \n") == "Chemical equilibrium"
    assert derive_query("Cálculo") == "Cálculo"  # NFC
    assert derive_query("x" * 300) == "x" * 120


@pytest.mark.parametrize(
    "text",
    ["ada.lovelace@uni.edu notes", "see https://evil.example", "student 20231234", "CHEM 101 equilibrium",
     "ab12cd34ef56", "Lovelace thesis", "one two three four five six seven eight nine", "x",
     "00000000-0000-0000-0000-000000000007"],
)  # fmt: skip
def test_screens_refuse_identifiers_and_personal_names(text):
    with pytest.raises(QueryRefused):
        screen(text, CONTEXT)


def test_screens_accept_plain_topics_but_are_not_the_guarantee():
    screen("Chemical equilibrium", CONTEXT)
    screen("Le Chatelier's principle", CONTEXT)
    screen("Bonding", CONTEXT)  # 'Bo' is too short to screen as a name part
    assert queries.__doc__ and "NOT the guarantee" in queries.__doc__


@pytest.fixture
def setup(world):
    owner = world.user("professor", name="Grace Hopper")
    course = world.course(owner, code="CHEM 101")
    source = wikipedia_source(world)
    opt_in(world, course, source)
    return world, course, sessionmaker(bind=world.engine)


def test_approval_binds_to_the_exact_derived_text(setup):
    world, course, factory = setup
    topic = world.topic(course, "Chemical  equilibrium")
    with factory() as session:
        with pytest.raises(QueryRefused, match="differs"):
            queries.approve(session, course, topic, "chemical equilibrium", 20)
        session.rollback()
    with factory() as session:
        query_id = queries.approve(session, course, topic, "Chemical equilibrium", 20)
        session.commit()
    with factory() as session:
        assert [q.text for q in queries.usable_queries(session, course, 20)] == ["Chemical equilibrium"]
    # The topic is renamed: the old approval no longer covers what would be sent.
    world.execute("UPDATE topics SET name = 'Chemical kinetics' WHERE id = :t", t=topic)
    with factory() as session:
        assert queries.usable_queries(session, course, 20) == []
        new_id = queries.approve(session, course, topic, "Chemical kinetics", 20)
        session.commit()
    assert new_id != query_id
    statuses = {r["id"]: r["status"] for r in world.rows("SELECT id, status FROM harvest_queries WHERE topic_id = :t", t=topic)}
    assert statuses == {query_id: "revoked", new_id: "approved"}


def test_approval_refuses_member_names_course_codes_and_unconfigured_courses(setup):
    world, course, factory = setup
    for name in ("Hopper compiler notes", "CHEM 101 review"):
        topic = world.topic(course, name)
        with factory() as session:
            with pytest.raises(QueryRefused):
                queries.approve(session, course, topic, name, 20)
            session.rollback()
    other = world.course(world.user("professor"))
    topic = world.topic(other, "Thermodynamics")
    with factory() as session:
        with pytest.raises(QueryRefused, match="course-opt-in"):
            queries.approve(session, other, topic, "Thermodynamics", 20)
        session.rollback()


def test_query_count_is_bounded_and_revocation_stops_use(setup):
    world, course, factory = setup
    first, second = world.topic(course, "Acids"), world.topic(course, "Bases")
    with factory() as session:
        query_id = queries.approve(session, course, first, "Acids", 1)
        session.commit()
    with factory() as session:
        with pytest.raises(QueryRefused, match="limit 1"):
            queries.approve(session, course, second, "Bases", 1)
        session.rollback()
    with factory() as session:
        assert queries.revoke(session, query_id) is True
        session.commit()
    with factory() as session:
        assert queries.usable_queries(session, course, 20) == []
        proposals = queries.propose(session, course)
    assert {p.query_text: p.refused for p in proposals} == {"Acids": None, "Bases": None}
