#!/usr/bin/env python3
r"""Gate a Jira/Confluence draft FILE before sending it through the MCP server.

Why a script rather than a grep you retype: the recorded failure is never a weak
check, it is a check run on something other than the bytes that got sent. One
command that takes the very file the write will read makes the safe path the
shortest path.

The converter itself refuses the fatal construct, so this is not the only thing
standing between a draft and a refusal. It stays for the two defects the
converter cannot refuse -- they produce VALID ADF, are accepted, and post
mangled -- and because seeing the whole list before the first send is cheaper
than seeing it as a tool error.

Run it as::

    uv run --frozen --project <plugin root> \
        python <plugin root>/plugin/scripts/check_draft.py draft.md

Exit 0 = safe to send. 1 = fatal construct, the write would be refused whole.
2 = usage or environment problem -- do NOT read that as "clean".
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# A line opening a bold run it never closes: CommonMark would carry it to the
# next line, this converter does not, so it stores literal asterisks. Not fatal,
# but it is the rendering defect no ADF check can see.
UNCLOSED = re.compile(r"(?<!\*)\*\*(?!\*)")
CODESPAN = re.compile(r"`[^`\n]*`")

USAGE = "usage: check_draft.py <draft.md> [more.md ...]"


def source_root() -> Path:
    """The tree whose converter is the authority for this check.

    This file sits at ``<root>/plugin/scripts/``, so the root is two levels up.
    ``MCP_ATLASSIAN_SRC`` overrides it, which is how a separate working tree is
    exercised -- the same override the server and the write guard honour, so all
    three agree on which converter is being checked against.
    """
    override = os.environ.get("MCP_ATLASSIAN_SRC")
    if override and not override.lstrip().startswith("${"):
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent.parent


def empty_text_nodes(node: Any, path: str = "doc") -> Iterator[str]:
    """ADF requires a text node's ``text`` to be non-empty; Jira refuses an empty one."""
    if isinstance(node, dict):
        if node.get("type") == "text" and not node.get("text"):
            yield path
        for n, child in enumerate(node.get("content") or []):
            yield from empty_text_nodes(child, f"{path}/{node.get('type')}[{n}]")


def line_warnings(name: str, text: str) -> list[str]:
    """The two defects that SUCCEED and post mangled, so nothing signals them."""
    warnings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if len(UNCLOSED.findall(line)) % 2:
            warnings.append(
                f"WARN   {name}:{lineno} bold marker unclosed on its line — this "
                "converter stores literal asterisks, it does not span the break"
            )
        # A pipe inside a code span on a table row splits the cell. The write
        # succeeds and posts a mangled table, so only reading the response back
        # reveals it.
        if line.lstrip().startswith("|"):
            for span in CODESPAN.findall(line):
                if "|" in span:
                    warnings.append(
                        f"WARN   {name}:{lineno} pipe inside a code span on a "
                        f"table row — the cell will split: {span}"
                    )
    return warnings


def main(argv: list[str]) -> int:
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2

    src = source_root()
    if not (src / "pyproject.toml").is_file():
        print(f"converter checkout not found at {src}", file=sys.stderr)
        print(
            "set MCP_ATLASSIAN_SRC to a clone of the repository — there is no "
            "gate without it",
            file=sys.stderr,
        )
        return 2
    sys.path.insert(0, str(src / "src"))

    try:
        # find_mark_conflicts is the server's own check and the AUTHORITY here:
        # it runs against the document Jira will see, so a body that documents
        # this defect inside a fence or a double-backtick span passes, exactly as
        # Jira would accept it. _markdown_to_adf_unchecked is imported
        # deliberately despite the underscore: this script wants the document AND
        # the located conflicts in one pass, and the public entry point raises
        # instead of returning the document. A private name in a local gate is
        # cheaper than widening the server's API to suit it.
        from mcp_atlassian.models.jira.adf import (  # noqa: PLC0415
            _markdown_to_adf_unchecked,
            find_mark_conflicts,
        )
    except ImportError as exc:
        print(f"cannot import the converter from {src}: {exc}", file=sys.stderr)
        print(
            "run this through `uv run --frozen --project <plugin root>` so the "
            "dependencies resolve",
            file=sys.stderr,
        )
        return 2

    paths = []
    for name in argv:
        path = Path(name)
        if not path.is_file() or not os.access(path, os.R_OK):
            print(f"cannot read: {name}", file=sys.stderr)
            return 2
        paths.append(path.resolve())

    rc = 0
    for path in paths:
        name = str(path)
        text = path.read_text(encoding="utf-8")

        conflicts = find_mark_conflicts(text)
        empties = list(empty_text_nodes(_markdown_to_adf_unchecked(text)))

        if conflicts or empties:
            rc = 1
            print(f"FATAL — {name} would be REFUSED WHOLE:")
            for conflict in conflicts:
                print(conflict.describe())
            for empty in empties:
                print(f"  empty text node at {empty}")
            print("  Fix every line above before sending; one edit clears the file.")
        else:
            print(f"clean  {name}")

        for warning in line_warnings(name, text):
            print(warning)

    if rc == 0:
        print(
            "SAFE TO SEND — send THESE files' contents, unedited. Any edit voids "
            "this run."
        )
    else:
        # The findings went to stdout and the verdict goes to stderr, which are
        # separately buffered: without this flush the verdict can surface above
        # the findings it refers to.
        sys.stdout.flush()
        print(
            "DO NOT SEND — fix, then re-run this script on the same files.",
            file=sys.stderr,
        )
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
