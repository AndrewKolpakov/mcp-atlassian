import re

import pytest

from mcp_atlassian.preprocessing.confluence import ConfluencePreprocessor
from mcp_atlassian.preprocessing.jira import (
    _ESCAPABLE_CHARS,
    JiraPreprocessor,
    _protect_escaped_chars,
)
from tests.fixtures.confluence_mocks import MOCK_COMMENTS_RESPONSE, MOCK_PAGE_RESPONSE
from tests.fixtures.jira_mocks import MOCK_JIRA_ISSUE_RESPONSE
from tests.utils.mocks import MockConfluenceClient


@pytest.fixture
def preprocessor_with_jira():
    return JiraPreprocessor(base_url="https://example.atlassian.net")


@pytest.fixture
def preprocessor_with_jira_markup_translation_disabled():
    return JiraPreprocessor(
        base_url="https://example.atlassian.net", disable_translation=True
    )


@pytest.fixture
def preprocessor_with_confluence():
    return ConfluencePreprocessor(base_url="https://example.atlassian.net")


def test_init():
    """Test JiraPreprocessor initialization."""
    processor = JiraPreprocessor("https://example.atlassian.net/")
    assert processor.base_url == "https://example.atlassian.net"


def test_process_confluence_page_content(preprocessor_with_confluence):
    """Test processing Confluence page content using mock data."""
    html_content = MOCK_PAGE_RESPONSE["body"]["storage"]["value"]
    processed_html, processed_markdown = (
        preprocessor_with_confluence.process_html_content(
            html_content, confluence_client=MockConfluenceClient()
        )
    )

    # Verify user mention is processed
    assert "@Test User user123" in processed_markdown

    # Verify basic HTML elements are converted
    assert "Date" in processed_markdown
    assert "Goals" in processed_markdown
    assert "Example goal" in processed_markdown


def test_process_confluence_comment_content(preprocessor_with_confluence):
    """Test processing Confluence comment content using mock data."""
    html_content = MOCK_COMMENTS_RESPONSE["results"][0]["body"]["view"]["value"]
    processed_html, processed_markdown = (
        preprocessor_with_confluence.process_html_content(
            html_content, confluence_client=MockConfluenceClient()
        )
    )

    assert "Comment content here" in processed_markdown


def test_clean_jira_issue_content(preprocessor_with_jira):
    """Test cleaning Jira issue content using mock data."""
    description = MOCK_JIRA_ISSUE_RESPONSE["fields"]["description"]
    cleaned_text = preprocessor_with_jira.clean_jira_text(description)

    assert "test issue description" in cleaned_text.lower()

    # Test comment cleaning
    comment = MOCK_JIRA_ISSUE_RESPONSE["fields"]["comment"]["comments"][0]["body"]
    cleaned_comment = preprocessor_with_jira.clean_jira_text(comment)

    assert "test comment" in cleaned_comment.lower()


def test_process_html_content_basic(preprocessor_with_confluence):
    """Test basic HTML content processing."""
    html = "<p>Simple text</p>"
    processed_html, processed_markdown = (
        preprocessor_with_confluence.process_html_content(
            html, confluence_client=MockConfluenceClient()
        )
    )

    assert processed_html == "<p>Simple text</p>"
    assert processed_markdown.strip() == "Simple text"


def test_process_html_content_preserves_confluence_date_lozenge(
    preprocessor_with_confluence,
):
    """Date lozenges retain values stored only in datetime attributes."""
    html = '<p>Meeting date: <time datetime="2026-02-04" /></p>'

    processed_html, processed_markdown = (
        preprocessor_with_confluence.process_html_content(html)
    )

    assert '<time datetime="2026-02-04">2026-02-04</time>' in processed_html
    assert processed_markdown.strip() == "Meeting date: 2026-02-04"


def test_process_html_content_with_user_mentions(preprocessor_with_confluence):
    """Test HTML content processing with user mentions."""
    html = """
    <ac:link>
        <ri:user ri:account-id="123456"/>
    </ac:link>
    <p>Some text</p>
    """
    processed_html, processed_markdown = (
        preprocessor_with_confluence.process_html_content(
            html, confluence_client=MockConfluenceClient()
        )
    )

    assert "@Test User 123456" in processed_html
    assert "@Test User 123456" in processed_markdown


def test_clean_jira_text_empty(preprocessor_with_jira):
    """Test cleaning empty Jira text."""
    assert preprocessor_with_jira.clean_jira_text("") == ""
    assert preprocessor_with_jira.clean_jira_text(None) == ""


def test_clean_jira_text_user_mentions(preprocessor_with_jira):
    """Test cleaning Jira text with user mentions."""
    text = "Hello [~accountid:123456]!"
    cleaned = preprocessor_with_jira.clean_jira_text(text)
    assert cleaned == "Hello User:123456!"


def test_clean_jira_text_smart_links(preprocessor_with_jira):
    """Test cleaning Jira text with smart links."""
    base_url = "https://example.atlassian.net"

    # Test Jira issue link
    text = f"[Issue|{base_url}/browse/PROJ-123|smart-link]"
    cleaned = preprocessor_with_jira.clean_jira_text(text)
    assert cleaned == f"[PROJ-123]({base_url}/browse/PROJ-123)"

    # Test Confluence page link from mock data.  The title has its "+"
    # separators turned into spaces, while the URL keeps them verbatim --
    # stripping them would produce a dead link.
    confluence_url = (
        f"{base_url}/wiki/spaces/PROJ/pages/987654321/Example+Meeting+Notes"
    )
    text = f"[Meeting Notes|{confluence_url}|smart-link]"
    cleaned = preprocessor_with_jira.clean_jira_text(text)
    assert cleaned == f"[Example Meeting Notes]({confluence_url})"


@pytest.mark.parametrize(
    ("issue_key", "expected"),
    [
        (
            "B7-214-68901",
            "[B7-214-68901](https://example.atlassian.net/browse/B7-214-68901)",
        ),
        (
            "B7-214--68901",
            "[Issue](https://example.atlassian.net/browse/B7-214--68901)",
        ),
        (
            "B7-214-",
            "[Issue](https://example.atlassian.net/browse/B7-214-)",
        ),
        (
            "B7-214-68901A",
            "[Issue](https://example.atlassian.net/browse/B7-214-68901A)",
        ),
    ],
)
def test_clean_jira_text_smart_links_validate_hyphenated_issue_keys(
    preprocessor_with_jira, issue_key, expected
):
    """Smart links preserve valid keys and reject malformed suffix segments."""
    base_url = "https://example.atlassian.net"
    text = f"[Issue|{base_url}/browse/{issue_key}|smart-link]"

    assert preprocessor_with_jira.clean_jira_text(text) == expected


def test_clean_jira_text_smart_links_strip_complete_hyphenated_issue_key(
    preprocessor_with_jira,
):
    """Confluence smart-link titles strip the complete issue key."""
    base_url = "https://example.atlassian.net"
    confluence_url = (
        f"{base_url}/wiki/spaces/PROJ/pages/987654321/"
        "B7-214-68901+Example+Meeting+Notes"
    )
    text = f"[Meeting Notes|{confluence_url}|smart-link]"

    assert preprocessor_with_jira._process_smart_links(text) == (
        f"[Example Meeting Notes]({confluence_url})"
    )


def test_clean_jira_text_html_content(preprocessor_with_jira):
    """Test cleaning Jira text with HTML content."""
    text = "<p>This is <b>bold</b> text</p>"
    cleaned = preprocessor_with_jira.clean_jira_text(text)
    assert cleaned.strip() == "This is **bold** text"


def test_clean_jira_text_combined(preprocessor_with_jira):
    """Test cleaning Jira text with multiple elements."""
    base_url = "https://example.atlassian.net"
    text = f"""
    <p>Hello [~accountid:123456]!</p>
    <p>Check out [PROJ-123|{base_url}/browse/PROJ-123|smart-link]</p>
    """
    cleaned = preprocessor_with_jira.clean_jira_text(text)
    assert "Hello User:123456!" in cleaned
    assert f"[PROJ-123]({base_url}/browse/PROJ-123)" in cleaned


def test_process_html_content_error_handling(preprocessor_with_confluence):
    """Test error handling in process_html_content."""
    with pytest.raises(Exception):
        preprocessor_with_confluence.process_html_content(
            None, confluence_client=MockConfluenceClient()
        )


def test_clean_jira_text_with_invalid_html(preprocessor_with_jira):
    """Test cleaning Jira text with invalid HTML."""
    text = "<p>Unclosed paragraph with <b>bold</b"
    cleaned = preprocessor_with_jira.clean_jira_text(text)
    assert "Unclosed paragraph with **bold**" in cleaned


def test_process_mentions_error_handling(preprocessor_with_jira):
    """Test error handling in _process_mentions."""
    text = "[~accountid:invalid]"
    processed = preprocessor_with_jira._process_mentions(text, r"\[~accountid:(.*?)\]")
    assert "User:invalid" in processed


def test_jira_to_markdown(preprocessor_with_jira):
    """Test conversion of Jira markup to Markdown."""
    # Test headers
    assert preprocessor_with_jira.jira_to_markdown("h1. Heading 1") == "# Heading 1"
    assert preprocessor_with_jira.jira_to_markdown("h2. Heading 2") == "## Heading 2"

    # Test text formatting
    assert preprocessor_with_jira.jira_to_markdown("*bold text*") == "**bold text**"
    assert preprocessor_with_jira.jira_to_markdown("_italic text_") == "*italic text*"

    # Test code blocks
    assert preprocessor_with_jira.jira_to_markdown("{{code}}") == "`code`"

    # For multiline code blocks, check content is preserved rather than exact format
    converted_code_block = preprocessor_with_jira.jira_to_markdown(
        "{code}\nmultiline code\n{code}"
    )
    assert "```" in converted_code_block
    assert "multiline code" in converted_code_block

    # Test lists
    assert preprocessor_with_jira.jira_to_markdown("* Item 1") == "- Item 1"
    assert preprocessor_with_jira.jira_to_markdown("# Item 1") == "1. Item 1"

    # Test complex Jira markup
    complex_jira = """
h1. Project Overview

h2. Introduction
This project aims to *improve* the user experience.

h3. Features
* Feature 1
* Feature 2

h3. Code Example
{code:python}
def hello():
    print("Hello World")
{code}

For more information, see [our website|https://example.com].
"""

    converted = preprocessor_with_jira.jira_to_markdown(complex_jira)
    assert "# Project Overview" in converted
    assert "## Introduction" in converted
    assert "**improve**" in converted
    assert "- Feature 1" in converted
    assert "```python" in converted
    assert "[our website](https://example.com)" in converted


