"""
Atlassian Document Format (ADF) utilities.

This module provides utilities for converting between ADF and other formats.
Supports both ADF → plain text (for reading) and Markdown → ADF (for writing).
"""

import copy
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_MEDIA_NODE_TYPES = frozenset({"media", "mediaSingle", "mediaGroup"})
_PANEL_TYPES = frozenset({"note", "info", "warning", "success", "error"})
# ADF has no "tip" panel type; Jira's wiki {tip} is the green one, which ADF
# spells "success". The read path emits :::success directly, so this alias only
# has to tolerate hand-authored Markdown.
_PANEL_TYPE_ALIASES = {"tip": "success"}
_TABLE_SEPARATOR_RE = re.compile(r"^:?-+:?$")
_JIRA_ISSUE_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_/-])"
    r"([A-Z][A-Z0-9_]+-\d+(?:-\d+)*)"
    r"(?![A-Za-z0-9_/-])"
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


def _parse_inline_formatting(
    text: str,
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse inline Markdown formatting into ADF inline nodes.

    Handles: bold (**), italic (*), inline code (`), links ([text](url)),
    strikethrough (~~), and Jira-flavored user mentions
    ([~accountid:ACCOUNT_ID] or @[Display Name](accountid:ACCOUNT_ID)).

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

    Args:
        text: Raw text potentially containing inline Markdown formatting.
        jira_base_url: Jira base URL used to link bare issue keys.
        known_project_keys: Normalized allowlist of project keys; only a bare
            key whose project prefix is in the set is linked. None (the
            default) links every issue-key-shaped token.

    Returns:
        List of ADF inline nodes (text nodes with optional marks, plus
        mention nodes when [~accountid:...] or
        @[Display Name](accountid:...) is present).
    """
    if not text:
        return []

    nodes: list[dict[str, Any]] = []
    # Pattern order matters: mention before link, bold before italic,
    # code before others.
    inline_re = re.compile(
        r"\[~accountid:(?P<wiki_mention_id>[^\]]+)\]"
        r"|@\[(?P<display_mention_text>[^\]]+)\]"
        r"\(accountid:(?P<display_mention_id>[^)]+)\)"
        r"|`(?P<code_inner>[^`]+)`"
        r"|\*\*(?P<bold_inner>.+?)\*\*"
        r"|~~(?P<strike_inner>.+?)~~"
        r"|\[(?P<link_text>[^\]]+)\]\((?P<link_href>[^)]+)\)"
        r"|(?<!\*)\*(?!\*)(?P<italic_inner>.+?)(?<!\*)\*(?!\*)"
    )

    pos = 0
    for m in inline_re.finditer(text):
        # Add any plain text before this match
        if m.start() > pos:
            plain = text[pos : m.start()]
            _append_text_nodes(
                nodes, plain, jira_base_url, known_project_keys=known_project_keys
            )

        if m.group("wiki_mention_id") is not None:
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
        elif m.group("bold_inner") is not None:
            _append_text_nodes(
                nodes,
                m.group("bold_inner"),
                jira_base_url,
                [{"type": "strong"}],
                known_project_keys,
            )
        elif m.group("strike_inner") is not None:
            _append_text_nodes(
                nodes,
                m.group("strike_inner"),
                jira_base_url,
                [{"type": "strike"}],
                known_project_keys,
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
            _append_text_nodes(
                nodes,
                m.group("italic_inner"),
                jira_base_url,
                [{"type": "em"}],
                known_project_keys,
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

    return nodes


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
            cells = [c.strip() for c in line.strip("|").split("|")]
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
            "no other wiki construct is translated%s",
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

    Returns:
        The body lines (opening and closing fence excluded) and the index of
        the first line after the fence.
    """
    body: list[str] = []
    index = start + 1
    while index < len(lines) and lines[index].strip() != ":::":
        body.append(lines[index])
        index += 1
    # Skip the closing :::
    if index < len(lines):
        index += 1
    return body, index


def markdown_to_adf(
    markdown_text: str,
    jira_base_url: str = "",
    known_project_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Convert Markdown text to ADF (Atlassian Document Format) document.

    Implements a line-by-line parser that handles common Markdown constructs.
    ``:::info|note|warning|success|error`` fences become ADF panels, ``:::tip``
    being an accepted alias for ``success``. Jira Cloud expand blocks can be
    written as ``:::expand Title ... :::`` or ``{expand:Title}...{expand}``.
    Table rows pasted as Jira wiki markup (``||header||header||``) are
    recognised alongside Markdown pipe tables. No external dependencies
    required.

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
            expand_title = expand_match.group(1) or ""
            expand_lines: list[str] = []
            i += 1
            while i < len(lines) and not re.match(r"^\{expand\}\s*$", lines[i]):
                expand_lines.append(lines[i])
                i += 1
            # Skip closing {expand}
            if i < len(lines):
                i += 1
            # Recursively parse the inner content as ADF
            inner_markdown = "\n".join(expand_lines)
            inner_doc = markdown_to_adf(inner_markdown, jira_base_url, project_keys)
            expand_node: dict[str, Any] = {
                "type": "expand",
                "attrs": {"title": expand_title},
                "content": inner_doc.get("content", []),
            }
            doc["content"].append(expand_node)
            continue

        # --- Fenced code block ---
        if line.startswith("```"):
            lang = line[3:].strip()
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            # Skip closing ```
            if i < len(lines):
                i += 1
            cb: dict[str, Any] = {
                "type": "codeBlock",
                "attrs": {"language": lang} if lang else {},
                "content": [{"type": "text", "text": "\n".join(code_lines)}],
            }
            doc["content"].append(cb)
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
                quote_lines.append(lines[i][2:])
                i += 1
            bq_content = [
                _make_paragraph(ln, jira_base_url, project_keys) for ln in quote_lines
            ]
            doc["content"].append({"type": "blockquote", "content": bq_content})
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
            inner_doc = markdown_to_adf(
                "\n".join(fence_lines), jira_base_url, project_keys
            )
            # Unlike a panel, an expand's content is left exactly as parsed.
            doc["content"].append(
                {
                    "type": "expand",
                    "attrs": {"title": fence_title},
                    "content": inner_doc["content"],
                }
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
                inner_doc = markdown_to_adf(
                    "\n".join(panel_lines), jira_base_url, project_keys
                )
                panel_node: dict[str, Any] = {
                    "type": "panel",
                    "attrs": {"panelType": panel_type},
                    "content": inner_doc["content"],
                }
                doc["content"].append(panel_node)
                continue

        # --- Unordered list ---
        if re.match(r"^[-*]\s+", line):
            items: list[dict[str, Any]] = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i]):
                item_text = re.sub(r"^[-*]\s+", "", lines[i])
                items.append(_make_list_item(item_text, jira_base_url, project_keys))
                i += 1
            doc["content"].append({"type": "bulletList", "content": items})
            continue

        # --- Ordered list ---
        if re.match(r"^\d+\.\s+", line):
            items_ol: list[dict[str, Any]] = []
            while i < len(lines):
                if re.match(r"^\d+\.\s+", lines[i]):
                    item_text = re.sub(r"^\d+\.\s+", "", lines[i])
                    items_ol.append(
                        _make_list_item(item_text, jira_base_url, project_keys)
                    )
                    i += 1
                elif (
                    not lines[i].strip()
                    and i + 1 < len(lines)
                    and re.match(r"^\d+\.\s+", lines[i + 1])
                ):
                    i += 1
                else:
                    break
            doc["content"].append({"type": "orderedList", "content": items_ol})
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
            attrs = adf_content.get("attrs", {})
            return attrs.get("text") or f"@{attrs.get('id', 'unknown')}"

        # Check if this is an emoji node
        if adf_content.get("type") == "emoji":
            attrs = adf_content.get("attrs", {})
            return attrs.get("text") or attrs.get("shortName", "")

        # Check if this is a date node
        if adf_content.get("type") == "date":
            attrs = adf_content.get("attrs", {})
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
            attrs = adf_content.get("attrs", {})
            return f"[{attrs.get('text', '')}]"

        # Check if this is an inlineCard node
        if adf_content.get("type") == "inlineCard":
            attrs = adf_content.get("attrs", {})
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
