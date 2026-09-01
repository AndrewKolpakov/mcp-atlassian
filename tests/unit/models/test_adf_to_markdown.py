"""Contract for the ADF -> Markdown read path.

``adf_to_markdown`` is the inverse of ``markdown_to_adf``: it may only emit
Markdown that function reads back, so that one read+write cycle is a fixed
point. Two kinds of test live here:

* per-construct tests, which pin the spelling each ADF node is rendered as --
  the same vocabulary the wiki read path emits (``:::info``, ``:::expand``,
  ``> ``, fenced code, pipe tables), so both read paths speak one dialect;
* the fixed-point tests at the bottom, which feed a whole document through
  ADF -> Markdown -> ADF and require the structure to come back unchanged.

The node shapes used here are the ones Jira Cloud returns on ``/rest/api/3``:
a panel carries ``panelType``, an expand carries ``title``, a task item
carries ``state``, and a table's header cells are ``tableHeader``.
"""

from typing import Any

import pytest

from mcp_atlassian.models.jira.adf import adf_to_markdown, markdown_to_adf


def _doc(*content: dict[str, Any]) -> dict[str, Any]:
    """Wrap nodes in an ADF document."""
    return {"version": 1, "type": "doc", "content": list(content)}


def _text(text: str, *marks: dict[str, Any]) -> dict[str, Any]:
    """A text node, optionally marked."""
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


def _para(*content: dict[str, Any]) -> dict[str, Any]:
    """A paragraph node."""
    return {"type": "paragraph", "content": list(content)}


def _md(node: dict[str, Any] | list[Any]) -> str:
    """Render and assert a string came back, so ``str`` flows on from here."""
    rendered = adf_to_markdown(node)
    assert rendered is not None
    return rendered


def _shape(node: dict[str, Any]) -> dict[str, Any]:
    """Reduce a node to what this contract pins.

    Keeps the type, the attrs that carry meaning, the mark types and the text.
    ``localId`` and friends are incidental and would only make a failure
    brittle.
    """
    significant = frozenset(
        {"panelType", "level", "language", "title", "state", "order"}
    )
    shape: dict[str, Any] = {"type": node.get("type")}
    attrs = {
        key: value
        for key, value in (node.get("attrs") or {}).items()
        if key in significant
    }
    if attrs:
        shape["attrs"] = attrs
    marks = [mark["type"] for mark in node.get("marks", [])]
    if marks:
        shape["marks"] = marks
    if "text" in node:
        shape["text"] = node["text"]
    content = node.get("content")
    if isinstance(content, list):
        shape["content"] = [_shape(c) for c in content if isinstance(c, dict)]
    return shape


class TestBlockConstructs:
    """One ADF block node in, one Markdown spelling out."""

    def test_headings_use_their_level(self) -> None:
        doc = _doc(
            {"type": "heading", "attrs": {"level": 2}, "content": [_text("Title")]}
        )
        assert adf_to_markdown(doc) == "## Title"

    def test_a_heading_level_out_of_range_is_clamped(self) -> None:
        """Markdown stops at six ``#``; ADF should never send more, but if it
        does the read must not emit a heading no parser recognises."""
        doc = _doc(
            {"type": "heading", "attrs": {"level": 9}, "content": [_text("Deep")]}
        )
        assert adf_to_markdown(doc) == "###### Deep"

    def test_blocks_are_separated_by_one_blank_line(self) -> None:
        doc = _doc(_para(_text("one")), _para(_text("two")))
        assert adf_to_markdown(doc) == "one\n\ntwo"

    def test_code_block_keeps_its_language_and_body_verbatim(self) -> None:
        doc = _doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [_text("def f():\n    return 1")],
            }
        )
        assert adf_to_markdown(doc) == "```python\ndef f():\n    return 1\n```"

    def test_code_block_without_a_language_opens_a_bare_fence(self) -> None:
        doc = _doc({"type": "codeBlock", "content": [_text("plain")]})
        assert adf_to_markdown(doc) == "```\nplain\n```"

    def test_rule_becomes_three_dashes(self) -> None:
        assert adf_to_markdown(_doc({"type": "rule"})) == "---"

    @pytest.mark.parametrize(
        "panel_type", ["info", "note", "warning", "success", "error"]
    )
    def test_each_panel_type_gets_its_own_fence(self, panel_type: str) -> None:
        doc = _doc(
            {
                "type": "panel",
                "attrs": {"panelType": panel_type},
                "content": [_para(_text("body"))],
            }
        )
        assert adf_to_markdown(doc) == f":::{panel_type}\nbody\n:::"

    def test_an_unknown_panel_type_still_reads_as_a_panel(self) -> None:
        """``custom`` (a hand-picked colour) has no fence of its own. Falling
        back to ``info`` keeps it a panel through a rewrite; falling back to a
        paragraph would lose the panel entirely."""
        doc = _doc(
            {
                "type": "panel",
                "attrs": {"panelType": "custom"},
                "content": [_para(_text("body"))],
            }
        )
        assert adf_to_markdown(doc) == ":::info\nbody\n:::"

    def test_expand_carries_its_title_on_the_fence_line(self) -> None:
        doc = _doc(
            {
                "type": "expand",
                "attrs": {"title": "Full trace"},
                "content": [_para(_text("hidden"))],
            }
        )
        assert adf_to_markdown(doc) == ":::expand Full trace\nhidden\n:::"

    def test_an_untitled_expand_opens_a_bare_expand_fence(self) -> None:
        doc = _doc({"type": "expand", "attrs": {}, "content": [_para(_text("x"))]})
        assert adf_to_markdown(doc) == ":::expand\nx\n:::"

    def test_nested_expand_is_spelled_like_an_expand(self) -> None:
        doc = _doc(
            {
                "type": "nestedExpand",
                "attrs": {"title": "Inner"},
                "content": [_para(_text("x"))],
            }
        )
        assert adf_to_markdown(doc) == ":::expand Inner\nx\n:::"

    def test_a_blockquote_marks_every_line_including_blank_ones(self) -> None:
        """The writer holds a quote together only while consecutive lines start
        with ``"> "``. A bare ``>`` on the blank line would end the quote and
        leave a ``>`` paragraph between two quotes."""
        doc = _doc(
            {
                "type": "blockquote",
                "content": [_para(_text("first")), _para(_text("second"))],
            }
        )
        assert adf_to_markdown(doc) == "> first\n> \n> second"

    def test_a_code_block_inside_a_quote_stays_inside_it(self) -> None:
        doc = _doc(
            {
                "type": "blockquote",
                "content": [
                    _para(_text("see:")),
                    {
                        "type": "codeBlock",
                        "attrs": {"language": "bash"},
                        "content": [_text("ls -l")],
                    },
                ],
            }
        )
        assert adf_to_markdown(doc) == "> see:\n> \n> ```bash\n> ls -l\n> ```"

    def test_layout_columns_are_flattened(self) -> None:
        """A layout has no Markdown spelling, so its columns are read one after
        another rather than dropped."""
        doc = _doc(
            {
                "type": "layoutSection",
                "content": [
                    {"type": "layoutColumn", "content": [_para(_text("left"))]},
                    {"type": "layoutColumn", "content": [_para(_text("right"))]},
                ],
            }
        )
        assert adf_to_markdown(doc) == "left\n\nright"


