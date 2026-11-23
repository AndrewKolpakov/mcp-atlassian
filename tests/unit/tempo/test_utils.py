"""Tests for Tempo value parsing helpers."""

import pytest

from mcp_atlassian.tempo.utils import (
    format_duration,
    parse_date,
    parse_duration,
    parse_start_time,
)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("1h", 3600),
        ("1h 30m", 5400),
        ("1h30m", 5400),
        ("90m", 5400),
        ("1.5h", 5400),
        ("45m 30s", 2730),
        (" 2H ", 7200),
    ],
)
def test_parse_duration_accepts_hours_minutes_seconds(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["1d", "1w 2h", "2 d"])
def test_parse_duration_refuses_days_and_weeks(text: str) -> None:
    with pytest.raises(ValueError, match="days or weeks"):
        parse_duration(text)


@pytest.mark.parametrize("text", ["", "abc", "1h abc", "abc 1h", "3600", "1x"])
def test_parse_duration_refuses_garbage_instead_of_guessing(text: str) -> None:
    """The Jira worklog parser turns garbage into 60s; a Tempo write must not."""
    with pytest.raises(ValueError):
        parse_duration(text)


def test_parse_duration_refuses_zero() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        parse_duration("0h 0m")


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(5400, "1h 30m"), (3600, "1h"), (45, "45s"), (0, "0s"), (None, None)],
)
def test_format_duration(seconds: int | None, text: str | None) -> None:
    assert format_duration(seconds) == text


def test_parse_date_validates_calendar_dates() -> None:
    assert parse_date("2026-11-02", "date") == "2026-11-02"
    with pytest.raises(ValueError, match="date must be a date"):
        parse_date("2026-02-30", "date")
    with pytest.raises(ValueError):
        parse_date("02.11.2026", "date")


@pytest.mark.parametrize(
    ("text", "normalized"),
    [("9:00", None), ("09:00", "09:00:00"), ("13:45:10", "13:45:10")],
)
def test_parse_start_time(text: str, normalized: str | None) -> None:
    if normalized is None:
        with pytest.raises(ValueError):
            parse_start_time(text)
    else:
        assert parse_start_time(text) == normalized


def test_parse_start_time_refuses_offsets() -> None:
    with pytest.raises(ValueError, match="without offset"):
        parse_start_time("09:00+02:00")
