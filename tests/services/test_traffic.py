"""Traffic counting and the privacy rules around it.

Most of the people this table counts have no account, so the constraints are
tighter than for the rest of analytics: the raw visitor id must never be stored,
and a crafted path must not turn the table into a free-text log.
"""

import pytest

from app.services.traffic import hash_visitor, normalise_path


def test_visitor_id_is_not_stored_in_the_clear():
    raw = "abc123-visitor"
    digest = hash_visitor(raw)
    assert raw not in digest
    assert len(digest) == 64


def test_same_visitor_hashes_the_same_way():
    """Otherwise unique-visitor counts would equal page views."""
    assert hash_visitor("same") == hash_visitor("same")


def test_different_visitors_do_not_collide():
    assert hash_visitor("one") != hash_visitor("two")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/dasturlar", "/dasturlar"),
        ("/dasturlar/", "/dasturlar"),
        ("/", "/"),
        ("/kabinet", "/kabinet"),
    ],
)
def test_known_routes_are_kept(raw, expected):
    assert normalise_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "/dasturlar?search=her+name",
        "/kabinet#section",
        "/dasturlar?token=secret",
    ],
)
def test_query_strings_and_fragments_are_dropped(raw):
    """A search term can be a person's name; it has no business in analytics."""
    cleaned = normalise_path(raw)
    assert "?" not in cleaned
    assert "#" not in cleaned
    assert "search" not in cleaned
    assert "secret" not in cleaned


@pytest.mark.parametrize(
    "raw",
    [
        "/unknown-page",
        "/../../etc/passwd",
        "/" + "x" * 400,
        "javascript:alert(1)",
    ],
)
def test_unexpected_paths_collapse_to_other(raw):
    """The column is a fixed vocabulary, not somewhere to write arbitrary text."""
    assert normalise_path(raw) == "other"


def test_empty_path_means_the_home_page():
    """An empty string from a client is the root, not an unknown route."""
    assert normalise_path("") == "/"


def test_path_never_exceeds_the_column_width():
    assert len(normalise_path("/" + "a" * 5000)) <= 120