class TestLists:
    """Lists, including the nesting ADF expresses and Markdown indents."""

    def test_bullet_list_items_use_a_dash(self) -> None:
        doc = _doc(
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [_para(_text("a"))]},
                    {"type": "listItem", "content": [_para(_text("b"))]},
                ],
            }
        )
        assert adf_to_markdown(doc) == "- a\n- b"

    def test_ordered_list_items_are_numbered(self) -> None:
        doc = _doc(
            {
                "type": "orderedList",
                "content": [
                    {"type": "listItem", "content": [_para(_text("a"))]},
                    {"type": "listItem", "content": [_para(_text("b"))]},
                ],
            }
        )
        assert adf_to_markdown(doc) == "1. a\n2. b"

    def test_an_ordered_list_starts_where_its_order_attr_says(self) -> None:
        doc = _doc(
            {
                "type": "orderedList",
                "attrs": {"order": 5},
                "content": [{"type": "listItem", "content": [_para(_text("five"))]}],
            }
        )
        assert adf_to_markdown(doc) == "5. five"

    def test_a_nested_list_is_indented_under_its_parent_item(self) -> None:
        doc = _doc(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            _para(_text("outer")),
                            {
                                "type": "bulletList",
                                "content": [
                                    {
                                        "type": "listItem",
                                        "content": [_para(_text("inner"))],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        )
        assert adf_to_markdown(doc) == "- outer\n  - inner"

    def test_task_items_render_their_state_as_a_checkbox(self) -> None:
        doc = _doc(
            {
                "type": "taskList",
                "attrs": {"localId": "l"},
                "content": [
                    {
                        "type": "taskItem",
                        "attrs": {"localId": "1", "state": "DONE"},
                        "content": [_text("shipped")],
                    },
                    {
                        "type": "taskItem",
                        "attrs": {"localId": "2", "state": "TODO"},
                        "content": [_text("pending")],
                    },
                ],
            }
        )
        assert adf_to_markdown(doc) == "- [x] shipped\n- [ ] pending"


class TestTables:
    """A pipe table, with the header row Markdown requires."""

    @staticmethod
    def _table(*rows: list[tuple[str, str]]) -> dict[str, Any]:
        return {
            "type": "table",
            "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {"type": cell_type, "content": [_para(_text(text))]}
                        for cell_type, text in row
                    ],
                }
                for row in rows
            ],
        }

    def test_a_table_gets_a_header_separator_after_its_first_row(self) -> None:
        doc = _doc(
            self._table(
                [("tableHeader", "Env"), ("tableHeader", "Version")],
                [("tableCell", "prod"), ("tableCell", "1.2.3")],
            )
        )
        assert adf_to_markdown(doc) == (
            "| Env | Version |\n| --- | --- |\n| prod | 1.2.3 |"
        )

    def test_a_ragged_table_is_padded_to_its_widest_row(self) -> None:
        """A Markdown table has to be rectangular; ADF does not."""
        doc = _doc(
            self._table(
                [("tableHeader", "a"), ("tableHeader", "b")],
                [("tableCell", "only")],
            )
        )
        lines = _md(doc).split("\n")
        assert lines[0] == "| a | b |"
        assert lines[2] == "| only |  |"

    def test_a_multi_block_cell_is_flattened_to_one_line(self) -> None:
        """A pipe table row is one line, so a cell holding two paragraphs
        cannot keep the break."""
        doc = _doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableCell",
                                "content": [_para(_text("one")), _para(_text("two"))],
                            }
                        ],
                    }
                ],
            }
        )
        assert _md(doc).split("\n")[0] == "| one two |"

    def test_a_pipe_inside_a_cell_is_escaped(self) -> None:
        doc = _doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableCell",
                                "content": [_para(_text("a | b"))],
                            }
                        ],
                    }
                ],
            }
        )
        assert _md(doc).split("\n")[0] == "| a \\| b |"


