#!/usr/bin/env python3
"""PreToolUse guard: refuse a Jira write whose body Jira itself would refuse whole.

Why this exists when the converter refuses the same body:
`mcp_atlassian.models.jira.adf` raises `AdfMarkConflictError` on this construct,
so the write fails either way and with the same list of offences. What the hook
adds is only *when* — the refusal arrives before any network call, and before
`jira_batch_create_issues` has looked at a single issue.

It decides nothing of its own. The decision is `find_mark_conflicts`, the
function the server itself calls, so a false deny here would have to be a false
refusal there. The hand-measured map below is consulted for one case only: the
checkout being unreachable.

Scope is Jira only, deliberately. Confluence bodies go markdown -> storage
(XHTML), where `<strong><code>` is legal, so guarding them would deny writes
that would have succeeded. ADF is also Cloud-only in this server (`if
self.config.is_cloud`); on Server/DC the same body is sent as wiki markup and is
not refused, so a deny there is a false positive whose fix — splitting the
emphasis run — is harmless anyway.

This is the hook's entry point; there is no shell wrapper. It therefore **never
exits non-zero**: exit 0 with no output = allow, exit 0 with JSON = deny. Our own
failure must not block a write, so every unexpected error is swallowed and the
write is allowed through.

Run it as::

    uv run --frozen --project <plugin root> \
        python <plugin root>/plugin/scripts/jira_adf_guard.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from typing import Any

# Fields that reach Jira as ADF. `summary` is deliberately absent: it is a plain
# string field, so the construct is harmless there and denying would be wrong.
DIRECT_FIELDS = ("description", "comment", "body")
# Arguments that are JSON *strings* holding the fields above one level down.
JSON_STRING_FIELDS = ("fields", "additional_fields", "issues")

# Matched by SUFFIX, never by full name. The same tool is
# ``mcp__atlassian__jira_create_issue`` when the server is registered directly
# and ``mcp__plugin_mcp-atlassian_atlassian__jira_create_issue`` when it arrives
# through the plugin, and the prefix changes again if the plugin or the server
# is renamed. A set of full names stops matching silently when that happens, and
# a guard that silently stops guarding is worse than no guard at all -- the
# writes keep going, nothing reports anything.
#
# The hook matcher in plugin/hooks.json still has to list full names (matchers
# take a literal ``|`` list, not a pattern), so it stays a coarse filter and
# this is the real decision. A same-named tool from some other MCP server would
# also be checked; the check fails open, so that costs a moment, not a refusal.
WRITE_TOOL_NAMES = (
    "jira_create_issue",
    "jira_update_issue",
    "jira_add_comment",
    "jira_edit_comment",
    "jira_batch_create_issues",
)


def is_write_tool(tool_name: object) -> bool:
    """Whether this tool writes a field that reaches Jira as ADF."""
    if not isinstance(tool_name, str):
        return False
    return tool_name.rsplit("__", 1)[-1] in WRITE_TOOL_NAMES


# --- Degraded mode only, from here to the next divider ------------------------
#
# Which emphasis runs can carry a fatal code span, measured one construct at a
# time against the converter rather than assumed:
#   **x**, ***x***, ~~x~~ + code  -> fatal, AND an asterisk inside the code span
#                                    does not break the run
#   *x* + code                    -> fatal, but an asterisk inside the code span
#                                    terminates the run early, so it is NOT
#   __x__, _x_ + code             -> NOT fatal; the converter does not parse
#                                    underscore emphasis at all
#   `x` inside a link label       -> NOT fatal; code+link is the allowed pairing
#   a run spanning a line break   -> NOT fatal; the run is not parsed as emphasis
# This runs only when the checkout cannot be imported, so it errs toward missing
# a defect (which costs one Jira refusal, now a well-worded one) over a false
# deny. It is not consulted at all when the converter is available.
_STAR_OK = r"(?:[^*`\n]|`[^`\n]*`)"  # ** and *** delimiters survive an inner *
_NO_STAR = r"(?:[^*`\n]|`[^*`\n]*`)"  # a single * delimiter does not
FENCE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[^\n]*$|\Z)", re.S | re.M
)
# A code span delimited by TWO OR MORE backticks is how markdown quotes a span
# that itself contains backticks — i.e. exactly how this defect gets documented.
# Single-backtick spans must stay visible: seeing one inside an emphasis run is
# the whole detection.
QUOTING_SPAN = re.compile(r"(`{2,})(?!`).*?\1(?!`)", re.S)
EMPHASIS = re.compile(
    rf"\*\*\*{_STAR_OK}+?\*\*\*"
    rf"|\*\*{_STAR_OK}+?\*\*"
    rf"|(?<!\*)\*{_NO_STAR}+?\*(?!\*)"
    rf"|~~(?:[^~`\n]|`[^`\n]*`)+?~~"
)


def scrub(value: str) -> str:
    """Blank the regions that quote markup rather than apply it."""
    blanked = FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), value)
    return QUOTING_SPAN.sub(lambda m: "\n" * m.group(0).count("\n"), blanked)


# --- End of degraded mode -----------------------------------------------------


def candidate_texts(tool_input: dict) -> list[tuple[str, str]]:
    """(label, markdown) for every value that will be converted to ADF.

    The text is returned verbatim. The converter does its own masking of quoted
    regions and reports line numbers against the original, so blanking here
    would shift every number it prints.
    """
    found: list[tuple[str, str]] = []

    for key in DIRECT_FIELDS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            found.append((key, value))
    for key in JSON_STRING_FIELDS:
        raw = tool_input.get(key)
        if not raw:
            continue
        # The schema says JSON string, but a dict is accepted defensively — the
        # cost of guessing wrong here is a missed check, not a false deny.
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                continue
        items = raw if isinstance(raw, list) else [raw]
        for n, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for inner in DIRECT_FIELDS:
                value = item.get(inner)
                if isinstance(value, str) and value:
                    found.append((f"{key}[{n}].{inner}", value))
    return found


def load_finder() -> Callable[[str], Any] | None:
    """Import the server's own conflict finder, or None if unreachable."""
    if "--no-converter" in sys.argv:
        return None
    # This file sits at <root>/plugin/scripts/, so the repository root is two
    # levels up. Deriving it beats a hard-coded path: the guard has to work from
    # the plugin cache, from any clone, and when piped into by hand.
    src = os.environ.get("MCP_ATLASSIAN_SRC") or ""
    if not src or src.startswith("${"):
        src = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
    src = os.path.expanduser(src)
    sys.path.insert(0, os.path.join(src, "src"))
    try:
        from mcp_atlassian.models.jira.adf import find_mark_conflicts

        return find_mark_conflicts
    except Exception:
        return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        # Nothing to check. With no wrapper to retry there is no degraded pass to
        # fall back to either, so allow rather than block on our own confusion.
        return 0
    if not is_write_tool(event.get("tool_name")):
        return 0
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    finder = load_finder()
    problems: list[str] = []
    degraded = False

    for label, text in candidate_texts(tool_input):
        conflicts = None
        if finder is not None:
            try:
                conflicts = finder(text)
            except Exception:
                # A body the converter cannot parse at all is not evidence of
                # this defect; fall back for this text rather than deny.
                conflicts = None

        if conflicts is None:
            degraded = True
            spans = [span for span in EMPHASIS.findall(scrub(text)) if "`" in span]
            problems.extend(f"  {label}: {span}" for span in spans)
            continue

        for conflict in conflicts:
            problems.append(f"  {label} {conflict.describe().strip()}")

    if not problems:
        return 0

    listed = "\n".join(problems[:12])
    more = len(problems) - 12
    if more > 0:
        listed += f"\n  ... and {more} more."
    closing = (
        "Every offence is listed, so one edit clears the body — resending after "
        "fixing only the first costs a refusal per defect."
        if not degraded
        else "The converter could not be reached, so this is the regex fallback: "
        "it names the spans but not the lines, and may over-report a quoted "
        "example."
    )
    reason = (
        "Jira would refuse this whole write and store nothing: a code span "
        "shares an ADF text node with another mark, which the schema permits "
        "only for links — no field is written, not even the valid parts.\n"
        f"{listed}\n"
        f"{closing}"
    )
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
            "systemMessage": (
                "Jira write blocked: code span inside an emphasis run "
                "(ADF would refuse the whole document)."
            ),
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - a guard must not become a blocker
        # No traceback on stdout: the harness parses stdout as the hook's
        # decision, and a stray line there is worse than a missed check.
        sys.exit(0)
