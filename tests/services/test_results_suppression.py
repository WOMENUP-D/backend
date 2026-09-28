"""Small groups on the Results dashboard: hidden, and not recoverable.

A group of 1-4 people is not shown as a number. Because the total is always
shown beside the groups, one hidden group on its own could be worked out as
the total minus the rest — so it never stays on its own.
"""

from app.services.results import MIN_GROUP, _suppressed

KEYS = ["a", "b", "c", "d"]


def shown(groups) -> dict:
    return {g.key: g.value for g in groups}


def test_large_groups_and_empty_ones_are_shown_as_they_are():
    assert shown(_suppressed({"a": 12, "b": 5, "c": 0}, KEYS)) == {"a": 12, "b": 5, "c": 0, "d": 0}


def test_a_lone_small_group_takes_the_next_smallest_with_it():
    groups = _suppressed({"a": 40, "b": 7, "c": 2}, KEYS)
    assert shown(groups) == {"a": 40, "b": None, "c": None, "d": 0}
    assert [g.key for g in groups if g.suppressed] == ["b", "c"]


def test_two_small_groups_already_hide_each_other():
    assert shown(_suppressed({"a": 40, "b": 3, "c": 1}, KEYS)) == {
        "a": 40,
        "b": None,
        "c": None,
        "d": 0,
    }


def test_with_nothing_else_to_hide_an_empty_group_goes_with_it():
    # "3 people in b, 0 everywhere else" is still said by hiding an empty group.
    assert shown(_suppressed({"b": 3}, KEYS)) == {"a": None, "b": None, "c": 0, "d": 0}
    assert shown(_suppressed({"a": 100, "b": 3}, ["a", "b"])) == {"a": None, "b": None}


def test_the_threshold_is_five():
    assert MIN_GROUP == 5
    assert shown(_suppressed({"a": 5, "b": 4, "c": 9}, KEYS))["a"] is None
    assert shown(_suppressed({"a": 5, "b": 5}, KEYS)) == {"a": 5, "b": 5, "c": 0, "d": 0}
