"""Round-trip contract for Jira panels, quotes and admonition macros.

Every wiki string in this module, and every ADF shape it is asserted to
reproduce, was *measured* on the live Jira Cloud instance
``managego.atlassian.net`` (ticket PLOOP-12619) by writing ADF through
``/rest/api/3`` and reading it back through ``/rest/api/2`` -- the exact path
every mcp-atlassian read takes.  None of it is inferred, so a failure here is a
converter defect and not a stale expectation.

The invariant under test is that one read+write cycle is a fixed point::

    markdown_to_adf(preprocessor.jira_to_markdown(wiki))
        == the ADF node shape Jira rendered that wiki from

That ties the read path (:mod:`mcp_atlassian.preprocessing.jira`) and the write
path (:mod:`mcp_atlassian.models.jira.adf`) to one Markdown vocabulary:
``:::info``, ``:::note``, ``:::warning``, ``:::success``, ``:::error`` and
``:::expand <title>``, each closed by a bare ``:::``.  A panel has no title in
ADF, so a wiki panel title becomes a bold first paragraph *inside* the fence --
that spelling was measured to round-trip exactly.
"""

import json
from typing import Any

import pytest

from mcp_atlassian.models.jira.adf import markdown_to_adf
from mcp_atlassian.preprocessing.jira import JiraPreprocessor

# The attrs this contract is about.  Everything else a node may carry
# (localId and friends) is incidental and would only make failures brittle.
_SIGNIFICANT_ATTRS = frozenset({"panelType", "level", "language", "title"})


@pytest.fixture
def preprocessor() -> JiraPreprocessor:
    """A Jira preprocessor with translation enabled, as every read uses."""
    return JiraPreprocessor(base_url="https://example.atlassian.net")


def _shape(node: dict[str, Any]) -> dict[str, Any]:
    """Reduce an ADF node to the parts this contract pins.

    Keeps the node type, the meaningful attrs, the mark types and the text,
    and recurses into content.  Comparing shapes instead of raw ADF dicts
    keeps a failure readable while still catching a wrong ``panelType``, a
    dropped node or an extra one.
    """
    shape: dict[str, Any] = {"type": node["type"]}
    attrs = {
        key: value
        for key, value in (node.get("attrs") or {}).items()
        if key in _SIGNIFICANT_ATTRS
    }
    if attrs:
        shape["attrs"] = attrs
    marks = [mark["type"] for mark in node.get("marks", [])]
    if marks:
        shape["marks"] = marks
    if "text" in node:
        shape["text"] = node["text"]
    if node.get("content"):
        shape["content"] = [_shape(child) for child in node["content"]]
    return shape


def _shapes(adf: dict[str, Any]) -> list[dict[str, Any]]:
    """Reduce every top-level node of an ADF document to its shape."""
    return [_shape(node) for node in adf["content"]]


def _text(text: str, *marks: str) -> dict[str, Any]:
    """Build the expected shape of a text node with the given mark types."""
    shape: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        shape["marks"] = list(marks)
    return shape


def _para(text: str, *marks: str) -> dict[str, Any]:
    """Build the expected shape of a single-text-node paragraph."""
    return {"type": "paragraph", "content": [_text(text, *marks)]}


def _panel(panel_type: str, *content: dict[str, Any]) -> dict[str, Any]:
    """Build the expected shape of a panel of the given panelType."""
    return {
        "type": "panel",
        "attrs": {"panelType": panel_type},
        "content": list(content),
    }


def _quote(*content: dict[str, Any]) -> dict[str, Any]:
    """Build the expected shape of a blockquote."""
    return {"type": "blockquote", "content": list(content)}


def _code(language: str, text: str) -> dict[str, Any]:
    """Build the expected shape of a fenced code block."""
    return {
        "type": "codeBlock",
        "attrs": {"language": language},
        "content": [_text(text)],
    }


def _node_types(node: dict[str, Any]) -> set[str]:
    """Collect the type of a node and of every descendant it has."""
    types = {node["type"]}
    for child in node.get("content", []):
        types |= _node_types(child)
    return types


def _panel_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect every panel node at or below a node."""
    found = [node] if node.get("type") == "panel" else []
    for child in node.get("content", []):
        found.extend(_panel_nodes(child))
    return found


def _types_inside(panel: dict[str, Any]) -> set[str]:
    """Collect the node types appearing anywhere inside a panel's content."""
    inside: set[str] = set()
    for child in panel.get("content", []):
        inside |= _node_types(child)
    return inside


