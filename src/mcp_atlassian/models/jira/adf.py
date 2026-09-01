"""
Atlassian Document Format (ADF) utilities.

This module provides utilities for converting between ADF and other formats.
Supports both ADF → plain text (for reading) and Markdown → ADF (for writing).
"""

import copy
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_MEDIA_NODE_TYPES = frozenset({"media", "mediaSingle", "mediaGroup"})
_PANEL_TYPES = frozenset({"note", "info", "warning", "success", "error"})
# ADF has no "tip" panel type; Jira's wiki {tip} is the green one, which ADF
# spells "success". The read path emits :::success directly, so this alias only
# has to tolerate hand-authored Markdown.
_PANEL_TYPE_ALIASES = {"tip": "success"}
# Measured on Jira Cloud: a panel holding either of these is rejected with an
# opaque 400 INVALID_INPUT that names no field. See _flatten_panel_content.
_PANEL_FORBIDDEN_CHILD_TYPES = frozenset({"blockquote", "panel"})
_TABLE_SEPARATOR_RE = re.compile(r"^:?-+:?$")
# A rendered line matching this would be read back as a block construct rather
# than as the prose it is, so the read path escapes its first character. ``*``
# and the backtick are absent on purpose: inline escaping has already handled
# them by the time a line is assembled.
_BLOCK_START_PATTERN = re.compile(
    r"^(?:#{1,6}[ \t]|[-+][ \t]|>[ \t]|\||:::|\{expand|-{3,}[ \t]*$|_{3,}[ \t]*$)"
)
# An ordered-list start is escaped on its punctuation rather than its first
# character; see _escape_block_start.
_ORDERED_START_PATTERN = re.compile(r"^(?P<number>\d+)[.)][ \t]")
# Whether a rendered block is itself a list, i.e. attaches directly under the
# list item above it instead of being separated by a blank line.
_LIST_ITEM_START_PATTERN = re.compile(r"^(?:[-*][ \t]|\d+\.[ \t])")
# One list line: its indentation (which decides nesting), its marker and the
# item text. Used by both directions, so they agree on what a list line is.
_LIST_LINE_PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>[-*]|\d+\.)[ \t]+(?P<text>.*)$"
)
# A ``:::`` line that opens a block rather than closing one. Used to count
# depth, so a panel nested in an expand does not close the expand.
_NESTED_FENCE_OPEN_PATTERN = re.compile(r"^:::[A-Za-z]")
# The opening run of a code fence, and whatever follows it as the language.
_CODE_FENCE_PATTERN = re.compile(r"^(?P<fence>`{3,})(?P<language>.*)$")
# Punctuation a backslash may escape on the way in. Mirrors CommonMark's
# escapable set, so ``C:\path`` keeps its backslash while ``\*`` does not.
_MD_ESCAPABLE_PUNCTUATION = r"\\`*_{}\[\]()#+\-.!|~>:"
_JIRA_ISSUE_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_/-])"
    r"([A-Z][A-Z0-9_]+-\d+(?:-\d+)*)"
    r"(?![A-Za-z0-9_/-])"
)
_VALID_STATUS_COLORS = frozenset(
    {"neutral", "purple", "blue", "red", "yellow", "green"}
)


def _normalize_known_project_keys(
    known_project_keys: set[str] | None,
) -> set[str] | None:
    """Normalize an allowlist of project keys used for issue-key auto-linking.

    Args:
        known_project_keys: Project keys that are known to exist, or None when
            no allowlist is configured.

    Returns:
        Upper-cased, whitespace-stripped keys, or None when the caller passed
        None (meaning "link every issue-key-shaped token"). An explicitly empty
        allowlist stays empty, which disables auto-linking entirely.
    """
    if known_project_keys is None:
        return None
    return {key.strip().upper() for key in known_project_keys if key.strip()}


def _should_link_issue_key(issue_key: str, known_project_keys: set[str] | None) -> bool:
    """Check whether an issue-key-shaped token belongs to a known project.

    Args:
        issue_key: Matched token, for example ``"PROJ-123"``.
        known_project_keys: Normalized allowlist of project keys, or None to
            accept every token (the historical behavior).

    Returns:
        True when the token should be turned into a ``/browse/`` link.
    """
    if known_project_keys is None:
        return True
    return issue_key.split("-", 1)[0].upper() in known_project_keys


def _append_text_nodes(
    nodes: list[dict[str, Any]],
    text: str,
    jira_base_url: str = "",
    marks: list[dict[str, Any]] | None = None,
    known_project_keys: set[str] | None = None,
) -> None:
    """Append text nodes, linking Jira issue keys when a base URL is known.

    Args:
        nodes: Node list to append to; mutated in place.
        text: Raw text to emit.
        jira_base_url: Jira base URL used to link bare issue keys.
        marks: Marks applied to the emitted text nodes.
        known_project_keys: Normalized allowlist of project keys; only a key
            whose project prefix is in the set becomes a link. None (the
            default) links every issue-key-shaped token.
    """
    if not text:
        return

    normalized_base_url = jira_base_url.rstrip("/")
    if not normalized_base_url:
        node: dict[str, Any] = {"type": "text", "text": text}
        if marks:
            node["marks"] = marks
        nodes.append(node)
        return

    pos = 0
    for match in _JIRA_ISSUE_KEY_RE.finditer(text):
        issue_key = match.group(1)
        if not _should_link_issue_key(issue_key, known_project_keys):
            # Leave the token inside the surrounding plain-text run so a key
            # from an unknown project stays literal text instead of becoming
            # a dead link.
            continue

        if match.start() > pos:
            node = {"type": "text", "text": text[pos : match.start()]}
            if marks:
                node["marks"] = marks
            nodes.append(node)

        link_marks = [
            *(marks or []),
            {
                "type": "link",
                "attrs": {"href": f"{normalized_base_url}/browse/{issue_key}"},
            },
        ]
        nodes.append({"type": "text", "text": issue_key, "marks": link_marks})
        pos = match.end()

    if pos < len(text):
        node = {"type": "text", "text": text[pos:]}
        if marks:
            node["marks"] = marks
        nodes.append(node)


