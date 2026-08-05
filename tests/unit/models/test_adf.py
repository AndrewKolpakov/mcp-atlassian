"""
Tests for the ADF (Atlassian Document Format) parser.

These tests validate the conversion of ADF content to plain text,
including handling of various inline and block node types,
and the reverse conversion from Markdown to ADF.
"""

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.mcp_atlassian.models.jira.adf import (
    _flatten_panel_content,
    adf_to_text,
    extract_top_level_media_nodes,
    markdown_to_adf,
    merge_adf_with_preserved_media,
)


class TestAdfToText:
    """Tests for the adf_to_text function."""

    # Basic input handling

    def test_none_input(self):
        """Test that None input returns None."""
        assert adf_to_text(None) is None

    def test_string_input(self):
        """Test that string input is returned as-is."""
        assert adf_to_text("plain text") == "plain text"

    def test_empty_dict(self):
        """Test that empty dict returns None."""
        assert adf_to_text({}) is None

    def test_empty_list(self):
        """Test that empty list returns None."""
        assert adf_to_text([]) is None

    # Text node tests

    def test_text_node(self):
        """Test basic text node extraction."""
        node = {"type": "text", "text": "Hello, World!"}
        assert adf_to_text(node) == "Hello, World!"

    def test_text_node_empty(self):
        """Test text node with empty text."""
        node = {"type": "text", "text": ""}
        assert adf_to_text(node) == ""

    def test_text_node_missing_text(self):
        """Test text node without text field."""
        node = {"type": "text"}
        assert adf_to_text(node) == ""

    # hardBreak node tests

    def test_hard_break_node(self):
        """Test hardBreak node returns newline."""
        node = {"type": "hardBreak"}
        assert adf_to_text(node) == "\n"

    # Mention node tests

    def test_mention_with_text(self):
        """Test mention node with text attribute."""
        node = {
            "type": "mention",
            "attrs": {"id": "user123", "text": "@John Doe", "userType": "DEFAULT"},
        }
        assert adf_to_text(node) == "@John Doe"

    def test_mention_without_text(self):
        """Test mention node falls back to id."""
        node = {"type": "mention", "attrs": {"id": "user123"}}
        assert adf_to_text(node) == "@user123"

    def test_mention_without_attrs(self):
        """Test mention node with missing attrs."""
        node = {"type": "mention"}
        assert adf_to_text(node) == "@unknown"

    # Emoji node tests

    def test_emoji_with_text(self):
        """Test emoji node with unicode text."""
        node = {
            "type": "emoji",
            "attrs": {"shortName": ":smile:", "text": "😄"},
        }
        assert adf_to_text(node) == "😄"

    def test_emoji_without_text(self):
        """Test emoji node falls back to shortName."""
        node = {"type": "emoji", "attrs": {"shortName": ":custom_emoji:"}}
        assert adf_to_text(node) == ":custom_emoji:"

    def test_emoji_without_attrs(self):
        """Test emoji node with missing attrs."""
        node = {"type": "emoji"}
        assert adf_to_text(node) == ""

    # Date node tests

    def test_date_node(self):
        """Test date node formats timestamp correctly."""
        # 1582152559000 = 2020-02-19 21:49:19 UTC
        node = {"type": "date", "attrs": {"timestamp": "1582152559000"}}
        assert adf_to_text(node) == "2020-02-19"

    def test_date_node_integer_timestamp(self):
        """Test date node with integer timestamp."""
        node = {"type": "date", "attrs": {"timestamp": 1582152559000}}
        assert adf_to_text(node) == "2020-02-19"

    def test_date_node_invalid_timestamp(self):
        """Test date node with invalid timestamp returns raw value."""
        node = {"type": "date", "attrs": {"timestamp": "not-a-number"}}
        assert adf_to_text(node) == "not-a-number"

    def test_date_node_missing_timestamp(self):
        """Test date node without timestamp."""
        node = {"type": "date", "attrs": {}}
        assert adf_to_text(node) == ""

    def test_date_node_without_attrs(self):
        """Test date node with missing attrs."""
        node = {"type": "date"}
        assert adf_to_text(node) == ""

    def test_date_node_overflow_timestamp(self):
        """Regression test for #1033: overflow timestamps must not crash.

        On Windows, datetime.fromtimestamp raises OverflowError for sentinel
        timestamps (year 9999). adf_to_text should fall back to the raw string.
        """
        node = {"type": "date", "attrs": {"timestamp": "253402300799000"}}
        mock_dt = MagicMock()
        mock_dt.fromtimestamp.side_effect = OverflowError(
            "timestamp too large to convert to C _PyTime_t"
        )
        with patch("src.mcp_atlassian.models.jira.adf.datetime", mock_dt):
            result = adf_to_text(node)
            assert result == "253402300799000"

    # Status node tests

    def test_status_node(self):
        """Test status node wraps text in brackets."""
        node = {
            "type": "status",
            "attrs": {"text": "In Progress", "color": "yellow"},
        }
        assert adf_to_text(node) == "[In Progress]"

    def test_status_node_empty_text(self):
        """Test status node with empty text."""
        node = {"type": "status", "attrs": {"text": "", "color": "neutral"}}
        assert adf_to_text(node) == "[]"

    def test_status_node_without_attrs(self):
        """Test status node with missing attrs."""
        node = {"type": "status"}
        assert adf_to_text(node) == "[]"

    # inlineCard node tests

    def test_inline_card_with_url(self):
        """Test inlineCard node extracts URL."""
        node = {"type": "inlineCard", "attrs": {"url": "https://example.com"}}
        assert adf_to_text(node) == "https://example.com"

    def test_inline_card_with_data_url(self):
        """Test inlineCard node extracts URL from data."""
        node = {
            "type": "inlineCard",
            "attrs": {"data": {"url": "https://jira.example.com/issue/PROJ-123"}},
        }
        assert adf_to_text(node) == "https://jira.example.com/issue/PROJ-123"

    def test_inline_card_with_data_name(self):
        """Test inlineCard node falls back to name from data."""
        node = {
            "type": "inlineCard",
            "attrs": {"data": {"name": "PROJ-123: Fix bug"}},
        }
        assert adf_to_text(node) == "PROJ-123: Fix bug"

    def test_inline_card_empty(self):
        """Test inlineCard node with no data."""
        node = {"type": "inlineCard", "attrs": {}}
        assert adf_to_text(node) == ""

    def test_inline_card_without_attrs(self):
        """Test inlineCard node with missing attrs."""
        node = {"type": "inlineCard"}
        assert adf_to_text(node) == ""

    # codeBlock node tests

    def test_code_block(self):
        """Test codeBlock node wraps content in backticks."""
        node = {
            "type": "codeBlock",
            "attrs": {"language": "python"},
            "content": [{"type": "text", "text": "print('hello')"}],
        }
        assert adf_to_text(node) == "```\nprint('hello')\n```"

    def test_code_block_multiline(self):
        """Test codeBlock node with multiline content."""
        node = {
            "type": "codeBlock",
            "content": [{"type": "text", "text": "line1\nline2\nline3"}],
        }
        assert adf_to_text(node) == "```\nline1\nline2\nline3\n```"

    def test_code_block_empty(self):
        """Test codeBlock node with no content."""
        node = {"type": "codeBlock", "content": []}
        assert adf_to_text(node) == "```\n\n```"

    def test_code_block_without_content(self):
        """Test codeBlock node without content field."""
        node = {"type": "codeBlock"}
        assert adf_to_text(node) == "```\n\n```"

    # Nested content tests

    def test_paragraph_with_text(self):
        """Test paragraph node with nested text."""
        node = {
            "type": "paragraph",
            "content": [{"type": "text", "text": "Hello, World!"}],
        }
        assert adf_to_text(node) == "Hello, World!"

    def test_document_with_paragraphs(self):
        """Test full document structure."""
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "First"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Second"}]},
            ],
        }
        assert adf_to_text(doc) == "First\nSecond"

    def test_paragraph_with_mixed_content(self):
        """Test paragraph with text, mention, and emoji."""
        node = {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "mention", "attrs": {"id": "123", "text": "@John"}},
                {"type": "text", "text": " "},
                {"type": "emoji", "attrs": {"shortName": ":wave:", "text": "👋"}},
            ],
        }
        assert adf_to_text(node) == "Hello \n@John\n \n👋"

    def test_list_of_text_nodes(self):
        """Test list of text nodes joins with newlines."""
        nodes = [
            {"type": "text", "text": "Line 1"},
            {"type": "text", "text": "Line 2"},
        ]
        assert adf_to_text(nodes) == "Line 1\nLine 2"

    # Edge cases

    def test_unknown_node_type(self):
        """Test unknown node type without content returns None."""
        node = {"type": "unknownNode"}
        assert adf_to_text(node) is None

    def test_unknown_node_with_content(self):
        """Test unknown node type with content processes recursively."""
        node = {
            "type": "unknownNode",
            "content": [{"type": "text", "text": "nested text"}],
        }
        assert adf_to_text(node) == "nested text"

    def test_deeply_nested_content(self):
        """Test deeply nested ADF structure."""
        node = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item 1"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        assert adf_to_text(node) == "Item 1"