class TestInlineNodes:
    """Marks and inline nodes."""

    @pytest.mark.parametrize(
        ("mark", "expected"),
        [
            ({"type": "strong"}, "**x**"),
            ({"type": "em"}, "*x*"),
            ({"type": "strike"}, "~~x~~"),
            ({"type": "code"}, "`x`"),
        ],
    )
    def test_each_mark_has_its_marker(
        self, mark: dict[str, Any], expected: str
    ) -> None:
        assert adf_to_markdown(_doc(_para(_text("x", mark)))) == expected

    def test_marks_nest_inside_a_link(self) -> None:
        doc = _doc(
            _para(
                _text(
                    "x",
                    {"type": "strong"},
                    {"type": "link", "attrs": {"href": "https://e.example.com"}},
                )
            )
        )
        assert adf_to_markdown(doc) == "[**x**](https://e.example.com)"

    def test_code_wins_over_emphasis_on_the_same_node(self) -> None:
        """The writer reads a backtick span verbatim and cannot nest emphasis in
        one, so ``**`x`**`` would store literal backticks. The code span is
        kept and the emphasis dropped."""
        doc = _doc(_para(_text("x", {"type": "code"}, {"type": "strong"})))
        assert adf_to_markdown(doc) == "`x`"

    def test_whitespace_moves_outside_an_emphasis_marker(self) -> None:
        """``** x **`` is not emphasis to a Markdown reader."""
        doc = _doc(_para(_text(" x ", {"type": "strong"})))
        assert adf_to_markdown(doc) == " **x** "

    def test_marks_without_a_markdown_spelling_degrade_to_text(self) -> None:
        doc = _doc(
            _para(
                _text("u", {"type": "underline"}),
                _text("c", {"type": "textColor", "attrs": {"color": "#ff0000"}}),
            )
        )
        assert adf_to_markdown(doc) == "uc"

    def test_a_mention_with_a_display_name_keeps_it(self) -> None:
        """Both spellings parse back into a mention node; this one is also
        readable, unlike the bare account id."""
        doc = _doc(
            _para({"type": "mention", "attrs": {"id": "557058:x", "text": "@Ann"}})
        )
        assert adf_to_markdown(doc) == "@[Ann](accountid:557058:x)"

    def test_a_mention_without_a_display_name_uses_the_wiki_spelling(self) -> None:
        doc = _doc(_para({"type": "mention", "attrs": {"id": "557058:x"}}))
        assert adf_to_markdown(doc) == "[~accountid:557058:x]"

    def test_a_hard_break_becomes_a_newline(self) -> None:
        doc = _doc(_para(_text("a"), {"type": "hardBreak"}, _text("b")))
        assert adf_to_markdown(doc) == "a\nb"

    def test_a_date_is_rendered_as_an_iso_day(self) -> None:
        doc = _doc(_para({"type": "date", "attrs": {"timestamp": "1767225600000"}}))
        assert adf_to_markdown(doc) == "2026-01-01"

    def test_an_emoji_uses_its_text(self) -> None:
        doc = _doc(
            _para({"type": "emoji", "attrs": {"shortName": ":smile:", "text": "😄"}})
        )
        assert adf_to_markdown(doc) == "😄"

    def test_a_status_is_rendered_as_the_token_the_writer_parses(self) -> None:
        """The escaped-bracket spelling read back as literal text, losing the
        node; the token spelling comes back as a lozenge. Followed by
        parenthesised prose it must still not re-parse as a Markdown link."""
        doc = _doc(
            _para(
                {"type": "status", "attrs": {"text": "DONE", "color": "green"}},
                _text("(x)"),
            )
        )
        assert adf_to_markdown(doc) == "{status:color=green|title=DONE}(x)"
        assert "link" not in str(markdown_to_adf(_md(doc)))

    def test_a_status_whose_title_would_break_the_token_falls_back(self) -> None:
        """A ``}`` ends the token early, so the tail would be stranded. The
        escaped-bracket form keeps the label as its own text instead."""
        doc = _doc(_para({"type": "status", "attrs": {"text": "a}b"}}))
        assert adf_to_markdown(doc) == "\\[a}b\\]"
        assert "status" not in str(markdown_to_adf(_md(doc)))

    def test_an_inline_card_with_a_name_becomes_a_link(self) -> None:
        doc = _doc(
            _para(
                {
                    "type": "inlineCard",
                    "attrs": {
                        "url": "https://e.example.com/p",
                        "data": {"name": "The page"},
                    },
                }
            )
        )
        assert adf_to_markdown(doc) == "[The page](https://e.example.com/p)"

    def test_an_inline_card_without_a_name_is_its_url(self) -> None:
        doc = _doc(
            _para({"type": "inlineCard", "attrs": {"url": "https://e.example.com/p"}})
        )
        assert adf_to_markdown(doc) == "https://e.example.com/p"

    def test_media_becomes_a_marker_naming_it(self) -> None:
        """Media cannot be rebuilt from Markdown; the write path re-attaches the
        real nodes instead. The marker therefore carries no metacharacters, so
        it survives a rewrite as prose."""
        doc = _doc(
            {
                "type": "mediaSingle",
                "attrs": {"layout": "center"},
                "content": [
                    {
                        "type": "media",
                        "attrs": {"id": "a1", "type": "file", "alt": "diagram.png"},
                    }
                ],
            }
        )
        assert adf_to_markdown(doc) == "(media: diagram.png)"