# (wiki Jira returned, the ADF node shapes Jira rendered it from).  Measured
# pairs -- see the module docstring.
ACCEPTANCE_PAIRS = [
    pytest.param(
        "{panel:bgColor=#deebff}\ninfo body\n{panel}",
        [_panel("info", _para("info body"))],
        id="panel-info",
    ),
    pytest.param(
        "{panel:bgColor=#eae6ff}\nnote body\n{panel}",
        [_panel("note", _para("note body"))],
        id="panel-note",
    ),
    pytest.param(
        "{panel:bgColor=#fffae6}\nwarning body\n{panel}",
        [_panel("warning", _para("warning body"))],
        id="panel-warning",
    ),
    pytest.param(
        "{panel:bgColor=#e3fcef}\nsuccess body\n{panel}",
        [_panel("success", _para("success body"))],
        id="panel-success",
    ),
    pytest.param(
        "{panel:bgColor=#ffebe6}\nerror body\n{panel}",
        [_panel("error", _para("error body"))],
        id="panel-error",
    ),
    pytest.param(
        "{panel:bgColor=#deebff}\n*Titled*\n\nafter title\n{panel}",
        [
            _panel(
                "info",
                _para("Titled", "strong"),
                _para("after title"),
            )
        ],
        id="panel-info-bold-para-then-para",
    ),
    pytest.param(
        "{panel:bgColor=#eae6ff}\n* item\n{panel}",
        [
            _panel(
                "note",
                {
                    "type": "bulletList",
                    "content": [
                        {"type": "listItem", "content": [_para("item")]},
                    ],
                },
            )
        ],
        id="panel-note-bullet-list",
    ),
    pytest.param(
        "{panel:bgColor=#deebff}\nh3. H3 inside\n\nbody\n{panel}",
        [
            _panel(
                "info",
                {
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [_text("H3 inside")],
                },
                _para("body"),
            )
        ],
        id="panel-info-heading-then-para",
    ),
    pytest.param(
        "{panel:bgColor=#deebff}\nouter\n\n{code:python}y = 2{code}\n{panel}",
        [_panel("info", _para("outer"), _code("python", "y = 2"))],
        id="panel-info-code-block",
    ),
    pytest.param(
        "{quote}first quote{quote}\n\nprose between\n\n{quote}second quote{quote}",
        [
            _quote(_para("first quote")),
            _para("prose between"),
            _quote(_para("second quote")),
        ],
        id="two-quotes-with-prose-between",
    ),
    pytest.param(
        "{quote}q with code\n\n{code:python}x = 1{code}{quote}",
        [_quote(_para("q with code"), _code("python", "x = 1"))],
        id="quote-with-code-block",
    ),
]


class TestOneCycleIsAFixedPoint:
    """A wiki string must convert back to the ADF Jira rendered it from."""

    @pytest.mark.parametrize(("wiki", "expected"), ACCEPTANCE_PAIRS)
    def test_wiki_returns_to_the_adf_jira_rendered_it_from(
        self,
        preprocessor: JiraPreprocessor,
        wiki: str,
        expected: list[dict[str, Any]],
    ) -> None:
        """Read then write reproduces the original ADF node shape.

        Read and write must agree on how each construct is spelled in
        Markdown.  When they do not -- as when the reader drops a panel to a
        bare paragraph that the writer would never lift back -- the content
        degrades a little on every edit of the same issue, silently.
        """
        markdown = preprocessor.jira_to_markdown(wiki)

        assert _shapes(markdown_to_adf(markdown)) == expected, (
            f"intermediate markdown was {markdown!r}"
        )