def test_jira_to_markdown_citation(preprocessor_with_jira):
    """Test citation markup conversion and that unmatched ?? does not cause ReDoS."""
    # Matched citation
    assert "<cite>cited text</cite>" in preprocessor_with_jira.jira_to_markdown(
        "??cited text??"
    )

    # Citation with a single ? inside
    result = preprocessor_with_jira.jira_to_markdown("??is this cited? yes??")
    assert "<cite>" in result

    # Unmatched ?? followed by inline code must complete quickly (was ReDoS before fix)
    text = "* (??) Some weird formatting"
    result = preprocessor_with_jira.jira_to_markdown(text)
    assert "<cite>" not in result


def test_jira_to_markdown_citation_no_redos(preprocessor_with_jira):
    """Regression test: complex Jira wiki markup with unmatched ?? must not hang."""
    description = (
        "h2. Known limitations\n"
        "* (??) The {{retry-handler}} -> {{fallback}} path is *broken* "
        "if the upstream timeout during {{retry-handler}} has not "
        "elapsed yet. Each component would need to track pending "
        "requests and report a metric. _This means a request could "
        "be stuck in {{retry-handler}} indefinitely._\n"
        "* Each component must validate the configuration and *stop* "
        "after detecting an invalid setting.\n"
        "h2. Monitoring\n"
        "* Report the current status through a *metric*."
    )
    result = preprocessor_with_jira.jira_to_markdown(description)
    assert "Known limitations" in result
    assert "retry-handler" in result


def test_markdown_to_jira(preprocessor_with_jira):
    """Test conversion of Markdown to Jira markup."""
    # Test headers
    assert preprocessor_with_jira.markdown_to_jira("# Heading 1") == "h1. Heading 1"
    assert preprocessor_with_jira.markdown_to_jira("## Heading 2") == "h2. Heading 2"

    # Test text formatting
    assert preprocessor_with_jira.markdown_to_jira("**bold text**") == "*bold text*"
    assert preprocessor_with_jira.markdown_to_jira("*italic text*") == "_italic text_"

    # Test code blocks
    assert preprocessor_with_jira.markdown_to_jira("`code`") == "{{code}}"

    # For multiline code blocks, check content is preserved rather than exact format
    converted_code_block = preprocessor_with_jira.markdown_to_jira(
        "```\nmultiline code\n```"
    )
    assert "{code}" in converted_code_block
    assert "multiline code" in converted_code_block

    # Test lists
    list_conversion = preprocessor_with_jira.markdown_to_jira("- Item 1")
    assert "* Item 1" in list_conversion

    numbered_list = preprocessor_with_jira.markdown_to_jira("1. Item 1")
    assert "Item 1" in numbered_list
    assert "1" in numbered_list

    # Test complex Markdown
    complex_markdown = """
# Project Overview

## Introduction
This project aims to **improve** the user experience.

### Features
- Feature 1
- Feature 2

### Code Example
```python
def hello():
    print("Hello World")
```

For more information, see [our website](https://example.com).
"""

    converted = preprocessor_with_jira.markdown_to_jira(complex_markdown)
    assert "h1. Project Overview" in converted
    assert "h2. Introduction" in converted
    assert "*improve*" in converted
    assert "* Feature 1" in converted
    assert "{code:python}" in converted
    assert "[our website|https://example.com]" in converted


def test_markdown_nested_bullet_list_2space(preprocessor_with_jira):
    """Test that 2-space indented bullet lists convert correctly to Jira format."""
    markdown = "* Item A\n  * Sub-item A.1\n    * Sub-sub A.1.1\n* Item B"
    expected = "* Item A\n** Sub-item A.1\n*** Sub-sub A.1.1\n* Item B"
    result = preprocessor_with_jira.markdown_to_jira(markdown)
    assert result == expected


def test_markdown_nested_numbered_list_2space(preprocessor_with_jira):
    """Test that 2-space indented numbered lists convert correctly to Jira format."""
    markdown = "1. Item A\n  1. Sub-item A.1\n    1. Sub-sub A.1.1\n2. Item B"
    expected = "# Item A\n## Sub-item A.1\n### Sub-sub A.1.1\n# Item B"
    result = preprocessor_with_jira.markdown_to_jira(markdown)
    assert result == expected


def test_jira_markup_translation_disabled(
    preprocessor_with_jira_markup_translation_disabled,
):
    """Test that markup translation is disabled and original text is preserved."""
    mixed_markup = "h1. Jira Heading with **markdown bold** and {{jira code}} and *markdown italic*"

    # Both methods should return the original text unchanged
    assert (
        preprocessor_with_jira_markup_translation_disabled.markdown_to_jira(
            mixed_markup
        )
        == mixed_markup
    )
    assert (
        preprocessor_with_jira_markup_translation_disabled.jira_to_markdown(
            mixed_markup
        )
        == mixed_markup
    )

    # clean_jira_text should also preserve markup (only process mentions/links)
    result = preprocessor_with_jira_markup_translation_disabled.clean_jira_text(
        mixed_markup
    )
    assert "h1. Jira Heading" in result
    assert "**markdown bold**" in result
    assert "{{jira code}}" in result


def test_markdown_to_confluence_storage(preprocessor_with_confluence):
    """Test conversion of Markdown to Confluence storage format."""
    markdown = """# Heading 1

This is some **bold** and *italic* text.

- List item 1
- List item 2

[Link text](https://example.com)
"""

    # Convert markdown to storage format
    storage_format = preprocessor_with_confluence.markdown_to_confluence_storage(
        markdown
    )

    # Verify basic structure (we don't need to test the exact conversion, as that's handled by md2conf)
    assert "<h1>" in storage_format
    assert "Heading 1" in storage_format
    assert "<strong>" in storage_format or "<b>" in storage_format  # Bold
    assert "<em>" in storage_format or "<i>" in storage_format  # Italic
    assert "<a href=" in storage_format.lower()  # Link
    assert "example.com" in storage_format


def test_process_confluence_profile_macro(preprocessor_with_confluence):
    """Test processing Confluence User Profile Macro in page content."""
    html_content = MOCK_PAGE_RESPONSE["body"]["storage"]["value"]
    processed_html, processed_markdown = (
        preprocessor_with_confluence.process_html_content(
            html_content, confluence_client=MockConfluenceClient()
        )
    )
    # Should replace macro with @Test User user123
    assert "@Test User user123" in processed_html
    assert "@Test User user123" in processed_markdown


def test_process_confluence_profile_macro_malformed(preprocessor_with_confluence):
    """Test processing malformed User Profile Macro (missing user param and ri:user)."""
    # Macro missing ac:parameter
    html_missing_param = '<ac:structured-macro ac:name="profile"></ac:structured-macro>'
    processed_html, processed_markdown = (
        preprocessor_with_confluence.process_html_content(
            html_missing_param, confluence_client=MockConfluenceClient()
        )
    )
    assert "[User Profile Macro (Malformed)]" in processed_html
    assert "[User Profile Macro (Malformed)]" in processed_markdown

    # Macro with ac:parameter but missing ri:user
    html_missing_riuser = '<ac:structured-macro ac:name="profile"><ac:parameter ac:name="user"></ac:parameter></ac:structured-macro>'
    processed_html, processed_markdown = (
        preprocessor_with_confluence.process_html_content(
            html_missing_riuser, confluence_client=MockConfluenceClient()
        )
    )
    assert "[User Profile Macro (Malformed)]" in processed_html
    assert "[User Profile Macro (Malformed)]" in processed_markdown


def test_process_confluence_profile_macro_fallback():
    """Test fallback when confluence_client is None."""
    from mcp_atlassian.preprocessing.confluence import ConfluencePreprocessor

    html = (
        '<ac:structured-macro ac:name="profile">'
        '<ac:parameter ac:name="user">'
        '<ri:user ri:account-id="user999" />'
        "</ac:parameter>"
        "</ac:structured-macro>"
    )
    preprocessor = ConfluencePreprocessor(base_url="https://example.atlassian.net")
    processed_html, processed_markdown = preprocessor.process_html_content(
        html, confluence_client=None
    )
    assert "[User Profile: user999]" in processed_html
    assert "[User Profile: user999]" in processed_markdown


def test_process_user_profile_macro_multiple():
    """Test processing multiple User Profile Macros with account-id, userkey, and username."""
    from mcp_atlassian.preprocessing.confluence import ConfluencePreprocessor

    html = (
        "<p>This page mentions a user via profile macro: "
        '<ac:structured-macro ac:name="profile" ac:schema-version="1">'
        '<ac:parameter ac:name="user">'
        '<ri:user ri:account-id="test-account-id-123" />'
        "</ac:parameter>"
        "</ac:structured-macro>. "
        "And another one: "
        '<ac:structured-macro ac:name="profile" ac:schema-version="1">'
        '<ac:parameter ac:name="user">'
        '<ri:user ri:userkey="test-userkey-456" />'
        "</ac:parameter>"
        "</ac:structured-macro>. "
        "And a third: "
        '<ac:structured-macro ac:name="profile" ac:schema-version="1">'
        '<ac:parameter ac:name="user">'
        '<ri:user ri:username="test-username-789" />'
        "</ac:parameter>"
        "</ac:structured-macro>."
        "</p>"
    )

    class CustomMockConfluenceClient:
        def get_user_details_by_accountid(self, account_id):
            return (
                {"displayName": "Test User One"}
                if account_id == "test-account-id-123"
                else {}
            )

        def get_user_details_by_userkey(self, userkey):
            return (
                {"displayName": "Test User Two"}
                if userkey == "test-userkey-456"
                else {}
            )

        def get_user_details_by_username(self, username):
            return (
                {"displayName": "Test User Three"}
                if username == "test-username-789"
                else {}
            )

    preprocessor = ConfluencePreprocessor(base_url="https://example.atlassian.net")
    processed_html, processed_markdown = preprocessor.process_html_content(
        html, confluence_client=CustomMockConfluenceClient()
    )
    assert "@Test User One" in processed_html
    assert "@Test User Two" in processed_html
    assert "@Test User Three" in processed_html
    assert "@Test User One" in processed_markdown
    assert "@Test User Two" in processed_markdown
    assert "@Test User Three" in processed_markdown


def test_markdown_to_confluence_no_automatic_anchors():
    """Test that heading_anchors=False prevents automatic anchor generation (regression for issue #488)."""
    from mcp_atlassian.preprocessing.confluence import ConfluencePreprocessor

    markdown_with_headings = """
# Main Title
Some content here.

## Subsection
More content.

### Deep Section
Final content.
"""

    preprocessor = ConfluencePreprocessor(base_url="https://example.atlassian.net")
    result = preprocessor.markdown_to_confluence_storage(markdown_with_headings)

    # Should not contain automatically generated anchor IDs
    assert 'id="main-title"' not in result.lower()
    assert 'id="subsection"' not in result.lower()
    assert 'id="deep-section"' not in result.lower()

    # Should still contain proper heading tags
    assert "<h1>Main Title</h1>" in result
    assert "<h2>Subsection</h2>" in result
    assert "<h3>Deep Section</h3>" in result


