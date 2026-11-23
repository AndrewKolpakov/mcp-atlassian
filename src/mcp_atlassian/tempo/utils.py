"""Value parsing and formatting helpers for Tempo payloads."""

import re
from datetime import date, time

_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*([hms])", re.IGNORECASE)
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(value: str) -> int:
    """Parse a duration such as ``"1h 30m"``, ``"90m"`` or ``"1.5h"`` into seconds.

    Days and weeks are refused rather than guessed: Jira counts a day as the
    site's working day (often 8h), not 24h, and a worklog written with the
    wrong one cannot be noticed until someone reads the timesheet.

    Args:
        value: The duration text. Units: ``h``, ``m``, ``s``.

    Returns:
        The duration in whole seconds.

    Raises:
        ValueError: If the text is not entirely made of duration tokens, or
            amounts to zero.
    """
    text = value.strip()
    if not text:
        raise ValueError("Duration is empty; use e.g. '1h 30m', '45m' or '1.5h'.")
    if re.search(r"\d\s*[dw]\b", text, re.IGNORECASE):
        raise ValueError(
            f"Duration {value!r} uses days or weeks; give hours instead "
            "(a Jira day is the site's working day, not 24h)."
        )
    position = 0
    total = 0.0
    for match in _DURATION_TOKEN.finditer(text):
        if text[position : match.start()].strip():
            break
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
        position = match.end()
    if text[position:].strip() or position == 0:
        raise ValueError(
            f"Cannot parse duration {value!r}; use e.g. '1h 30m', '45m' or '1.5h'."
        )
    seconds = round(total)
    if seconds <= 0:
        raise ValueError(f"Duration {value!r} must be greater than zero.")
    return seconds


def format_duration(seconds: int | None) -> str | None:
    """Format seconds as ``"1h 30m"``; None stays None."""
    if seconds is None:
        return None
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def parse_date(value: str, field_name: str) -> str:
    """Validate a ``YYYY-MM-DD`` date and return it unchanged.

    Raises:
        ValueError: If the value is not a valid calendar date in that format.
    """
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as e:
        raise ValueError(
            f"{field_name} must be a date as YYYY-MM-DD, got {value!r}"
        ) from e


def parse_start_time(value: str) -> str:
    """Normalize a local start time to Tempo's ``HH:MM:SS``.

    Accepts ``HH:MM`` or ``HH:MM:SS``.

    Raises:
        ValueError: If the value is not a valid time of day.
    """
    text = value.strip()
    try:
        parsed = time.fromisoformat(text)
    except ValueError as e:
        raise ValueError(f"start_time must be HH:MM or HH:MM:SS, got {value!r}") from e
    if parsed.tzinfo is not None or parsed.microsecond:
        raise ValueError(
            f"start_time must be a local HH:MM or HH:MM:SS without offset, got {value!r}"
        )
    return parsed.strftime("%H:%M:%S")