class TestEscaping:
    """Prose that happens to look like markup has to stay prose.

    Every case here is checked twice: the Markdown emitted, and the text the
    writer stores when it reads that Markdown back. The second assertion is the
    one that matters -- an escape the writer does not understand would corrupt
    the stored text instead of protecting it.
    """

    @staticmethod
    def _round_trip_text(raw: str) -> str:
        """Read one paragraph of literal text back out of the writer."""
        markdown = _md(_doc(_para(_text(raw))))
        stored = markdown_to_adf(markdown)
        block = stored["content"][0]
        return "".join(node.get("text", "") for node in block.get("content", []))

    @pytest.mark.parametrize(
        "raw",
        [
            "2 * 3 * 4",
            "a `backtick` in prose",
            "[not a link]",
            "~~not struck~~",
            "**not bold**",
            "- not a list",
            "* not a list either",
            "# not a heading",
            "1. not an ordered list",
            "> not a quote",
            "| not | a table",
            "::: not a fence",
            "--- not a rule",
            "{expand} not a macro",
            "C:\\temp\\file",
            "backslash at the end \\",
            "[~accountid:not-a-mention]",
        ],
    )
    def test_literal_text_survives_a_rewrite(self, raw: str) -> None:
        assert self._round_trip_text(raw) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "- not a list",
            "# not a heading",
            "1. not an ordered list",
            "| not | a table",
        ],
    )
    def test_block_looking_prose_stays_a_paragraph(self, raw: str) -> None:
        stored = markdown_to_adf(_md(_doc(_para(_text(raw)))))
        assert stored["content"][0]["type"] == "paragraph"

    def test_an_ordered_list_escape_lands_on_the_punctuation(self) -> None:
        """A digit is not escapable, so ``\\1.`` would keep its backslash as
        text. The backslash goes on the dot instead."""
        assert adf_to_markdown(_doc(_para(_text("1. x")))) == "1\\. x"

    def test_a_code_span_is_not_escaped_inside(self) -> None:
        """The writer reads a backtick span verbatim, so an escape inside one
        would be stored literally."""
        doc = _doc(_para(_text("a * b", {"type": "code"})))
        assert adf_to_markdown(doc) == "`a * b`"

    def test_a_code_block_body_is_not_escaped(self) -> None:
        doc = _doc(
            {
                "type": "codeBlock",
                "content": [_text("if (a && *b) { return [1]; }")],
            }
        )
        assert adf_to_markdown(doc) == "```\nif (a && *b) { return [1]; }\n```"

    def test_an_underscore_is_left_alone(self) -> None:
        """The writer does not read ``_`` as emphasis, so escaping it would only
        add noise to snake_case prose."""
        assert adf_to_markdown(_doc(_para(_text("a_b_c")))) == "a_b_c"


class TestWriterUnderstandsWhatTheReaderEmits:
    """The two additions to the writer that make the pair symmetric."""

    def test_the_writer_unescapes_punctuation(self) -> None:
        doc = markdown_to_adf("2 \\* 3")
        assert doc["content"][0]["content"] == [{"type": "text", "text": "2 * 3"}]

    def test_an_escaped_run_is_stitched_back_into_one_text_node(self) -> None:
        """An escape splits a run of prose into three nodes; only text that
        escaped something is merged, so input without an escape keeps
        converting node-for-node."""
        doc = markdown_to_adf("a \\* b \\* c")
        assert doc["content"][0]["content"] == [{"type": "text", "text": "a * b * c"}]

    def test_a_non_punctuation_backslash_is_literal(self) -> None:
        """``C:\\temp`` must keep its backslash, as CommonMark says."""
        doc = markdown_to_adf("C:\\temp")
        assert doc["content"][0]["content"][0]["text"] == "C:\\temp"

    def test_the_writer_nests_an_indented_list(self) -> None:
        doc = markdown_to_adf("- outer\n  - inner")
        outer = doc["content"][0]
        assert outer["type"] == "bulletList"
        item = outer["content"][0]
        assert [child["type"] for child in item["content"]] == [
            "paragraph",
            "bulletList",
        ]

    def test_switching_marker_kind_starts_a_new_list(self) -> None:
        doc = markdown_to_adf("- a\n1. b")
        assert [node["type"] for node in doc["content"]] == [
            "bulletList",
            "orderedList",
        ]

    def test_an_indented_marker_in_prose_is_still_a_paragraph(self) -> None:
        """Indentation only means nesting once a list is open; a marker that
        never starts at column zero has always been prose here."""
        doc = markdown_to_adf("intro\n  - indented")
        assert [node["type"] for node in doc["content"]] == ["paragraph", "paragraph"]

    def test_a_loose_ordered_list_stays_one_list(self) -> None:
        doc = markdown_to_adf("1. a\n\n2. b")
        assert [node["type"] for node in doc["content"]] == ["orderedList"]
        assert len(doc["content"][0]["content"]) == 2

    def test_a_blank_line_still_ends_a_bullet_list(self) -> None:
        doc = markdown_to_adf("- a\n\n- b")
        assert [node["type"] for node in doc["content"]] == [
            "bulletList",
            "bulletList",
        ]

    def test_a_longer_code_fence_closes_only_on_a_run_as_long(self) -> None:
        doc = markdown_to_adf("````\ncode with ``` inside\n````")
        assert [node["type"] for node in doc["content"]] == ["codeBlock"]
        assert doc["content"][0]["content"][0]["text"] == "code with ``` inside"

    def test_a_language_is_still_read_off_a_three_backtick_fence(self) -> None:
        doc = markdown_to_adf("```python\nx = 1\n```")
        assert doc["content"][0]["attrs"] == {"language": "python"}

    def test_a_fence_line_still_closes_an_equally_long_fence(self) -> None:
        """The old behaviour, unchanged: ``` closes ```."""
        doc = markdown_to_adf("```\na\n```\n\nafter")
        assert [node["type"] for node in doc["content"]] == ["codeBlock", "paragraph"]

    def test_a_nested_fence_does_not_close_its_parent(self) -> None:
        doc = markdown_to_adf(":::expand Outer\n:::info\nx\n:::\ntail\n:::")
        assert [node["type"] for node in doc["content"]] == ["expand"]
        expand = doc["content"][0]
        assert [node["type"] for node in expand["content"]] == ["panel", "paragraph"]

    def test_an_unterminated_fence_still_runs_to_the_end(self) -> None:
        """A truncated document stays convertible instead of losing its tail."""
        doc = markdown_to_adf(":::info\nbody with no closing fence")
        assert doc["content"][0]["type"] == "panel"
        assert doc["content"][0]["content"][0]["content"][0]["text"] == (
            "body with no closing fence"
        )

    def test_an_indented_continuation_joins_the_item_above(self) -> None:
        doc = markdown_to_adf("- one\n\n  two\n- three")
        items = doc["content"][0]["content"]
        assert len(items) == 2
        assert [block["type"] for block in items[0]["content"]] == [
            "paragraph",
            "paragraph",
        ]

    def test_a_blank_line_then_unindented_prose_still_ends_the_list(self) -> None:
        doc = markdown_to_adf("- one\n\nplain paragraph")
        assert [node["type"] for node in doc["content"]] == [
            "bulletList",
            "paragraph",
        ]
        assert len(doc["content"][0]["content"]) == 1

    def test_an_ordered_list_keeps_its_start_number(self) -> None:
        doc = markdown_to_adf("3. third\n4. fourth")
        assert doc["content"][0]["attrs"] == {"order": 3}

    def test_an_escaped_pipe_does_not_split_a_table_cell(self) -> None:
        doc = markdown_to_adf("| a \\| b | c |\n| --- | --- |")
        row = doc["content"][0]["content"][0]
        texts = [cell["content"][0]["content"][0]["text"] for cell in row["content"]]
        assert texts == ["a | b", "c"]

    def test_a_table_without_escapes_splits_exactly_as_before(self) -> None:
        """The naive split is kept for input with no backslash in it."""
        doc = markdown_to_adf("| a | b |\n| --- | --- |\n| c | d |")
        rows = doc["content"][0]["content"]
        assert len(rows) == 2
        assert len(rows[0]["content"]) == 2