def test_markdown_to_confluence_style_preservation():
    """Test that styled content is preserved during conversion."""
    from mcp_atlassian.preprocessing.confluence import ConfluencePreprocessor

    markdown_with_styles = """
# Title with **bold** text

This paragraph has *italic* and **bold** text.

```python
def hello():
    return "world"
```

- Item with **bold**
- Item with *italic*

> Blockquote with **formatting**

[Link text](https://example.com) with description.
"""

    preprocessor = ConfluencePreprocessor(base_url="https://example.atlassian.net")
    result = preprocessor.markdown_to_confluence_storage(markdown_with_styles)

    # Check that formatting is preserved
    assert "<strong>bold</strong>" in result
    assert "<em>italic</em>" in result
    assert "<blockquote>" in result
    assert '<a href="https://example.com">Link text</a>' in result
    assert "ac:structured-macro" in result  # Code block macro
    assert 'ac:name="code"' in result
    assert '<ac:parameter ac:name="language">py</ac:parameter>' in result


def test_markdown_to_confluence_optional_anchor_generation():
    """Test that enable_heading_anchors parameter controls anchor generation."""
    from mcp_atlassian.preprocessing.confluence import ConfluencePreprocessor

    markdown_with_headings = """
# Main Title
Content here.

## Subsection
More content.
"""

    preprocessor = ConfluencePreprocessor(base_url="https://example.atlassian.net")

    # Test with anchors disabled (default)
    result_no_anchors = preprocessor.markdown_to_confluence_storage(
        markdown_with_headings
    )
    assert 'id="main-title"' not in result_no_anchors.lower()
    assert 'id="subsection"' not in result_no_anchors.lower()

    # Test with anchors enabled
    result_with_anchors = preprocessor.markdown_to_confluence_storage(
        markdown_with_headings, enable_heading_anchors=True
    )
    # When anchors are enabled, they should be present
    # Note: md2conf may use different anchor formats, so we check for presence of id attributes
    assert "<h1>" in result_with_anchors
    assert "<h2>" in result_with_anchors


# Regression tests: bare-filename images produce "Preview unavailable"


class TestFixAttachmentImages:
    """Unit tests for ConfluencePreprocessor._fix_attachment_images."""

    def setup_method(self):
        from mcp_atlassian.preprocessing.confluence import ConfluencePreprocessor

        self.fix = ConfluencePreprocessor._fix_attachment_images

    def test_bare_filename_replaced_with_attachment_macro(self):
        html = '<img src="chart.png" alt="Revenue chart"/>'
        result = self.fix(html)
        assert 'ac:alt="Revenue chart"' in result
        assert 'ri:filename="chart.png"' in result
        assert "<img" not in result

    def test_alt_text_preserved(self):
        html = '<img alt="My diagram" src="diagram.svg"/>'
        result = self.fix(html)
        assert 'ac:alt="My diagram"' in result
        assert 'ri:filename="diagram.svg"' in result

    def test_missing_alt_defaults_to_empty_string(self):
        html = '<img src="figure.png"/>'
        result = self.fix(html)
        assert 'ac:alt=""' in result
        assert 'ri:filename="figure.png"' in result

    def test_https_url_left_untouched(self):
        html = '<img src="https://example.com/logo.png" alt="logo"/>'
        assert self.fix(html) == html

    def test_http_url_left_untouched(self):
        html = '<img src="http://example.com/img.jpg" alt="x"/>'
        assert self.fix(html) == html

    def test_data_uri_left_untouched(self):
        html = '<img src="data:image/png;base64,abc123" alt="inline"/>'
        assert self.fix(html) == html

    def test_absolute_path_left_untouched(self):
        html = '<img src="/images/logo.png" alt="logo"/>'
        assert self.fix(html) == html

    def test_protocol_relative_url_left_untouched(self):
        html = '<img src="//cdn.example.com/logo.png" alt="logo"/>'
        assert self.fix(html) == html

    def test_anchor_reference_left_untouched(self):
        html = '<img src="#inline-image" alt="logo"/>'
        assert self.fix(html) == html

    def test_relative_path_uses_md2conf_attachment_name(self):
        html = '<img src="images/chart 1.png" alt="Chart"/>'
        result = self.fix(html)
        assert 'ri:filename="images_chart_1.png"' in result

    def test_xml_sensitive_values_are_escaped(self):
        html = '<img src="chart & q.png" alt="A & B"/>'
        result = self.fix(html)
        assert 'ac:alt="A &amp; B"' in result
        assert 'ri:filename="chart___q.png"' in result

    def test_dimensions_are_preserved(self):
        html = '<img src="chart.png" alt="Chart" width="600" height="400"/>'
        result = self.fix(html)
        assert 'ac:width="600"' in result
        assert 'ac:height="400"' in result

    def test_mixed_content_only_bare_filenames_rewritten(self):
        html = (
            '<img src="local.png" alt="local"/>'
            '<img src="https://cdn.example.com/remote.png" alt="remote"/>'
        )
        result = self.fix(html)
        assert "ri:filename" in result
        assert "https://cdn.example.com/remote.png" in result
        assert '<img src="local.png"' not in result

    def test_no_img_tags_unchanged(self):
        html = "<p>No images here</p>"
        assert self.fix(html) == html


def test_markdown_to_confluence_storage_attachment_image():
    """Regression: bare-filename images must produce ac:image attachment macros."""
    from mcp_atlassian.preprocessing.confluence import ConfluencePreprocessor

    preprocessor = ConfluencePreprocessor(base_url="https://example.atlassian.net")
    result = preprocessor.markdown_to_confluence_storage("![Revenue chart](chart.png)")
    assert "ri:filename" in result, (
        "Expected Confluence attachment macro, got: " + result
    )
    assert 'ri:filename="chart.png"' in result
    assert "<img" not in result, (
        "Raw <img> tag found - will show as 'Preview unavailable'"
    )


def test_markdown_to_confluence_storage_external_image_unchanged():
    """External image URLs must not be rewritten to attachment macros."""
    from mcp_atlassian.preprocessing.confluence import ConfluencePreprocessor

    preprocessor = ConfluencePreprocessor(base_url="https://example.atlassian.net")
    result = preprocessor.markdown_to_confluence_storage(
        "![Logo](https://example.com/logo.png)"
    )
    assert "https://example.com/logo.png" in result
    assert 'ri:filename="https://example.com/logo.png"' not in result


# Issue #786 regression tests - Wiki Markup Corruption


def test_markdown_to_jira_header_requires_space(preprocessor_with_jira):
    """Test that # requires space to be converted to heading (issue #786)."""
    # With space - Markdown heading, should convert
    assert preprocessor_with_jira.markdown_to_jira("# Heading") == "h1. Heading"
    assert preprocessor_with_jira.markdown_to_jira("## Subheading") == "h2. Subheading"
    assert preprocessor_with_jira.markdown_to_jira("### Level 3") == "h3. Level 3"

    # Without space - could be Jira numbered list, should NOT convert
    assert preprocessor_with_jira.markdown_to_jira("#item") == "#item"
    assert preprocessor_with_jira.markdown_to_jira("##nested") == "##nested"
    assert preprocessor_with_jira.markdown_to_jira("###deep") == "###deep"


def test_markdown_to_jira_preserves_jira_list_syntax(preprocessor_with_jira):
    """Test that Jira list syntax (asterisks + space) is preserved (issue #786)."""
    # Jira nested bullets - should NOT be converted to bold
    jira_list = "* First level\n** Second level\n*** Third level"
    result = preprocessor_with_jira.markdown_to_jira(jira_list)
    assert "** Second level" in result  # Preserved, not converted
    assert "*** Third level" in result  # Preserved, not converted

    # Single Jira bullet should also be preserved
    assert preprocessor_with_jira.markdown_to_jira("* Item") == "* Item"


def test_markdown_to_jira_inline_bold_still_converts(preprocessor_with_jira):
    """Test that inline Markdown bold/italic still converts (issue #786)."""
    # Inline bold should still work
    assert (
        preprocessor_with_jira.markdown_to_jira("text **bold** text")
        == "text *bold* text"
    )
    assert (
        preprocessor_with_jira.markdown_to_jira("text *italic* text")
        == "text _italic_ text"
    )


def test_markdown_to_jira_bold_without_space_still_converts(preprocessor_with_jira):
    """Test that Markdown bold (no space after **) still converts (issue #786)."""
    # These should still be converted (existing behavior preserved)
    assert preprocessor_with_jira.markdown_to_jira("**bold text**") == "*bold text*"
    assert preprocessor_with_jira.markdown_to_jira("*italic text*") == "_italic text_"


def test_markdown_to_jira_preserves_intraword_underscores(preprocessor_with_jira):
    """Intraword underscores are literal per CommonMark, not emphasis.

    The Jira wiki renderer italicizes ``_word_``, so identifiers containing
    underscores (snake_case, customfield IDs, etc.) must be emitted with the
    underscores escaped (``\\_``) — otherwise ``foo_bar_baz`` renders with a
    spurious italic span around ``bar``.
    """
    # Single intraword underscore.
    assert (
        preprocessor_with_jira.markdown_to_jira("the foo_bar identifier")
        == "the foo\\_bar identifier"
    )
    # Multiple intraword underscores in one token.
    assert (
        preprocessor_with_jira.markdown_to_jira("baseline_samples_5m paused")
        == "baseline\\_samples\\_5m paused"
    )
    # Custom field IDs are a common real-world case.
    assert (
        preprocessor_with_jira.markdown_to_jira("set customfield_10101 to 5")
        == "set customfield\\_10101 to 5"
    )
    # Two identifiers on one line must not pair into a cross-token italic span.
    assert (
        preprocessor_with_jira.markdown_to_jira("netflow_ap and sre_analytics")
        == "netflow\\_ap and sre\\_analytics"
    )
    # Double underscore runs inside identifiers are also literal in CommonMark.
    assert (
        preprocessor_with_jira.markdown_to_jira("the foo__bar__baz identifier")
        == "the foo\\_\\_bar\\_\\_baz identifier"
    )
    # Jira list lines still need escaping inside the item text.
    assert (
        preprocessor_with_jira.markdown_to_jira("* customfield_10101 item")
        == "* customfield\\_10101 item"
    )


def test_markdown_to_jira_word_boundary_underscore_still_italicizes(
    preprocessor_with_jira,
):
    """Genuine ``_emphasis_`` at word boundaries must still convert to italic."""
    assert (
        preprocessor_with_jira.markdown_to_jira("an _italic phrase_ here")
        == "an _italic phrase_ here"
    )
    assert preprocessor_with_jira.markdown_to_jira("_italic text_") == "_italic text_"
    assert preprocessor_with_jira.markdown_to_jira("__bold text__") == "*bold text*"


def test_markdown_to_jira_underscore_in_inline_code_untouched(preprocessor_with_jira):
    """Underscores inside inline code spans stay inside monospace, unescaped."""
    assert (
        preprocessor_with_jira.markdown_to_jira("call `find_provider_by_url` now")
        == "call {{find_provider_by_url}} now"
    )


