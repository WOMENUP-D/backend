"""What the telemetry pipeline keeps from a browser, and what it refuses.

Most of what reaches `/analytics/*` comes from people with no account, so the
rules are tighter than for the rest of the API: a User-Agent is reduced to
three words and forgotten, a referrer to its host, a property bag to a few
short scalars with allow-listed keys, and a route to a known screen.
"""

from __future__ import annotations

import pytest

from app.services import telemetry
from app.services.traffic import KNOWN_PATHS, normalise_path

IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Mobile Safari/537.36"
)
WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
)
MAC_FIREFOX = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:126.0) Gecko/20100101 Firefox/126.0"
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


@pytest.mark.parametrize(
    ("ua", "device", "browser", "os"),
    [
        (IPHONE, "mobile", "Safari", "iOS"),
        (ANDROID, "mobile", "Chrome", "Android"),
        (WINDOWS, "desktop", "Edge", "Windows"),
        (MAC_FIREFOX, "desktop", "Firefox", "macOS"),
        (GOOGLEBOT, "bot", "bot", None),
        (None, None, None, None),
        ("", None, None, None),
    ],
)
def test_user_agent_is_reduced_to_families(ua, device, browser, os):
    client = telemetry.classify_user_agent(ua)
    assert (client.device, client.browser, client.os) == (device, browser, os)


def test_user_agent_version_numbers_never_survive():
    client = telemetry.classify_user_agent(ANDROID)
    for word in (client.device, client.browser, client.os):
        assert word is None or not any(ch.isdigit() for ch in word)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.google.com/search?q=womanup+ro%27yxatdan+o%27tish", "google.com"),
        ("https://t.me/womanup_uz/42", "t.me"),
        ("http://localhost:3000/dasturlar", None),
        ("https://womanup.uz/kabinet", None),
        ("", None),
        (None, None),
        ("not a url", None),
    ],
)
def test_referrer_keeps_the_host_and_nothing_else(raw, expected):
    assert telemetry.referrer_host(raw, own_hosts=frozenset({"womanup.uz"})) == expected


@pytest.mark.parametrize(
    ("raw", "ok"),
    [("s_8f3kd92hAbcDef", True), ("short", False), ("has spaces here!", False), (None, False)],
)
def test_session_ids_are_validated(raw, ok):
    assert (telemetry.normalise_session(raw) is not None) is ok


def test_properties_are_reduced_to_allowed_short_scalars():
    cleaned = telemetry.clean_properties(
        {
            "results": 12,
            "resumed": True,
            "section": "education",
            "message": "Failed for dilnoza@example.com with card 1234567890",
            "answer": "my private answer",
            "nested": {"a": 1},
            "name": "x" * 500,
        }
    )
    assert cleaned["results"] == 12
    assert cleaned["resumed"] is True
    assert cleaned["section"] == "education"
    assert "answer" not in cleaned and "nested" not in cleaned
    assert "dilnoza@example.com" not in cleaned["message"]
    assert "1234567890" not in cleaned["message"]
    assert len(cleaned["name"]) == telemetry.MAX_PROPERTY_TEXT


def test_entity_references_are_short_identifiers_only():
    assert telemetry.clean_entity("program", "0bdd1f67-5d4c-4436-9d55-2df4b02ea482") == (
        "program",
        "0bdd1f67-5d4c-4436-9d55-2df4b02ea482",
    )
    assert telemetry.clean_entity(None, "x") == (None, None)
    with pytest.raises(telemetry.EventRejected):
        telemetry.clean_entity("program", "<script>alert(1)</script>")
    with pytest.raises(telemetry.EventRejected):
        telemetry.clean_entity("Program Name", "abc")


def test_event_vocabularies_do_not_overlap():
    """A browser must never be able to report an outcome the server owns."""
    assert not (telemetry.CLIENT_EVENTS & telemetry.SERVER_EVENTS)
    assert "registration_completed" in telemetry.SERVER_EVENTS
    assert "registration_completed" not in telemetry.CLIENT_EVENTS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/dasturlar/0bdd1f67-5d4c-4436-9d55-2df4b02ea482", "/dasturlar/:id"),
        ("/talim/kurslar/python-asoslari", "/talim/kurslar/:slug"),
        ("/talim/kurslar/python-asoslari/1-dars", "/talim/kurslar/:slug/:lesson"),
        ("/tadbirlar/42?utm=x", "/tadbirlar/:id"),
        ("/kasb/dasturchi", "/kasb/:slug"),
        ("/imkoniyatlar/arizalarim", "/imkoniyatlar/arizalarim"),
        ("/admin/login", "/admin/login"),
        ("/dasturlar/one/two", "other"),
    ],
)
def test_dynamic_segments_fold_to_one_screen(raw, expected):
    assert normalise_path(raw) == expected


def test_every_known_path_maps_to_itself():
    for path in KNOWN_PATHS:
        assert normalise_path(path) == path


def test_business_event_arguments_carry_a_dedupe_key():
    args = telemetry.business("program_enrolled", key="abc", entity_type="program", entity_id=7)
    assert args["dedupe_key"] == "program_enrolled:abc"
    assert args["entity_id"] == "7"