class TestMarkdownToAdf:
    """Tests for the markdown_to_adf function."""

    def _assert_valid_adf(self, result: dict) -> None:
        """Helper: assert the result is a valid ADF document."""
        assert result["version"] == 1
        assert result["type"] == "doc"
        assert isinstance(result["content"], list)

    # -- Structure -----------------------------------------------------------

    def test_structure(self):
        """Any input always produces version:1, type:doc, content:[...]."""
        result = markdown_to_adf("anything")
        self._assert_valid_adf(result)

    # -- Empty / whitespace -------------------------------------------------

    def test_empty_string(self):
        """Empty string produces a minimal ADF doc with an empty paragraph."""
        result = markdown_to_adf("")
        self._assert_valid_adf(result)
        assert len(result["content"]) >= 1
        assert result["content"][0]["type"] == "paragraph"

    # -- Paragraphs ---------------------------------------------------------

    def test_simple_paragraph(self):
        """Plain text becomes a paragraph with a text node."""
        result = markdown_to_adf("Hello world")
        self._assert_valid_adf(result)
        para = result["content"][0]
        assert para["type"] == "paragraph"
        texts = [n["text"] for n in para["content"] if n["type"] == "text"]
        assert "Hello world" in " ".join(texts)

    # -- Headings -----------------------------------------------------------

    @pytest.mark.parametrize(
        "md, level",
        [
            ("# H1", 1),
            ("## H2", 2),
            ("### H3", 3),
            ("#### H4", 4),
            ("##### H5", 5),
            ("###### H6", 6),
        ],
        ids=[f"heading_h{i}" for i in range(1, 7)],
    )
    def test_headings(self, md: str, level: int):
        """Headings produce heading nodes with the correct level attr."""
        result = markdown_to_adf(md)
        heading = result["content"][0]
        assert heading["type"] == "heading"
        assert heading["attrs"]["level"] == level

    # -- Inline formatting --------------------------------------------------

    def test_bold(self):
        """**bold** text gets a strong mark."""
        result = markdown_to_adf("**bold**")
        para = result["content"][0]
        bold_nodes = [
            n
            for n in para["content"]
            if n["type"] == "text"
            and any(m["type"] == "strong" for m in n.get("marks", []))
        ]
        assert len(bold_nodes) >= 1
        assert bold_nodes[0]["text"] == "bold"

    def test_italic(self):
        """*italic* text gets an em mark."""
        result = markdown_to_adf("*italic*")
        para = result["content"][0]
        italic_nodes = [
            n
            for n in para["content"]
            if n["type"] == "text"
            and any(m["type"] == "em" for m in n.get("marks", []))
        ]
        assert len(italic_nodes) >= 1
        assert italic_nodes[0]["text"] == "italic"

    def test_inline_code(self):
        """`code` text gets a code mark."""
        result = markdown_to_adf("`code`")
        para = result["content"][0]
        code_nodes = [
            n
            for n in para["content"]
            if n["type"] == "text"
            and any(m["type"] == "code" for m in n.get("marks", []))
        ]
        assert len(code_nodes) >= 1
        assert code_nodes[0]["text"] == "code"

    def test_strikethrough(self):
        """~~strike~~ text gets a strike mark."""
        result = markdown_to_adf("~~strike~~")
        para = result["content"][0]
        strike_nodes = [
            n
            for n in para["content"]
            if n["type"] == "text"
            and any(m["type"] == "strike" for m in n.get("marks", []))
        ]
        assert len(strike_nodes) >= 1
        assert strike_nodes[0]["text"] == "strike"

    # -- Links --------------------------------------------------------------

    def test_link(self):
        """[text](url) produces a text node with a link mark."""
        result = markdown_to_adf("[click here](https://example.com)")
        para = result["content"][0]
        link_nodes = [
            n
            for n in para["content"]
            if n["type"] == "text"
            and any(m["type"] == "link" for m in n.get("marks", []))
        ]
        assert len(link_nodes) >= 1
        assert link_nodes[0]["text"] == "click here"
        link_mark = next(m for m in link_nodes[0]["marks"] if m["type"] == "link")
        assert link_mark["attrs"]["href"] == "https://example.com"

    def test_link_without_inline_formatting_stays_one_node(self) -> None:
        """Unformatted link text stays a single text node with one link mark."""
        result = markdown_to_adf("[click here](https://example.com)")

        assert result["content"][0]["content"] == [
            {
                "type": "text",
                "text": "click here",
                "marks": [{"type": "link", "attrs": {"href": "https://example.com"}}],
            }
        ]

    def test_link_text_inline_formatting_becomes_nested_marks(self) -> None:
        """Markup inside link text becomes marks, not literal characters."""
        href = "https://example.com/x"
        result = markdown_to_adf(f"See [**bold** and `code` link]({href}) here")

        assert result["content"][0]["content"] == [
            {"type": "text", "text": "See "},
            {
                "type": "text",
                "text": "bold",
                "marks": [
                    {"type": "strong"},
                    {"type": "link", "attrs": {"href": href}},
                ],
            },
            {
                "type": "text",
                "text": " and ",
                "marks": [{"type": "link", "attrs": {"href": href}}],
            },
            {
                "type": "text",
                "text": "code",
                "marks": [
                    {"type": "code"},
                    {"type": "link", "attrs": {"href": href}},
                ],
            },
            {
                "type": "text",
                "text": " link",
                "marks": [{"type": "link", "attrs": {"href": href}}],
            },
            {"type": "text", "text": " here"},
        ]

    def test_link_text_bold_keeps_inner_asterisk(self) -> None:
        """Bold link text is unwrapped without leaking its asterisks."""
        result = markdown_to_adf("[**a*b**](u)")

        assert result["content"][0]["content"] == [
            {
                "type": "text",
                "text": "a*b",
                "marks": [
                    {"type": "strong"},
                    {"type": "link", "attrs": {"href": "u"}},
                ],
            }
        ]

    def test_link_text_strike_and_code_nest_under_link(self) -> None:
        """Every segment of a formatted link text keeps the link mark."""
        result = markdown_to_adf("[~~s~~ and `c`](u)")
        content = result["content"][0]["content"]

        assert [n["text"] for n in content] == ["s", " and ", "c"]
        assert [[m["type"] for m in n["marks"]] for n in content] == [
            ["strike", "link"],
            ["link"],
            ["code", "link"],
        ]
        assert all(
            next(m for m in n["marks"] if m["type"] == "link")["attrs"]["href"] == "u"
            for n in content
        )

    def test_link_text_italic_nests_em_mark(self) -> None:
        """Italic link text yields an em mark beside the link mark."""
        result = markdown_to_adf("[*i*](https://example.com)")

        assert result["content"][0]["content"] == [
            {
                "type": "text",
                "text": "i",
                "marks": [
                    {"type": "em"},
                    {"type": "link", "attrs": {"href": "https://example.com"}},
                ],
            }
        ]

    def test_link_text_issue_key_keeps_a_single_link_mark(self) -> None:
        """Issue keys inside link text do not add a second link mark."""
        result = markdown_to_adf(
            "[PROJ-123 details](https://example.com/x)",
            jira_base_url="https://jira.example.com",
        )
        content = result["content"][0]["content"]

        for node in content:
            link_marks = [m for m in node.get("marks", []) if m["type"] == "link"]
            assert len(link_marks) == 1
            assert link_marks[0]["attrs"]["href"] == "https://example.com/x"

    def test_link_marks_are_not_shared_between_nodes(self) -> None:
        """Each node gets its own link mark object, so edits stay local."""
        result = markdown_to_adf("[**b** t](https://example.com)")
        content = result["content"][0]["content"]
        first_link = next(m for m in content[0]["marks"] if m["type"] == "link")
        first_link["attrs"]["href"] = "https://mutated.example.com"

        second_link = next(m for m in content[1]["marks"] if m["type"] == "link")
        assert second_link["attrs"]["href"] == "https://example.com"

    def test_link_text_formatting_inside_list_item(self) -> None:
        """List items share the inline parser, so nested marks work there too."""
        result = markdown_to_adf("- see [**bold** link](https://example.com)")
        bullet_list = next(n for n in result["content"] if n["type"] == "bulletList")
        paragraph = bullet_list["content"][0]["content"][0]

        assert paragraph["content"] == [
            {"type": "text", "text": "see "},
            {
                "type": "text",
                "text": "bold",
                "marks": [
                    {"type": "strong"},
                    {"type": "link", "attrs": {"href": "https://example.com"}},
                ],
            },
            {
                "type": "text",
                "text": " link",
                "marks": [{"type": "link", "attrs": {"href": "https://example.com"}}],
            },
        ]

    # -- Mentions -----------------------------------------------------------

    def test_mention_modern_account_id(self):
        """[~accountid:712020:UUID] emits an ADF mention node with id attr."""
        account_id = "712020:1cfc6d16-950f-4096-8e57-f2c6c60d8ffa"
        result = markdown_to_adf(f"[~accountid:{account_id}]")
        para = result["content"][0]
        mentions = [n for n in para["content"] if n["type"] == "mention"]
        assert len(mentions) == 1
        assert mentions[0]["attrs"]["id"] == account_id

    def test_mention_legacy_account_id(self):
        """[~accountid:24-hex] (no 712020: prefix) is also accepted."""
        account_id = "6315cc7b3310c2492b5b1513"
        result = markdown_to_adf(f"[~accountid:{account_id}]")
        para = result["content"][0]
        mentions = [n for n in para["content"] if n["type"] == "mention"]
        assert len(mentions) == 1
        assert mentions[0]["attrs"]["id"] == account_id

    def test_mention_display_name_account_id(self) -> None:
        """@[Name](accountid:...) emits an ADF mention node with text attr."""
        account_id = "712020:abc-123-def-456"
        result = markdown_to_adf(f"@[John Doe](accountid:{account_id})")
        para = result["content"][0]
        mentions = [n for n in para["content"] if n["type"] == "mention"]
        assert len(mentions) == 1
        assert mentions[0]["attrs"] == {
            "id": account_id,
            "text": "@John Doe",
        }

    def test_mention_display_name_inline_with_link(self) -> None:
        """Display-name mentions coexist with normal Markdown links."""
        account_id = "712020:abc-def"
        result = markdown_to_adf(
            f"Ask @[Jane Doe](accountid:{account_id}) via "
            "[the runbook](https://example.com)."
        )
        para = result["content"][0]
        mentions = [n for n in para["content"] if n["type"] == "mention"]
        links = [
            n
            for n in para["content"]
            if n["type"] == "text"
            and any(m["type"] == "link" for m in n.get("marks", []))
        ]

        assert len(mentions) == 1
        assert mentions[0]["attrs"]["id"] == account_id
        assert mentions[0]["attrs"]["text"] == "@Jane Doe"
        assert len(links) == 1
        assert links[0]["text"] == "the runbook"

    def test_mixed_mention_syntaxes_in_one_paragraph(self) -> None:
        """Both supported mention syntaxes can appear in the same paragraph."""
        first = "712020:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        second = "712020:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        result = markdown_to_adf(f"@[Ada](accountid:{first}) and [~accountid:{second}]")
        para = result["content"][0]
        mentions = [n for n in para["content"] if n["type"] == "mention"]

        assert [n["attrs"]["id"] for n in mentions] == [first, second]
        assert mentions[0]["attrs"]["text"] == "@Ada"
        assert "text" not in mentions[1]["attrs"]

    def test_mention_inline_with_surrounding_text(self):
        """Mention preserves surrounding text nodes in the same paragraph."""
        account_id = "712020:abc-def"
        result = markdown_to_adf(f"hi [~accountid:{account_id}] please review")
        para = result["content"][0]
        types_in_order = [n["type"] for n in para["content"]]
        assert types_in_order == ["text", "mention", "text"]
        assert para["content"][0]["text"] == "hi "
        assert para["content"][1]["attrs"]["id"] == account_id
        assert para["content"][2]["text"] == " please review"

    def test_mention_does_not_swallow_regular_link(self):
        """[text](url) without the ~accountid: marker stays a link, not a mention."""
        result = markdown_to_adf("[click](https://example.com)")
        para = result["content"][0]
        mentions = [n for n in para["content"] if n["type"] == "mention"]
        links = [
            n
            for n in para["content"]
            if n["type"] == "text"
            and any(m["type"] == "link" for m in n.get("marks", []))
        ]
        assert mentions == []
        assert len(links) == 1

    def test_multiple_mentions_in_one_paragraph(self):
        """Several mentions in one line each get their own mention node."""
        a = "712020:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        b = "712020:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        result = markdown_to_adf(f"[~accountid:{a}] and [~accountid:{b}]")
        para = result["content"][0]
        mention_ids = [
            n["attrs"]["id"] for n in para["content"] if n["type"] == "mention"
        ]
        assert mention_ids == [a, b]

    @pytest.mark.parametrize(
        ("issue_key", "should_link"),
        [
            ("PROJ-123", True),
            ("B7-214-68901", True),
            ("B7-214--68901", False),
            ("B7-214-68901A", False),
        ],
    )
    def test_jira_issue_key_links_to_browse_url(
        self, issue_key: str, should_link: bool
    ) -> None:
        """Valid Jira issue keys become links without matching malformed keys."""
        result = markdown_to_adf(
            f"Blocked by {issue_key}.",
            jira_base_url="https://jira.example.com/",
        )
        para = result["content"][0]
        issue_node = next(n for n in para["content"] if issue_key in n.get("text", ""))
        link_marks = [m for m in issue_node.get("marks", []) if m["type"] == "link"]
        if should_link:
            assert link_marks[0]["attrs"]["href"] == (
                f"https://jira.example.com/browse/{issue_key}"
            )
        else:
            assert not link_marks

    @staticmethod
    def _linked_texts(nodes: list[dict[str, Any]]) -> set[str]:
        """Collect the text of every node carrying a link mark."""
        return {
            node["text"]
            for node in nodes
            if any(mark["type"] == "link" for mark in node.get("marks", []))
        }

    def test_known_project_keys_scopes_issue_key_links(self) -> None:
        """Only keys of known projects are linked; other tokens stay plain text."""
        text = "Tracked in PLOOP-12466. Sequence PR-1 .. PR-17."
        result = markdown_to_adf(
            text,
            jira_base_url="https://jira.example.com",
            known_project_keys={"PLOOP"},
        )
        nodes = result["content"][0]["content"]

        assert self._linked_texts(nodes) == {"PLOOP-12466"}
        # Nothing is dropped: the unknown keys survive as literal text.
        assert "".join(node["text"] for node in nodes) == text
        pr_node = next(node for node in nodes if "PR-1" in node["text"])
        assert pr_node.get("marks", []) == []

    def test_issue_key_links_stay_unscoped_by_default(self) -> None:
        """Omitting the allowlist keeps the historical link-everything behavior."""
        result = markdown_to_adf(
            "Tracked in PLOOP-12466. Sequence PR-1 .. PR-17.",
            jira_base_url="https://jira.example.com",
        )
        nodes = result["content"][0]["content"]

        assert self._linked_texts(nodes) == {"PLOOP-12466", "PR-1", "PR-17"}

    @pytest.mark.parametrize("configured_key", ["ploop", "  PLOOP  ", "\tPlOoP\n"])
    def test_known_project_keys_ignore_case_and_padding(
        self, configured_key: str
    ) -> None:
        """A lower-case or whitespace-padded project key still matches."""
        result = markdown_to_adf(
            "Tracked in PLOOP-12466 and PR-1.",
            jira_base_url="https://jira.example.com",
            known_project_keys={configured_key},
        )
        nodes = result["content"][0]["content"]

        assert self._linked_texts(nodes) == {"PLOOP-12466"}

    def test_known_project_keys_apply_inside_inline_formatting(self) -> None:
        """The allowlist also scopes keys inside bold text and list items."""
        result = markdown_to_adf(
            "**PLOOP-12466 blocks PR-1**\n\n- PLOOP-12466 and PR-17",
            jira_base_url="https://jira.example.com",
            known_project_keys={"PLOOP"},
        )
        bold_nodes = result["content"][0]["content"]
        item_nodes = result["content"][1]["content"][0]["content"][0]["content"]

        assert all(
            any(mark["type"] == "strong" for mark in node["marks"])
            for node in bold_nodes
        )
        assert self._linked_texts(bold_nodes) == {"PLOOP-12466"}
        assert self._linked_texts(item_nodes) == {"PLOOP-12466"}

    @pytest.mark.parametrize(
        ("table", "table_id"),
        [
            pytest.param(
                "| Key | Note |\n|---|---|\n| PLOOP-12466 | see PR-17 |",
                "markdown",
                id="markdown-table",
            ),
            pytest.param(
                "||Key||Note||\n|PLOOP-12466|see PR-17|",
                "wiki",
                id="wiki-table",
            ),
        ],
    )
    def test_known_project_keys_apply_inside_table_cells(
        self, table: str, table_id: str
    ) -> None:
        """A table cell is no more permissive than running prose.

        Table cells are built by a separate helper from the one that handles
        paragraphs, so the allowlist has to be threaded into it explicitly. A
        cell that parsed its content without the allowlist would auto-link
        every key-shaped token again, in either dialect, and no paragraph-level
        test would notice.
        """
        result = markdown_to_adf(
            table,
            jira_base_url="https://jira.example.com",
            known_project_keys={"PLOOP"},
        )

        table_node = next(n for n in result["content"] if n["type"] == "table")
        paragraphs = [
            paragraph
            for row in table_node["content"]
            for cell in row["content"]
            for paragraph in cell["content"]
        ]
        linked: set[str] = set()
        for paragraph in paragraphs:
            linked |= self._linked_texts(paragraph.get("content", []))

        assert linked == {"PLOOP-12466"}, table_id

    # -- Code blocks --------------------------------------------------------

    def test_code_block_with_lang(self):
        """Fenced code block with language attr."""
        md = "```python\nprint('hi')\n```"
        result = markdown_to_adf(md)
        cb = next(n for n in result["content"] if n["type"] == "codeBlock")
        assert cb["attrs"]["language"] == "python"
        code_text = cb["content"][0]["text"]
        assert "print('hi')" in code_text

    def test_code_block_no_lang(self):
        """Fenced code block without language."""
        md = "```\nsome code\n```"
        result = markdown_to_adf(md)
        cb = next(n for n in result["content"] if n["type"] == "codeBlock")
        # language should be absent or empty
        lang = cb.get("attrs", {}).get("language", "")
        assert lang == "" or lang is None or "language" not in cb.get("attrs", {})

    # -- Lists --------------------------------------------------------------

    def test_bullet_list(self):
        """- items produce a bulletList with listItem > paragraph > text."""
        md = "- alpha\n- beta"
        result = markdown_to_adf(md)
        bl = next(n for n in result["content"] if n["type"] == "bulletList")
        items = bl["content"]
        assert len(items) == 2
        for item in items:
            assert item["type"] == "listItem"
            # listItem must contain paragraph (not bare text)
            assert item["content"][0]["type"] == "paragraph"

    def test_ordered_list(self):
        """1. items produce an orderedList with listItem > paragraph > text."""
        md = "1. first\n2. second"
        result = markdown_to_adf(md)
        ol = next(n for n in result["content"] if n["type"] == "orderedList")
        items = ol["content"]
        assert len(items) == 2
        for item in items:
            assert item["type"] == "listItem"
            assert item["content"][0]["type"] == "paragraph"

    @pytest.mark.parametrize(
        "md",
        [
            "1. first\n\n2. second\n\n3. third",
            "1. first\n\n1. second\n\n1. third",
        ],
    )
    def test_ordered_list_with_blank_lines(self, md: str) -> None:
        """Blank lines between items keep one loose ordered list."""
        result = markdown_to_adf(md)
        ordered_lists = [
            node for node in result["content"] if node["type"] == "orderedList"
        ]

        assert len(ordered_lists) == 1
        assert len(ordered_lists[0]["content"]) == 3

    def test_task_list_checked(self):
        """- [x] items produce a taskList with taskItem state=DONE."""
        md = "- [x] done task"
        result = markdown_to_adf(md)
        tl = next(n for n in result["content"] if n["type"] == "taskList")
        assert len(tl["content"]) == 1
        item = tl["content"][0]
        assert item["type"] == "taskItem"
        assert item["attrs"]["state"] == "DONE"
        assert item["content"][0]["text"] == "done task"

    def test_task_list_unchecked(self):
        """- [ ] items produce a taskList with taskItem state=TODO."""
        md = "- [ ] pending task"
        result = markdown_to_adf(md)
        tl = next(n for n in result["content"] if n["type"] == "taskList")
        item = tl["content"][0]
        assert item["attrs"]["state"] == "TODO"

    def test_task_list_mixed(self):
        """Mixed checked/unchecked items in one taskList."""
        md = "- [x] done\n- [ ] todo\n- [X] also done"
        result = markdown_to_adf(md)
        tl = next(n for n in result["content"] if n["type"] == "taskList")
        assert len(tl["content"]) == 3
        assert tl["content"][0]["attrs"]["state"] == "DONE"
        assert tl["content"][1]["attrs"]["state"] == "TODO"
        assert tl["content"][2]["attrs"]["state"] == "DONE"

    def test_task_list_not_confused_with_bullet_list(self):
        """Regular - items without [ ] are still bulletList, not taskList."""
        md = "- regular item"
        result = markdown_to_adf(md)
        assert any(n["type"] == "bulletList" for n in result["content"])
        assert not any(n["type"] == "taskList" for n in result["content"])

    # -- Blockquote ---------------------------------------------------------

    def test_blockquote(self):
        """> text produces a blockquote wrapping a paragraph."""
        result = markdown_to_adf("> quoted text")
        bq = next(n for n in result["content"] if n["type"] == "blockquote")
        assert bq["content"][0]["type"] == "paragraph"

    def test_blockquote_with_fenced_code_block(self) -> None:
        """A fence inside a quote is a codeBlock, not three paragraphs.

        This is the exact Markdown the read path produces for Jira's own
        ``{quote}q with code\\n\\n{code:python}x = 1{code}{quote}``, and an ADF
        blockquote containing a codeBlock is accepted by Cloud (measured), so
        the fence must not leak as literal text.
        """
        md = "> q with code\n> \n> ```python\n> x = 1\n> ```"
        result = markdown_to_adf(md)
        assert [n["type"] for n in result["content"]] == ["blockquote"]
        bq = result["content"][0]
        assert [n["type"] for n in bq["content"]] == ["paragraph", "codeBlock"]
        assert bq["content"][0]["content"][0]["text"] == "q with code"
        code = bq["content"][1]
        assert code["attrs"] == {"language": "python"}
        assert code["content"] == [{"type": "text", "text": "x = 1"}]

    def test_blockquote_code_fence_keeps_indentation(self) -> None:
        """Only the "> " marker is stripped, so code indentation survives."""
        md = "> ```python\n> if x:\n>     pass\n> ```"
        result = markdown_to_adf(md)
        code = result["content"][0]["content"][0]
        assert code["type"] == "codeBlock"
        assert code["content"][0]["text"] == "if x:\n    pass"

    def test_blockquote_code_fence_keeps_blank_lines(self) -> None:
        """A blank quoted line is literal content inside a fence."""
        md = "> ```\n> a\n> \n> b\n> ```"
        result = markdown_to_adf(md)
        code = result["content"][0]["content"][0]
        assert code["type"] == "codeBlock"
        assert code["attrs"] == {}
        assert code["content"][0]["text"] == "a\n\nb"

    def test_blockquote_blank_line_outside_a_fence_is_a_break(self) -> None:
        """Outside a fence a blank quoted line separates paragraphs."""
        md = "> first\n> \n> second"
        result = markdown_to_adf(md)
        bq = result["content"][0]
        assert [n["type"] for n in bq["content"]] == ["paragraph", "paragraph"]
        assert bq["content"][1]["content"][0]["text"] == "second"

    def test_blockquote_of_only_blank_lines_keeps_one_paragraph(self) -> None:
        """A blockquote with no content at all is invalid ADF."""
        result = markdown_to_adf("> ")
        bq = result["content"][0]
        assert bq["type"] == "blockquote"
        assert [n["type"] for n in bq["content"]] == ["paragraph"]

    def test_blockquote_promotes_only_what_the_schema_places_there(self) -> None:
        """A quote promotes a code fence and a list, and nothing else.

        ``bulletList``, ``orderedList`` and ``codeBlock`` are declared children
        of ``blockquote`` in the ADF schema, so a quoted list has to become a
        real list -- leaving it a paragraph stored ``- item`` as literal text,
        which is then what a read showed. ``heading`` is not a declared child
        and stays paragraph text.
        """
        result = markdown_to_adf("> - item\n> ## heading")
        bq = result["content"][0]
        assert [n["type"] for n in bq["content"]] == ["bulletList", "paragraph"]
        item = bq["content"][0]["content"][0]
        assert item["content"][0]["content"][0]["text"] == "item"
        assert bq["content"][1]["content"][0]["text"] == "## heading"

    # -- Horizontal rule ----------------------------------------------------

    @pytest.mark.parametrize(
        "md", ["---", "***", "___"], ids=["dashes", "stars", "underscores"]
    )
    def test_horizontal_rule(self, md: str):
        """Horizontal rule markers produce a rule node."""
        result = markdown_to_adf(md)
        rule_nodes = [n for n in result["content"] if n["type"] == "rule"]
        assert len(rule_nodes) >= 1

    # -- Expand/collapse block ----------------------------------------------

    def test_expand_with_title(self) -> None:
        """Expand block with title produces an expand node."""
        md = "{expand:Details}\n* bullet one\n* bullet two\n{expand}"
        result = markdown_to_adf(md)
        expand = next(n for n in result["content"] if n["type"] == "expand")
        assert expand["attrs"]["title"] == "Details"
        assert any(n["type"] == "bulletList" for n in expand["content"])

    def test_expand_without_title(self) -> None:
        """Expand block without a title uses an empty string."""
        md = "{expand}\nSome content\n{expand}"
        result = markdown_to_adf(md)
        expand = next(n for n in result["content"] if n["type"] == "expand")
        assert expand["attrs"]["title"] == ""
        assert any(n["type"] == "paragraph" for n in expand["content"])

    def test_expand_with_nested_formatting(self) -> None:
        """Expand block recursively parses inner markdown."""
        md = "{expand:Steps}\n## Heading\n1. First\n1. Second\n{expand}"
        result = markdown_to_adf(md)
        expand = next(n for n in result["content"] if n["type"] == "expand")
        inner_types = [n["type"] for n in expand["content"]]
        assert "heading" in inner_types
        assert "orderedList" in inner_types

    def test_expand_preserves_surrounding_content(self) -> None:
        """Content before and after expand block is preserved."""
        md = "Before\n{expand:Title}\nInside\n{expand}\nAfter"
        result = markdown_to_adf(md)
        types = [n["type"] for n in result["content"]]
        assert types == ["paragraph", "expand", "paragraph"]

    @pytest.mark.parametrize(
        "opening",
        [
            "{expand:More detail}",
            "{expand:title=More detail}",
            "{expand:Title=More detail}",
            "{expand:title = More detail}",
        ],
        ids=["positional", "keyword", "keyword-capitalised", "keyword-spaced"],
    )
    def test_expand_title_spellings_agree(self, opening: str) -> None:
        """Jira writes an expand title positionally and as a title= keyword."""
        result = markdown_to_adf(f"{opening}\nhidden body\n{{expand}}")
        expand = next(n for n in result["content"] if n["type"] == "expand")
        assert expand["attrs"]["title"] == "More detail"
        assert expand["content"][0]["content"][0]["text"] == "hidden body"

    def test_expand_title_padding_is_trimmed(self) -> None:
        """Whitespace around a macro title is not part of the title."""
        result = markdown_to_adf("{expand:  More detail  }\nbody\n{expand}")
        expand = next(n for n in result["content"] if n["type"] == "expand")
        assert expand["attrs"]["title"] == "More detail"

    def test_expand_title_containing_equals_is_kept(self) -> None:
        """Only a leading title= is stripped, so 'a=b' stays the title."""
        result = markdown_to_adf("{expand:a=b}\nbody\n{expand}")
        expand = next(n for n in result["content"] if n["type"] == "expand")
        assert expand["attrs"]["title"] == "a=b"

    def test_expand_macro_and_fence_build_the_same_node(self) -> None:
        """Both spellings share one node builder, so they cannot drift."""
        from_macro = markdown_to_adf("{expand:T}\nbody\n- item\n{expand}")
        from_fence = markdown_to_adf(":::expand T\nbody\n- item\n:::")
        assert from_macro["content"] == from_fence["content"]
        assert from_macro["content"][0]["type"] == "expand"

    # -- Mixed formatting ---------------------------------------------------

    def test_mixed_formatting(self):
        """Bold and italic in the same line get correct marks per segment."""
        result = markdown_to_adf("**bold** and *italic*")
        para = result["content"][0]
        assert para["type"] == "paragraph"
        # Find bold
        bold = [
            n
            for n in para["content"]
            if n["type"] == "text"
            and any(m["type"] == "strong" for m in n.get("marks", []))
        ]
        # Find italic
        italic = [
            n
            for n in para["content"]
            if n["type"] == "text"
            and any(m["type"] == "em" for m in n.get("marks", []))
        ]
        assert len(bold) >= 1
        assert len(italic) >= 1
        assert bold[0]["text"] == "bold"
        assert italic[0]["text"] == "italic"

    # -- Tables -------------------------------------------------------------

    def test_table_basic(self):
        """Pipe-delimited table produces table > tableRow > tableHeader/tableCell."""
        md = "| Name | Age |\n|---|---|\n| Alice | 30 |\n| Bob | 25 |"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        rows = table["content"]
        assert len(rows) == 3  # header + 2 data rows

        # First row is header
        header_row = rows[0]
        assert header_row["type"] == "tableRow"
        assert all(c["type"] == "tableHeader" for c in header_row["content"])
        header_texts = [
            c["content"][0]["content"][0]["text"] for c in header_row["content"]
        ]
        assert header_texts == ["Name", "Age"]

        # Remaining rows are data cells
        for data_row in rows[1:]:
            assert data_row["type"] == "tableRow"
            assert all(c["type"] == "tableCell" for c in data_row["content"])

        # Verify data values
        data_texts = [c["content"][0]["content"][0]["text"] for c in rows[1]["content"]]
        assert data_texts == ["Alice", "30"]

    def test_table_with_inline_formatting(self):
        """Table cells preserve inline formatting (bold, code)."""
        md = "| Feature | Status |\n|---|---|\n| **Auth** | `done` |"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        data_row = table["content"][1]  # skip header

        # First cell should have bold
        first_cell_content = data_row["content"][0]["content"][0]["content"]
        bold_nodes = [
            n
            for n in first_cell_content
            if any(m["type"] == "strong" for m in n.get("marks", []))
        ]
        assert len(bold_nodes) == 1
        assert bold_nodes[0]["text"] == "Auth"

        # Second cell should have code
        second_cell_content = data_row["content"][1]["content"][0]["content"]
        code_nodes = [
            n
            for n in second_cell_content
            if any(m["type"] == "code" for m in n.get("marks", []))
        ]
        assert len(code_nodes) == 1
        assert code_nodes[0]["text"] == "done"

    def test_table_attrs(self):
        """Table node has required ADF attrs (isNumberColumnEnabled, layout)."""
        md = "| A |\n|---|\n| B |"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        assert table["attrs"]["isNumberColumnEnabled"] is False
        assert table["attrs"]["layout"] == "default"

    def test_table_alignment_separator(self):
        """Alignment separators (:---|:---:|---:) are skipped correctly."""
        md = "| Left | Center | Right |\n|:---|:---:|---:|\n| a | b | c |"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        # Should have 2 rows: header + 1 data (separator skipped)
        assert len(table["content"]) == 2

    # -- Jira wiki tables ---------------------------------------------------

    @staticmethod
    def _row_texts(row: dict[str, Any]) -> list[str]:
        """Return the first text value of every cell in an ADF table row."""
        return [c["content"][0]["content"][0]["text"] for c in row["content"]]

    @staticmethod
    def _column_counts(table: dict[str, Any]) -> list[int]:
        """Return the cell count of every row in an ADF table."""
        return [len(row["content"]) for row in table["content"]]

    @staticmethod
    def _adf_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
        """Return only the log records emitted by the ADF converter module."""
        return [r for r in records if r.name.endswith("models.jira.adf")]

    @staticmethod
    def _column_count_warnings(records: list[logging.LogRecord]) -> list[str]:
        """Return warning messages emitted about mismatched column counts."""
        return [
            r.getMessage()
            for r in records
            if r.levelno == logging.WARNING and "column counts" in r.getMessage()
        ]

    def test_wiki_table_header_column_count_matches_data_rows(self) -> None:
        """Wiki '||h||h||' header yields N cells, not the 2N-1 ragged split."""
        md = "||Name||Age||City||\n|Alice|30|Paris|\n|Bob|25|Rome|"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")

        assert self._column_counts(table) == [3, 3, 3]
        header_row = table["content"][0]
        assert all(c["type"] == "tableHeader" for c in header_row["content"])
        assert self._row_texts(header_row) == ["Name", "Age", "City"]
        assert all(c["type"] == "tableCell" for c in table["content"][1]["content"])
        assert self._row_texts(table["content"][1]) == ["Alice", "30", "Paris"]
        assert self._row_texts(table["content"][2]) == ["Bob", "25", "Rome"]

    def test_wiki_table_single_column_header_is_detected(self) -> None:
        """A closed one-column '||Only||' row is read as a wiki header row.

        Its cell count is the same whichever dialect is assumed, so the
        detection is only observable in the data row: inside a recognised wiki
        table a '|' in a code span stays literal instead of splitting the row.
        """
        md = "||Only||\n|`a | b`|"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [1, 1]
        assert table["content"][0]["content"][0]["type"] == "tableHeader"
        assert self._row_texts(table["content"][0]) == ["Only"]
        assert self._row_texts(table["content"][1]) == ["a | b"]

    def test_wiki_table_unterminated_header_row(self) -> None:
        """A wiki header missing its trailing '||' still yields N columns."""
        md = "||Name||Age\n|Alice|30|"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [2, 2]
        assert self._row_texts(table["content"][0]) == ["Name", "Age"]

    def test_wiki_table_later_header_row_is_a_header(self) -> None:
        """Every '||...||' row is a header row, other rows are data cells."""
        md = "||A||B||\n|1|2|\n||C||D||\n|3|4|"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        cell_types = [row["content"][0]["type"] for row in table["content"]]
        assert cell_types == [
            "tableHeader",
            "tableCell",
            "tableHeader",
            "tableCell",
        ]

    def test_midtable_wiki_row_keeps_the_markdown_header_row(self) -> None:
        """A single wiki-looking row does not demote a Markdown header row."""
        md = (
            "| Env | Host |\n|---|---|\n| dev | d.example.com |\n"
            "||see note||\n| prod | p.example.com |"
        )
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        cell_types = [row["content"][0]["type"] for row in table["content"]]
        assert cell_types == [
            "tableHeader",
            "tableCell",
            "tableHeader",
            "tableCell",
        ]
        assert self._row_texts(table["content"][0]) == ["Env", "Host"]
        assert self._row_texts(table["content"][2]) == ["see note", ""]

    def test_wiki_table_cells_keep_inline_formatting(self) -> None:
        """Inline Markdown inside wiki cells is still converted to marks."""
        md = "||**Feature**||Status||\n|Auth|`done`|"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [2, 2]
        header_content = table["content"][0]["content"][0]["content"][0]["content"]
        assert header_content[0]["text"] == "Feature"
        assert header_content[0]["marks"] == [{"type": "strong"}]
        data_content = table["content"][1]["content"][1]["content"][0]["content"]
        assert data_content[0]["marks"] == [{"type": "code"}]

    def test_wiki_table_cells_link_issue_keys(self) -> None:
        """Wiki cells go through the same issue-key linking as Markdown cells."""
        md = "||Key||ABC-2||\n|ABC-1|x|"
        result = markdown_to_adf(md, "https://jira.example.com")
        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [2, 2]
        header_node = table["content"][0]["content"][1]["content"][0]["content"][0]
        assert header_node["text"] == "ABC-2"
        assert header_node["marks"] == [
            {
                "type": "link",
                "attrs": {"href": "https://jira.example.com/browse/ABC-2"},
            }
        ]
        data_node = table["content"][1]["content"][0]["content"][0]["content"][0]
        assert data_node["marks"] == [
            {
                "type": "link",
                "attrs": {"href": "https://jira.example.com/browse/ABC-1"},
            }
        ]

    def test_wiki_table_separator_row_is_skipped(self) -> None:
        """A '||---||---||' separator row is dropped, like its Markdown twin."""
        md = "||A||B||\n||---||---||\n|1|2|"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [2, 2]
        assert self._row_texts(table["content"][1]) == ["1", "2"]

    @pytest.mark.parametrize(
        "separator", ["||---||---||", "||-||-||", "||:---||---:||"]
    )
    def test_wiki_spelled_separator_alone_is_not_a_wiki_table(
        self, separator: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A separator row that is dropped cannot give a table wiki semantics."""
        md = f"| a | b |\n{separator}\n| 1 | 2 |"
        with caplog.at_level(logging.DEBUG):
            result = markdown_to_adf(md)

        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [2, 2]
        assert all(c["type"] == "tableHeader" for c in table["content"][0]["content"])
        assert self._row_texts(table["content"][0]) == ["a", "b"]
        assert all(c["type"] == "tableCell" for c in table["content"][1]["content"])
        assert self._adf_records(caplog.records) == []

    def test_wiki_table_double_pipe_in_code_span_is_not_a_delimiter(self) -> None:
        """'||' inside a code span does not split a wiki cell."""
        md = "||`a || b`||Note||\n|x|y|"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [2, 2]
        assert self._row_texts(table["content"][0]) == ["a || b", "Note"]

    def test_wiki_table_double_pipe_in_link_target_is_not_a_delimiter(self) -> None:
        """'||' inside a Markdown link target does not split a wiki cell."""
        md = "||[docs](https://e.example.com/a||b)||Note||\n|1|2|"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [2, 2]
        first_cell = table["content"][0]["content"][0]["content"][0]["content"][0]
        assert first_cell["text"] == "docs"
        assert first_cell["marks"] == [
            {
                "type": "link",
                "attrs": {"href": "https://e.example.com/a||b"},
            }
        ]

    def test_wiki_table_pipe_in_data_row_code_span_is_not_a_delimiter(self) -> None:
        """A single '|' inside a code span keeps a wiki data row intact."""
        md = "||Cmd||Note||\n|`a | b`|works|"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [2, 2]
        assert self._row_texts(table["content"][1]) == ["a | b", "works"]

    # -- Wiki detection false positives -------------------------------------

    def test_double_pipe_in_prose_is_not_a_table(self) -> None:
        """Prose containing '||' stays a paragraph."""
        result = markdown_to_adf("Use a || b for logical OR")
        assert [n["type"] for n in result["content"]] == ["paragraph"]
        assert result["content"][0]["content"][0]["text"] == "Use a || b for logical OR"

    def test_double_pipe_in_inline_code_is_not_a_table(self) -> None:
        """'||' inside inline code stays a paragraph with a code mark."""
        result = markdown_to_adf("Compare with `a || b` today")
        assert [n["type"] for n in result["content"]] == ["paragraph"]
        code_nodes = [
            n
            for n in result["content"][0]["content"]
            if any(m["type"] == "code" for m in n.get("marks", []))
        ]
        assert [n["text"] for n in code_nodes] == ["a || b"]

    def test_double_pipe_in_fenced_code_block_is_not_a_table(self) -> None:
        """Wiki table lines inside a fenced code block are left verbatim."""
        md = "```\n||A||B||\n|1|2|\n```"
        result = markdown_to_adf(md)
        assert [n["type"] for n in result["content"]] == ["codeBlock"]
        assert result["content"][0]["content"][0]["text"] == "||A||B||\n|1|2|"

    def test_leading_double_pipe_without_second_delimiter_is_not_wiki(self) -> None:
        """'|| text' has no second '||', so Markdown parsing is unchanged."""
        result = markdown_to_adf("|| not really a table")
        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [1]
        assert self._row_texts(table["content"][0]) == ["not really a table"]

    def test_markdown_row_with_empty_first_cell_is_not_wiki(self) -> None:
        """'|| b | c |' is a Markdown row with an empty first cell, not wiki."""
        md = "|| b | c |\n|---|---|\n| 1 | 2 |"
        result = markdown_to_adf(md)
        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [2, 2]
        assert self._row_texts(table["content"][0]) == ["b", "c"]

    # -- Ragged tables ------------------------------------------------------

    def test_wiki_table_warns_once_naming_the_construct(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One warning per wiki table names it and counts its wiki rows only."""
        md = "||A||B||\n|1|2|\n|3|4|\n|5|6|"
        with caplog.at_level(logging.DEBUG):
            markdown_to_adf(md)

        records = self._adf_records(caplog.records)
        assert [r.levelno for r in records] == [logging.WARNING]
        message = records[0].getMessage()
        assert message.startswith("Converted 1 Jira wiki markup table row(s)")
        assert "column counts" not in message

    @pytest.mark.parametrize(
        ("md", "expected_counts"),
        [
            ("| a | b | c |\n|---|---|---|\n| 1 |\n| 2 |", [3, 1, 1]),
            ("| a | b |\n|---|---|\n||x|", [2, 1]),
        ],
    )
    def test_ragged_markdown_table_is_left_alone_and_silent(
        self,
        md: str,
        expected_counts: list[int],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A table without wiki rows keeps its ragged shape and logs nothing.

        Padding it would change output that has nothing to do with wiki
        headers, and warning about it would be noise for content the caller
        often did not author, so the Markdown path is left untouched.
        """
        with caplog.at_level(logging.DEBUG):
            result = markdown_to_adf(md)

        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == expected_counts
        assert self._adf_records(caplog.records) == []

    def test_ragged_wiki_table_is_padded_and_warns_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A short wiki data row is padded, with one warning for the table."""
        md = "||A||B||C||\n|1|2|\n|3|4|"
        with caplog.at_level(logging.WARNING):
            result = markdown_to_adf(md)

        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [3, 3, 3]
        assert self._row_texts(table["content"][1]) == ["1", "2", ""]
        warnings = self._column_count_warnings(caplog.records)
        assert len(warnings) == 1
        assert "[2, 3]" in warnings[0]
        assert "3 columns" in warnings[0]

    def test_wiki_table_wider_data_row_pads_the_header(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A data row wider than the header pads the header, not truncates."""
        md = "||A||B||\n|1|2|3|"
        with caplog.at_level(logging.WARNING):
            result = markdown_to_adf(md)

        table = next(n for n in result["content"] if n["type"] == "table")
        assert self._column_counts(table) == [3, 3]
        assert self._row_texts(table["content"][0]) == ["A", "B", ""]
        assert self._row_texts(table["content"][1]) == ["1", "2", "3"]
        assert len(self._column_count_warnings(caplog.records)) == 1

    def test_wiki_table_inside_expand_block(self) -> None:
        """Wiki tables are recognised in recursively parsed blocks too."""
        md = "{expand:Rooms}\n||Room||Floor||\n|101|1|\n{expand}"
        result = markdown_to_adf(md)
        expand = next(n for n in result["content"] if n["type"] == "expand")
        table = next(n for n in expand["content"] if n["type"] == "table")
        assert self._column_counts(table) == [2, 2]
        assert self._row_texts(table["content"][0]) == ["Room", "Floor"]

    def test_well_formed_tables_emit_no_column_count_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rectangular tables in either dialect log no *column-count* warning.

        A wiki table still logs the conversion notice — that one fires for any
        table carrying a wiki header row, by design, because translating a
        dialect the writer does not otherwise speak is worth telling the
        operator about. It is only the "mismatched column counts" clause of
        that message that a rectangular table must not carry, and a Markdown
        table must log nothing at all.
        """
        with caplog.at_level(logging.WARNING):
            markdown_to_adf("| a | b |\n|---|---|\n| 1 | 2 |")
        assert caplog.records == []

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            markdown_to_adf("||A||B||\n|1|2|")
        assert self._column_count_warnings(caplog.records) == []
        assert len(caplog.records) == 1
        assert "Converted 1 Jira wiki markup table row(s)" in (
            caplog.records[0].getMessage()
        )

    def test_markdown_table_output_unchanged_by_wiki_support(self) -> None:
        """A well-formed Markdown table converts exactly as it did before."""
        md = "| Name | Age |\n|---|---|\n| Alice | 30 |"
        assert markdown_to_adf(md) == {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "table",
                    "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [
                                                {"type": "text", "text": "Name"}
                                            ],
                                        }
                                    ],
                                },
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [
                                                {"type": "text", "text": "Age"}
                                            ],
                                        }
                                    ],
                                },
                            ],
                        },
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [
                                                {"type": "text", "text": "Alice"}
                                            ],
                                        }
                                    ],
                                },
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "30"}],
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        }

    # -- Known gaps, unchanged by wiki header support ------------------------

    def test_wiki_table_inside_code_macro_is_still_a_table(self) -> None:
        """'{code}' does not shield its content, before or after this fix.

        Only fenced ``` blocks are honoured, so wiki rows inside a '{code}'
        macro become a table either way -- ragged before, rectangular now.
        Wiki header support neither introduces nor closes that gap.
        """
        md = "{code}\n||A||B||\n|1|2|\n{code}"
        result = markdown_to_adf(md)
        assert [n["type"] for n in result["content"]] == [
            "paragraph",
            "table",
            "paragraph",
        ]

    def test_markdown_only_table_still_splits_naively(self) -> None:
        """The code-span-aware splitter runs only inside a wiki table.

        That asymmetry is what keeps Markdown output byte-identical, at the
        price of the same row parsing differently depending on whether the
        block also holds a wiki row.
        """
        row = "|`a | b`|c|"
        markdown_only = next(
            n for n in markdown_to_adf(row)["content"] if n["type"] == "table"
        )
        assert self._column_counts(markdown_only) == [3]

        with_wiki_header = next(
            n
            for n in markdown_to_adf(f"||H1||H2||\n{row}")["content"]
            if n["type"] == "table"
        )
        assert self._column_counts(with_wiki_header) == [2, 2]

    def test_pipe_in_link_text_splits_a_data_row_in_both_dialects(self) -> None:
        """A '|' in link text always splits: it is the data-row delimiter.

        Only the link target (after ']('), not the link text, is shielded, so
        '|[a || b](url)|c|' loses its link on the Markdown and the wiki path
        alike.
        """
        row = "|[a || b](https://e.example.com/x)|c|"
        for md in (row, f"||H1||H2||\n{row}"):
            table = next(
                n for n in markdown_to_adf(md)["content"] if n["type"] == "table"
            )
            assert self._column_counts(table)[-1] == 4

    # -- Roundtrip ----------------------------------------------------------

    def test_roundtrip(self):
        """markdown_to_adf → adf_to_text preserves the original words."""
        original = "Hello world with **bold** and *italic* text"
        adf = markdown_to_adf(original)
        text_back = adf_to_text(adf) or ""
        for word in ["Hello", "world", "bold", "italic", "text"]:
            assert word in text_back


