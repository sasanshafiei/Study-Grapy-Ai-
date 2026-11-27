import uuid

import simulate


def test_core_rule_based_module_loads_unchanged():
    rules, Material = simulate.load_rule_based()
    assert rules.BASIS == "rule_based/1"
    topic = rules.TopicEvidence(uuid.uuid4(), "Kinetics", 3, 3, 0, (uuid.uuid4(),) * 3, None)
    out = rules.build([topic], {topic.topic_id: Material(uuid.uuid4(), "Notes", "class", 2)}, {}, True)
    assert [i["type"] for i in out["items"]] == ["review_topic", "class_material", "practice_quiz"]


def test_small_run_is_deterministic_and_contract_valid():
    first = simulate.run(learners=150, seed=11)
    assert first == simulate.run(learners=150, seed=11)  # ranker responses pass check_response inside run()
    typical = first["scenarios"]["typical (4-12 answers)"]
    assert typical["ranker, recent answers + mastery"]["hit_at_3"] >= typical["rule_based/1 (core-api today)"]["hit_at_3"]