class TestQuoteBlocksAreMatchedPerBlock:
    """Two quote blocks in one field must stay two quote blocks."""

    TWO_QUOTES = (
        "{quote}first quote{quote}\n\nprose between\n\n{quote}second quote{quote}"
    )

    def test_prose_between_two_quotes_stays_prose(
        self, preprocessor: JiraPreprocessor
    ) -> None:
        """Each ``{quote}`` pair becomes its own blockquote.

        Jira really does emit two separate quote blocks in one field, so a
        greedy match spans from the first opening token to the last closing
        one and swallows everything between -- here, a paragraph of ordinary
        prose that is not quoted at all.
        """
        adf = markdown_to_adf(preprocessor.jira_to_markdown(self.TWO_QUOTES))

        assert _shapes(adf) == [
            _quote(_para("first quote")),
            _para("prose between"),
            _quote(_para("second quote")),
        ]

    def test_no_quote_token_leaks_into_the_converted_text(
        self, preprocessor: JiraPreprocessor
    ) -> None:
        """A converted ``{quote}`` leaves no literal token behind.

        A greedy match consumes the inner closing and opening tokens as if
        they were quoted text, so they resurface as visible ``{quote}`` in the
        rendered issue.
        """
        markdown = preprocessor.jira_to_markdown(self.TWO_QUOTES)
        adf = markdown_to_adf(markdown)

        assert "{quote}" not in markdown
        assert "{quote}" not in json.dumps(adf)


class TestPanelBackgroundColourSelectsThePanelType:
    """The colour Jira emits carries the whole semantic of a panel."""

    @pytest.mark.parametrize(
        ("bg_color", "panel_type"),
        [
            pytest.param("#deebff", "info", id="deebff-info"),
            pytest.param("#eae6ff", "note", id="eae6ff-note"),
            pytest.param("#fffae6", "warning", id="fffae6-warning"),
            pytest.param("#e3fcef", "success", id="e3fcef-success"),
            pytest.param("#ffebe6", "error", id="ffebe6-error"),
        ],
    )
    def test_each_measured_colour_reaches_its_own_panel_type(
        self,
        preprocessor: JiraPreprocessor,
        bg_color: str,
        panel_type: str,
    ) -> None:
        """The five colours Jira Cloud emits map to five distinct types.

        Jira Cloud never emits ``title=`` on a panel it rendered from ADF; it
        emits ``bgColor``, and the colour is the only surviving record of
        whether the author meant info, warning or error.  Asserting the type
        rather than merely that *a* panel appeared is the point: a reader that
        collapsed every colour to ``info`` would satisfy the weaker check
        while destroying the distinction.
        """
        wiki = f"{{panel:bgColor={bg_color}}}\nbody\n{{panel}}"

        adf = markdown_to_adf(preprocessor.jira_to_markdown(wiki))

        assert _shapes(adf) == [_panel(panel_type, _para("body"))]

    def test_colour_matching_ignores_hex_case(
        self, preprocessor: JiraPreprocessor
    ) -> None:
        """An upper-case hex value selects the same panel type."""
        wiki = "{panel:bgColor=#DEEBFF}\nbody\n{panel}"

        adf = markdown_to_adf(preprocessor.jira_to_markdown(wiki))

        assert _shapes(adf) == [_panel("info", _para("body"))]

    def test_other_panel_params_do_not_defeat_colour_matching(
        self, preprocessor: JiraPreprocessor
    ) -> None:
        """Only the hex value is compared; Jira may add params beside it."""
        wiki = "{panel:borderStyle=dashed|bgColor=#fffae6}\nbody\n{panel}"

        adf = markdown_to_adf(preprocessor.jira_to_markdown(wiki))

        assert _shapes(adf) == [_panel("warning", _para("body"))]

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param("", id="no-params"),
            pytest.param(":bgColor=#123456", id="unrecognised-colour"),
        ],
    )
    def test_a_panel_without_a_known_colour_is_still_a_panel(
        self, preprocessor: JiraPreprocessor, params: str
    ) -> None:
        """An unknown or absent colour falls back to info, never to prose."""
        wiki = f"{{panel{params}}}\nbody\n{{panel}}"

        adf = markdown_to_adf(preprocessor.jira_to_markdown(wiki))

        assert _shapes(adf) == [_panel("info", _para("body"))]

    def test_a_panel_title_becomes_a_bold_first_paragraph_inside_the_panel(
        self, preprocessor: JiraPreprocessor
    ) -> None:
        """ADF has no title on a panel, so the title moves inside it.

        Measured: a bold first paragraph inside the fence round-trips exactly,
        which is why the title is spelled that way rather than dropped or kept
        as a fence argument the writer cannot express.
        """
        wiki = "{panel:bgColor=#fffae6|title=Heads up}\nbody\n{panel}"

        adf = markdown_to_adf(preprocessor.jira_to_markdown(wiki))

        assert _shapes(adf) == [
            _panel("warning", _para("Heads up", "strong"), _para("body"))
        ]