class TestAdfMediaPreservation:
    """Tests for preserving existing media nodes during description rewrites."""

    def test_extract_top_level_media_nodes(self):
        """Media blocks are extracted from the document root."""
        media_single = {
            "type": "mediaSingle",
            "attrs": {"layout": "center"},
            "content": [
                {
                    "type": "media",
                    "attrs": {
                        "id": "video-123",
                        "type": "file",
                        "collection": "",
                    },
                }
            ],
        }
        adf = {
            "version": 1,
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Intro"}]},
                media_single,
            ],
        }

        extracted = extract_top_level_media_nodes(adf)

        assert extracted == [media_single]

    def test_merge_adf_with_preserved_media_appends_media_blocks(self):
        """Existing media blocks are preserved in the outgoing ADF document."""
        media_single = {
            "type": "mediaSingle",
            "attrs": {"layout": "center"},
            "content": [
                {
                    "type": "media",
                    "attrs": {
                        "id": "video-123",
                        "type": "file",
                        "collection": "",
                    },
                }
            ],
        }
        source_adf = {
            "version": 1,
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Old"}]},
                media_single,
            ],
        }
        target_adf = markdown_to_adf("Updated text")

        merged = merge_adf_with_preserved_media(target_adf, source_adf)

        assert merged["content"][-1] == media_single
        assert merged["content"][0]["type"] == "paragraph"

    def test_merge_adf_with_preserved_media_skips_duplicates(self):
        """Media nodes already present in the target are not duplicated."""
        media_single = {
            "type": "mediaSingle",
            "attrs": {"layout": "center"},
            "content": [
                {
                    "type": "media",
                    "attrs": {
                        "id": "video-123",
                        "type": "file",
                        "collection": "",
                    },
                }
            ],
        }
        target_adf = {
            "version": 1,
            "type": "doc",
            "content": [media_single],
        }
        source_adf = {
            "version": 1,
            "type": "doc",
            "content": [media_single],
        }

        merged = merge_adf_with_preserved_media(target_adf, source_adf)

        assert merged["content"] == [media_single]