def test_markdown_to_jira_preserves_underscores_in_url_targets(
    preprocessor_with_jira,
):
    """URL targets keep literal underscores while visible text is escaped."""
    assert (
        preprocessor_with_jira.markdown_to_jira(
            "[runbook](https://example.com/foo_bar)"
        )
        == "[runbook|https://example.com/foo_bar]"
    )
    assert (
        preprocessor_with_jira.markdown_to_jira(
            "[foo_bar](https://example.com/foo_bar)"
        )
        == "[foo\\_bar|https://example.com/foo_bar]"
    )
    assert (
        preprocessor_with_jira.markdown_to_jira(
            "![diagram](https://example.com/foo_bar.png)"
        )
        == "!https://example.com/foo_bar.png|alt=diagram!"
    )
    assert (
        preprocessor_with_jira.markdown_to_jira("![](https://example.com/foo_bar.png)")
        == "!https://example.com/foo_bar.png!"
    )
    assert (
        preprocessor_with_jira.markdown_to_jira("<https://example.com/foo_bar>")
        == "[https://example.com/foo_bar]"
    )


# Issue #893 regression tests - Code Block Content Corruption


def test_markdown_to_jira_code_block_preserves_hash(preprocessor_with_jira):
    """Test that # characters inside code blocks are preserved (issue #893)."""
    markdown = """Here's a script:

```
#!/bin/bash

# This is a comment
echo "hello"
```"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)

    # The shebang and comment should be preserved, not converted to headings
    assert "#!/bin/bash" in result
    assert "# This is a comment" in result
    assert "h1." not in result  # Should NOT have heading conversion


def test_markdown_to_jira_code_block_with_language_preserves_hash(
    preprocessor_with_jira,
):
    """Test that # in code blocks with language specifier is preserved (issue #893)."""
    markdown = """```python
# Python comment
def hello():
    print("world")
```"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)

    assert "# Python comment" in result
    assert "h1." not in result


def test_markdown_to_jira_code_block_multiple_hash_lines(preprocessor_with_jira):
    """Test multiple # lines in code block are all preserved (issue #893)."""
    markdown = """```bash
# First comment
# Second comment
# Third comment
echo "test"
```"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)

    assert "# First comment" in result
    assert "# Second comment" in result
    assert "# Third comment" in result
    assert result.count("h1.") == 0


def test_markdown_to_jira_inline_code_preserves_hash(preprocessor_with_jira):
    """Test that # in inline code is preserved (issue #893)."""
    markdown = "The shebang line is `#!/bin/bash` in shell scripts."
    result = preprocessor_with_jira.markdown_to_jira(markdown)

    assert "#!/bin/bash" in result
    assert "h1." not in result


def test_markdown_to_jira_mixed_code_and_headers(preprocessor_with_jira):
    """Test that headers outside code blocks still convert while code is preserved."""
    markdown = """# Real Heading

Here's some code:

```
# This is a comment, not a heading
```

## Another Heading"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)

    # Headers should convert
    assert "h1. Real Heading" in result
    assert "h2. Another Heading" in result

    # Code block content should be preserved
    assert "# This is a comment" in result


# Language mapping tests for code blocks (issue #669)


def test_normalize_code_language_valid_jira_languages(preprocessor_with_jira):
    """Test that valid JIRA languages pass through unchanged."""
    # Official JIRA-supported languages should be returned as-is (lowercase)
    # Source: https://jira.atlassian.com/browse/JRASERVER-21067
    assert preprocessor_with_jira._normalize_code_language("python") == "python"
    assert preprocessor_with_jira._normalize_code_language("java") == "java"
    assert preprocessor_with_jira._normalize_code_language("javascript") == "javascript"
    assert preprocessor_with_jira._normalize_code_language("bash") == "bash"
    assert preprocessor_with_jira._normalize_code_language("sql") == "sql"
    assert preprocessor_with_jira._normalize_code_language("xml") == "xml"
    assert preprocessor_with_jira._normalize_code_language("json") == "json"
    assert preprocessor_with_jira._normalize_code_language("go") == "go"
    assert preprocessor_with_jira._normalize_code_language("ruby") == "ruby"
    assert preprocessor_with_jira._normalize_code_language("none") == "none"


def test_normalize_code_language_case_insensitive(preprocessor_with_jira):
    """Test that language normalization is case-insensitive."""
    assert preprocessor_with_jira._normalize_code_language("Python") == "python"
    assert preprocessor_with_jira._normalize_code_language("JAVA") == "java"
    assert preprocessor_with_jira._normalize_code_language("JavaScript") == "javascript"
    assert preprocessor_with_jira._normalize_code_language("BASH") == "bash"


def test_normalize_code_language_mapped_languages(preprocessor_with_jira):
    """Test that unsupported languages map to their closest JIRA equivalent."""
    # Dockerfile → bash (similar syntax)
    assert preprocessor_with_jira._normalize_code_language("dockerfile") == "bash"
    assert preprocessor_with_jira._normalize_code_language("docker") == "bash"

    # TypeScript/JSX → javascript
    assert preprocessor_with_jira._normalize_code_language("typescript") == "javascript"
    assert preprocessor_with_jira._normalize_code_language("ts") == "javascript"
    assert preprocessor_with_jira._normalize_code_language("tsx") == "javascript"
    assert preprocessor_with_jira._normalize_code_language("jsx") == "javascript"

    # Kotlin → java (JVM-based)
    assert preprocessor_with_jira._normalize_code_language("kotlin") == "java"
    assert preprocessor_with_jira._normalize_code_language("kt") == "java"

    # Build files → bash
    assert preprocessor_with_jira._normalize_code_language("makefile") == "bash"
    assert preprocessor_with_jira._normalize_code_language("make") == "bash"


def test_normalize_code_language_unmapped_returns_none(preprocessor_with_jira):
    """Test that unmapped languages return None for plain {code} blocks."""
    # Languages with no good JIRA alternative should return None
    assert preprocessor_with_jira._normalize_code_language("rust") is None
    assert preprocessor_with_jira._normalize_code_language("toml") is None
    assert preprocessor_with_jira._normalize_code_language("markdown") is None
    assert preprocessor_with_jira._normalize_code_language("unknownlang") is None
    assert preprocessor_with_jira._normalize_code_language("zig") is None


def test_normalize_code_language_empty_input(preprocessor_with_jira):
    """Test that empty/None language returns None."""
    assert preprocessor_with_jira._normalize_code_language("") is None
    assert preprocessor_with_jira._normalize_code_language(None) is None


def test_markdown_to_jira_code_block_valid_language(preprocessor_with_jira):
    """Test code block conversion with valid JIRA language."""
    markdown = """```python
def hello():
    print("Hello World")
```"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)
    assert "{code:python}" in result
    assert "def hello():" in result
    assert "{code}" in result


def test_markdown_to_jira_code_block_dockerfile_maps_to_bash(preprocessor_with_jira):
    """Test that dockerfile code blocks map to bash (issue #669)."""
    markdown = """```dockerfile
FROM ubuntu:22.04
RUN apt-get update
CMD ["/bin/bash"]
```"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)
    assert "{code:bash}" in result
    assert "FROM ubuntu:22.04" in result
    assert "{code}" in result


def test_markdown_to_jira_code_block_typescript_maps_to_javascript(
    preprocessor_with_jira,
):
    """Test that typescript code blocks map to javascript."""
    markdown = """```typescript
interface User {
    name: string;
    age: number;
}
```"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)
    assert "{code:javascript}" in result
    assert "interface User" in result


def test_markdown_to_jira_code_block_jsx_maps_to_javascript(preprocessor_with_jira):
    """Test that jsx code blocks map to javascript (issue #669)."""
    markdown = """```jsx
const Component = () => {
  return <div>Hello</div>;
}
```"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)
    assert "{code:javascript}" in result
    assert "const Component" in result


def test_markdown_to_jira_code_block_unmapped_language_plain(preprocessor_with_jira):
    """Test that unmapped languages produce plain {code} blocks."""
    markdown = """```rust
fn main() {
    println!("Hello, world!");
}
```"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)
    # Should produce {code} without language specifier
    assert "{code}" in result
    assert "{code:rust}" not in result
    assert "fn main()" in result


def test_markdown_to_jira_code_block_no_language_plain(preprocessor_with_jira):
    """Test that code blocks without language produce plain {code}."""
    markdown = """```
plain text code
no syntax highlighting
```"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)
    assert "{code}" in result
    # Should not have any language specifier
    assert "{code:" not in result
    assert "plain text code" in result


def test_markdown_to_jira_multiple_code_blocks_mixed_languages(preprocessor_with_jira):
    """Test multiple code blocks with different language mappings."""
    markdown = """
Python code:
```python
print("hello")
```

Dockerfile:
```dockerfile
FROM alpine
```

