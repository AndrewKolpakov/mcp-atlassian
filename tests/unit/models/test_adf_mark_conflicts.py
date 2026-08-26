"""Tests for the refusal of bodies Jira would reject whole.

A text node may carry ``code`` beside exactly one other mark, ``link``. Paired
with ``strong``, ``em`` or ``strike``, Jira Cloud answers the write with a 400
that names no field and stores *nothing* -- not the offending paragraph, the
entire field. The converter used to emit that document happily and the author
learnt about it from the API, one defect at a time.

These tests pin three things: that such a body is refused, that the refusal
carries EVERY offence rather than the first (which is what stops a caller
paying one round trip per defect), and that the two constructs which look like
the defect and are not stay accepted.

Note the import path. ``tests/unit/models/test_adf.py`` imports through
``src.mcp_atlassian...``, which is a *different module object* from
``mcp_atlassian...`` -- with a different ``AdfMarkConflictError`` class, so an
``except`` against one does not catch the other. Everything here uses the
canonical path, the one the running server imports.
"""

from unittest.mock import MagicMock, patch

import pytest

from mcp_atlassian.models.jira.adf import (
    AdfMarkConflictError,
    find_mark_conflicts,
    markdown_to_adf,
)

FLAGSHIP = "- **T1 of `plans/foo.md`** (tail)"


class TestRefusal:
    """The constructs Jira refuses, and the ones it does not."""

    def test_bold_around_a_code_span_is_refused(self):
        with pytest.raises(AdfMarkConflictError) as excinfo:
            markdown_to_adf(FLAGSHIP)

        (conflict,) = excinfo.value.conflicts
        assert conflict.text == "plans/foo.md"
        assert conflict.conflicting_marks == ("strong",)

    @pytest.mark.parametrize(
        ("body", "expected_mark"),
        [
            ("*the `run_id` value*", "em"),
            ("~~drop `legacy` now~~", "strike"),
            ("***all `three` at once***", "em"),
            ("***all `three` at once***", "strong"),
        ],
    )
    def test_every_emphasis_is_refused(self, body, expected_mark):
        with pytest.raises(AdfMarkConflictError) as excinfo:
            markdown_to_adf(body)

        assert expected_mark in excinfo.value.conflicts[0].conflicting_marks

    def test_a_code_span_inside_a_link_is_accepted(self):
        """``link`` is the one mark ADF permits beside ``code``."""
        document = markdown_to_adf("[`code in link`](https://example.com)")

        marks = document["content"][0]["content"][0]["marks"]
        assert {mark["type"] for mark in marks} == {"code", "link"}

    def test_a_fenced_quote_of_the_construct_is_accepted(self):
        """A body documenting this defect must not trip over its own example.

        Inside a fence the text is a ``codeBlock`` and carries no marks at all,
        so Jira accepts it. Refusing it would be the false-positive class this
        check exists to avoid: a gate that blocks valid writes gets turned off.
        """
        markdown_to_adf(f"Never write this:\n\n```\n{FLAGSHIP}\n```\n")

    def test_a_single_asterisk_closed_by_an_inner_asterisk_is_accepted(self):
        """``*see `a/*` here*`` is not italic at all -- the span ends early.

        A lone ``*`` is terminated by the asterisk inside the code span, so no
        ``em`` mark is ever stacked. Only a scan that decides for itself gets
        this wrong; reading the converted document cannot.
        """
        markdown_to_adf("see *the `aws-actions/*` path* here")

    def test_code_alone_is_accepted(self):
        markdown_to_adf("a plain `code` span and **plain bold**")


