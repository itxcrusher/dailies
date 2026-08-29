"""Tests for the production graph.

The interesting property here is not the arithmetic, it is *which* arithmetic. Slack on a
shot is not "deadline minus my own work": a shot that other shots wait on cannot use the
whole window, and a shot that waits on others cannot start at ``now``. Both directions are
pinned below, because getting only the downstream half right produces a graph that looks
correct on a leaf and quietly over-reports slack on everything with a predecessor.

Every test names an explicit ``now_epoch``. The module reads no clock, so a test that
passed yesterday passes tomorrow and a board can recompute the same numbers for any
timestamp it likes.

The graph node is called ``ShotNode``, not ``Shot``: ``dailies_api.state.Shot`` is a
different thing that already exists, and one name per concept beats a per-call-site alias
in every module that ends up importing both.
"""

import pytest
from dailies_graph.model import Dependency, Production, ShotNode, slack_seconds


def test_a_shot_with_no_dependents_has_slack_to_the_deadline():
    p = Production(deadline_epoch=3600)
    p.add_shot(ShotNode(id="SH010", estimated_seconds=600))
    assert slack_seconds(p, "SH010", now_epoch=0) == 3000


def test_a_blocking_shot_loses_its_dependents_duration_from_its_slack():
    """SH010 feeds SH020. SH010 cannot use the whole window; SH020 needs its share."""
    p = Production(deadline_epoch=3600)
    p.add_shot(ShotNode(id="SH010", estimated_seconds=600))
    p.add_shot(ShotNode(id="SH020", estimated_seconds=1200))
    p.add_dependency(Dependency(upstream="SH010", downstream="SH020"))
    # SH010 must finish 1200s before the deadline, so its own window is 2400s.
    assert slack_seconds(p, "SH010", now_epoch=0) == 1800
    assert slack_seconds(p, "SH020", now_epoch=0) == 1800


def test_the_longest_chain_sets_the_critical_path():
    p = Production(deadline_epoch=3600)
    for sid, secs in [("A", 300), ("B", 600), ("C", 900)]:
        p.add_shot(ShotNode(id=sid, estimated_seconds=secs))
    p.add_dependency(Dependency(upstream="A", downstream="B"))
    p.add_dependency(Dependency(upstream="B", downstream="C"))
    assert p.critical_path() == ["A", "B", "C"]


def test_a_cycle_is_rejected_rather_than_looping_forever():
    p = Production(deadline_epoch=3600)
    p.add_shot(ShotNode(id="A", estimated_seconds=1))
    p.add_shot(ShotNode(id="B", estimated_seconds=1))
    p.add_dependency(Dependency(upstream="A", downstream="B"))
    with pytest.raises(ValueError, match="cycle"):
        p.add_dependency(Dependency(upstream="B", downstream="A"))


def test_slack_goes_negative_rather_than_clamping_at_zero():
    """How far past the deadline a shot already is, is the number a supervisor needs."""
    p = Production(deadline_epoch=3600)
    p.add_shot(ShotNode(id="SH010", estimated_seconds=1200))
    assert slack_seconds(p, "SH010", now_epoch=3000) == -600


def test_unconnected_shots_each_get_the_whole_window():
    """No edge between them means no ordering, so neither steals the other's room."""
    p = Production(deadline_epoch=3600)
    p.add_shot(ShotNode(id="SH010", estimated_seconds=600))
    p.add_shot(ShotNode(id="SH020", estimated_seconds=1200))
    assert slack_seconds(p, "SH010", now_epoch=0) == 3000
    assert slack_seconds(p, "SH020", now_epoch=0) == 2400


def test_the_longest_chain_wins_over_the_one_with_more_shots():
    """Critical path is duration, not hop count; a long single shot can outweigh three."""
    p = Production(deadline_epoch=3600)
    for sid, secs in [("A", 10), ("B", 10), ("C", 10), ("X", 500)]:
        p.add_shot(ShotNode(id=sid, estimated_seconds=secs))
    p.add_dependency(Dependency(upstream="A", downstream="B"))
    p.add_dependency(Dependency(upstream="B", downstream="C"))
    assert p.critical_path() == ["X"]