Unknown language:
```unknownlang
some code
```
"""
    result = preprocessor_with_jira.markdown_to_jira(markdown)
    assert "{code:python}" in result
    assert "{code:bash}" in result  # dockerfile mapped to bash
    assert 'print("hello")' in result
    assert "FROM alpine" in result
    assert "some code" in result


# Confluence ac:image tag processing tests


class TestImageProcessing:
    """Tests for Confluence ac:image tag processing."""

    @pytest.fixture
    def preprocessor(self):
        return ConfluencePreprocessor(base_url="https://example.net")

    @pytest.mark.parametrize(
        "test_id, html, content_id, attachments, expected",
        [
            pytest.param(
                "ri-attachment-basic",
                '<ac:image><ri:attachment ri:filename="shot.png"/></ac:image>',
                "123",
                None,
                "![shot.png](https://example.net/download/attachments/123/shot.png)",
                id="ri-attachment-basic",
            ),
            pytest.param(
                "ri-url-basic",
                '<ac:image><ri:url ri:value="https://cdn/logo.png"/></ac:image>',
                "",
                None,
                "![logo.png](https://cdn/logo.png)",
                id="ri-url-basic",
            ),
            pytest.param(
                "width-attr",
                '<ac:image ac:width="600"><ri:attachment ri:filename="d.png"/></ac:image>',
                "123",
                None,
                "![d.png]",
                id="width-attr",
            ),
            pytest.param(
                "mixed-content",
                '<p>Text</p><ac:image><ri:attachment ri:filename="x.png"/></ac:image><p>More</p>',
                "123",
                None,
                "x.png",
                id="mixed-content",
            ),
            pytest.param(
                "no-content-id",
                '<ac:image><ri:attachment ri:filename="test.png"/></ac:image>',
                "",
                None,
                "![test.png](test.png)",
                id="no-content-id",
            ),
            pytest.param(
                "attachment-lookup",
                '<ac:image><ri:attachment ri:filename="doc.png"/></ac:image>',
                "123",
                [
                    {
                        "title": "doc.png",
                        "_links": {"download": "/download/attachments/123/doc.png"},
                    }
                ],
                "![doc.png](https://example.net/download/attachments/123/doc.png)",
                id="attachment-lookup",
            ),
            pytest.param(
                "filename-spaces",
                '<ac:image><ri:attachment ri:filename="Screen Shot 2024.png"/></ac:image>',
                "123",
                None,
                "Screen%20Shot%202024.png",
                id="filename-spaces",
            ),
            pytest.param(
                "unknown-inner",
                "<ac:image><ri:unknown/></ac:image>",
                "123",
                None,
                "[unsupported image]",
                id="unknown-inner",
            ),
            pytest.param(
                "no-image-tags",
                "<p>Normal content</p>",
                "123",
                None,
                "Normal content",
                id="no-image-tags",
            ),
        ],
    )
    def test_image_processing(
        self,
        preprocessor,
        test_id: str,
        html: str,
        content_id: str,
        attachments: list[dict] | None,
        expected: str,
    ):
        """Test ac:image tag processing with various inputs."""
        _, markdown = preprocessor.process_html_content(
            html,
            content_id=content_id,
            attachments=attachments,
        )
        assert expected in markdown

    def test_width_attr_in_img(self, preprocessor):
        """Verify width attribute is preserved in the img tag."""
        html = (
            '<ac:image ac:width="600"><ri:attachment ri:filename="d.png"/></ac:image>'
        )
        processed_html, _ = preprocessor.process_html_content(html, content_id="123")
        assert 'width="600"' in processed_html

    def test_mixed_content_has_all_parts(self, preprocessor):
        """Verify mixed content retains both text and image."""
        html = '<p>Text</p><ac:image><ri:attachment ri:filename="x.png"/></ac:image><p>More</p>'
        _, markdown = preprocessor.process_html_content(html, content_id="123")
        assert "Text" in markdown
        assert "x.png" in markdown
        assert "More" in markdown

    def test_attachment_lookup_uses_download_url(self, preprocessor):
        """Verify attachment lookup prefers _links.download over fallback."""
        attachments = [
            {
                "title": "doc.png",
                "_links": {"download": "/download/attachments/123/doc.png"},
            }
        ]
        html = '<ac:image><ri:attachment ri:filename="doc.png"/></ac:image>'
        _, markdown = preprocessor.process_html_content(
            html, content_id="123", attachments=attachments
        )
        # Should use the download link, not the fallback construction
        assert "/download/attachments/123/doc.png" in markdown

    def test_cross_page_attachment_uses_filename_fallback(self, preprocessor):
        """Cross-page ri:attachment should not use current page's content_id."""
        html = (
            "<ac:image>"
            '<ri:attachment ri:filename="img.png">'
            '<ri:page ri:content-title="Other Page" ri:space-key="X"/>'
            "</ri:attachment>"
            "</ac:image>"
        )
        _, markdown = preprocessor.process_html_content(html, content_id="999")
        # Should NOT contain /999/ (wrong page ID); should fall back to
        # filename-only reference since we can't resolve the other page
        assert "/999/" not in markdown
        assert "![img.png](img.png)" in markdown

    def test_backward_compatibility(self, preprocessor):
        """Ensure existing calls without new params still work."""
        html = "<p>Simple text</p>"
        processed_html, processed_markdown = preprocessor.process_html_content(html)
        assert "Simple text" in processed_markdown


# Issue #1052 - {panel} blocks drop links during wiki-to-markdown conversion


class TestPanelBlocks:
    """Tests for {panel} block conversion and bare link handling."""

    @pytest.fixture
    def preprocessor(self):
        return JiraPreprocessor(base_url="https://example.atlassian.net")

    @pytest.mark.parametrize(
        "test_id, input_text, expected_present, expected_absent",
        [
            pytest.param(
                "panel-bare-url",
                "{panel:title=Spec}[https://example.com]{panel}",
                ["**Spec**", "https://example.com"],
                ["{panel"],
                id="panel-bare-url",
            ),
            pytest.param(
                "panel-named-link",
                "{panel:title=Spec}[Link Text|https://example.com]{panel}",
                ["**Spec**", "[Link Text](https://example.com)"],
                ["{panel"],
                id="panel-named-link",
            ),
            pytest.param(
                "panel-no-title",
                "{panel}some content{panel}",
                ["some content"],
                ["{panel"],
                id="panel-no-title",
            ),
            pytest.param(
                "panel-extra-params",
                "{panel:borderColor=#ccc|title=Info}text{panel}",
                ["**Info**", "text"],
                ["{panel"],
                id="panel-extra-params",
            ),
            pytest.param(
                "panel-multiline",
                "{panel:title=Notes}line one\nline two{panel}",
                ["line one", "line two"],
                ["{panel"],
                id="panel-multiline",
            ),
            pytest.param(
                "multiple-panels",
                "{panel:title=A}content A{panel}\n{panel:title=B}content B{panel}",
                ["**A**", "content A", "**B**", "content B"],
                ["{panel"],
                id="multiple-panels",
            ),
        ],
    )
    def test_panel_conversion(
        self,
        preprocessor,
        test_id: str,
        input_text: str,
        expected_present: list[str],
        expected_absent: list[str],
    ):
        """Test {panel} block conversion to markdown."""
        result = preprocessor.jira_to_markdown(input_text)
        for expected in expected_present:
            assert expected in result, f"[{test_id}] Expected '{expected}' in: {result}"
        for absent in expected_absent:
            assert absent not in result, (
                f"[{test_id}] Unexpected '{absent}' in: {result}"
            )

    def test_panel_full_pipeline(self, preprocessor):
        """Test panel with URL link through the full clean_jira_text pipeline (the reported bug)."""
        input_text = "{panel:title=Spec}[https://example.com]{panel}"
        result = preprocessor.clean_jira_text(input_text)
        assert "https://example.com" in result, f"URL dropped in pipeline: {result}"

    def test_bare_link_without_panel(self, preprocessor):
        """Test bare [url] link is preserved outside panels too."""
        result = preprocessor.jira_to_markdown("[https://example.com] more text")
        assert "https://example.com" in result, f"URL dropped: {result}"


# Code block placeholder protection tests


class TestCodeBlockProtection:
    """Tests for code block content protection via placeholder extraction."""

    @pytest.fixture
    def preprocessor(self):
        return JiraPreprocessor(base_url="https://example.atlassian.net")

    @pytest.mark.parametrize(
        "test_id, input_text, expected_in_fence, description",
        [
            pytest.param(
                "quote-in-code",
                "{code}{quote}quoted{quote}{code}",
                "{quote}quoted{quote}",
                "Quote not converted inside code",
                id="quote-in-code",
            ),
            pytest.param(
                "color-in-code",
                "{code}{color:red}text{color}{code}",
                "{color:red}text{color}",
                "Color not converted inside code",
                id="color-in-code",
            ),
            pytest.param(
                "panel-in-code",
                "{code}{panel:title=X}content{panel}{code}",
                "{panel:title=X}content{panel}",
                "Panel not converted inside code",
                id="panel-in-code",
            ),
            pytest.param(
                "noformat-with-quote",
                "{noformat}{quote}q{quote}{noformat}",
                "{quote}q{quote}",
                "Quote not converted inside noformat",
                id="noformat-with-quote",
            ),
            pytest.param(
                "code-with-lang",
                "{code:python}{quote}q{quote}{code}",
                "{quote}q{quote}",
                "Language preserved, content literal",
                id="code-with-lang",
            ),
            pytest.param(
                "mixed-outside-inside",
                "{quote}real quote{quote}\n{code}{quote}not a quote{quote}{code}",
                "{quote}not a quote{quote}",
                "Outside converted, inside preserved",
                id="mixed-outside-inside",
            ),
        ],
    )
    def test_jira_to_markdown_code_block_preserves_content(
        self,
        preprocessor,
        test_id: str,
        input_text: str,
        expected_in_fence: str,
        description: str,
    ):
        """Test that markup inside {code}/{noformat} is not converted."""
        result = preprocessor.jira_to_markdown(input_text)
        assert "```" in result, f"[{test_id}] No code fence in: {result}"
        fence_pattern = r"```(?:\w*)\n([\s\S]*?)\n```"
        fence_match = re.search(fence_pattern, result)
        assert fence_match, (
            f"[{test_id}] Could not extract fence content from: {result}"
        )
        fence_content = fence_match.group(1)
        assert expected_in_fence in fence_content, (
            f"[{test_id}] Expected '{expected_in_fence}' "
            f"inside fence, got: '{fence_content}'"
        )

    def test_inline_code_inside_code_block_preserved(self, preprocessor):
        """Test that {{inline}} inside {code} blocks is preserved."""
        input_text = "{code}use {{var}} here{code}"
        result = preprocessor.jira_to_markdown(input_text)
        assert "```" in result
        # The {{var}} should appear as literal text, not
        # converted to backtick inline code
        fence_match = re.search(r"```\n([\s\S]*?)\n```", result)
        assert fence_match
        assert "{{var}}" in fence_match.group(1)

    def test_round_trip_preserves_code_block(self, preprocessor):
        """Test jira->md->jira round-trip preserves code content."""
        jira_input = "{code:python}# comment\nprint('hi'){code}"
        md = preprocessor.jira_to_markdown(jira_input)
        assert "```python" in md
        assert "# comment" in md
        jira_output = preprocessor.markdown_to_jira(md)
        assert "{code:python}" in jira_output
        assert "# comment" in jira_output
        assert "print('hi')" in jira_output

    def test_quote_wrapping_code_loses_blockquote_on_inner_lines(
        self,
        preprocessor,
    ):
        """Document trade-off: {quote} around {code} loses blockquote context.

        Placeholder extraction protects code content from markup
        corruption, but the {quote} handler cannot reach inside the
        already-extracted block.  The opening fence line may carry
        "> " while inner code lines do not.  This is the expected
        (intentional) behavior.
        """
        result = preprocessor.jira_to_markdown("{quote}{code}x = 1{code}{quote}")
        # Code content is preserved literally
        assert "x = 1" in result
        # Code fence is present
        assert "```" in result
        # The opening fence gets blockquote prefix from {quote}
        assert "> ```" in result
        # Inner code line does NOT get blockquote prefix -- this
        # is the known trade-off of placeholder-based protection.
        lines = result.strip().splitlines()
        code_lines = [ln for ln in lines if ln.strip() and "```" not in ln]
        for ln in code_lines:
            assert not ln.startswith("> "), (
                f"Inner code line unexpectedly blockquoted: {ln!r}"
            )