class TestFixedPoint:
    """ADF -> Markdown -> ADF must not change the document."""

    DOCUMENT = _doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [_text("Release")]},
        _para(
            _text("See "),
            _text(
                "the ticket",
                {"type": "link", "attrs": {"href": "https://e.example.com/x?a=1"}},
            ),
            _text(" — ping "),
            {"type": "mention", "attrs": {"id": "557058:x", "text": "@Ann"}},
        ),
        _para(
            _text("bold", {"type": "strong"}),
            _text(" and "),
            _text("em", {"type": "em"}),
        ),
        {
            "type": "panel",
            "attrs": {"panelType": "warning"},
            "content": [
                _para(_text("Careful", {"type": "strong"})),
                _para(_text("Do not deploy on Friday.")),
            ],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        _para(_text("outer")),
                        {
                            "type": "bulletList",
                            "content": [
                                {"type": "listItem", "content": [_para(_text("inner"))]}
                            ],
                        },
                    ],
                },
                {"type": "listItem", "content": [_para(_text("second"))]},
            ],
        },
        {
            "type": "orderedList",
            "content": [
                {"type": "listItem", "content": [_para(_text("one"))]},
                {"type": "listItem", "content": [_para(_text("two"))]},
            ],
        },
        {
            "type": "taskList",
            "attrs": {"localId": "l"},
            "content": [
                {
                    "type": "taskItem",
                    "attrs": {"localId": "1", "state": "DONE"},
                    "content": [_text("done")],
                }
            ],
        },
        {
            "type": "blockquote",
            "content": [
                _para(_text("quoted one")),
                _para(_text("quoted two")),
                {
                    "type": "codeBlock",
                    "attrs": {"language": "bash"},
                    "content": [_text("ls -l")],
                },
            ],
        },
        {
            "type": "expand",
            "attrs": {"title": "Trace"},
            "content": [
                {
                    "type": "codeBlock",
                    "attrs": {"language": "python"},
                    "content": [_text("raise ValueError('x')")],
                }
            ],
        },
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {"type": "tableHeader", "content": [_para(_text("Env"))]},
                        {"type": "tableHeader", "content": [_para(_text("Ver"))]},
                    ],
                },
                {
                    "type": "tableRow",
                    "content": [
                        {"type": "tableCell", "content": [_para(_text("prod"))]},
                        {"type": "tableCell", "content": [_para(_text("1.2.3"))]},
                    ],
                },
            ],
        },
        {"type": "rule"},
        {
            "type": "orderedList",
            "attrs": {"order": 7},
            "content": [
                {"type": "listItem", "content": [_para(_text("seventh"))]},
            ],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        _para(_text("item with two blocks")),
                        _para(_text("its second paragraph")),
                    ],
                }
            ],
        },
        {
            "type": "blockquote",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {"type": "listItem", "content": [_para(_text("quoted item"))]}
                    ],
                }
            ],
        },
        {
            "type": "expand",
            "attrs": {"title": "Holds a panel"},
            "content": [
                {
                    "type": "panel",
                    "attrs": {"panelType": "error"},
                    "content": [_para(_text("nested panel body"))],
                },
                _para(_text("after the nested panel")),
            ],
        },
        {
            "type": "codeBlock",
            "content": [_text("outer\n```\ninner fence line\n```\nend")],
        },
        _para(
            _text("a]b", {"type": "link", "attrs": {"href": "https://e.example.com"}}),
            _text(" then "),
            _text("a*b", {"type": "strong"}),
            _text(" then "),
            _text("both", {"type": "strong"}, {"type": "em"}),
        ),
        _para(_text("- literal dash, 2 * 3, a [bracket] and a `tick`")),
    )

    def test_one_cycle_reproduces_the_same_structure(self) -> None:
        rebuilt = markdown_to_adf(_md(self.DOCUMENT))
        assert _shape(rebuilt) == _shape(self.DOCUMENT)

    def test_a_second_cycle_reproduces_the_same_markdown(self) -> None:
        """Byte stability, not just structural: the Markdown a read emits is
        what a read of the rewritten document emits."""
        first = _md(self.DOCUMENT)
        second = adf_to_markdown(markdown_to_adf(first))
        assert first == second

    def test_no_panel_in_the_rewritten_document_holds_a_forbidden_child(self) -> None:
        """Jira rejects a panel containing a blockquote or another panel with an
        opaque 400, so the read must never produce Markdown that builds one."""
        rebuilt = markdown_to_adf(_md(self.DOCUMENT))

        def walk(node: dict[str, Any]) -> None:
            children = node.get("content") or []
            if node.get("type") == "panel":
                for child in children:
                    assert child.get("type") not in ("blockquote", "panel")
            for child in children:
                if isinstance(child, dict):
                    walk(child)

        walk(rebuilt)