class TestAdmonitionAndExpandMacrosConvert:
    """Server/DC authors write admonitions as macros, not as panels."""

    @pytest.mark.parametrize(
        ("macro", "panel_type"),
        [
            pytest.param("info", "info", id="info"),
            pytest.param("note", "note", id="note"),
            pytest.param("warning", "warning", id="warning"),
            pytest.param("tip", "success", id="tip-is-success"),
        ],
    )
    def test_an_admonition_macro_becomes_a_panel_of_its_own_type(
        self,
        preprocessor: JiraPreprocessor,
        macro: str,
        panel_type: str,
    ) -> None:
        """Each admonition macro reaches the panel type it means.

        ADF has no ``tip`` panel, so ``{tip}`` maps onto ``success`` -- the
        closest type Jira accepts.
        """
        wiki = f"{{{macro}}}\nbody\n{{{macro}}}"

        adf = markdown_to_adf(preprocessor.jira_to_markdown(wiki))

        assert _shapes(adf) == [_panel(panel_type, _para("body"))]

    def test_an_admonition_macro_title_becomes_a_bold_first_paragraph(
        self, preprocessor: JiraPreprocessor
    ) -> None:
        """A macro title is spelled like a panel title: bold, inside."""
        wiki = "{info:title=Heads up}\nbody\n{info}"

        adf = markdown_to_adf(preprocessor.jira_to_markdown(wiki))

        assert _shapes(adf) == [
            _panel("info", _para("Heads up", "strong"), _para("body"))
        ]

    @pytest.mark.parametrize(
        "wiki",
        [
            pytest.param(
                "{expand:More detail}\nhidden body\n{expand}",
                id="positional-title",
            ),
            pytest.param(
                "{expand:title=More detail}\nhidden body\n{expand}",
                id="title-keyword",
            ),
        ],
    )
    def test_an_expand_macro_becomes_an_expand_node_carrying_its_title(
        self, preprocessor: JiraPreprocessor, wiki: str
    ) -> None:
        """Both spellings of the macro title reach ``attrs.title``.

        Jira writes the title either positionally or as ``title=``; both are
        the same construct to an author, so both must convert.
        """
        adf = markdown_to_adf(preprocessor.jira_to_markdown(wiki))

        assert _shapes(adf) == [
            {
                "type": "expand",
                "attrs": {"title": "More detail"},
                "content": [_para("hidden body")],
            }
        ]

    def test_an_untitled_expand_macro_gets_an_empty_title(
        self, preprocessor: JiraPreprocessor
    ) -> None:
        """A macro with no title yields ``attrs.title == ""``, not a drop."""
        adf = markdown_to_adf(
            preprocessor.jira_to_markdown("{expand}\nhidden body\n{expand}")
        )

        assert _shapes(adf) == [
            {
                "type": "expand",
                "attrs": {"title": ""},
                "content": [_para("hidden body")],
            }
        ]


class TestPanelsAreNeverNestedIllegally:
    """Jira rejects two nestings outright, so the writer must flatten them.

    Both were measured against ``/rest/api/3`` on PLOOP-12619: an ADF
    ``panel`` containing a ``blockquote`` and an ADF ``panel`` containing
    another ``panel`` are each answered with **400 INVALID_INPUT**.  Emitting
    either would turn today's lossy read into a hard write failure on the next
    edit of the same issue -- strictly worse than losing the nesting.
    """

    def test_a_wiki_panel_wrapping_a_quote_flattens_the_quote(
        self, preprocessor: JiraPreprocessor
    ) -> None:
        """A quote inside a panel becomes a paragraph inside the panel.

        Server/DC authors do put a ``{quote}`` inside a ``{panel}``, so this
        input is reachable from real content, not hypothetical.  Jira answers
        400 INVALID_INPUT for an ADF panel containing a blockquote, so the
        quote has to be flattened rather than nested.
        """
        wiki = "{panel:bgColor=#fffae6}\n{quote}quoted inside{quote}\n{panel}"

        adf = markdown_to_adf(preprocessor.jira_to_markdown(wiki))

        assert _shapes(adf) == [_panel("warning", _para("quoted inside"))]

    @pytest.mark.parametrize(
        "markdown",
        [
            pytest.param(
                ":::warning\n> quoted inside\n:::",
                id="quote-inside-a-fence",
            ),
            pytest.param(
                ":::info\n:::warning\ninner\n:::\n:::",
                id="fence-inside-a-fence",
            ),
        ],
    )
    def test_no_produced_panel_contains_a_blockquote_or_another_panel(
        self, markdown: str
    ) -> None:
        """The forbidden nestings never appear, however the fence is authored.

        Jira answers 400 INVALID_INPUT for a ``panel`` holding a
        ``blockquote`` and for a ``panel`` holding a ``panel``.  Only the
        absence is pinned here: how the writer flattens the inner block is its
        own choice, and the measured contract does not fix it.
        """
        adf = markdown_to_adf(markdown)
        panels = _panel_nodes(adf)

        assert panels, f"no panel was produced from {markdown!r}"
        for panel in panels:
            inside = _types_inside(panel)
            assert "blockquote" not in inside, "400 INVALID_INPUT: panel > blockquote"
            assert "panel" not in inside, "400 INVALID_INPUT: panel > panel"