class TestHtmlConversionCodeProtection:
    """Tests that _convert_html_to_markdown protects code spans from HTML parsing.

    When markdown code spans contain angle brackets (e.g. `<script>`),
    BeautifulSoup must not interpret them as HTML tags.
    """

    @pytest.fixture
    def preprocessor(self):
        return JiraPreprocessor(base_url="https://example.atlassian.net")

    # --- End-to-end tests via clean_jira_text (Jira markup → markdown) ---

    @pytest.mark.parametrize(
        "jira_input, expected_substr",
        [
            pytest.param(
                "{{npm run <script>}}",
                "`npm run <script>`",
                id="inline-script-tag",
            ),
            pytest.param(
                "{{<div>content</div>}}",
                "`<div>content</div>`",
                id="inline-div-tag",
            ),
            pytest.param(
                "{{List<String>}}",
                "`List<String>`",
                id="inline-generic",
            ),
            pytest.param(
                "{code:html}\n<script>alert(1)</script>\n{code}",
                "<script>alert(1)</script>",
                id="fenced-html",
            ),
            pytest.param(
                "{{<b>not bold</b>}} and <b>real bold</b>",
                "`<b>not bold</b>`",
                id="mixed-code-and-html",
            ),
            pytest.param(
                "{{<Tag>}} and {{<Elem>}}",
                "`<Tag>`",
                id="multiple-inline",
            ),
            pytest.param(
                "{{npm run <script>}} entries in {{package.json}}. More content.",
                "More content",
                id="text-after-preserved",
            ),
        ],
    )
    def test_clean_jira_text_preserves_code_with_angle_brackets(
        self,
        preprocessor,
        jira_input: str,
        expected_substr: str,
    ):
        """End-to-end: Jira markup with angle brackets in code spans."""
        result = preprocessor.clean_jira_text(jira_input)
        assert expected_substr in result, f"Expected '{expected_substr}' in: {result!r}"

    def test_mixed_code_and_html_converts_real_html(self, preprocessor):
        """Real HTML outside code spans should still be converted."""
        result = preprocessor.clean_jira_text(
            "{{<b>not bold</b>}} and <b>real bold</b>"
        )
        assert "**real bold**" in result

    # --- Direct _convert_html_to_markdown tests (markdown input) ---

    @pytest.mark.parametrize(
        "md_input, expected_substr",
        [
            pytest.param(
                "`npm run <script>`",
                "`npm run <script>`",
                id="backtick-script",
            ),
            pytest.param(
                "```html\n<script>alert(1)</script>\n```",
                "<script>alert(1)</script>",
                id="fenced-block-script",
            ),
            pytest.param(
                "```\n<b>code</b>\n```\n<b>bold</b>",
                "<b>code</b>",
                id="fenced-plus-real-html",
            ),
            pytest.param(
                "Hello <b>world</b>",
                "**world**",
                id="no-code-html-still-converted",
            ),
        ],
    )
    def test_convert_html_to_markdown_protects_code(
        self,
        preprocessor,
        md_input: str,
        expected_substr: str,
    ):
        """Direct test of _convert_html_to_markdown with markdown code spans."""
        result = preprocessor._convert_html_to_markdown(md_input)
        assert expected_substr in result, f"Expected '{expected_substr}' in: {result!r}"