def _statuses(node: Any) -> list[tuple[Any, Any]]:
    """Every status node in a document, as (text, color) pairs."""
    found: list[tuple[Any, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "status":
            attrs = node.get("attrs") or {}
            found.append((attrs.get("text"), attrs.get("color")))
        for value in node.values():
            found.extend(_statuses(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_statuses(item))
    return found


def _status(text: str, color: Any = None) -> dict[str, Any]:
    attrs: dict[str, Any] = {"text": text, "style": ""}
    if color is not None:
        attrs["color"] = color
    return {"type": "status", "attrs": attrs}


class TestStatusRoundTrip:
    """The reader and the writer must agree on the status lozenge.

    Upstream #1624 taught ``markdown_to_adf`` to read
    ``{status:color=X|title=Y}`` while the reader still emitted ``\\[Y\\]``.
    That left the pair disagreeing in both directions: a real lozenge was lost
    on the way out, and prose that merely contained the token was turned into a
    lozenge on the way back in.
    """

    @pytest.mark.parametrize(
        "color", ["neutral", "purple", "blue", "red", "yellow", "green"]
    )
    def test_a_lozenge_keeps_its_color_across_a_cycle(self, color: str) -> None:
        doc = _doc(_para(_status("Done", color)))
        assert _statuses(markdown_to_adf(_md(doc))) == [("Done", color)]

    @pytest.mark.parametrize("color", ["lime", None, 42, "", "GREEN"])
    def test_an_unusable_color_costs_the_color_not_the_lozenge(
        self, color: Any
    ) -> None:
        """The writer coerces an unknown color to neutral, so the reader does
        too rather than falling back and dropping the node."""
        expected = "green" if color == "GREEN" else "neutral"
        doc = _doc(_para(_status("Done", color)))
        assert _statuses(markdown_to_adf(_md(doc))) == [("Done", expected)]

    @pytest.mark.parametrize(
        "title",
        ["a|b", "color=red|title=x", "*bold* [x]", "snake_case", "a`b`c", "100%"],
    )
    def test_an_awkward_title_still_round_trips(self, title: str) -> None:
        doc = _doc(_para(_status(title, "green")))
        assert _statuses(markdown_to_adf(_md(doc))) == [(title, "green")]

    @pytest.mark.parametrize("title", ["a}b", "", "a\nb"])
    def test_a_title_the_token_cannot_carry_falls_back(self, title: str) -> None:
        """Better a lost lozenge than a corrupted document: none of these may
        produce a status node with the wrong label."""
        doc = _doc(_para(_status(title, "green")))
        assert _statuses(markdown_to_adf(_md(doc))) == []

    @pytest.mark.parametrize(
        "prose",
        [
            "config uses {status:title=Done} as a sentinel",
            "see {status:color=green|title=Done} in the docs",
            "plain {braces} and {status:nope} stay alone",
        ],
    )
    def test_prose_that_looks_like_a_token_is_not_turned_into_a_lozenge(
        self, prose: str
    ) -> None:
        """The regression the merge of upstream #1624 introduced: the writer
        read a token the reader did not escape."""
        doc = _doc(_para(_text(prose)))
        back = markdown_to_adf(_md(doc))
        assert _statuses(back) == []
        assert back["content"][0]["content"] == [{"type": "text", "text": prose}]

    def test_a_lozenge_in_a_table_cell_survives_without_its_color(self) -> None:
        """The color form separates its fields with ``|``, which a cell escapes
        as ``\\|`` -- a spelling the writer does not read. The title-only form
        keeps the node, which the escaped-bracket spelling did not."""
        doc = _doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [_para(_text("h"))]}
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableCell",
                                "content": [_para(_status("Done", "green"))],
                            }
                        ],
                    },
                ],
            }
        )
        assert "{status:title=Done}" in _md(doc)
        assert _statuses(markdown_to_adf(_md(doc))) == [("Done", "neutral")]

    def test_token_shaped_prose_in_a_cell_keeps_its_color_text(self) -> None:
        """The cell drops a real lozenge's colour, but prose that merely looks
        like one is escaped, not markup -- rewriting it would edit the author's
        text, which is the corruption the escape exists to prevent."""
        prose = "see {status:color=green|title=Done} in docs"
        doc = _doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [_para(_text("h"))]}
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [_para(_text(prose))]}
                        ],
                    },
                ],
            }
        )
        back = markdown_to_adf(_md(doc))
        cell = back["content"][0]["content"][1]["content"][0]["content"][0]["content"]
        assert "".join(node.get("text", "") for node in cell) == prose
        assert _statuses(back) == []

    def test_the_escape_only_fires_on_a_complete_token(self) -> None:
        """Escaping every brace would add noise to JSON-ish prose."""
        doc = _doc(_para(_text('{"a": 1} and {status:} and {status:title=x}')))
        assert _md(doc) == '{"a": 1} and {status:} and \\{status:title=x}'