class TestEveryOffenceIsReported:
    """One conversion, one error, every offence -- the round-trip criterion."""

    def test_one_error_carries_every_violation_not_the_first(self):
        body = (
            "## Plan\n\n"
            f"{FLAGSHIP}\n"
            "- plain row, fine\n"
            "- **The `aws-actions/*` namespace is not mirrored**\n\n"
            "Prose mentioning *the `run_id` value* inline.\n\n"
            "~~Dropped `legacy_flag` entirely~~\n"
        )

        with pytest.raises(AdfMarkConflictError) as excinfo:
            markdown_to_adf(body)

        assert len(excinfo.value.conflicts) == 4

    def test_a_violation_inside_a_panel_is_found_with_the_one_after_it(self):
        """The reason parsing and validation are separate passes.

        A panel re-enters the parser. Validating inside that recursive call
        would raise before the outer walk reached the sibling below it, and the
        author would fix the panel, resend, and be refused again -- exactly the
        round trip this design removes.
        """
        body = ":::info\n- **A `b` c**\n:::\n\n- **D `e` f**\n"

        with pytest.raises(AdfMarkConflictError) as excinfo:
            markdown_to_adf(body)

        assert [c.text for c in excinfo.value.conflicts] == ["b", "e"]

    def test_the_message_names_a_line_for_every_offence(self):
        body = f"{FLAGSHIP}\n\n*the `run_id` value*\n"

        with pytest.raises(AdfMarkConflictError) as excinfo:
            markdown_to_adf(body)

        message = str(excinfo.value)
        assert "line 1" in message
        assert "line 3" in message
        assert "plans/foo.md" in message
        assert "run_id" in message

    def test_line_numbers_skip_fenced_blocks(self):
        """A quoted example must not steal the line number from a real one.

        The quote has to hold the SAME code span as the real violation, or the
        search would never have looked at it and the test proves nothing about
        masking -- the first version of this test made exactly that mistake and
        passed with the masking removed.
        """
        body = f"Never write this:\n\n```\n{FLAGSHIP}\n```\n\n{FLAGSHIP}\n"

        with pytest.raises(AdfMarkConflictError) as excinfo:
            markdown_to_adf(body)

        (conflict,) = excinfo.value.conflicts
        assert conflict.line == 7
        assert conflict.source_line == FLAGSHIP

    def test_repeated_spans_point_at_successive_lines(self):
        """Two identical mistakes name two places to edit, not one twice."""
        body = f"{FLAGSHIP}\n\n{FLAGSHIP}\n"

        with pytest.raises(AdfMarkConflictError) as excinfo:
            markdown_to_adf(body)

        assert [c.line for c in excinfo.value.conflicts] == [1, 3]

    def test_a_pathological_body_summarises_instead_of_listing(self):
        body = "\n\n".join(f"- **row {n} of `path/{n}`**" for n in range(30))

        with pytest.raises(AdfMarkConflictError) as excinfo:
            markdown_to_adf(body)

        message = str(excinfo.value)
        assert len(excinfo.value.conflicts) == 30
        assert "and 10 more of the same kind" in message


class TestSuggestedFix:
    """The offered fix has to be correct, or it costs a round trip of its own."""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (FLAGSHIP, "- **T1 of** `plans/foo.md` (tail)"),
            ("*the `run_id` value*", "*the* `run_id` *value*"),
            ("~~drop `legacy` now~~", "~~drop~~ `legacy` ~~now~~"),
            ("***all `three` at once***", "***all*** `three` ***at once***"),
        ],
    )
    def test_the_fix_closes_the_emphasis_around_the_code_span(self, body, expected):
        (conflict,) = find_mark_conflicts(body)

        assert conflict.suggestion == expected

    def test_an_emphasis_holding_only_a_code_span_loses_the_emphasis(self):
        """Emphasising nothing would emit ``****``, which is a new defect."""
        (conflict,) = find_mark_conflicts("**`justcode`**")

        assert conflict.suggestion == "`justcode`"

    @pytest.mark.parametrize(
        "body",
        [
            FLAGSHIP,
            "*the `run_id` value*",
            "~~drop `legacy` now~~",
            "***all `three` at once***",
            "**`justcode`**",
            "**a `x` b `y` c**",
            "- **The `aws-actions/*` namespace is not mirrored**",
        ],
    )
    def test_every_suggested_fix_is_itself_accepted(self, body):
        """The fix must not be markup that is broken a second way.

        ``**T1 of **`` is not bold in CommonMark -- a closing run may not follow
        whitespace -- so a naive split would hand back something that silently
        renders wrong instead of being refused, which is strictly harder to
        notice than the refusal it replaced.
        """
        for conflict in find_mark_conflicts(body):
            assert conflict.suggestion is not None
            markdown_to_adf(conflict.suggestion)

    def test_applying_every_fix_clears_the_whole_body_in_one_edit(self):
        """The claim the error message makes, tested end to end."""
        body = (
            "## Plan\n\n"
            f"{FLAGSHIP}\n"
            "- **The `aws-actions/*` namespace is not mirrored**\n\n"
            "Prose mentioning *the `run_id` value* inline.\n\n"
            "~~Dropped `legacy_flag` entirely~~\n"
        )
        conflicts = find_mark_conflicts(body)
        assert len(conflicts) == 4

        lines = body.split("\n")
        for conflict in conflicts:
            assert conflict.line is not None
            lines[conflict.line - 1] = conflict.suggestion

        markdown_to_adf("\n".join(lines))