def _apply_marks(
    nodes: list[dict[str, Any]],
    marks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add marks to already-parsed inline nodes, keeping their own marks first.

    Mark types a node already carries are skipped, so a node never ends up
    with two marks of the same type (two ``link`` marks on a single text node
    are invalid ADF).

    Args:
        nodes: Inline nodes to decorate; non-text nodes are left untouched.
        marks: Marks to add to every text node.

    Returns:
        The same list, with the marks merged into its text nodes in place.
    """
    if not marks:
        return nodes

    for node in nodes:
        if node.get("type") != "text":
            continue
        existing: list[dict[str, Any]] = node.get("marks", [])
        existing_types = {mark.get("type") for mark in existing}
        extra = [
            copy.deepcopy(mark)
            for mark in marks
            if mark.get("type") not in existing_types
        ]
        if extra:
            node["marks"] = [*existing, *extra]

    return nodes


def _parse_marked_span(
    inner: str,
    marks: list[dict[str, Any]],
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse the inside of an emphasis span and stack its marks on the result.

    The body is parsed recursively rather than taken literally, exactly as link
    text is, so an escape or a nested marker inside the span is understood:
    ``**a\\*b**`` is bold text containing an asterisk, not bold text containing
    a backslash. Issue-key auto-linking still applies, which taking the body
    literally also used to do.

    Args:
        inner: Text between the markers.
        marks: Marks to stack onto every text node the body produces.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys whose issue
            keys may be linked; None links every issue-key-shaped token.

    Returns:
        Inline nodes carrying both their own marks and the span's.
    """
    return _apply_marks(
        _parse_inline_formatting(inner, jira_base_url, known_project_keys),
        marks,
    )


def _parse_inline_formatting(
    text: str,
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse inline Markdown formatting into ADF inline nodes.

    Handles: bold (**), italic (*), inline code (`), links ([text](url)),
    strikethrough (~~), Jira-flavored user mentions
    ([~accountid:ACCOUNT_ID] or @[Display Name](accountid:ACCOUNT_ID)), and
    status lozenges ({status:color=green|title=Done}).

    Link text is parsed recursively, so inline markup inside it becomes
    nested marks instead of literal characters. Issue-key autolinking is left
    off in that recursion -- the surrounding link already owns the href, and a
    second link mark on one text node is invalid ADF -- so neither
    ``jira_base_url`` nor the allowlist is consulted there.

    Bare Jira issue keys are converted to links when ``jira_base_url`` is set,
    restricted to ``known_project_keys`` when that allowlist is given.

    The [~accountid:...] mention syntax mirrors what Jira's v2 wiki text
    returns when a real ADF mention is read back, so the read and write paths
    are symmetric. The display-name syntax also includes the mention text in
    the ADF node.

    The {status:...} syntax follows Jira wiki markup conventions. ``color`` is
    optional and falls back to "neutral" when omitted or unrecognized.

    Args:
        text: Raw text potentially containing inline Markdown formatting.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys; only a bare
            key whose project prefix is in the set is linked. None (the
            default) links every issue-key-shaped token.

    Returns:
        List of ADF inline nodes (text nodes with optional marks, plus
        mention nodes when [~accountid:...] or
        @[Display Name](accountid:...) is present, plus status nodes when
        {status:...} is present).
    """
    if not text:
        return []

    nodes: list[dict[str, Any]] = []
    # Pattern order matters: an escape before everything (a backslash has to
    # win over the marker it protects), then mention before link, the triple
    # marker before bold (``***x***`` is bold *and* italic, and reading it as
    # bold-then-stray-asterisk would corrupt it), bold before italic, code
    # before others.  Status sits after code so a backticked ``{status:...}``
    # stays literal.
    inline_re = re.compile(
        rf"\\(?P<escaped>[{_MD_ESCAPABLE_PUNCTUATION}])"
        r"|\[~accountid:(?P<wiki_mention_id>[^\]]+)\]"
        r"|@\[(?P<display_mention_text>[^\]]+)\]"
        r"\(accountid:(?P<display_mention_id>[^)]+)\)"
        r"|`(?P<code_inner>[^`]+)`"
        r"|\{status:(?:color=(?P<status_color>\w+)\|)?"
        r"title=(?P<status_title>[^}]+)\}"
        r"|\*\*\*(?P<bold_italic_inner>.+?)\*\*\*"
        r"|\*\*(?P<bold_inner>.+?)\*\*"
        r"|~~(?P<strike_inner>.+?)~~"
        # A label may hold an escaped bracket, so ``[a\]b](url)`` stays one
        # link instead of falling out of the pattern and becoming literal text.
        r"|\[(?P<link_text>(?:\\.|[^\]\\])+)\]\((?P<link_href>[^)]+)\)"
        r"|(?<!\*)\*(?!\*)(?P<italic_inner>.+?)(?<!\*)\*(?!\*)"
    )

    pos = 0
    had_escape = False
    for m in inline_re.finditer(text):
        # Add any plain text before this match
        if m.start() > pos:
            plain = text[pos : m.start()]
            _append_text_nodes(
                nodes, plain, jira_base_url, known_project_keys=known_project_keys
            )

        if m.group("escaped") is not None:
            # The escaped character is literal text: emit it without letting
            # it open a marker. Issue-key autolinking is skipped -- a single
            # punctuation character can never be a key.
            had_escape = True
            nodes.append({"type": "text", "text": m.group("escaped")})
        elif m.group("wiki_mention_id") is not None:
            nodes.append(
                {
                    "type": "mention",
                    "attrs": {"id": m.group("wiki_mention_id")},
                }
            )
        elif m.group("display_mention_id") is not None:
            nodes.append(
                {
                    "type": "mention",
                    "attrs": {
                        "id": m.group("display_mention_id"),
                        "text": f"@{m.group('display_mention_text')}",
                    },
                }
            )
        elif m.group("code_inner") is not None:
            nodes.append(
                {
                    "type": "text",
                    "text": m.group("code_inner"),
                    "marks": [{"type": "code"}],
                }
            )
        elif m.group("status_title") is not None:
            color = (m.group("status_color") or "neutral").lower()
            if color not in _VALID_STATUS_COLORS:
                color = "neutral"
            nodes.append(
                {
                    "type": "status",
                    "attrs": {
                        "text": m.group("status_title"),
                        "color": color,
                        "style": "",
                    },
                }
            )
        elif m.group("bold_italic_inner") is not None:
            nodes.extend(
                _parse_marked_span(
                    m.group("bold_italic_inner"),
                    [{"type": "strong"}, {"type": "em"}],
                    jira_base_url,
                    known_project_keys,
                )
            )
        elif m.group("bold_inner") is not None:
            nodes.extend(
                _parse_marked_span(
                    m.group("bold_inner"),
                    [{"type": "strong"}],
                    jira_base_url,
                    known_project_keys,
                )
            )
        elif m.group("strike_inner") is not None:
            nodes.extend(
                _parse_marked_span(
                    m.group("strike_inner"),
                    [{"type": "strike"}],
                    jira_base_url,
                    known_project_keys,
                )
            )
        elif m.group("link_text") is not None:
            link_mark: dict[str, Any] = {
                "type": "link",
                "attrs": {"href": m.group("link_href")},
            }
            # Link text can carry its own inline markup, so parse it
            # recursively and stack the link mark on top of the nested marks.
            # Issue-key autolinking stays off inside link text: a second link
            # mark on the same text node is invalid ADF.
            nodes.extend(
                _apply_marks(
                    _parse_inline_formatting(m.group("link_text")),
                    [link_mark],
                )
            )
        elif m.group("italic_inner") is not None:
            nodes.extend(
                _parse_marked_span(
                    m.group("italic_inner"),
                    [{"type": "em"}],
                    jira_base_url,
                    known_project_keys,
                )
            )

        pos = m.end()

    # Remaining plain text after last match
    if pos < len(text):
        remaining = text[pos:]
        _append_text_nodes(
            nodes, remaining, jira_base_url, known_project_keys=known_project_keys
        )

    # If no patterns matched, return the whole thing as plain text
    if not nodes and text:
        _append_text_nodes(
            nodes, text, jira_base_url, known_project_keys=known_project_keys
        )

    if had_escape:
        # An escape splits what was one run of prose into three nodes ("2 ",
        # "*", " 3"). Only text that escaped something is stitched back, so
        # input without an escape keeps converting node-for-node as before.
        nodes = _merge_adjacent_text_nodes(nodes)

    return nodes


def _merge_adjacent_text_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join neighbouring text nodes that carry identical marks.

    Args:
        nodes: Inline nodes, in document order.

    Returns:
        An equivalent list with adjacent same-marked text runs concatenated.
    """
    merged: list[dict[str, Any]] = []
    for node in nodes:
        if (
            merged
            and node.get("type") == "text"
            and merged[-1].get("type") == "text"
            and merged[-1].get("marks", []) == node.get("marks", [])
        ):
            merged[-1] = {**merged[-1], "text": merged[-1]["text"] + node["text"]}
            continue
        merged.append(node)
    return merged


def _make_paragraph(
    text: str,
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Create an ADF paragraph node from text with inline formatting.

    Args:
        text: Line of Markdown text.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys whose issue
            keys may be linked; None links every issue-key-shaped token.

    Returns:
        ADF paragraph node.
    """
    content = _parse_inline_formatting(text, jira_base_url, known_project_keys)
    if not content:
        content = [{"type": "text", "text": ""}]
    return {"type": "paragraph", "content": content}


def _make_list_item(
    text: str,
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Create an ADF listItem node wrapping a paragraph.

    Args:
        text: Item text without its list marker.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys whose issue
            keys may be linked; None links every issue-key-shaped token.

    Returns:
        ADF listItem node.
    """
    return {
        "type": "listItem",
        "content": [_make_paragraph(text, jira_base_url, known_project_keys)],
    }


def _make_task_item(
    text: str,
    checked: bool,
    local_id: str,
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Create an ADF taskItem node.

    Args:
        text: Item text without its checkbox marker.
        checked: Whether the checkbox is ticked.
        local_id: ADF localId for the task item.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys whose issue
            keys may be linked; None links every issue-key-shaped token.

    Returns:
        ADF taskItem node.
    """
    return {
        "type": "taskItem",
        "attrs": {"localId": local_id, "state": "DONE" if checked else "TODO"},
        "content": _parse_inline_formatting(text, jira_base_url, known_project_keys)
        or [{"type": "text", "text": text}],
    }


def _list_indent_width(indent: str) -> int:
    """Measure a list marker's indentation, counting a tab as four columns."""
    return len(indent.expandtabs(4))


def _parse_list_block(
    lines: list[str],
    start: int,
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Parse one list, nesting a deeper-indented list inside its parent item.

    Both markers are handled here so that indentation means the same thing to
    each: a line indented past the current level opens a sub-list attached to
    the item above it, which is how ADF spells nesting (a ``bulletList`` inside
    a ``listItem``). Switching marker kind at one level ends the list, so
    ``- a`` followed by ``1. b`` stays two lists.

    A blank line between two ordered items is tolerated -- Markdown calls that
    a loose list and Jira renders one list -- while a blank line ends a bullet
    list, which is the behaviour this module already had.

    Lines indented past the marker that are not themselves list lines are the
    item's continuation blocks, and are parsed as their own document so that a
    second paragraph or a code block inside an item stays inside it. An ADF
    ``listItem`` holds one or more blocks, so a multi-paragraph item is
    ordinary content and not an edge case.

    An ordered list whose first marker is not ``1`` carries that number as its
    ``order`` attr, which is what ADF uses for the starting number.

    Args:
        lines: All lines of the document being converted.
        start: Index of the first list line; its indentation sets the level.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys whose issue
            keys may be linked; None links every issue-key-shaped token.

    Returns:
        The list node and the index of the first line after the list.
    """
    first = _LIST_LINE_PATTERN.match(lines[start])
    if first is None:  # pragma: no cover - callers match the pattern first
        return {"type": "bulletList", "content": []}, start + 1

    base_indent = _list_indent_width(first.group("indent"))
    ordered = first.group("marker") not in ("-", "*")
    items: list[dict[str, Any]] = []
    index = start

    while index < len(lines):
        match = _LIST_LINE_PATTERN.match(lines[index])
        if match is None:
            if (
                ordered
                and not lines[index].strip()
                and index + 1 < len(lines)
                and (following := _LIST_LINE_PATTERN.match(lines[index + 1]))
                and _list_indent_width(following.group("indent")) == base_indent
                and following.group("marker") not in ("-", "*")
            ):
                index += 1
                continue
            break

        indent = _list_indent_width(match.group("indent"))
        if indent < base_indent:
            break
        if indent > base_indent:
            if not items:
                break
            nested, index = _parse_list_block(
                lines, index, jira_base_url, known_project_keys
            )
            items[-1]["content"].append(nested)
            # A block after the sub-list is still the outer item's, so the
            # continuation scan runs again rather than letting it fall out of
            # the list.
            index = _absorb_list_item_continuation(
                lines,
                index,
                base_indent,
                items[-1],
                jira_base_url,
                known_project_keys,
            )
            continue
        if (match.group("marker") not in ("-", "*")) != ordered:
            break

        items.append(
            _make_list_item(match.group("text"), jira_base_url, known_project_keys)
        )
        index += 1
        index = _absorb_list_item_continuation(
            lines, index, base_indent, items[-1], jira_base_url, known_project_keys
        )

    node: dict[str, Any] = {
        "type": "orderedList" if ordered else "bulletList",
        "content": items,
    }
    if ordered:
        try:
            first_number = int(first.group("marker").rstrip("."))
        except ValueError:  # pragma: no cover - the pattern guarantees digits
            first_number = 1
        if first_number != 1:
            node["attrs"] = {"order": first_number}
    return node, index


def _absorb_list_item_continuation(
    lines: list[str],
    index: int,
    base_indent: int,
    item: dict[str, Any],
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> int:
    """Attach an item's indented continuation blocks to it.

    A line indented past the marker that is not itself a list line continues
    the item above -- a second paragraph, a code block, a quote. Blank lines are
    kept only when indented content follows them, so a blank line that really
    ends the list still ends it. The collected lines are dedented by their own
    smallest indent, which keeps indentation *inside* a continuation code block
    intact, and parsed as their own document.

    Args:
        lines: All lines of the document being converted.
        index: Index of the first line after the item's marker line.
        base_indent: Indentation of the marker that owns these lines.
        item: The ``listItem`` node to extend; mutated in place.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys whose issue
            keys may be linked; None links every issue-key-shaped token.

    Returns:
        Index of the first line that does not belong to the item.
    """
    collected: list[str] = []
    cursor = index
    while cursor < len(lines):
        line = lines[cursor]
        if not line.strip():
            lookahead = cursor
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            if lookahead >= len(lines):
                break
            following = lines[lookahead]
            if (
                _LIST_LINE_PATTERN.match(following)
                or _list_indent_width(
                    following[: len(following) - len(following.lstrip())]
                )
                <= base_indent
            ):
                break
            collected.extend([""] * (lookahead - cursor))
            cursor = lookahead
            continue
        if _LIST_LINE_PATTERN.match(line):
            break
        if _list_indent_width(line[: len(line) - len(line.lstrip())]) <= base_indent:
            break
        collected.append(line)
        cursor += 1

    if not collected:
        return index

    body = [line for line in collected if line.strip()]
    strip_width = min(len(line) - len(line.lstrip()) for line in body)
    dedented = [line[strip_width:] if line.strip() else "" for line in collected]
    inner = _markdown_to_adf_unchecked(
        "\n".join(dedented), jira_base_url, known_project_keys
    )
    item["content"].extend(inner["content"])
    return cursor


def _make_code_block(language: str, code_lines: list[str]) -> dict[str, Any]:
    """Create an ADF codeBlock node from a fence's language and body lines.

    Args:
        language: Fence info string; an empty string leaves ``attrs`` empty
            rather than declaring a blank language.
        code_lines: Body lines, joined verbatim with newlines.

    Returns:
        ADF codeBlock node.
    """
    return {
        "type": "codeBlock",
        "attrs": {"language": language} if language else {},
        "content": [{"type": "text", "text": "\n".join(code_lines)}],
    }


def _split_table_cells(text: str, delimiter: str) -> list[str]:
    """Split a table row on a delimiter, ignoring code spans and link targets.

    Delimiters inside a backtick code span (``a || b``) or inside a Markdown
    link target (``[text](https://x/a||b)``) are literal characters, not cell
    separators. An unterminated backtick is treated as a literal character.

    Args:
        text: Row body with the outer delimiters already removed.
        delimiter: Cell delimiter to split on (``"|"`` or ``"||"``).

    Returns:
        List of raw (unstripped) cell strings; always at least one item.
    """
    cells: list[str] = []
    buffer: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            # An escaped character is one unit: ``\|`` is a literal pipe in a
            # cell, not the delimiter between two cells.
            buffer.append(text[index : index + 2])
            index += 2
            continue
        if char == "`":
            closing = text.find("`", index + 1)
            if closing != -1:
                buffer.append(text[index : closing + 1])
                index = closing + 1
                continue
        elif text.startswith("](", index):
            closing = text.find(")", index + 2)
            if closing != -1:
                buffer.append(text[index : closing + 1])
                index = closing + 1
                continue
        elif text.startswith(delimiter, index):
            cells.append("".join(buffer))
            buffer = []
            index += len(delimiter)
            continue
        buffer.append(char)
        index += 1
    cells.append("".join(buffer))
    return cells


def _split_escaped_pipes(text: str) -> list[str]:
    """Split a Markdown table row on unescaped pipes only.

    ``str.split("|")`` breaks a cell holding an escaped pipe in half, which is
    exactly what the read path emits for a cell whose text contains one. Input
    with no backslash in it splits identically to ``str.split``, so a table that
    never needed an escape keeps converting byte-for-byte as before.

    Args:
        text: Row body with the outer pipes already removed.

    Returns:
        Raw cell strings, escapes still in place for the inline parser to
        resolve; always at least one item.
    """
    cells: list[str] = []
    buffer: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            buffer.append(text[index : index + 2])
            index += 2
            continue
        if char == "|":
            cells.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    cells.append("".join(buffer))
    return cells


def _wiki_table_row_cells(line: str) -> list[str] | None:
    """Split a Jira wiki table header row (``||a||b||``) into cell texts.

    Jira's wiki markup marks header cells with ``||`` while Markdown uses a
    single ``|`` everywhere, so a wiki header fed to the Markdown splitter
    yields ``2N - 1`` cells for ``N`` columns. Detection is deliberately
    narrow: the row must start with ``||`` and either end with ``||`` or carry
    a further top-level ``||``. Prose such as ``|| means logical or`` and
    Markdown rows with an empty first cell (``|| b | c |``) are therefore left
    to the Markdown path.

    Args:
        line: Single table line, still including its outer pipes.

    Returns:
        Stripped cell texts, or None when the line is not a wiki header row.
    """
    stripped = line.strip()
    if not stripped.startswith("||"):
        return None

    body = stripped[2:]
    closed = body.endswith("||")
    body = body[:-2] if closed else body.rstrip("|")
    cells = _split_table_cells(body, "||")
    if not closed and len(cells) < 2:
        return None
    return [cell.strip() for cell in cells]


def _build_table_node(
    table_lines: list[str],
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> dict[str, Any] | None:
    """Build an ADF table node from Markdown or Jira wiki table lines.

    Each row is parsed in its own dialect: rows written as ``||a||b||`` are
    split on ``||`` and become header cells, every other row is split on a
    single ``|``. The first row is a header row in both dialects, so a wiki
    row further down a Markdown table adds a header row instead of demoting
    the Markdown one. Alignment separators (``|---|:---:|`` and their wiki
    spelling ``||---||---||``) are dropped before the dialect is decided, so a
    row that is thrown away cannot give a Markdown table wiki semantics.

    Only a table that really holds a wiki row is changed beyond that: its rows
    are padded with empty cells so the ADF table stays rectangular, and one
    warning per table names the construct that was translated. A table without
    wiki rows is emitted exactly as the Markdown-only converter emitted it --
    ragged if the source was ragged, and silently.

    Known limits: the code-span-aware splitter is used for data rows only
    inside a recognised wiki table, so ``|[a || b](https://e.example.com)|c|``
    still splits naively in a pure Markdown table, and a delimiter inside a
    double-backtick span still splits, because nothing in this module
    understands double-backtick spans.

    Args:
        table_lines: Consecutive lines forming one table block.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist scoping that linking; a cell is
            no more permissive than running prose.

    Returns:
        ADF table node, or None when every line was a separator.
    """
    wiki_rows = [_wiki_table_row_cells(line) for line in table_lines]

    # Separators are dropped first: a row that is thrown away must not decide
    # the dialect. Their cells hold only "-", ":" and spaces, on which both
    # splitters agree, so the naive split recognises them either way.
    kept: list[tuple[str, list[str] | None]] = []
    for line, wiki_cells in zip(table_lines, wiki_rows, strict=True):
        probe = (
            wiki_cells
            if wiki_cells is not None
            else [c.strip() for c in line.strip("|").split("|")]
        )
        if all(_TABLE_SEPARATOR_RE.match(c) for c in probe if c):
            continue
        kept.append((line, wiki_cells))

    if not kept:
        return None

    has_wiki_header = any(wiki_cells is not None for _, wiki_cells in kept)

    parsed_rows: list[tuple[bool, list[str]]] = []
    for line, wiki_cells in kept:
        if wiki_cells is not None:
            cells = wiki_cells
        elif has_wiki_header:
            cells = [c.strip() for c in _split_table_cells(line.strip("|"), "|")]
        else:
            cells = [c.strip() for c in _split_escaped_pipes(line.strip("|"))]
        parsed_rows.append((wiki_cells is not None, cells))

    widths = {len(cells) for _, cells in parsed_rows}
    column_count = max(widths)
    if has_wiki_header:
        mismatch = ""
        if len(widths) > 1:
            mismatch = (
                f". Rows had mismatched column counts {sorted(widths)}; "
                f"padded every row to {column_count} columns"
            )
        logger.warning(
            'Converted %d Jira wiki markup table row(s) ("||a||b||") to ADF; '
            "no other wiki construct in a table is translated, cell text is "
            "parsed as Markdown%s",
            sum(1 for is_wiki, _ in parsed_rows if is_wiki),
            mismatch,
        )

    adf_rows: list[dict[str, Any]] = []
    for idx, (is_wiki_row, cells) in enumerate(parsed_rows):
        cell_type = "tableHeader" if is_wiki_row or idx == 0 else "tableCell"
        # Only a wiki table is squared off; a ragged Markdown table stays
        # ragged so that Markdown input keeps converting byte-identically.
        padded = (
            cells + [""] * (column_count - len(cells)) if has_wiki_header else cells
        )
        adf_cells = []
        for cell_text in padded:
            content = _parse_inline_formatting(
                cell_text, jira_base_url, known_project_keys
            )
            if not content:
                content = [{"type": "text", "text": ""}]
            adf_cells.append(
                {
                    "type": cell_type,
                    "content": [{"type": "paragraph", "content": content}],
                }
            )
        adf_rows.append({"type": "tableRow", "content": adf_cells})

    return {
        "type": "table",
        "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
        "content": adf_rows,
    }


def _collect_fence_lines(lines: list[str], start: int) -> tuple[list[str], int]:
    """Collect the body of a ``:::`` fence opened on line ``start``.

    An unterminated fence runs to the end of the input, which keeps a truncated
    document convertible instead of dropping its tail.

    Args:
        lines: All lines of the document being converted.
        start: Index of the opening fence line.

    A fence opened inside the body is counted, so a panel or expand nested in
    an expand closes with its own delimiter instead of the inner one ending the
    outer block and leaving a stray ``:::`` paragraph behind. Both delimiters
    are spelled ``:::``, so depth is the only way to tell them apart.

    Returns:
        The body lines (opening and closing fence excluded) and the index of
        the first line after the fence.
    """
    body: list[str] = []
    index = start + 1
    depth = 1
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped == ":::":
            depth -= 1
            if depth == 0:
                break
        elif _NESTED_FENCE_OPEN_PATTERN.match(stripped):
            depth += 1
        body.append(lines[index])
        index += 1
    # Skip the closing :::
    if index < len(lines):
        index += 1
    return body, index


def _collect_code_fence(lines: list[str], start: int) -> tuple[str, list[str], int]:
    """Collect a ``` fenced code block opened on line ``start``.

    Args:
        lines: Lines being scanned; already stripped of a ``> `` quote prefix
            when the fence sits inside a blockquote.
        start: Index of the opening fence line.

    The fence may be longer than three backticks, and only a run at least as
    long closes it. That is what lets a code block whose body contains a
    ``` line survive: the read path opens a longer fence for exactly that case,
    and a three-backtick line inside it is then body text rather than the end
    of the block.

    Returns:
        The fence's language (empty when unlabelled), its body lines verbatim
        -- internal indentation and blank lines included -- and the index of
        the first line after the fence. An unterminated fence runs to the end
        of the input.
    """
    opening = _CODE_FENCE_PATTERN.match(lines[start])
    fence = opening.group("fence") if opening else "```"
    language = (opening.group("language") if opening else lines[start][3:]).strip()
    body: list[str] = []
    index = start + 1
    while index < len(lines):
        closing = _CODE_FENCE_PATTERN.match(lines[index])
        if closing and len(closing.group("fence")) >= len(fence):
            break
        body.append(lines[index])
        index += 1
    # Skip the closing ```
    if index < len(lines):
        index += 1
    return language, body, index


def _parse_quoted_block(
    quote_lines: list[str],
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse the body of a blockquote, keeping a fenced code block intact.

    Jira renders a wiki ``{quote}`` holding a ``{code}`` block as a fence
    inside the quote, and an ADF ``blockquote`` containing a ``codeBlock`` is
    accepted by Cloud (measured), so that fence has to become a real
    ``codeBlock`` instead of three literal paragraphs.

    A list is promoted too. ``bulletList`` and ``orderedList`` are declared
    children of ``blockquote`` in the ADF schema -- unlike ``blockquote`` and
    ``panel`` inside a ``panel``, which the schema does not allow and Cloud
    rejects with an opaque 400 (see ``_flatten_panel_content``). Leaving a list
    line as a paragraph would store ``- x`` as literal text, which is what the
    read path would then show. Nothing beyond these three is promoted: a
    construct the schema does not place here stays a paragraph.

    A blank line separates paragraphs outside a fence and is literal content
    inside one.

    Args:
        quote_lines: Quote body lines with the ``> `` prefix already removed.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys whose issue
            keys may be linked; None links every issue-key-shaped token.

    Returns:
        ADF nodes for the blockquote's content; never empty, because a
        blockquote with no content is invalid ADF.
    """
    content: list[dict[str, Any]] = []
    index = 0
    while index < len(quote_lines):
        line = quote_lines[index]
        if line.startswith("```"):
            language, code_lines, index = _collect_code_fence(quote_lines, index)
            content.append(_make_code_block(language, code_lines))
            continue
        if _LIST_LINE_PATTERN.match(line) and not line[:1].isspace():
            list_node, index = _parse_list_block(
                quote_lines, index, jira_base_url, known_project_keys
            )
            content.append(list_node)
            continue
        if line.strip():
            content.append(_make_paragraph(line, jira_base_url, known_project_keys))
        index += 1

    return content or [_make_paragraph("", jira_base_url, known_project_keys)]


def _flatten_panel_content(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lift blockquote and nested-panel children out of a panel's content.

    Jira rejects an ADF ``panel`` that contains a ``blockquote`` or another
    ``panel`` with an opaque ``400 INVALID_INPUT`` naming no field (measured on
    Cloud). A wiki panel *can* hold a quote, so the read path can legitimately
    hand us ``> `` lines inside a ``:::`` fence -- nesting them would turn a
    lossy-but-working read into a failed write. The offending wrapper is
    therefore dropped and its children hoisted into the panel, recursively, so
    a quote inside a nested panel is flattened too. Inline nodes and their marks
    are untouched: only the block-level wrapper is lost.

    ``paragraph``, ``heading``, ``bulletList``, ``orderedList`` and
    ``codeBlock`` are all measured-accepted inside a panel and keep nesting.

    Args:
        nodes: Nodes parsed from a panel fence body.

    Returns:
        The same nodes with every forbidden wrapper replaced by its own
        flattened children.
    """
    flattened: list[dict[str, Any]] = []
    dropped: list[str] = []
    for node in nodes:
        node_type = node.get("type")
        if node_type in _PANEL_FORBIDDEN_CHILD_TYPES:
            dropped.append(str(node_type))
            flattened.extend(_flatten_panel_content(node.get("content", [])))
        else:
            flattened.append(node)

    if dropped:
        logger.warning(
            "Flattened %s inside an ADF panel; Jira rejects a panel containing "
            "one with 400 INVALID_INPUT, so the wrapper is dropped and its "
            "content kept",
            ", ".join(sorted(set(dropped))),
        )

    return flattened


def _wiki_expand_title(raw_title: str) -> str:
    """Normalize the title of a wiki ``{expand}`` macro.

    Jira spells the title both positionally (``{expand:Foo}``) and as a macro
    keyword (``{expand:title=Foo}``); both mean the same thing to an author, so
    the keyword is dropped. Only that one leading prefix is removed, which
    leaves a positional title containing an ``=`` (``{expand:a=b}``) intact.

    The ``:::expand`` fence deliberately does not go through this: its title is
    the literal remainder of the fence line, and the read path has already
    resolved the wiki spelling by the time it emits one.

    Args:
        raw_title: Text captured after ``{expand:``.

    Returns:
        The title the author meant, without surrounding whitespace.
    """
    return re.sub(r"^\s*title\s*=\s*", "", raw_title, flags=re.IGNORECASE).strip()


def _make_expand_node(
    title: str,
    body_lines: list[str],
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Create an ADF expand node from a title and its unparsed body lines.

    Both spellings of an expand block -- the wiki macro
    ``{expand:Title}...{expand}`` and the ``:::expand Title ... :::`` fence the
    read path emits -- build their node here, so the two cannot drift. The body
    is parsed recursively as its own document, exactly as panel content is.

    Args:
        title: Title attribute; the empty string when the source had none.
        body_lines: Body lines of the block, without either delimiter.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys whose issue
            keys may be linked; None links every issue-key-shaped token.

    Returns:
        ADF expand node.
    """
    inner_doc = _markdown_to_adf_unchecked(
        "\n".join(body_lines), jira_base_url, known_project_keys
    )
    return {
        "type": "expand",
        "attrs": {"title": title},
        "content": inner_doc["content"],
    }


def _markdown_to_adf_unchecked(
    markdown_text: str,
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Convert Markdown text to ADF (Atlassian Document Format) document.

    Produces the document WITHOUT checking it for mark conflicts, so that a
    recursive call from a blockquote, panel or expand cannot abort the outer
    walk and hide every violation that follows it. Callers want
    :func:`markdown_to_adf`, which parses through here and then validates the
    finished document once, at the top, with the original text still in hand
    to report line numbers against.

    Implements a line-by-line parser that handles common Markdown constructs.
    ``:::info|note|warning|success|error`` fences become ADF panels, ``:::tip``
    being an accepted alias for ``success``; a blockquote or panel inside such a
    fence is flattened, because Jira rejects that nesting (see
    ``_flatten_panel_content``). Jira Cloud expand blocks can be written as
    ``:::expand Title ... :::`` or ``{expand:Title}...{expand}``. Table rows
    pasted as Jira wiki markup (``||header||header||``) are recognised alongside
    Markdown pipe tables. No external dependencies required.

    Args:
        markdown_text: Markdown-formatted text to convert.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Project keys that are known to exist. When given,
            only a bare issue key whose project prefix is in the set is
            auto-linked, so prose such as "PR-1 .. PR-17" is left as plain
            text instead of gaining dead ``/browse/`` links. The allowlist
            itself is case-insensitive — its entries are upper-cased before
            comparison — but the text is not: only an upper-case key token is
            recognised as an issue key in the first place. None (the default)
            links every issue-key-shaped token.

    Returns:
        ADF document dict with version, type, and content keys.
    """
    project_keys = _normalize_known_project_keys(known_project_keys)
    doc: dict[str, Any] = {"version": 1, "type": "doc", "content": []}

    if not markdown_text:
        doc["content"].append({"type": "paragraph", "content": []})
        return doc

    lines = markdown_text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # --- Expand/collapse block ({expand:Title}...{expand}) ---
        expand_match = re.match(r"^\{expand(?::(.+?))?\}\s*$", line)
        if expand_match:
            expand_title = _wiki_expand_title(expand_match.group(1) or "")
            expand_lines: list[str] = []
            i += 1
            while i < len(lines) and not re.match(r"^\{expand\}\s*$", lines[i]):
                expand_lines.append(lines[i])
                i += 1
            # Skip closing {expand}
            if i < len(lines):
                i += 1
            doc["content"].append(
                _make_expand_node(
                    expand_title, expand_lines, jira_base_url, project_keys
                )
            )
            continue

        # --- Fenced code block ---
        if line.startswith("```"):
            lang, code_lines, i = _collect_code_fence(lines, i)
            doc["content"].append(_make_code_block(lang, code_lines))
            continue

        # --- Horizontal rule ---
        stripped = line.strip()
        if stripped in ("---", "***", "___") or (
            len(stripped) >= 3
            and all(c == stripped[0] for c in stripped)
            and stripped[0] in "-*_"
        ):
            # Make sure it's not a list item like "- --"
            if not line.startswith("- ") and not line.startswith("* "):
                doc["content"].append({"type": "rule"})
                i += 1
                continue

        # --- Heading ---
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2)
            heading_node: dict[str, Any] = {
                "type": "heading",
                "attrs": {"level": level},
                "content": _parse_inline_formatting(text, jira_base_url, project_keys),
            }
            doc["content"].append(heading_node)
            i += 1
            continue

        # --- Blockquote ---
        if line.startswith("> "):
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].startswith("> "):
                # Strip exactly the "> " marker, so indentation inside a quoted
                # code fence survives.
                quote_lines.append(lines[i][2:])
                i += 1
            doc["content"].append(
                {
                    "type": "blockquote",
                    "content": _parse_quoted_block(
                        quote_lines, jira_base_url, project_keys
                    ),
                }
            )
            continue

        # --- Task list (- [ ] / - [x]) ---
        if re.match(r"^[-*]\s+\[[ xX]\]\s+", line):
            task_items: list[dict[str, Any]] = []
            task_counter = 0
            while i < len(lines) and re.match(r"^[-*]\s+\[[ xX]\]\s+", lines[i]):
                checked = bool(re.match(r"^[-*]\s+\[[xX]\]\s+", lines[i]))
                item_text = re.sub(r"^[-*]\s+\[[ xX]\]\s+", "", lines[i])
                task_counter += 1
                task_items.append(
                    _make_task_item(
                        item_text,
                        checked,
                        f"task-{id(doc)}-{task_counter}",
                        jira_base_url,
                        project_keys,
                    )
                )
                i += 1
            doc["content"].append(
                {
                    "type": "taskList",
                    "attrs": {"localId": f"tasklist-{id(doc)}-{i}"},
                    "content": task_items,
                }
            )
            continue

        # --- Expand fence (":::expand <title>") ---
        # Matched before the panel fence: the title is the rest of the fence
        # line, so it may contain spaces, which the \w+ panel fence cannot.
        expand_fence_match = re.match(r"^:::expand(?:\s+(.+?))?\s*$", line)
        if expand_fence_match:
            fence_title = expand_fence_match.group(1) or ""
            fence_lines, i = _collect_fence_lines(lines, i)
            # Unlike a panel, an expand's content is left exactly as parsed.
            doc["content"].append(
                _make_expand_node(fence_title, fence_lines, jira_base_url, project_keys)
            )
            continue

        # --- Panel block ---
        panel_match = re.match(r"^:::(\w+)\s*$", line)
        if panel_match:
            fence_name = panel_match.group(1).lower()
            panel_type = _PANEL_TYPE_ALIASES.get(fence_name, fence_name)
            if panel_type in _PANEL_TYPES:
                panel_lines, i = _collect_fence_lines(lines, i)
                # Recursively parse panel content
                inner_doc = _markdown_to_adf_unchecked(
                    "\n".join(panel_lines), jira_base_url, project_keys
                )
                panel_node: dict[str, Any] = {
                    "type": "panel",
                    "attrs": {"panelType": panel_type},
                    "content": _flatten_panel_content(inner_doc["content"]),
                }
                doc["content"].append(panel_node)
                continue

        # --- List (unordered or ordered, nested by indentation) ---
        # The marker must sit at column zero to start a list, which is what
        # this parser has always required; indentation only means nesting once
        # a list is open, so an indented line in running prose stays a
        # paragraph.
        if _LIST_LINE_PATTERN.match(line) and not line[:1].isspace():
            list_node, i = _parse_list_block(lines, i, jira_base_url, project_keys)
            doc["content"].append(list_node)
            continue

        # --- Table (Markdown "|a|b|" or Jira wiki "||a||b||") ---
        if line.startswith("|") and "|" in line[1:]:
            table_lines: list[str] = []
            while i < len(lines) and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1

            table_node = _build_table_node(table_lines, jira_base_url, project_keys)
            if table_node is not None:
                doc["content"].append(table_node)
            continue

        # --- Empty line (skip) ---
        if not stripped:
            i += 1
            continue

        # --- Paragraph (default) ---
        doc["content"].append(_make_paragraph(line, jira_base_url, project_keys))
        i += 1

    # Ensure at least one content node
    if not doc["content"]:
        doc["content"].append({"type": "paragraph", "content": []})

    return doc


# --- Mark-conflict validation -------------------------------------------------
#
# A text node may carry ``code`` beside exactly one other mark, ``link``. Pair
# it with ``strong``, ``em``, ``strike`` or anything else and Jira Cloud refuses
# the WHOLE document with a 400 that names no field -- not the offending
# paragraph, the entire write, so nothing is stored at all. That was measured
# against the live API four times before this check existed.
#
# The refusal below is deliberately an error rather than a silent repair. The
# converter could drop the outer mark and produce a document Jira accepts, and
# that was the first design; it is worse. A body silently rewritten on the way
# out is a body the author never learns was wrong, and the next one repeats it.
_CODE_COMPATIBLE_MARK_TYPES = frozenset({"code", "link"})

# Emphasis spans, used ONLY to build the suggested rewrite. Detection runs on
# the converted document, which is authoritative; if this pattern misses a span
# the violation is still reported, just without a ready-made fix. Keeping it out
# of the decision is what stops it from becoming a second, drifting definition
# of what Jira refuses.
#
# A ``**`` or ``***`` delimiter survives an asterisk inside a code span, a lone
# ``*`` does not -- ``*see `a/*` here*`` closes at the inner asterisk. Hence two
# character classes rather than one.
_EMPHASIS_STAR_BODY = r"(?:[^*`\n]|`[^`\n]*`)"
_EMPHASIS_NOSTAR_BODY = r"(?:[^*`\n]|`[^*`\n]*`)"
_EMPHASIS_SPAN_RE = re.compile(
    rf"(?P<d3>\*\*\*)(?P<b3>{_EMPHASIS_STAR_BODY}+?)\*\*\*"
    rf"|(?P<d2>\*\*)(?P<b2>{_EMPHASIS_STAR_BODY}+?)\*\*"
    rf"|(?<!\*)(?P<d1>\*)(?P<b1>{_EMPHASIS_NOSTAR_BODY}+?)\*(?!\*)"
    rf"|(?P<ds>~~)(?P<bs>(?:[^~`\n]|`[^`\n]*`)+?)~~"
)

# A fenced block, blanked before the source is searched for an offending code
# span. A body that DOCUMENTS this defect quotes the construct inside a fence,
# where it is a codeBlock and carries no marks at all; without this the quote
# would steal the line number from the real violation further down.
_FENCED_BLOCK_RE = re.compile(
    r"^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}(?P=fence)[^\n]*$|\Z)",
    re.S | re.M,
)

_CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)")


@dataclass(frozen=True)
class MarkConflict:
    """One text run Jira would refuse: ``code`` stacked with another mark."""

    text: str
    conflicting_marks: tuple[str, ...]
    line: int | None = None
    source_line: str | None = None
    suggestion: str | None = None
    origin: str | None = None

    def describe(self) -> str:
        """Render one violation as the two or three lines a fixer needs."""
        where = f"line {self.line}" if self.line is not None else "line not located"
        prefix = f"{self.origin} " if self.origin else ""
        marks = ", ".join(self.conflicting_marks)
        out = [f"  {prefix}{where}: code span `{self.text}` also carries: {marks}"]
        if self.source_line is not None:
            out.append(f"      written: {self.source_line.strip()}")
        if self.suggestion is not None:
            out.append(f"      fix to:  {self.suggestion.strip()}")
        return "\n".join(out)


class AdfMarkConflictError(ValueError):
    """Raised when a body would make Jira refuse the write and store nothing.

    Carries EVERY offending construct found in one conversion, never the first
    one only. Fail-fast would be cheaper to write and costs the author a round
    trip per defect: fix one, resend, get refused on the next. The list is the
    whole point of the exception -- one edit clears the body.
    """

    #: Past this many the message summarises instead of listing, so a
    #: pathological body cannot produce an error nobody can read.
    MAX_LISTED = 20

    def __init__(
        self, conflicts: list[MarkConflict], subject: str = "this body"
    ) -> None:
        self.conflicts = list(conflicts)
        self.subject = subject
        super().__init__(self._render())

    def _render(self) -> str:
        total = len(self.conflicts)
        plural = "s" if total != 1 else ""
        head = (
            f"Jira would refuse this write and store nothing: {total} "
            f"construct{plural} in {self.subject} put a `code` span inside "
            "another mark, which ADF permits only for links.\n"
            "All of them are listed below, so one edit clears the text; the "
            "usual fix is to close the emphasis before the code span and "
            "reopen it after."
        )
        shown = self.conflicts[: self.MAX_LISTED]
        body = "\n".join(conflict.describe() for conflict in shown)
        tail = ""
        if total > len(shown):
            tail = f"\n  ... and {total - len(shown)} more of the same kind."
        return f"{head}\n\n{body}{tail}"


def _iter_code_mark_conflicts(node: Any) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Walk a finished document, yielding every text run Jira would refuse.

    Args:
        node: Any ADF node, list of nodes, or the document itself.

    Yields:
        The code span's text and the mark types that may not sit beside it,
        in document order.
    """
    if isinstance(node, list):
        for item in node:
            yield from _iter_code_mark_conflicts(item)
        return
    if not isinstance(node, dict):
        return

    if node.get("type") == "text":
        marks = node.get("marks") or []
        types = [
            str(mark["type"])
            for mark in marks
            if isinstance(mark, dict) and mark.get("type")
        ]
        if "code" in types:
            offending = tuple(
                sorted({t for t in types if t not in _CODE_COMPATIBLE_MARK_TYPES})
            )
            if offending:
                yield str(node.get("text", "")), offending

    for value in node.values():
        if isinstance(value, list | dict):
            yield from _iter_code_mark_conflicts(value)


def _mask_fenced_blocks(markdown_text: str) -> str:
    """Blank fenced blocks, keeping every line number and every newline."""
    return _FENCED_BLOCK_RE.sub(
        lambda match: re.sub(r"[^\n]", " ", match.group(0)), markdown_text
    )


def _emphasise(fragment: str, delimiter: str) -> str:
    """Wrap a fragment in a delimiter, keeping its surrounding whitespace out.

    ``**T1 of **`` is not bold in CommonMark -- a closing run may not follow
    whitespace -- so a naive split would hand the author markup that is broken
    in a second way. An all-whitespace fragment is returned bare, because
    emphasising nothing would emit ``****``.
    """
    core = fragment.strip()
    if not core:
        return fragment
    lead = fragment[: len(fragment) - len(fragment.lstrip())]
    trail = fragment[len(fragment.rstrip()) :]
    return f"{lead}{delimiter}{core}{delimiter}{trail}"


def _split_emphasis_around_code(body: str, delimiter: str) -> str | None:
    """Rebuild an emphasis span so its code spans sit outside the emphasis."""
    pieces: list[str] = []
    position = 0
    for match in _CODE_SPAN_RE.finditer(body):
        pieces.append(_emphasise(body[position : match.start()], delimiter))
        pieces.append(match.group(0))
        position = match.end()
    if not pieces:
        return None
    pieces.append(_emphasise(body[position:], delimiter))
    return "".join(pieces)


def _suggest_rewrite(source_line: str, code_text: str) -> str | None:
    """Return the line rewritten so Jira accepts it, or None if unclear.

    Best-effort by design: a suggestion that cannot be derived is omitted, and
    the violation is still reported with its line number.
    """
    for match in _EMPHASIS_SPAN_RE.finditer(source_line):
        body = next(
            group
            for group in (
                match.group("b3"),
                match.group("b2"),
                match.group("b1"),
                match.group("bs"),
            )
            if group is not None
        )
        delimiter = next(
            group
            for group in (
                match.group("d3"),
                match.group("d2"),
                match.group("d1"),
                match.group("ds"),
            )
            if group is not None
        )
        if code_text not in body:
            continue
        rebuilt = _split_emphasis_around_code(body, delimiter)
        if rebuilt is None:
            return None
        return source_line[: match.start()] + rebuilt + source_line[match.end() :]
    return None


def _locate_conflicts(
    raw_conflicts: list[tuple[str, tuple[str, ...]]], markdown_text: str
) -> list[MarkConflict]:
    """Attach a line number, the written line and a suggested fix to each.

    Repeats of the same code span are handed successive occurrences rather than
    all pointing at the first, so a body that goes wrong twice the same way
    still names two places to edit.
    """
    masked_lines = _mask_fenced_blocks(markdown_text).split("\n")
    raw_lines = markdown_text.split("\n")
    seen: dict[str, int] = {}
    located: list[MarkConflict] = []

    for code_text, offending in raw_conflicts:
        hits = [i for i, line in enumerate(masked_lines) if f"`{code_text}`" in line]
        if not hits:
            # A span written with two or more backticks, or one holding a
            # backtick of its own; the bare text still locates the line.
            hits = [i for i, line in enumerate(masked_lines) if code_text in line]
        index = seen.get(code_text, 0)
        seen[code_text] = index + 1

        line_number = None
        source_line = None
        suggestion = None
        if index < len(hits):
            line_number = hits[index] + 1
            source_line = raw_lines[hits[index]]
            suggestion = _suggest_rewrite(source_line, code_text)

        located.append(
            MarkConflict(
                text=code_text,
                conflicting_marks=offending,
                line=line_number,
                source_line=source_line,
                suggestion=suggestion,
            )
        )

    return located


def find_mark_conflicts(
    markdown_text: str,
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> list[MarkConflict]:
    """Report every construct in a body that Jira would refuse the write over.

    The check a caller wants when it needs the list rather than an exception --
    a pre-send gate, or a batch validating several bodies before writing any.

    Args:
        markdown_text: Body to check, in Markdown.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys whose issue
            keys may be linked; None links every issue-key-shaped token.

    Returns:
        Every conflict in document order; empty when the body is acceptable.
    """
    document = _markdown_to_adf_unchecked(
        markdown_text, jira_base_url, known_project_keys
    )
    return _locate_conflicts(list(_iter_code_mark_conflicts(document)), markdown_text)


def markdown_to_adf(
    markdown_text: str,
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Convert Markdown to an ADF document, refusing one Jira will not store.

    Parsing is :func:`_markdown_to_adf_unchecked`; this adds the one check that
    cannot be left to the API, because the API's answer is a 400 naming no
    field. Validation runs once on the finished document, so a violation inside
    a panel, blockquote or expand is found together with the rest and the
    author gets the whole list at once.

    Args:
        markdown_text: Text in Markdown format.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys whose issue
            keys may be linked; None links every issue-key-shaped token.

    Returns:
        The ADF document.

    Raises:
        AdfMarkConflictError: If any text run pairs ``code`` with a mark ADF
            does not allow beside it. Carries every such run, not the first.
    """
    document = _markdown_to_adf_unchecked(
        markdown_text, jira_base_url, known_project_keys
    )
    conflicts = _locate_conflicts(
        list(_iter_code_mark_conflicts(document)), markdown_text
    )
    if conflicts:
        raise AdfMarkConflictError(conflicts)
    return document


def _adf_node_contains_media(node: dict[str, Any]) -> bool:
    """Return True when an ADF node contains media content."""
    if node.get("type") in _MEDIA_NODE_TYPES:
        return True

    content = node.get("content")
    if isinstance(content, list):
        return any(
            _adf_node_contains_media(child)
            for child in content
            if isinstance(child, dict)
        )

    return False


def extract_top_level_media_nodes(
    adf_document: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Extract top-level ADF nodes that contain media content."""
    if not isinstance(adf_document, dict):
        return []

    content = adf_document.get("content")
    if not isinstance(content, list):
        return []

    return [
        copy.deepcopy(node)
        for node in content
        if isinstance(node, dict) and _adf_node_contains_media(node)
    ]


def merge_adf_with_preserved_media(
    target_adf: dict[str, Any],
    source_adf: dict[str, Any] | None,
) -> dict[str, Any]:
    """Append existing media nodes from one ADF document into another.

    This is intentionally narrow: it preserves existing media-bearing blocks
    when a caller rewrites the surrounding description text, without attempting
    to merge all prior formatting or layout.
    """
    preserved_media = extract_top_level_media_nodes(source_adf)
    if not preserved_media:
        return target_adf

    merged = copy.deepcopy(target_adf)
    content = merged.get("content")
    if not isinstance(content, list):
        content = []
        merged["content"] = content

    existing_signatures = {
        json.dumps(node, sort_keys=True, separators=(",", ":"))
        for node in extract_top_level_media_nodes(merged)
    }
    for media_node in preserved_media:
        signature = json.dumps(media_node, sort_keys=True, separators=(",", ":"))
        if signature in existing_signatures:
            continue
        content.append(media_node)
        existing_signatures.add(signature)

    return merged


def _escape_inline_markdown(text: str) -> str:
    """Escape the characters ``_parse_inline_formatting`` would otherwise consume.

    Only the metacharacters that parser actually reads are escaped, so the
    output stays legible: a backslash (which introduces an escape), ``*``
    (bold and italic), a backtick (code), both brackets (links and the two
    mention spellings -- the closing one matters inside a link label, where an
    unescaped ``]`` would end the label early and lose the link) and the first
    ``~`` of a ``~~`` run (strikethrough). ``_`` is
    deliberately left alone -- the writer does not read it as emphasis, so
    escaping it would only add noise to ``snake_case`` prose.

    Args:
        text: Literal text from an ADF ``text`` node.

    Returns:
        The same text, safe to re-parse as Markdown.
    """
    return re.sub(r"[\\`*\[\]]|~(?=~)", lambda m: "\\" + m.group(0), text)


def _escape_block_start(line: str) -> str:
    """Escape a line that would otherwise re-parse as a block construct.

    A paragraph whose text happens to begin with ``- ``, ``# `` or ``|`` is
    prose, not a list, heading or table. Inline escaping has already dealt with
    ``*`` and backticks, so only the markers that are literal characters
    elsewhere are handled here.

    Args:
        line: One already-inline-escaped rendered line.

    Returns:
        The line, with a backslash prefixed when it needs one.
    """
    ordered = _ORDERED_START_PATTERN.match(line)
    if ordered:
        # The backslash has to land on the punctuation, not on the digit: a
        # digit is not escapable, so ``\1.`` would keep its backslash as text
        # while ``1\.`` unescapes back to the prose it came from.
        cut = ordered.end("number")
        return f"{line[:cut]}\\{line[cut:]}"
    if _BLOCK_START_PATTERN.match(line):
        return "\\" + line
    return line


def _render_inline_nodes(nodes: list[Any]) -> str:
    """Render a run of ADF inline nodes as Markdown.

    Args:
        nodes: Inline node list (``text``, ``hardBreak``, ``mention``, ...).

    Returns:
        Markdown for the run; the empty string when nothing renders.
    """
    parts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        parts.append(_render_inline_node(node))
    return "".join(parts)


def _wrap_marked_text(text: str, opener: str, closer: str) -> str:
    """Wrap text in an emphasis marker, keeping surrounding spaces outside.

    ``** bold **`` is not emphasis in most Markdown readers, and Jira's own
    renderer agrees, so the padding is moved out of the marker.

    Args:
        text: Already-escaped text to wrap.
        opener: Opening marker, e.g. ``"**"``.
        closer: Closing marker.

    Returns:
        The wrapped text, or the input unchanged when it holds no non-space.
    """
    stripped = text.strip()
    if not stripped:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return f"{lead}{opener}{stripped}{closer}{trail}"


def _render_inline_node(node: dict[str, Any]) -> str:
    """Render one ADF inline node as Markdown.

    Marks are applied innermost-first: ``code`` wins outright, because the
    writer's inline parser reads a backtick span verbatim and cannot nest
    emphasis inside one -- emitting ``**`x`**`` would put literal backticks in
    the stored text. Emphasis then nests inside a link, which is the one
    spelling the writer parses recursively.

    Args:
        node: Inline ADF node.

    Returns:
        Markdown for the node; the empty string when it carries nothing.
    """
    node_type = node.get("type")

    if node_type == "text":
        text = node.get("text", "")
        if not isinstance(text, str):
            return ""
        marks = {
            mark.get("type"): mark
            for mark in _adf_marks(node)
            if isinstance(mark, dict)
        }
        if "code" in marks:
            # Backtick spans are verbatim on the way back in, so the content is
            # left unescaped; a span holding a backtick cannot be expressed and
            # is emitted as-is.
            rendered = f"`{text}`" if text.strip() else text
        else:
            rendered = _escape_inline_markdown(text)
            if "strike" in marks:
                rendered = _wrap_marked_text(rendered, "~~", "~~")
            if "em" in marks:
                rendered = _wrap_marked_text(rendered, "*", "*")
            if "strong" in marks:
                rendered = _wrap_marked_text(rendered, "**", "**")
        link = marks.get("link")
        if isinstance(link, dict) and rendered:
            href = _adf_attrs(link).get("href")
            if href:
                rendered = f"[{rendered}]({href})"
        return rendered

    if node_type == "hardBreak":
        return "\n"

    if node_type == "mention":
        attrs = _adf_attrs(node)
        account_id = attrs.get("id", "")
        text = attrs.get("text") or ""
        if text:
            # Both spellings are parsed back into a mention node; this one also
            # carries the display name, so the read stays readable.
            return f"@[{text.lstrip('@')}](accountid:{account_id})"
        return f"[~accountid:{account_id}]"

    if node_type == "emoji":
        attrs = _adf_attrs(node)
        return str(attrs.get("text") or attrs.get("shortName") or "")

    if node_type == "date":
        attrs = _adf_attrs(node)
        timestamp = attrs.get("timestamp")
        if timestamp:
            try:
                dt = datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)
                return dt.strftime("%Y-%m-%d")
            except (ValueError, OSError, TypeError, OverflowError):
                return str(timestamp)
        return ""

    if node_type == "status":
        attrs = _adf_attrs(node)
        # The bracket is escaped so that a status followed by parenthesised
        # prose cannot re-parse as a Markdown link.
        return f"\\[{_escape_inline_markdown(str(attrs.get('text', '')))}\\]"

    if node_type == "inlineCard":
        attrs = _adf_attrs(node)
        url = attrs.get("url") or (attrs.get("data") or {}).get("url")
        name = (attrs.get("data") or {}).get("name")
        if url and name:
            return f"[{_escape_inline_markdown(str(name))}]({url})"
        if url:
            return str(url)
        return str(name or "")

    if node_type in _MEDIA_NODE_TYPES:
        return _render_media_marker(node)

    # Unknown inline node: keep whatever text it wraps rather than dropping it.
    children = _adf_children(node)
    return _render_inline_nodes(children) if children else ""


def _render_media_marker(node: dict[str, Any]) -> str:
    """Describe a media node in plain text.

    Neither an attachment nor an embedded image can be rebuilt from Markdown,
    and the write path re-attaches the real nodes itself (see
    ``merge_adf_with_preserved_media``), so the read emits a marker that
    carries no Markdown metacharacters and therefore survives a rewrite as
    plain prose.

    Args:
        node: A ``media``, ``mediaSingle`` or ``mediaGroup`` node.

    Returns:
        A parenthesised marker naming the media, e.g. ``(media: diagram.png)``.
    """
    names: list[str] = []
    stack: list[dict[str, Any]] = [node]
    while stack:
        current = stack.pop()
        if current.get("type") == "media":
            attrs = _adf_attrs(current)
            name = attrs.get("alt") or attrs.get("id") or "attachment"
            names.append(str(name))
        stack.extend(
            child for child in _adf_children(current) if isinstance(child, dict)
        )
    if not names:
        return "(media)"
    return " ".join(f"(media: {name})" for name in names)


def _adf_children(node: dict[str, Any]) -> list[Any]:
    """Return a node's content list, or an empty list when it has none."""
    content = node.get("content")
    return content if isinstance(content, list) else []


def _adf_attrs(node: dict[str, Any]) -> dict[str, Any]:
    """Return a node's attrs, or an empty dict when they are missing or junk.

    ``node.get("attrs") or {}`` is not enough: a truthy non-dict value (an ADF
    document is JSON from a remote service, so ``"attrs": "oops"`` is possible)
    would pass that guard and raise ``AttributeError`` on the next ``.get``.
    This read path runs on every issue fetch, so it degrades instead.
    """
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, dict) else {}


def _adf_marks(node: dict[str, Any]) -> list[Any]:
    """Return a node's marks, or an empty list when they are missing or junk."""
    marks = node.get("marks")
    return marks if isinstance(marks, list) else []


def _render_paragraph(node: dict[str, Any]) -> str:
    """Render a paragraph, escaping every line that could re-parse as a block."""
    rendered = _render_inline_nodes(_adf_children(node))
    return "\n".join(_escape_block_start(line) for line in rendered.split("\n"))


def _render_code_block(node: dict[str, Any]) -> str:
    """Render a codeBlock as a fenced block, keeping its body verbatim."""
    attrs = _adf_attrs(node)
    language = attrs.get("language") or ""
    body_parts: list[str] = []
    for child in _adf_children(node):
        if isinstance(child, dict) and child.get("type") == "text":
            body_parts.append(str(child.get("text", "")))
        elif isinstance(child, dict) and child.get("type") == "hardBreak":
            body_parts.append("\n")
    body = "".join(body_parts)
    # A ``` line inside the body would close a three-backtick fence, splitting
    # one code block into two and a paragraph. Opening a longer fence keeps it
    # body text; _collect_code_fence closes only on a run at least as long.
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"^`{3,}", body, re.MULTILINE)),
        default=0,
    )
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}{language}\n{body}\n{fence}"


def _render_blockquote(node: dict[str, Any]) -> str:
    """Render a blockquote, prefixing every line -- blank ones included.

    The writer holds a blockquote together only while consecutive lines start
    with ``"> "``, so an interior blank line keeps its marker instead of
    ending the quote.
    """
    body = _render_block_nodes(_adf_children(node))
    if not body:
        return "> "
    # A blank line keeps the marker *and* its space: the writer holds the quote
    # together only while every line starts with exactly "> ", and a bare ">"
    # would end it, splitting one quote into two with a ">" paragraph between.
    return "\n".join(f"> {line}" if line else "> " for line in body.split("\n"))


def _render_panel(node: dict[str, Any]) -> str:
    """Render a panel as the ``:::<type>`` fence the writer parses back."""
    attrs = _adf_attrs(node)
    panel_type = str(attrs.get("panelType") or "info").lower()
    if panel_type not in _PANEL_TYPES:
        # "custom" (and anything newer) has no fence of its own; info is the
        # neutral one and still round-trips as a panel.
        panel_type = "info"
    body = _render_block_nodes(_adf_children(node))
    return f":::{panel_type}\n{body}\n:::" if body else f":::{panel_type}\n:::"


def _render_expand(node: dict[str, Any]) -> str:
    """Render an expand (or nestedExpand) as the ``:::expand`` fence."""
    attrs = _adf_attrs(node)
    title = str(attrs.get("title") or "").strip()
    opener = f":::expand {title}" if title else ":::expand"
    body = _render_block_nodes(_adf_children(node))
    return f"{opener}\n{body}\n:::" if body else f"{opener}\n:::"


def _indent_continuation(marker: str, body: str) -> str:
    """Put the first body line after a list marker and indent the rest under it.

    Args:
        marker: List marker including its trailing space, e.g. ``"- "``.
        body: Rendered body of the item, possibly several lines.

    Returns:
        The item's lines, aligned under the marker.
    """
    lines = body.split("\n")
    padding = " " * len(marker)
    first = f"{marker}{lines[0]}"
    return "\n".join(
        [first, *(f"{padding}{line}" if line else "" for line in lines[1:])]
    )


def _render_list_item_body(item: dict[str, Any]) -> str:
    """Render one listItem's blocks, keeping a nested list attached to it."""
    parts: list[str] = []
    for child in _adf_children(item):
        if not isinstance(child, dict):
            continue
        rendered = _render_block(child)
        if rendered is None:
            continue
        parts.append(rendered)
    if not parts:
        return ""
    # A nested list follows its parent item directly; anything else is a
    # separate block and keeps a blank line before it.
    body = parts[0]
    for part in parts[1:]:
        separator = "\n" if _LIST_ITEM_START_PATTERN.match(part.lstrip()) else "\n\n"
        body += separator + part
    return body


def _render_list(node: dict[str, Any], *, ordered: bool) -> str:
    """Render a bulletList or orderedList, nesting by indentation."""
    number = 1
    if ordered:
        attrs = _adf_attrs(node)
        try:
            number = int(attrs.get("order", 1) or 1)
        except (TypeError, ValueError):
            number = 1

    lines: list[str] = []
    for item in _adf_children(node):
        if not isinstance(item, dict):
            continue
        body = _render_list_item_body(item)
        marker = f"{number}. " if ordered else "- "
        if ordered:
            number += 1
        lines.append(_indent_continuation(marker, body))
    return "\n".join(lines)


def _render_task_list(node: dict[str, Any]) -> str:
    """Render a taskList as ``- [ ]`` / ``- [x]`` items, at one level.

    A nested taskList is flattened into its parent rather than indented. The
    writer recognises a checkbox only at the start of a line, so an indented
    ``- [x]`` would come back as a paragraph holding that text literally --
    losing the checkbox as well as the nesting. Flattening loses only the
    nesting.
    """
    lines: list[str] = []
    for item in _adf_children(node):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "taskList":
            nested = _render_task_list(item)
            lines.extend(line for line in nested.split("\n") if line)
            continue
        if item.get("type") != "taskItem":
            continue
        attrs = _adf_attrs(item)
        checked = str(attrs.get("state", "")).upper() == "DONE"
        inline_children = [
            child
            for child in _adf_children(item)
            if isinstance(child, dict) and child.get("type") != "taskList"
        ]
        marker = "- [x] " if checked else "- [ ] "
        lines.append(f"{marker}{_render_inline_nodes(inline_children)}")
        for child in _adf_children(item):
            if isinstance(child, dict) and child.get("type") == "taskList":
                nested = _render_task_list(child)
                lines.extend(line for line in nested.split("\n") if line)
    return "\n".join(lines)


def _render_table_cell(cell: dict[str, Any]) -> str:
    """Flatten one table cell to a single Markdown line.

    A pipe table row is one line, so a cell holding several blocks is joined
    with spaces and its pipes are escaped.
    """
    body = _render_block_nodes(_adf_children(cell))
    flattened = " ".join(part for part in body.split("\n") if part.strip())
    return flattened.replace("|", "\\|").strip()


def _render_table(node: dict[str, Any]) -> str | None:
    """Render a table as a Markdown pipe table.

    The writer reads the first row as the header row, which is what Jira's own
    tables carry, so the header separator is always emitted after row one. A
    table whose first row holds no ``tableHeader`` cell therefore gains a
    header on a rewrite -- ADF can express a headerless table and Markdown
    cannot.
    """
    rows: list[list[str]] = []
    for row in _adf_children(node):
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue
        cells = [
            _render_table_cell(cell)
            for cell in _adf_children(row)
            if isinstance(cell, dict)
        ]
        rows.append(cells)

    rows = [row for row in rows if row]
    if not rows:
        return None

    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(padded[0]) + " |"]
    lines.append("|" + "|".join([" --- "] * width) + "|")
    lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
    return "\n".join(lines)


def _render_block(node: dict[str, Any]) -> str | None:
    """Render one block-level ADF node as Markdown.

    Args:
        node: Block-level ADF node.

    Returns:
        Markdown for the node, or None when it contributes nothing.
    """
    node_type = node.get("type")

    if node_type == "paragraph":
        return _render_paragraph(node)

    if node_type == "heading":
        attrs = _adf_attrs(node)
        try:
            level = int(attrs.get("level", 1) or 1)
        except (TypeError, ValueError):
            level = 1
        level = min(max(level, 1), 6)
        return f"{'#' * level} {_render_inline_nodes(_adf_children(node))}".rstrip()

    if node_type == "codeBlock":
        return _render_code_block(node)

    if node_type == "blockquote":
        return _render_blockquote(node)

    if node_type == "panel":
        return _render_panel(node)

    if node_type in ("expand", "nestedExpand"):
        return _render_expand(node)

    if node_type == "bulletList":
        return _render_list(node, ordered=False) or None

    if node_type == "orderedList":
        return _render_list(node, ordered=True) or None

    if node_type == "taskList":
        return _render_task_list(node) or None

    if node_type == "table":
        return _render_table(node)

    if node_type == "rule":
        return "---"

    if node_type in _MEDIA_NODE_TYPES:
        return _render_media_marker(node)

    if node_type in ("layoutSection", "layoutColumn", "bodiedExtension", "extension"):
        # A layout carries no Markdown meaning; its columns are emitted one
        # after another.
        return _render_block_nodes(_adf_children(node)) or None

    if node_type in ("decisionList", "decisionItem"):
        if node_type == "decisionItem":
            return f"- {_render_inline_nodes(_adf_children(node))}"
        rendered = [
            _render_block(child)
            for child in _adf_children(node)
            if isinstance(child, dict)
        ]
        return "\n".join(part for part in rendered if part) or None

    children = _adf_children(node)
    if children:
        # An unknown wrapper still has its content read, so a node type this
        # module has not met yet degrades to its text instead of vanishing.
        if all(
            isinstance(child, dict)
            and child.get("type") in ("text", "hardBreak", "mention", "emoji")
            for child in children
        ):
            return _render_paragraph(node)
        return _render_block_nodes(children) or None

    return None


def _render_block_nodes(nodes: list[Any]) -> str:
    """Render a list of block nodes, separated by a blank line."""
    rendered: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        block = _render_block(node)
        if block is None:
            continue
        rendered.append(block)
    return "\n\n".join(rendered)


def adf_to_markdown(adf_content: dict | list | str | None) -> str | None:
    """Convert an ADF document to the Markdown this module also parses.

    This is the inverse of :func:`markdown_to_adf`, and deliberately emits only
    the vocabulary that function reads back -- ``:::info``/``:::note``/
    ``:::warning``/``:::success``/``:::error`` panels, ``:::expand <title>``,
    ``> `` quotes, fenced code, pipe tables, ``- ``/``1. `` lists and
    ``- [ ]`` tasks -- so that one read+write cycle is a fixed point. Text is
    escaped for the same reason: the writer understands a backslash escape, so
    prose containing ``*`` or a backtick survives a rewrite as prose.

    Unlike :func:`adf_to_text`, which flattens everything to bare text, this
    keeps the document's structure. Prefer it for anything a user or an agent
    reads; ``adf_to_text`` remains for callers that want no markup at all.

    Known lossy edges, all measured against the writer:

    * a text node carrying both ``code`` and an emphasis mark keeps only the
      code span, because Markdown cannot nest emphasis inside a backtick span
      that Jira reads verbatim;
    * ``underline``, ``textColor`` and ``subsup`` marks have no Markdown
      spelling and are emitted as plain text;
    * a ``hardBreak`` becomes a newline, which a rewrite stores as two
      paragraphs rather than one paragraph with a break in it;
    * whitespace padding an emphasis mark (``em`` over ``" x "``) moves outside
      the marker, because ``** x **`` is not emphasis to a Markdown reader; the
      rendering is unchanged but the mark's text loses the spaces;
    * a ``status`` node degrades to bracketed text and an ``inlineCard`` to a
      link or a bare URL, neither of which is rebuilt as its own node;
    * media becomes a ``(media: ...)`` marker -- the write path re-attaches
      the real nodes instead of rebuilding them from text;
    * a table whose first row is not a header row gains one on a rewrite, and a
      cell holding several blocks is flattened onto the row's single line;
    * a ``nestedExpand`` comes back as an ``expand``, which is the same block
      with the same title;
    * a nested ``taskList`` is flattened to one level of checkboxes, because an
      indented checkbox is not one to the writer;
    * a URL containing ``)`` ends the link early on the way back in, because
      the writer reads an href as ``[^)]+``.

    Args:
        adf_content: ADF document (dict), content list, string, or None.

    Returns:
        Markdown text, or None when the input is None. A document with no
        renderable content yields the empty string.
    """
    if adf_content is None:
        return None

    if isinstance(adf_content, str):
        return adf_content

    if isinstance(adf_content, list):
        return _render_block_nodes(adf_content)

    if isinstance(adf_content, dict):
        if adf_content.get("type") == "doc":
            return _render_block_nodes(_adf_children(adf_content))
        rendered = _render_block(adf_content)
        if rendered is not None:
            return rendered
        return _render_inline_node(adf_content)

    return None


def adf_to_text(adf_content: dict | list | str | None) -> str | None:
    """
    Convert Atlassian Document Format (ADF) content to plain text.

    ADF is Jira Cloud's rich text format returned for fields like description.
    This function recursively extracts text content from the ADF structure.

    Args:
        adf_content: ADF document (dict), content list, string, or None

    Returns:
        Plain text string or None if no content
    """
    if adf_content is None:
        return None

    if isinstance(adf_content, str):
        return adf_content

    if isinstance(adf_content, list):
        texts = []
        for item in adf_content:
            text = adf_to_text(item)
            if text:
                texts.append(text)
        return "\n".join(texts) if texts else None

    if isinstance(adf_content, dict):
        # Check if this is a text node
        if adf_content.get("type") == "text":
            return adf_content.get("text", "")

        # Check if this is a hardBreak node
        if adf_content.get("type") == "hardBreak":
            return "\n"

        # Check if this is a mention node
        if adf_content.get("type") == "mention":
            attrs = _adf_attrs(adf_content)
            return attrs.get("text") or f"@{attrs.get('id', 'unknown')}"

        # Check if this is an emoji node
        if adf_content.get("type") == "emoji":
            attrs = _adf_attrs(adf_content)
            return attrs.get("text") or attrs.get("shortName", "")

        # Check if this is a date node
        if adf_content.get("type") == "date":
            attrs = _adf_attrs(adf_content)
            timestamp = attrs.get("timestamp")
            if timestamp:
                try:
                    dt = datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)
                    return dt.strftime("%Y-%m-%d")
                except (ValueError, OSError, TypeError, OverflowError):
                    return str(timestamp)
            return ""

        # Check if this is a status node
        if adf_content.get("type") == "status":
            attrs = _adf_attrs(adf_content)
            return f"[{attrs.get('text', '')}]"

        # Check if this is an inlineCard node
        if adf_content.get("type") == "inlineCard":
            attrs = _adf_attrs(adf_content)
            url = attrs.get("url")
            if url:
                return url
            data = attrs.get("data", {})
            return data.get("url") or data.get("name", "")

        # Check if this is a codeBlock node
        if adf_content.get("type") == "codeBlock":
            content = adf_content.get("content", [])
            code_text = adf_to_text(content) or ""
            return f"```\n{code_text}\n```"

        # Recursively process content
        content = adf_content.get("content")
        if content:
            return adf_to_text(content)

        return None

    return None