class TestDegenerateInput:
    """The read path is on every issue fetch, so it cannot raise."""

    def test_none_stays_none(self) -> None:
        assert adf_to_markdown(None) is None

    def test_a_string_passes_through(self) -> None:
        assert adf_to_markdown("already text") == "already text"

    def test_an_empty_document_is_the_empty_string(self) -> None:
        assert adf_to_markdown(_doc()) == ""

    def test_an_empty_paragraph_is_the_empty_string(self) -> None:
        assert adf_to_markdown(_doc(_para())) == ""

    def test_content_that_is_not_a_list_is_tolerated(self) -> None:
        assert adf_to_markdown({"type": "doc", "content": "oops"}) == ""

    def test_a_non_dict_child_is_skipped(self) -> None:
        doc = {"type": "doc", "content": [None, _para(_text("kept")), 7]}
        assert adf_to_markdown(doc) == "kept"

    def test_an_unknown_node_wrapping_text_keeps_the_text(self) -> None:
        doc = _doc({"type": "somethingNew", "content": [_text("still readable")]})
        assert adf_to_markdown(doc) == "still readable"

    def test_an_unknown_leaf_contributes_nothing(self) -> None:
        assert adf_to_markdown(_doc({"type": "somethingNew"}, _para(_text("x")))) == "x"

    def test_a_bare_node_is_rendered_on_its_own(self) -> None:
        assert adf_to_markdown(_para(_text("loose"))) == "loose"

    def test_a_content_list_is_rendered_as_blocks(self) -> None:
        assert adf_to_markdown([_para(_text("a")), _para(_text("b"))]) == "a\n\nb"

    def test_a_malformed_date_falls_back_to_its_timestamp(self) -> None:
        doc = _doc(_para({"type": "date", "attrs": {"timestamp": "not-a-number"}}))
        assert adf_to_markdown(doc) == "not-a-number"

    def test_a_malformed_heading_level_falls_back_to_one(self) -> None:
        doc = _doc(
            {"type": "heading", "attrs": {"level": "x"}, "content": [_text("t")]}
        )
        assert adf_to_markdown(doc) == "# t"

    def test_a_malformed_list_order_falls_back_to_one(self) -> None:
        doc = _doc(
            {
                "type": "orderedList",
                "attrs": {"order": "x"},
                "content": [{"type": "listItem", "content": [_para(_text("a"))]}],
            }
        )
        assert adf_to_markdown(doc) == "1. a"


