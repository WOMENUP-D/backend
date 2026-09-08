"""When the eight-hourly news ingest is due.

The worker's existing hourly job starts from `last_risk_scan = 0.0` and
therefore fires on every worker start. That is harmless for a database scan
over inactive users and is not harmless for a job that spends money on model
calls and publishes to the portal's front page: a crash-looping container would
run it on every boot.

So the ingest reads its last run out of the audit log and works out the next
due time from that. The arithmetic is a pure function precisely so this can be
asserted rather than described in a comment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.worker import next_due

INTERVAL = 8 * 3600
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
MONO = 1_000.0


def test_a_job_that_has_never_run_fires_immediately():
    """The one case where an immediate run is right: there is no marker, so
    there is nothing to wait for."""
    assert next_due(None, interval_seconds=INTERVAL, now_mono=MONO, now=NOW) == MONO


def test_a_run_two_hours_ago_is_due_in_six():
    previous = NOW - timedelta(hours=2)
    due = next_due(previous, interval_seconds=INTERVAL, now_mono=MONO, now=NOW)
    assert due == MONO + 6 * 3600


def test_a_restart_five_minutes_after_a_run_does_not_re_fire():
    """The whole point. The worker has just booted — its monotonic clock is
    near zero — and the marker says the job ran five minutes ago."""
    previous = NOW - timedelta(minutes=5)
    due = next_due(previous, interval_seconds=INTERVAL, now_mono=0.0, now=NOW)
    assert due > 0.0
    assert round(due) == INTERVAL - 300


def test_a_run_older_than_the_interval_is_due_now():
    previous = NOW - timedelta(hours=20)
    assert next_due(previous, interval_seconds=INTERVAL, now_mono=MONO, now=NOW) == MONO


def test_a_naive_timestamp_from_the_database_is_read_as_utc():
    """`AuditLog.created_at` comes back naive on some drivers, and subtracting
    it from an aware `now` would raise inside the worker loop."""
    previous = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    assert next_due(previous, interval_seconds=INTERVAL, now_mono=MONO, now=NOW) == MONO + 6 * 3600