class TestFindMarkConflicts:
    """The reporting entry point, for callers that need the list."""

    def test_it_reports_instead_of_raising(self):
        conflicts = find_mark_conflicts(FLAGSHIP)

        assert [c.text for c in conflicts] == ["plans/foo.md"]

    def test_a_clean_body_reports_nothing(self):
        assert find_mark_conflicts("- **T1 of** `plans/foo.md` (tail)") == []

    def test_an_empty_body_reports_nothing(self):
        assert find_mark_conflicts("") == []


class TestTheWritePathDoesNotSwallowIt:
    """``_markdown_to_jira`` degrades other failures; it must not degrade this.

    Its ``except Exception`` returns the author's Markdown as a plain-text
    paragraph and logs a warning. For a body Jira refuses whole that is the
    worst available answer: the write *succeeds*, the field renders as literal
    Markdown, and nobody is told. The narrow re-raise is what makes the check
    reach a caller at all.
    """

    @pytest.fixture
    def cloud_client(self):
        with patch("atlassian.Jira"):
            from mcp_atlassian.jira.client import JiraClient

            client = MagicMock(spec=JiraClient)
            client.config = MagicMock()
            client.config.is_cloud = True
            client.config.url = "https://example.atlassian.net"
            client.config.projects_filter = None
            client.preprocessor = MagicMock()
            client._markdown_to_jira = JiraClient._markdown_to_jira.__get__(
                client, JiraClient
            )
            return client

    @pytest.fixture
    def server_client(self):
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

    def test_cloud_refuses_rather_than_publishing_literal_markdown(self, cloud_client):
        with pytest.raises(AdfMarkConflictError):
            cloud_client._markdown_to_jira(FLAGSHIP)

    def test_the_refusal_reaches_the_caller_with_every_line_in_it(self, cloud_client):
        """What a tool error will actually show, since ``str(e)`` is preserved
        all the way to ``ToolError``."""
        body = f"{FLAGSHIP}\n\n*the `run_id` value*\n"

        with pytest.raises(AdfMarkConflictError) as excinfo:
            cloud_client._markdown_to_jira(body)

        message = str(excinfo.value)
        assert "line 1" in message
        assert "line 3" in message
        assert "fix to:" in message

    def test_other_conversion_failures_still_degrade(self, cloud_client):
        """The swallow was narrowed, not removed."""
        with patch(
            "mcp_atlassian.jira.client.markdown_to_adf",
            side_effect=RuntimeError("boom"),
        ):
            result = cloud_client._markdown_to_jira("# Hello")

        assert result["content"][0]["content"][0]["text"] == "# Hello"

    def test_server_is_untouched(self, server_client):
        """Server/DC sends wiki markup, so there is no ADF to be refused over."""
        assert server_client._markdown_to_jira(FLAGSHIP) == "wiki markup"