class TestFindingsFromReview:
    """Regressions for the round-trip defects a review of this change found.

    Each one was reproduced before it was fixed: the ADF listed here went out
    as Markdown the writer then read back as something else. They are grouped
    together because they share a cause -- the reader emitting a construct the
    writer could not parse -- and because that is the class of bug this pair
    has to stay free of.
    """

    @staticmethod
    def _cycle(node: dict[str, Any]) -> dict[str, Any]:
        """One read+write cycle over a single top-level node."""
        return markdown_to_adf(_md(_doc(node)))

    def test_an_escape_inside_a_bold_span_is_not_stored_literally(self) -> None:
        """The reader escapes before wrapping marks, so the writer has to parse
        a span's body rather than take it verbatim -- otherwise bold ``a*b``
        came back as bold ``a\\*b``."""
        rebuilt = self._cycle(_para(_text("a*b", {"type": "strong"})))
        assert rebuilt["content"][0]["content"] == [
            {"type": "text", "text": "a*b", "marks": [{"type": "strong"}]}
        ]

    def test_a_node_marked_bold_and_italic_keeps_both(self) -> None:
        """``***x***`` has to be read as one triple marker. Read as bold first,
        it became bold ``*x`` plus a stray ``*``."""
        rebuilt = self._cycle(_para(_text("x", {"type": "strong"}, {"type": "em"})))
        (node,) = rebuilt["content"][0]["content"]
        assert node["text"] == "x"
        assert {mark["type"] for mark in node["marks"]} == {"strong", "em"}

    def test_a_list_item_keeps_its_second_paragraph(self) -> None:
        """An ADF listItem holds one or more blocks. The continuation used to
        fall out of the list and become a top-level paragraph of ``"  two"``."""
        rebuilt = self._cycle(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [_para(_text("one")), _para(_text("two"))],
                    }
                ],
            }
        )
        assert [node["type"] for node in rebuilt["content"]] == ["bulletList"]
        item = rebuilt["content"][0]["content"][0]
        assert [block["type"] for block in item["content"]] == [
            "paragraph",
            "paragraph",
        ]
        assert item["content"][1]["content"][0]["text"] == "two"

    def test_a_list_item_keeps_a_code_block(self) -> None:
        """Indentation inside a continuation code block has to survive too."""
        rebuilt = self._cycle(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            _para(_text("run this")),
                            {
                                "type": "codeBlock",
                                "attrs": {"language": "python"},
                                "content": [_text("if x:\n    go()")],
                            },
                        ],
                    }
                ],
            }
        )
        item = rebuilt["content"][0]["content"][0]
        code = item["content"][1]
        assert code["type"] == "codeBlock"
        assert code["content"][0]["text"] == "if x:\n    go()"

    def test_a_list_inside_a_blockquote_stays_a_list(self) -> None:
        """Left as a paragraph, ``- x`` was stored as literal text."""
        rebuilt = self._cycle(
            {
                "type": "blockquote",
                "content": [
                    {
                        "type": "bulletList",
                        "content": [
                            {"type": "listItem", "content": [_para(_text("x"))]}
                        ],
                    }
                ],
            }
        )
        quote = rebuilt["content"][0]
        assert quote["type"] == "blockquote"
        assert [node["type"] for node in quote["content"]] == ["bulletList"]

    def test_an_ordered_list_keeps_its_starting_number(self) -> None:
        """``order`` is the first item's number in ADF; dropping it renumbered
        the list from 1 on the next read."""
        rebuilt = self._cycle(
            {
                "type": "orderedList",
                "attrs": {"order": 5},
                "content": [{"type": "listItem", "content": [_para(_text("five"))]}],
            }
        )
        assert rebuilt["content"][0]["attrs"]["order"] == 5

    def test_an_ordered_list_starting_at_one_carries_no_order_attr(self) -> None:
        """The common case stays byte-identical to what it was before."""
        rebuilt = self._cycle(
            {
                "type": "orderedList",
                "content": [{"type": "listItem", "content": [_para(_text("one"))]}],
            }
        )
        assert "attrs" not in rebuilt["content"][0]

    def test_a_code_block_containing_a_fence_line_survives(self) -> None:
        """A ``` line in the body closed a three-backtick fence, splitting one
        code block into two and a paragraph. The reader opens a longer fence."""
        node = {"type": "codeBlock", "content": [_text("before\n```\nafter")]}
        markdown = _md(_doc(node))
        assert markdown.startswith("````")
        rebuilt = markdown_to_adf(markdown)
        assert [block["type"] for block in rebuilt["content"]] == ["codeBlock"]
        assert rebuilt["content"][0]["content"][0]["text"] == "before\n```\nafter"

    def test_a_panel_inside_an_expand_does_not_leak_a_fence(self) -> None:
        """Both delimiters are spelled ``:::``, so the inner one used to close
        the expand and the outer one became a paragraph of literal ``:::``."""
        rebuilt = self._cycle(
            {
                "type": "expand",
                "attrs": {"title": "Outer"},
                "content": [
                    {
                        "type": "panel",
                        "attrs": {"panelType": "info"},
                        "content": [_para(_text("inner"))],
                    },
                    _para(_text("after panel")),
                ],
            }
        )
        assert [node["type"] for node in rebuilt["content"]] == ["expand"]
        expand = rebuilt["content"][0]
        assert [node["type"] for node in expand["content"]] == ["panel", "paragraph"]
        assert ":::" not in str(rebuilt)

    def test_a_block_after_a_nested_list_stays_in_its_item(self) -> None:
        """The continuation scan has to run again after a sub-list, or the
        trailing paragraph falls out of the list entirely."""
        rebuilt = self._cycle(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            _para(_text("a")),
                            {
                                "type": "bulletList",
                                "content": [
                                    {"type": "listItem", "content": [_para(_text("n"))]}
                                ],
                            },
                            _para(_text("tail")),
                        ],
                    }
                ],
            }
        )
        assert [node["type"] for node in rebuilt["content"]] == ["bulletList"]
        item = rebuilt["content"][0]["content"][0]
        assert [block["type"] for block in item["content"]] == [
            "paragraph",
            "bulletList",
            "paragraph",
        ]
        assert item["content"][2]["content"][0]["text"] == "tail"

    def test_a_nested_task_list_is_flattened_not_indented(self) -> None:
        """An indented checkbox is not a checkbox to the writer, so nesting is
        given up to keep the checkboxes. Both items survive as tasks."""
        node = {
            "type": "taskList",
            "attrs": {"localId": "a"},
            "content": [
                {
                    "type": "taskItem",
                    "attrs": {"localId": "1", "state": "TODO"},
                    "content": [_text("outer")],
                },
                {
                    "type": "taskList",
                    "attrs": {"localId": "b"},
                    "content": [
                        {
                            "type": "taskItem",
                            "attrs": {"localId": "2", "state": "DONE"},
                            "content": [_text("inner")],
                        }
                    ],
                },
            ],
        }
        assert _md(_doc(node)) == "- [ ] outer\n- [x] inner"
        rebuilt = self._cycle(node)
        assert [n["type"] for n in rebuilt["content"]] == ["taskList"]
        states = [item["attrs"]["state"] for item in rebuilt["content"][0]["content"]]
        assert states == ["TODO", "DONE"]

    def test_a_link_label_containing_a_bracket_stays_a_link(self) -> None:
        """An unescaped ``]`` ended the label early, so the whole spelling fell
        out of the pattern and became literal text with the link mark lost."""
        rebuilt = self._cycle(
            _para(
                _text(
                    "a]b",
                    {"type": "link", "attrs": {"href": "https://e.example.com"}},
                )
            )
        )
        (node,) = rebuilt["content"][0]["content"]
        assert node["text"] == "a]b"
        assert node["marks"][0]["type"] == "link"


class TestMalformedAdfDoesNotRaise:
    """Every one of these raised before; the read path is on every issue fetch."""

    @pytest.mark.parametrize(
        ("label", "node"),
        [
            ("marks is null", _para({"type": "text", "text": "x", "marks": None})),
            ("marks is a string", _para({"type": "text", "text": "x", "marks": "b"})),
            (
                "panel attrs is a string",
                {"type": "panel", "attrs": "oops", "content": [_para(_text("x"))]},
            ),
            (
                "code block attrs is a string",
                {"type": "codeBlock", "attrs": "oops", "content": [_text("x")]},
            ),
            (
                "heading attrs is a string",
                {"type": "heading", "attrs": "oops", "content": [_text("x")]},
            ),
            (
                "ordered list attrs is a string",
                {"type": "orderedList", "attrs": "oops", "content": []},
            ),
            ("mention attrs is a string", _para({"type": "mention", "attrs": "oops"})),
            ("expand attrs is a string", {"type": "expand", "attrs": "oops"}),
            (
                "task item attrs is a string",
                {
                    "type": "taskList",
                    "content": [{"type": "taskItem", "attrs": "oops", "content": []}],
                },
            ),
            ("date attrs is a string", _para({"type": "date", "attrs": "oops"})),
            ("media attrs is a string", {"type": "media", "attrs": "oops"}),
            (
                "inline card attrs is a string",
                _para({"type": "inlineCard", "attrs": 1}),
            ),
            ("status attrs is a string", _para({"type": "status", "attrs": "oops"})),
            ("emoji attrs is a string", _para({"type": "emoji", "attrs": "oops"})),
            ("text is not a string", _para({"type": "text", "text": 7})),
        ],
    )
    def test_it_degrades_instead_of_raising(
        self, label: str, node: dict[str, Any]
    ) -> None:
        assert isinstance(adf_to_markdown(_doc(node)), str), label