def test_a_shot_cannot_depend_on_itself():
    p = Production(deadline_epoch=3600)
    p.add_shot(ShotNode(id="A", estimated_seconds=1))
    with pytest.raises(ValueError, match="cycle"):
        p.add_dependency(Dependency(upstream="A", downstream="A"))


def test_the_named_cycle_appears_in_the_error():
    """A supervisor fixing the data needs the offending chain, not just its existence."""
    p = Production(deadline_epoch=3600)
    for sid in ("A", "B", "C"):
        p.add_shot(ShotNode(id=sid, estimated_seconds=1))
    p.add_dependency(Dependency(upstream="A", downstream="B"))
    p.add_dependency(Dependency(upstream="B", downstream="C"))
    with pytest.raises(ValueError, match="A -> B -> C -> A"):
        p.add_dependency(Dependency(upstream="C", downstream="A"))


def test_an_edge_to_an_unknown_shot_is_refused():
    """A phantom node would silently absorb slack from every shot pointing at it."""
    p = Production(deadline_epoch=3600)
    p.add_shot(ShotNode(id="A", estimated_seconds=1))
    with pytest.raises(ValueError, match="SH999"):
        p.add_dependency(Dependency(upstream="A", downstream="SH999"))


def test_slack_on_a_shot_the_production_does_not_hold_is_refused():
    p = Production(deadline_epoch=3600)
    with pytest.raises(ValueError, match="SH999"):
        slack_seconds(p, "SH999", now_epoch=0)


def test_an_empty_production_has_no_critical_path():
    assert Production(deadline_epoch=3600).critical_path() == []


def test_a_fan_out_costs_the_longest_branch_not_the_sum():
    """Two shots waiting on the same upstream have no edge between them, so they overlap.

    The plausible wrong implementation adds every dependent's duration together. It gives
    the right answer on a straight chain and understates slack on every fan-out, which is
    the shape a sequence handed to two artists actually has.
    """
    p = Production(deadline_epoch=3600)
    p.add_shot(ShotNode(id="A", estimated_seconds=600))
    p.add_shot(ShotNode(id="B", estimated_seconds=300))
    p.add_shot(ShotNode(id="C", estimated_seconds=900))
    p.add_dependency(Dependency(upstream="A", downstream="B"))
    p.add_dependency(Dependency(upstream="A", downstream="C"))
    assert slack_seconds(p, "A", now_epoch=0) == 3600 - 600 - 900
    assert slack_seconds(p, "B", now_epoch=0) == 3600 - 600 - 300
    assert p.critical_path() == ["A", "C"]


def test_a_production_deserialised_with_a_cycle_is_rejected_at_construction():
    """``add_dependency`` is not the only door. A production can arrive as data."""
    data = {
        "deadline_epoch": 3600,
        "shots": {
            "A": {"id": "A", "estimated_seconds": 1},
            "B": {"id": "B", "estimated_seconds": 1},
        },
        "dependencies": [
            {"upstream": "A", "downstream": "B"},
            {"upstream": "B", "downstream": "A"},
        ],
    }
    with pytest.raises(ValueError, match="cycle"):
        Production.model_validate(data)


def test_a_production_deserialised_with_a_dangling_edge_is_rejected_at_construction():
    data = {
        "deadline_epoch": 3600,
        "shots": {"A": {"id": "A", "estimated_seconds": 1}},
        "dependencies": [{"upstream": "A", "downstream": "SH999"}],
    }
    with pytest.raises(ValueError, match="SH999"):
        Production.model_validate(data)


def test_the_same_edge_added_twice_changes_no_answer():
    """A repeated write is stored, not dropped, and must stay arithmetically inert."""
    p = Production(deadline_epoch=3600)
    p.add_shot(ShotNode(id="A", estimated_seconds=600))
    p.add_shot(ShotNode(id="B", estimated_seconds=1200))
    p.add_dependency(Dependency(upstream="A", downstream="B"))
    p.add_dependency(Dependency(upstream="A", downstream="B"))
    assert len(p.dependencies) == 2
    assert slack_seconds(p, "A", now_epoch=0) == 1800
    assert slack_seconds(p, "B", now_epoch=0) == 1800
    assert p.critical_path() == ["A", "B"]