class TestCodeMacroProtectionDoesNotRegress:
    """Markup inside ``{code}``/``{noformat}`` must stay literal (#1059)."""

    @pytest.mark.parametrize(
        ("wiki", "markdown"),
        [
            pytest.param(
                "{code}{quote}quoted{quote}{code}",
                "```\n{quote}quoted{quote}\n```",
                id="quote-in-code",
            ),
            pytest.param(
                "{code}{color:red}c{color}{code}",
                "```\n{color:red}c{color}\n```",
                id="color-in-code",
            ),
            pytest.param(
                "{code}{panel:title=X}body{panel}{code}",
                "```\n{panel:title=X}body{panel}\n```",
                id="panel-in-code",
            ),
        ],
    )
    def test_markup_inside_a_code_macro_is_not_converted(
        self,
        preprocessor: JiraPreprocessor,
        wiki: str,
        markdown: str,
    ) -> None:
        """Sample code that mentions a macro keeps mentioning it.

        The quote, colour and panel passes all run over the whole field, so
        each one has to respect the placeholder extraction that shields code
        content.  Widening any of those patterns is exactly how that shield
        gets breached.
        """
        assert preprocessor.jira_to_markdown(wiki) == markdown


class TestExpandRoundTripBoundary:
    """What is guaranteed for ``expand``, and what Jira itself takes away."""

    def test_an_expand_node_is_produced_from_the_fence_that_spells_it(
        self,
    ) -> None:
        """Writing an ``expand`` is guaranteed; reading one back is not.

        The limitation here is Jira's, not this converter's.  Measured on
        PLOOP-12619: an ADF ``expand`` survives the write -- a subsequent v3
        read still showed the node typed ``expand`` -- but Jira's own ADF to
        wiki conversion on the v2 read, the path every mcp-atlassian read
        takes, flattens it to ``*title*`` followed by the inlined content.  No
        ``{expand}`` macro ever reaches the reader from a Cloud instance, so a
        Cloud ``expand`` cannot round-trip and this module does not ask it to.

        What is guaranteed is this write half, plus the wiki-authored read
        half that Server/DC content still needs, pinned by
        ``test_an_expand_macro_becomes_an_expand_node_carrying_its_title``.
        """
        adf = markdown_to_adf(":::expand More detail\nhidden body\n:::")

        assert _shapes(adf) == [
            {
                "type": "expand",
                "attrs": {"title": "More detail"},
                "content": [_para("hidden body")],
            }
        ]

    def test_the_flattened_wiki_jira_returns_carries_no_expand(
        self, preprocessor: JiraPreprocessor
    ) -> None:
        """The wiki a Cloud read really returns has no ``expand`` left in it.

        This is the other side of the boundary above, and it is a statement
        about Jira, not a defect to fix: by the time the bytes reach the
        reader the collapsible block is already gone, so no read-path rule
        could recover it.
        """
        flattened_by_jira = "*More detail*\nhidden body"

        adf = markdown_to_adf(preprocessor.jira_to_markdown(flattened_by_jira))

        assert "expand" not in {
            node_type for node in adf["content"] for node_type in _node_types(node)
        }