class TestMarkdownToAdfPanels:
    """Tests for panel node support in markdown_to_adf."""

    def _assert_valid_adf(self, result: dict[str, Any]) -> None:
        """Helper: assert the result is a valid ADF document."""
        assert result["version"] == 1
        assert result["type"] == "doc"
        assert isinstance(result["content"], list)

    @pytest.mark.parametrize(
        "panel_type",
        ["note", "info", "warning", "success", "error"],
        ids=["note", "info", "warning", "success", "error"],
    )
    def test_all_valid_panel_types(self, panel_type: str) -> None:
        """All five valid panel types produce a panel node."""
        result = markdown_to_adf(f":::{panel_type}\ntext\n:::")
        self._assert_valid_adf(result)
        assert result["content"][0]["type"] == "panel"
        assert result["content"][0]["attrs"]["panelType"] == panel_type

    def test_panel_content_is_paragraph(self) -> None:
        """Panel body text becomes a paragraph node inside the panel."""
        result = markdown_to_adf(":::note\nThis is a note.\n:::")
        panel = result["content"][0]
        assert panel["type"] == "panel"
        assert panel["content"][0]["type"] == "paragraph"

    def test_panel_with_nested_heading_and_list(self) -> None:
        """Panel content supports nested headings and bullet lists."""
        result = markdown_to_adf(":::info\n## Title\n- item 1\n- item 2\n:::")
        panel = result["content"][0]
        assert panel["type"] == "panel"
        assert panel["attrs"]["panelType"] == "info"
        inner_types = [n["type"] for n in panel["content"]]
        assert "heading" in inner_types
        assert "bulletList" in inner_types

    def test_invalid_panel_type_falls_through_as_paragraph(self) -> None:
        """Unknown panel type (:::custom) is not converted to a panel node."""
        result = markdown_to_adf(":::custom\nsome text\n:::")
        types = [n["type"] for n in result["content"]]
        assert "panel" not in types

    def test_panel_mixed_with_other_content(self) -> None:
        """Panel can appear alongside headings and lists in a document."""
        result = markdown_to_adf("## Heading\n\n:::note\nA note.\n:::\n\n- list item")
        types = [n["type"] for n in result["content"]]
        assert types == ["heading", "panel", "bulletList"]

    # -- :::tip alias -------------------------------------------------------

    def test_tip_is_an_alias_for_the_success_panel(self) -> None:
        """:::tip becomes a success panel; ADF has no "tip" panelType."""
        result = markdown_to_adf(":::tip\nHandy hint.\n:::")
        self._assert_valid_adf(result)
        assert len(result["content"]) == 1
        panel = result["content"][0]
        assert panel["type"] == "panel"
        assert panel["attrs"]["panelType"] == "success"
        assert panel["content"][0]["type"] == "paragraph"

    def test_tip_fence_is_case_insensitive(self) -> None:
        """:::TIP resolves through the alias like every other fence name."""
        result = markdown_to_adf(":::TIP\nHandy hint.\n:::")
        assert result["content"][0]["attrs"]["panelType"] == "success"

    def test_tip_no_longer_leaks_literal_fence_paragraphs(self) -> None:
        """Before the alias, :::tip fell through as three literal paragraphs."""
        result = markdown_to_adf(":::tip\nHandy hint.\n:::")
        paragraph_texts = [
            node["content"][0]["text"]
            for node in result["content"]
            if node["type"] == "paragraph" and node["content"]
        ]
        assert ":::tip" not in paragraph_texts
        assert ":::" not in paragraph_texts

    # -- Panel nesting guard ------------------------------------------------

    def test_blockquote_in_panel_is_flattened_not_nested(self) -> None:
        """A quote inside a panel is hoisted into paragraphs.

        Measured on Jira Cloud: an ADF panel containing a blockquote is
        rejected with an opaque 400 INVALID_INPUT that names no field. Wiki
        panels may hold quotes, so the read path can produce this input; the
        flattening keeps the write working at the cost of the quote wrapper.
        """
        result = markdown_to_adf(":::warning\nbefore\n> quoted line\nafter\n:::")
        panel = result["content"][0]
        assert panel["type"] == "panel"
        inner_types = [n["type"] for n in panel["content"]]
        assert "blockquote" not in inner_types
        assert inner_types == ["paragraph", "paragraph", "paragraph"]
        assert panel["content"][1]["content"][0]["text"] == "quoted line"

    def test_nested_panel_is_flattened_not_nested(self) -> None:
        """A panel inside a panel is hoisted into its parent's content.

        Measured on Jira Cloud: an ADF panel containing another panel is
        rejected with the same opaque 400 INVALID_INPUT.
        """
        result = markdown_to_adf(":::info\nouter\n:::note\ninner\n:::")
        panel = result["content"][0]
        assert panel["type"] == "panel"
        assert panel["attrs"]["panelType"] == "info"
        inner_types = [n["type"] for n in panel["content"]]
        assert "panel" not in inner_types
        assert inner_types == ["paragraph", "paragraph"]
        assert panel["content"][1]["content"][0]["text"] == "inner"

    def test_flattening_recurses_into_a_forbidden_wrapper(self) -> None:
        """No forbidden wrapper survives, however deeply it is nested.

        The Markdown parser cannot currently build a quote inside a panel
        inside a panel -- each fence flattens its own body -- so the guard is
        exercised directly rather than depending on that being true forever.
        """
        nested = [
            {"type": "paragraph", "content": [{"type": "text", "text": "outer"}]},
            {
                "type": "panel",
                "attrs": {"panelType": "note"},
                "content": [
                    {
                        "type": "blockquote",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "deep"}],
                            }
                        ],
                    }
                ],
            },
        ]
        flattened = _flatten_panel_content(nested)
        assert [n["type"] for n in flattened] == ["paragraph", "paragraph"]
        assert flattened[1]["content"][0]["text"] == "deep"

    def test_flattened_quote_keeps_its_inline_marks(self) -> None:
        """Only the block wrapper is dropped; inline marks are untouched."""
        result = markdown_to_adf(":::error\n> **bold** quote\n:::")
        paragraph = result["content"][0]["content"][0]
        assert paragraph["type"] == "paragraph"
        bold = next(
            node
            for node in paragraph["content"]
            if any(mark["type"] == "strong" for mark in node.get("marks", []))
        )
        assert bold["text"] == "bold"

    def test_flattening_warns_naming_the_dropped_wrapper(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The lossy rewrite is logged, not silent."""
        with caplog.at_level(logging.WARNING):
            markdown_to_adf(":::warning\n> quoted line\n:::")

        messages = [
            r.getMessage()
            for r in caplog.records
            if r.name.endswith("models.jira.adf") and r.levelno == logging.WARNING
        ]
        assert len(messages) == 1
        assert messages[0].startswith("Flattened blockquote inside an ADF panel")
        assert "INVALID_INPUT" in messages[0]

    def test_panel_without_forbidden_children_logs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A well-formed panel converts silently."""
        with caplog.at_level(logging.WARNING):
            markdown_to_adf(":::info\nbody\n- item\n:::")

        assert [r for r in caplog.records if r.name.endswith("models.jira.adf")] == []

    @pytest.mark.parametrize(
        ("markdown", "expected_type"),
        [
            ("body", "paragraph"),
            ("### Inner heading", "heading"),
            ("- item", "bulletList"),
            ("1. item", "orderedList"),
            ("```python\ny = 2\n```", "codeBlock"),
        ],
        ids=["paragraph", "heading", "bulletList", "orderedList", "codeBlock"],
    )
    def test_panel_keeps_accepted_children_nested(
        self, markdown: str, expected_type: str
    ) -> None:
        """Every measured-accepted child type still nests inside the panel."""
        result = markdown_to_adf(f":::info\n{markdown}\n:::")
        panel = result["content"][0]
        assert panel["type"] == "panel"
        assert [n["type"] for n in panel["content"]] == [expected_type]

    def test_blockquote_outside_a_panel_still_nests(self) -> None:
        """The guard is scoped to panels: a top-level quote is unaffected."""
        result = markdown_to_adf("> quoted line")
        assert result["content"][0]["type"] == "blockquote"
        assert result["content"][0]["content"][0]["type"] == "paragraph"


class TestMarkdownToAdfExpandFence:
    """Tests for the ``:::expand <title>`` fence in markdown_to_adf."""

    def test_expand_fence_with_title(self) -> None:
        """The fence line's remainder becomes the expand title."""
        result = markdown_to_adf(":::expand Deployment details\nbody\n:::")
        assert len(result["content"]) == 1
        expand = result["content"][0]
        assert expand["type"] == "expand"
        assert expand["attrs"] == {"title": "Deployment details"}
        assert [n["type"] for n in expand["content"]] == ["paragraph"]

    def test_expand_fence_without_title(self) -> None:
        """A missing title is the empty string, never None."""
        result = markdown_to_adf(":::expand\nbody\n:::")
        expand = result["content"][0]
        assert expand["type"] == "expand"
        assert expand["attrs"]["title"] == ""

    def test_expand_fence_title_whitespace_is_trimmed(self) -> None:
        """Padding around the title is dropped; inner spaces are kept."""
        result = markdown_to_adf(":::expand   Two  words   \nbody\n:::")
        assert result["content"][0]["attrs"]["title"] == "Two  words"

    def test_expand_fence_content_is_parsed_recursively(self) -> None:
        """Fence content is parsed exactly as panel content is."""
        result = markdown_to_adf(
            ":::expand Steps\n## Heading\n- item\n```\ncode\n```\n:::"
        )
        expand = result["content"][0]
        assert [n["type"] for n in expand["content"]] == [
            "heading",
            "bulletList",
            "codeBlock",
        ]

    def test_expand_fence_preserves_surrounding_content(self) -> None:
        """Content before and after the fence is preserved."""
        result = markdown_to_adf("Before\n:::expand Title\nInside\n:::\nAfter")
        types = [n["type"] for n in result["content"]]
        assert types == ["paragraph", "expand", "paragraph"]

    def test_expand_fence_does_not_flatten_a_blockquote(self) -> None:
        """The panel nesting guard is deliberately not applied to expand.

        Only ``panel`` was measured against Jira; the ADF schema allows a
        blockquote inside ``expand``, so it is left nested.
        """
        result = markdown_to_adf(":::expand Title\n> quoted line\n:::")
        expand = result["content"][0]
        assert expand["type"] == "expand"
        assert [n["type"] for n in expand["content"]] == ["blockquote"]

    def test_expand_fence_keeps_a_literal_title_keyword(self) -> None:
        """A fence title is literal text, unlike the wiki macro's title=.

        The read path resolves ``{expand:title=T}`` to ``:::expand T`` itself,
        so a fence still carrying ``title=`` is either hand-authored or a
        read-path bug; stripping it here would hide the latter.
        """
        result = markdown_to_adf(":::expand title=X\nbody\n:::")
        assert result["content"][0]["attrs"]["title"] == "title=X"

    def test_expand_prefixed_word_is_not_an_expand_fence(self) -> None:
        """:::expanded is not the expand fence and is left to fall through."""
        result = markdown_to_adf(":::expanded\nbody\n:::")
        assert not any(n["type"] == "expand" for n in result["content"])
        assert not any(n["type"] == "panel" for n in result["content"])

    def test_expand_fence_and_panel_fence_coexist(self) -> None:
        """Both fence spellings can appear in one document."""
        result = markdown_to_adf(":::expand T\ninside\n:::\n:::tip\nhint\n:::")
        types = [n["type"] for n in result["content"]]
        assert types == ["expand", "panel"]
        assert result["content"][1]["attrs"]["panelType"] == "success"


class TestMarkdownToJiraDispatch:
    """Tests for _markdown_to_jira Cloud/Server dispatch."""

    @pytest.fixture
    def cloud_client(self):
        """Create a mock JiraClient configured for Cloud."""
        with patch("atlassian.Jira"):
            from mcp_atlassian.jira.client import JiraClient

            client = MagicMock(spec=JiraClient)
            client.config = MagicMock()
            client.config.is_cloud = True
            client.preprocessor = MagicMock()
            # Bind the real method to the mock
            client._markdown_to_jira = JiraClient._markdown_to_jira.__get__(
                client, JiraClient
            )
            return client

    @pytest.fixture
    def server_client(self):
        """Create a mock JiraClient configured for Server/DC."""
        with patch("atlassian.Jira"):
            from mcp_atlassian.jira.client import JiraClient

            client = MagicMock(spec=JiraClient)
            client.config = MagicMock()
            client.config.is_cloud = False
            client.preprocessor = MagicMock()
            client.preprocessor.markdown_to_jira.return_value = "wiki markup"
            client._markdown_to_jira = JiraClient._markdown_to_jira.__get__(
                client, JiraClient
            )
            return client

    def test_server_returns_string(self, server_client):
        """Server/DC path returns a string (wiki markup)."""
        result = server_client._markdown_to_jira("# Hello")
        assert isinstance(result, str)

    def test_cloud_returns_adf_dict(self, cloud_client):
        """Cloud path returns an ADF dict with version/type/content."""
        result = cloud_client._markdown_to_jira("# Hello")
        assert isinstance(result, dict)
        assert result["version"] == 1
        assert result["type"] == "doc"
        assert isinstance(result["content"], list)

    def test_cloud_empty(self, cloud_client):
        """Cloud path with empty string returns an ADF dict."""
        result = cloud_client._markdown_to_jira("")
        assert isinstance(result, dict)
        assert result["version"] == 1
        assert result["type"] == "doc"

    def test_server_empty(self, server_client):
        """Server/DC path with empty string returns empty string."""
        result = server_client._markdown_to_jira("")
        assert result == ""