class TestInlineMarkupBoundaries:
    """Boundary rules for Jira inline markup on the read path.

    The v2 read endpoint returns literal characters as literals (Jira escapes
    some of them with a backslash), so ``+``, ``^``, ``~`` and ``??`` turn up
    in ordinary prose.  Any second occurrence used to close a span, which ate
    characters out of text nobody wrote as markup.  A span now has to look
    like markup: its content holds no whitespace, or else it opens at the
    start of a line or after whitespace -- and it never sits inside a URL token
    that carries an explicit scheme.

    ``*`` takes only the space-side half of that rule, because Jira does render
    intraword bold, and ``_`` keeps its unbounded pairing because Jira
    italicises intraword underscores as well.

    The rule narrows the corruption class rather than closing it, and it costs
    conversions Jira does render.  Both limits are pinned below rather than
    left to be discovered, each by a named test:

    * ``^`` and ``~`` still pair across punctuation-flanked prose --
      ``test_punctuation_flanked_sup_and_sub_still_pair``.
    * A URL without a scheme is not protected --
      ``test_scheme_less_urls_are_not_protected``.
    * ``+`` and ``??`` stop converting altogether in scripts written without
      spaces between words --
      ``test_plus_and_citation_are_unconvertible_in_unspaced_scripts``.
    * A multi-word span opening against punctuation is left alone wherever the
      punctuation comes from --
      ``test_multiword_span_against_punctuation_is_not_converted``,
      ``test_multiword_span_inside_a_macro_is_not_converted``.
    * A span glued to the following word is left alone --
      ``test_span_glued_to_the_following_word_is_not_converted``.
    """

    @pytest.fixture
    def preprocessor(self):
        return JiraPreprocessor(base_url="https://example.atlassian.net")

    @pytest.mark.parametrize(
        "text, expected",
        [
            pytest.param(
                "see ~/tmp and ~/var",
                "see ~/tmp and ~/var",
                id="two-home-paths",
            ),
            pytest.param("x^2 plus y^2", "x^2 plus y^2", id="two-exponents"),
            pytest.param(
                "checklist + kit; date + 4 weeks",
                "checklist + kit; date + 4 weeks",
                id="two-plus-signs",
            ),
            pytest.param("why?? really??", "why?? really??", id="two-citations"),
            pytest.param(
                "rows 3-5 and 7-9",
                "rows 3-5 and 7-9",
                id="two-hyphen-ranges",
            ),
            pytest.param(
                "12:30~13:00 and 14:00~15:00",
                "12:30~13:00 and 14:00~15:00",
                id="two-time-ranges",
            ),
            pytest.param(
                "Q1 ~ Q3 growth ~ 5%",
                "Q1 ~ Q3 growth ~ 5%",
                id="two-approximations",
            ),
            pytest.param(
                "cost $5+$6 and $7+$8",
                "cost $5+$6 and $7+$8",
                id="two-sums",
            ),
            pytest.param(
                'see "~/tmp" and "~/var"',
                'see "~/tmp" and "~/var"',
                id="quoted-home-paths",
            ),
            pytest.param(
                "(~/tmp) and (~/var)",
                "(~/tmp) and (~/var)",
                id="parenthesised-home-paths",
            ),
            pytest.param(
                "ranges (1..5) (+/-) (+/-)",
                "ranges (1..5) (+/-) (+/-)",
                id="parenthesised-plus-minus",
            ),
            pytest.param(
                "a+b and c+d",
                "a+b and c+d",
                id="two-intraword-plus-signs",
            ),
        ],
    )
    def test_literal_prose_survives(self, preprocessor, text: str, expected: str):
        """Prose whose marker characters never form a markup-shaped span.

        Each marker here either wraps whitespace-edged content, or -- when it
        wraps several words -- opens against punctuation rather than
        whitespace, so none of them pair.

        ``two-time-ranges`` is evidence for the spaced form only.  Written
        without the spaces -- ``12:30~13:00,14:00~15:00`` -- the same prose
        still loses characters, because a tight ``~`` span asks only that its
        content hold no whitespace; see
        ``test_punctuation_flanked_sup_and_sub_still_pair``.

        One parameter is a characterization case with no detection power
        against this change: ``two-hyphen-ranges`` passes identically without
        it, because the substitution this change deleted rewrote ``-text-`` to
        itself.  It stays to document that hyphens are out of scope.
        """
        assert preprocessor.jira_to_markdown(text) == expected
        assert preprocessor.clean_jira_text(text) == expected

    def test_bracketed_marker_prose_keeps_its_markers(self, preprocessor):
        """``[+] done and [+] pending`` keeps both plus signs.

        The brackets themselves are dropped further down the pipeline, where
        ``[text]`` is Jira's own link syntax; that is orthogonal to the marker
        rule, which is what this test pins.
        """
        result = preprocessor.jira_to_markdown("[+] done and [+] pending")
        assert "<ins>" not in result
        assert result == "+ done and + pending"

    @pytest.mark.parametrize(
        "text, expected",
        [
            pytest.param("+inserted+", "<ins>inserted</ins>", id="inserted"),
            pytest.param("^sup^", "<sup>sup</sup>", id="superscript"),
            pytest.param("~sub~", "<sub>sub</sub>", id="subscript"),
            pytest.param("??cited??", "<cite>cited</cite>", id="citation"),
            pytest.param(
                "see +this+ and +that+",
                "see <ins>this</ins> and <ins>that</ins>",
                id="two-inserted-spans",
            ),
            pytest.param(
                "a ??quoted bit?? here",
                "a <cite>quoted bit</cite> here",
                id="citation-mid-sentence",
            ),
            pytest.param(
                "this is +very important+.",
                "this is <ins>very important</ins>.",
                id="span-before-full-stop",
            ),
            pytest.param("(+ok+)", "(<ins>ok</ins>)", id="single-token-in-brackets"),
            pytest.param("|+ok+|", "|<ins>ok</ins>|", id="single-token-in-table-cell"),
            pytest.param(
                "??is this cited? yes??",
                "<cite>is this cited? yes</cite>",
                id="citation-containing-question-mark",
            ),
            pytest.param(
                "the +new+'s value",
                "the <ins>new</ins>'s value",
                id="span-before-an-apostrophe",
            ),
            pytest.param("*+ins+*", "**<ins>ins</ins>**", id="single-token-in-bold"),
        ],
    )
    def test_genuine_markup_still_converts(
        self, preprocessor, text: str, expected: str
    ):
        """Marker pairs that look like markup are still converted."""
        assert preprocessor.jira_to_markdown(text) == expected

    @pytest.mark.parametrize(
        "text, expected",
        [
            pytest.param("H~2~O", "H<sub>2</sub>O", id="chemical-formula"),
            pytest.param(
                "CO~2~ emissions",
                "CO<sub>2</sub> emissions",
                id="subscript-then-space",
            ),
            pytest.param("2^10^ bytes", "2<sup>10</sup> bytes", id="power-of-two"),
            pytest.param("1^st^ place", "1<sup>st</sup> place", id="ordinal"),
            pytest.param("m^3^", "m<sup>3</sup>", id="cubic-metre"),
            pytest.param(
                "T~1~ and T~2~",
                "T<sub>1</sub> and T<sub>2</sub>",
                id="two-subscripts",
            ),
            pytest.param(
                "e^x^ + e^y^",
                "e<sup>x</sup> + e<sup>y</sup>",
                id="two-superscripts-around-a-plus",
            ),
            pytest.param(
                "你好~sub~世界", "你好<sub>sub</sub>世界", id="chinese-subscript"
            ),
            pytest.param(
                "面積は2^10^です",
                "面積は2<sup>10</sup>です",
                id="japanese-superscript",
            ),
        ],
    )
    def test_intraword_superscript_and_subscript_convert(
        self, preprocessor, text: str, expected: str
    ):
        """Superscript and subscript are converted inside a word.

        That is where Jira's renderer puts them and where they are practically
        always written, and it is what ``markdown_to_jira`` emits, so refusing
        to read them back would mean writing markup this library cannot parse
        (see ``test_sub_sup_and_ins_round_trip``).  It stays unambiguous
        because the span content may not contain whitespace, which is what
        separates ``H~2~O`` from prose like ``x^2 plus y^2`` -- and no further:
        punctuation-flanked prose still pairs, see
        ``test_punctuation_flanked_sup_and_sub_still_pair``.

        Having no word-character condition is also why these two keep working
        in scripts that do not separate words with spaces, where ``+`` and
        ``??`` stop converting entirely.
        """
        assert preprocessor.jira_to_markdown(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("a+b+c", id="intraword-inserted"),
            pytest.param("x??inline??y", id="intraword-citation"),
        ],
    )
    def test_intraword_plus_and_citation_are_left_alone(self, preprocessor, text: str):
        """``+`` and ``??`` get no intraword arm, unlike ``^`` and ``~``.

        Intraword ``+`` is how Confluence page paths and URL query strings
        join words (``.../Example+Meeting+Notes``), and ``a??b??c`` is
        null-coalescing code; both are far more common than intraword
        inserted text or citations.  The cost is a marker left visible rather
        than a mangled link, and the write path can produce such markup, so
        this is a deliberate one-way trade.

        Both examples here are ASCII, where the spaced form ``+ins+`` remains
        available.  That is not true everywhere; see
        ``test_plus_and_citation_are_unconvertible_in_unspaced_scripts``.
        """
        assert preprocessor.jira_to_markdown(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("これは+挿入+です", id="japanese-inserted"),
            pytest.param("これは??引用??です", id="japanese-citation"),
            pytest.param(
                "品質は+良い+。", id="japanese-inserted-before-ideographic-stop"
            ),
            pytest.param("café+ins+naïve", id="accented-latin-inserted"),
        ],
    )
    def test_plus_and_citation_are_unconvertible_in_unspaced_scripts(
        self, preprocessor, text: str
    ):
        """The word-character condition on ``+`` and ``??`` is a Unicode one.

        Python's ``\\w`` covers every script, so a Japanese or Chinese span is
        intraword by construction -- and that prose has no whitespace-delimited
        alternative to fall back on.  For those languages the two markers stop
        converting altogether rather than merely losing their intraword form:
        every case here converted before this change and does not now.

        It is a real cost, kept because the reason for the condition does not
        depend on the script: ``+`` still joins words in a Confluence page path
        and ``??`` is still null-coalescing code, and a Japanese enumeration
        (``牛乳+パン+卵``) would be corrupted by the same rule that recovers
        ``+挿入+``.  Markers left visible are lossless; eaten characters are
        not.  ``^`` and ``~`` are unaffected -- see
        ``test_intraword_superscript_and_subscript_convert``.
        """
        assert preprocessor.jira_to_markdown(text) == text
        assert preprocessor.clean_jira_text(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("https://ex.com/s?q=+foo+", id="plus-in-query"),
            pytest.param("https://ex.com/a?b=+x+&c=1", id="plus-in-query-param"),
            pytest.param(
                "https://ex.com/wiki/Example+Meeting+Notes",
                id="plus-in-confluence-path",
            ),
            pytest.param("https://ex.com/~user/~backup", id="tilde-in-path"),
            pytest.param("https://ex.com/a^b^c", id="caret-in-path"),
            pytest.param(
                "https://ex.com/a_b_c and https://ex.com/d_e_f",
                id="underscores-in-two-paths",
            ),
            pytest.param(
                "see https://ex.com/x?q=+a+ and https://ex.com/y?q=+b+ now",
                id="two-urls-in-a-sentence",
            ),
        ],
    )
    def test_scheme_ful_urls_keep_every_marker_character(self, preprocessor, text: str):
        """No inline conversion reaches inside a URL token that has a scheme.

        A rewritten marker turns a live link into a dead one, which is the
        headline symptom this module guards against.  The guard covers the
        still-unbounded italic pairing too, so a path with underscores stays
        clickable.

        The scheme is what makes the token recognisable, and it is all the
        guard recognises; see ``test_scheme_less_urls_are_not_protected``.
        """
        assert preprocessor.jira_to_markdown(text) == text
        assert preprocessor.clean_jira_text(text) == text

    @pytest.mark.parametrize(
        "text, expected, expected_clean",
        [
            pytest.param(
                "www.ex.com/a_b_c",
                "www.ex.com/a*b*c",
                "www.ex.com/a*b*c",
                id="underscores-in-a-bare-host-path",
            ),
            pytest.param(
                "example.com/~user/~backup",
                "example.com/<sub>user/</sub>backup",
                "example.com/user/backup",
                id="tildes-in-a-bare-host-path",
            ),
        ],
    )
    def test_scheme_less_urls_are_not_protected(
        self, preprocessor, text: str, expected: str, expected_clean: str
    ):
        """Characterization test: a URL without a scheme is still rewritten.

        ``_URL_TOKEN_PATTERN`` matches ``http(s)://`` and ``ftp://`` only, and
        ``_sub_outside_urls`` short-circuits on text holding no ``://`` at all,
        so these two come out as dead links.  Both assertions hold identically
        without the boundary rules -- this is unchanged behaviour, recorded so
        the guard is not read as covering every URL; the sibling test above
        says scheme-ful in its name for the same reason.

        The second parameter shows what that costs on the plain-text path,
        where the tags are stripped again: ``example.com/~user/~backup`` is read
        back as ``example.com/user/backup``, two characters short.

        It is left as is rather than widened: nothing distinguishes a bare host
        from a sentence, so protecting ``example.com/~user`` means guessing
        where prose ends.
        """
        assert preprocessor.jira_to_markdown(text) == expected
        assert preprocessor.clean_jira_text(text) == expected_clean

    def test_markup_wrapping_a_url_still_converts(self, preprocessor):
        """A span may still wrap a whole URL; only spans inside one are kept."""
        assert (
            preprocessor.jira_to_markdown("+https://ex.com/a+")
            == "<ins>https://ex.com/a</ins>"
        )

    @pytest.mark.parametrize(
        "text, expected",
        [
            pytest.param("(+two words+)", "(+two words+)", id="in-brackets"),
            pytest.param("*+ins bold+*", "**+ins bold+**", id="nested-in-bold"),
            pytest.param("|+two ok+|", "|+two ok+|", id="unpadded-table-cell"),
            pytest.param(
                "|+ok+|+two ok+|",
                "|<ins>ok</ins>|+two ok+|",
                id="unpadded-cells-tight-and-loose",
            ),
        ],
    )
    def test_multiword_span_against_punctuation_is_not_converted(
        self, preprocessor, text: str, expected: str
    ):
        """Known, deliberate false negatives -- more than the bracketed one.

        A multi-word span has to open at the start of a line or after
        whitespace, because that is the only thing separating markup from
        prose such as ``see "~/tmp" and "~/var"``.  Any punctuation in front of
        the marker fails that, wherever it comes from: the user's own brackets,
        an enclosing emphasis marker, or a table-cell pipe.  Each one pays with
        a marker left visible -- lossless, unlike silently eating the
        characters between two literal markers.  The tight, single-token form
        keeps working in every one of those positions: the last parameter shows
        both in one table row, and ``(+ok+)``, ``|+ok+|`` and ``*+ins+*`` are
        pinned in ``test_genuine_markup_still_converts``.
        """
        assert preprocessor.jira_to_markdown(text) == expected

    def test_multiword_span_inside_a_macro_is_not_converted(self, preprocessor):
        """The same false negative when the punctuation comes from a macro.

        ``{color:red}`` closes with a brace, so the span behind it opens
        against punctuation and stays literal.  The colour macro's own output
        is asserted loosely on purpose: how ``{color}`` renders is another
        rule's business, and the point here is only that the ``+`` markers
        survive as text.
        """
        result = preprocessor.jira_to_markdown("{color:red}+two words+{color}")
        assert "+two words+" in result
        assert "<ins>" not in result

        tight = preprocessor.jira_to_markdown("{color:red}+ins+{color}")
        assert "<ins>ins</ins>" in tight

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("+word+s", id="inserted-then-word"),
            pytest.param("??cited??s", id="citation-then-word"),
        ],
    )
    def test_span_glued_to_the_following_word_is_not_converted(
        self, preprocessor, text: str
    ):
        """A closing ``+`` or ``??`` may not be followed by a word character.

        ``+word+s`` used to convert and no longer does, which is the price of
        the symmetric rule that keeps ``?q=+foo+bar`` and other word-joining
        text intact.  Only a directly suffixed span is affected: punctuation is
        not a word character, so ``the +new+'s value`` still converts (see
        ``test_genuine_markup_still_converts``).
        """
        assert preprocessor.jira_to_markdown(text) == text
        assert preprocessor.clean_jira_text(text) == text

    @pytest.mark.parametrize(
        "text, expected",
        [
            pytest.param(r"\+literal\+", r"\+literal\+", id="escaped-plus"),
            pytest.param(r"\*not bold\*", r"\*not bold\*", id="escaped-asterisk"),
            pytest.param(r"a\_b.py", r"a\_b.py", id="escaped-underscore"),
            pytest.param(r"\~home", r"\~home", id="escaped-tilde"),
            pytest.param(r"2\^10", r"2\^10", id="escaped-caret"),
            pytest.param(
                r"\?\?not cited\?\?", r"\?\?not cited\?\?", id="escaped-question-marks"
            ),
            pytest.param(r"\-not struck\-", r"\-not struck\-", id="escaped-hyphens"),
            pytest.param(
                r"checklist \+ kit", r"checklist \+ kit", id="escaped-plus-in-prose"
            ),
            pytest.param(
                r"\*not bold\* and *bold*",
                r"\*not bold\* and **bold**",
                id="escaped-and-real-bold",
            ),
            pytest.param(
                r"a\_b.py and _italic_",
                r"a\_b.py and *italic*",
                id="escaped-and-real-italic",
            ),
        ],
    )
    def test_escaped_markers_stay_escaped(self, preprocessor, text: str, expected: str):
        """A backslash-escaped marker opens no span and stays literal.

        The escape is kept rather than dropped: ``\\*not bold\\*`` read as
        markdown must not turn into emphasis, and every one of these
        characters is ASCII punctuation, for which ``\\c`` is a valid markdown
        escape rendering as the bare character.

        The two mixed parameters put an escaped marker beside a real span, so
        they are the ones that reach the ``*``/``_`` conversion: without the
        escape extraction the escaped marker pairs with the genuine one and
        both halves come out wrong.
        """
        assert preprocessor.jira_to_markdown(text) == expected

    def test_escaped_backslash_does_not_swallow_the_next_marker(self, preprocessor):
        r"""``C:\\+foo+`` is a literal backslash followed by real markup."""
        assert preprocessor.jira_to_markdown("C:\\\\+foo+") == "C:\\\\<ins>foo</ins>"

    def test_escape_restoration_does_not_scale_with_occurrences(self, preprocessor):
        """Restoring escapes must cost one pass, not one pass per escape.

        A per-occurrence placeholder list needs a full-text ``str.replace``
        for every stored item, which made a single 32 KB field of escaped
        markers (Jira's field cap) take ~1.5 s of pure read path.

        The placeholder count is the whole property, so it is asserted directly
        rather than timed: it is one per *escapable character*, never one per
        occurrence, and stays so no matter how large the field grows. A
        wall-clock threshold would only observe that property indirectly, and
        would go flaky on a loaded runner.
        """
        protected = _protect_escaped_chars(r"\+" * 5000 + r"\_" * 5000)
        placeholders = set(re.findall(r"\x00ESC\d+\x00", protected))
        assert len(placeholders) == 2, "one placeholder per character, not per escape"
        assert len(placeholders) < len(_ESCAPABLE_CHARS)

        # A full 32 KB field of one escape: distinct placeholders stay at one,
        # though the escape occurs 16383 times. The regressed implementation
        # minted a placeholder per occurrence, so this set tracked the
        # occurrence count and every member cost its own full-text replace.
        field = r"\+" * 16383
        occurrences = re.findall(r"\x00ESC\d+\x00", _protect_escaped_chars(field))
        assert len(occurrences) == 16383
        assert len(set(occurrences)) == 1
        assert preprocessor.jira_to_markdown(field) == field

    @pytest.mark.parametrize(
        "text, expected",
        [
            pytest.param(
                "12:30~13:00,14:00~15:00",
                "12:30<sub>13:00,14:00</sub>15:00",
                id="comma-separated-time-ranges",
            ),
            pytest.param(
                "12:30~13:00;14:00~15:00",
                "12:30<sub>13:00;14:00</sub>15:00",
                id="semicolon-separated-time-ranges",
            ),
            pytest.param(
                "9:00~12:00、13:00~17:00",
                "9:00<sub>12:00、13:00</sub>17:00",
                id="ideographic-comma-separated-time-ranges",
            ),
            pytest.param(
                "楽しみ9:00~12:00で14:00~17:00",
                "楽しみ9:00<sub>12:00で14:00</sub>17:00",
                id="japanese-time-ranges",
            ),
            pytest.param("x^2,y^2", "x<sup>2,y</sup>2", id="comma-separated-exponents"),
            pytest.param("1.0~rc1~2", "1.0<sub>rc1</sub>2", id="version-with-tildes"),
        ],
    )
    def test_punctuation_flanked_sup_and_sub_still_pair(
        self, preprocessor, text: str, expected: str
    ):
        """Characterization test: the residual ``^``/``~`` corruption class.

        Every assertion here holds identically without the boundary rules, so
        none of them has detection power against this change.  They are here
        because the change is easy to over-read: it fixes ``x^2 plus y^2`` and
        ``12:30~13:00 and 14:00~15:00``, and it fixes nothing at all when the
        neighbours are punctuation instead of spaces.  A tight ``^``/``~`` span
        asks only that its content hold no whitespace, so ``~13:00,14:00~``
        pairs exactly as before -- and ``clean_jira_text`` then drops the tags,
        leaving ``12:3013:00,14:0015:00``, with the characters gone.

        The rule is kept because nothing separates ``~13:00,14:00~`` from the
        ``~2~`` in ``H~2~O``, which Jira renders and which
        ``markdown_to_jira`` emits: closing this hole means refusing to read
        back markup this library writes.  Anything narrower -- banning specific
        punctuation inside the content, say -- would be a guess dressed up as a
        rule, unverifiable against Atlassian's parser from here.
        """
        assert preprocessor.jira_to_markdown(text) == expected

    def test_spans_do_not_cross_a_line_break(self, preprocessor):
        """A marker on one line never pairs with a marker on the next."""
        text = "first ~line\nsecond~ line"
        assert preprocessor.jira_to_markdown(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("\x00ESC8\x00", id="index-past-the-table"),
            pytest.param("\x00ESC12\x00", id="two-digit-index"),
            pytest.param("\x00ESC0000000009\x00", id="zero-padded-index"),
            pytest.param("desc \x00ESC12\x00 end", id="unknown-token-in-prose"),
            pytest.param("\x00ESC7\x00", id="token-this-module-does-write"),
            pytest.param("\x00ESC1\x00", id="plus-token"),
            pytest.param("\x00ESC1\x00 and \\+", id="token-beside-a-real-escape"),
            pytest.param("\x00NUL\x00", id="nul-token-itself"),
            pytest.param("a\x00b", id="bare-nul"),
        ],
    )
    def test_placeholder_shaped_field_text_survives(self, preprocessor, text: str):
        r"""A field carrying the escape placeholders' own bytes reads back as is.

        Escapes are held as \x00ESC<n>\x00 placeholders while the conversions
        run, and a field can arrive already holding that sequence: JSON has an
        escape for the NUL byte, so an API client can put one in a description
        even though a browser cannot type it.  Restoring by an index parsed out
        of the text made \x00ESC12\x00 raise IndexError from the read path --
        any field, any caller -- and rewrote \x00ESC7\x00 into \- silently.

        Now only whole tokens this module actually wrote are restored, and an
        incoming NUL is itself tokenised first so it cannot open one.  The text
        comes back byte for byte, as it did before escapes were handled at all.

        The last two parameters carry no evidence about the old defect -- they
        passed before it was fixed as well.  What they pin is that tokenising
        the incoming NUL is exactly reversible, which is the part of the fix
        that could regress quietly.
        """
        assert preprocessor.jira_to_markdown(text) == text
        assert preprocessor.clean_jira_text(text) == text

    def test_strikethrough_marker_is_left_untouched(self, preprocessor):
        """``-text-`` is deliberately not converted.

        Characterization test: it passes identically without the production
        change, so it has no detection power against it -- the substitution
        this change deleted rewrote ``-text-`` to itself.  What it documents
        is that hyphens are out of scope: a flanking rule cannot separate
        markup from prose on them, where ranges such as "rows 3-5 and 7-9"
        look exactly like a span.  The dead no-op was removed rather than
        turned into a real conversion.
        """
        assert preprocessor.jira_to_markdown("-text-") == "-text-"

    @pytest.mark.parametrize(
        "text, expected",
        [
            pytest.param(
                "area = 5 * 3 and volume = 2 * 4",
                "area = 5 * 3 and volume = 2 * 4",
                id="two-multiplications",
            ),
            pytest.param("x* y *z", "x* y *z", id="markers-facing-outwards"),
        ],
    )
    def test_asterisk_prose_survives(self, preprocessor, text: str, expected: str):
        """``*`` takes the space-side half of the flanking rule.

        Arithmetic prose used to read back as ``area = 5 ** 3 and volume =
        2 ** 4``, which is corruption rather than markup.  The opening ``*``
        must now be followed by a non-space and the closing one preceded by a
        non-space, which is exactly what multiplication signs fail.
        """
        assert preprocessor.jira_to_markdown(text) == expected
        assert preprocessor.clean_jira_text(text) == expected

    @pytest.mark.parametrize(
        "text, expected",
        [
            pytest.param("*bold text*", "**bold text**", id="whole-field"),
            pytest.param("a*b*c", "a**b**c", id="intraword"),
            pytest.param("x *y* z", "x **y** z", id="mid-sentence"),
            pytest.param("a *b c* d", "a **b c** d", id="multi-word"),
        ],
    )
    def test_bold_still_converts_including_intraword(
        self, preprocessor, text: str, expected: str
    ):
        """``*`` gets the space-side rule only -- never a word-character one.

        Every case here also passes without the production change, so this has
        no detection power against it either.  What it does detect is the
        over-correction: Jira renders intraword bold, so adding the
        word-character guards that ``+`` and ``??`` carry would silently drop
        markup Jira itself renders, and ``a*b*c`` would stop converting.
        """
        assert preprocessor.jira_to_markdown(text) == expected

    def test_underscore_boundaries_are_out_of_scope(self, preprocessor):
        """``_`` keeps its unbounded pairing on purpose, unlike ``*``.

        Characterization test: this assertion holds identically without the
        production change, so it has no detection power against it.  It is
        here to document the deliberate asymmetry.  The Jira renderer
        italicises even intraword underscores, so ``a_b.py and c_d.py`` really
        does render with italics in Jira; the write path escapes intraword
        underscore runs precisely because of that (see
        ``escape_intraword_underscore_runs``).  Only escape handling and the
        URL guard were added for this marker -- changing the pairing means
        changing the fidelity contract with Jira's own renderer.
        """
        assert (
            preprocessor.jira_to_markdown("files a_b.py and c_d.py")
            == "files a*b.py and c*d.py"
        )

    def test_confluence_smart_link_url_keeps_its_plus_signs(self, preprocessor):
        """A Confluence page URL must survive the read path intact."""
        url = (
            "https://example.atlassian.net/wiki/spaces/PROJ/pages/987654321/"
            "Example+Meeting+Notes"
        )
        result = preprocessor.clean_jira_text(f"[Meeting Notes|{url}|smart-link]")
        assert result == f"[Example Meeting Notes]({url})"

    @pytest.mark.parametrize(
        "markdown",
        [
            pytest.param("H<sub>2</sub>O", id="chemical-formula"),
            pytest.param("CO<sub>2</sub> emissions", id="subscript-then-space"),
            pytest.param("2<sup>10</sup> bytes", id="power-of-two"),
            pytest.param("1<sup>st</sup> place", id="ordinal"),
            pytest.param("m<sup>3</sup>", id="cubic-metre"),
            pytest.param("T<sub>1</sub> and T<sub>2</sub>", id="two-subscripts"),
            pytest.param("e<sup>x</sup> + e<sup>y</sup>", id="two-superscripts"),
            pytest.param("the <ins>new</ins> value", id="inserted-span"),
            pytest.param("<cite>quoted</cite> text", id="citation-span"),
        ],
    )
    def test_sub_sup_and_ins_round_trip(self, preprocessor, markdown: str):
        """What the write path emits, the read path reads back.

        ``markdown_to_jira`` maps ``<sub>``/``<sup>``/``<ins>``/``<cite>``
        onto ``~``/``^``/``+``/``??`` with no boundary logic at all, so every
        span it can write has to survive the round trip.
        """
        jira = preprocessor.markdown_to_jira(markdown)
        assert preprocessor.jira_to_markdown(jira) == markdown

    @pytest.mark.parametrize(
        "markdown, jira",
        [
            pytest.param("a<ins>b</ins>c", "a+b+c", id="intraword-inserted"),
            pytest.param("x<cite>y</cite>z", "x??y??z", id="intraword-citation"),
        ],
    )
    def test_intraword_ins_and_cite_do_not_round_trip(
        self, preprocessor, markdown: str, jira: str
    ):
        """The two spans the write path can emit and the read path will not take.

        Reading them back would mean converting the ``+`` in every Confluence
        page path and the ``??`` in every null-coalescing expression; see
        ``test_intraword_plus_and_citation_are_left_alone``.  The markup stays
        visible in the markdown instead of being silently dropped.
        """
        assert preprocessor.markdown_to_jira(markdown) == jira
        assert preprocessor.jira_to_markdown(jira) == jira
