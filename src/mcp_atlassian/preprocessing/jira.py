"""Jira-specific text preprocessing module."""

import logging
import re
from bisect import bisect_right
from collections.abc import Callable
from typing import Any

from .base import BasePreprocessor, _extract_blocks, _restore_blocks

logger = logging.getLogger("mcp-atlassian")

_ISSUE_KEY_PATTERN = r"[A-Z][A-Z0-9_]+-\d+(?:-\d+)*"

# Characters Jira escapes with a leading backslash when they are meant
# literally: the markup markers, plus the backslash itself.  Every escape is
# pulled out of the text before any markup conversion runs, so an escaped
# character can neither open nor close a span, and is put back afterwards as
# the markdown escape ``\c`` -- keeping the escape is what keeps the character
# literal for a markdown reader too, since a bare ``*not bold*`` would render
# as emphasis.  ``\\`` restores as ``\\``, a literal backslash in markdown.
#
# The restored text is fully determined by the character, so there is one
# fixed placeholder per character rather than one per occurrence: restoring an
# indexed placeholder list costs a full-text pass per stored item, which made
# escaped text quadratic (1.5 s for a single 32 KB field).
_ESCAPABLE_CHARS = "\\+*_~^?-"
_ESCAPED_CHAR_PATTERN = re.compile(
    "\\\\([" + "".join(re.escape(char) for char in _ESCAPABLE_CHARS) + "])"
)
# Kept short on purpose: a field of nothing but escapes is rewritten into
# these placeholders, and every downstream regex then walks the longer text.
_ESCAPE_PLACEHOLDERS = {
    char: f"\x00ESC{index}\x00" for index, char in enumerate(_ESCAPABLE_CHARS)
}
# The placeholders live in NUL, which no Jira field is meant to contain -- but
# the JSON that carries a field can encode one, so the text may arrive already
# holding something shaped like a placeholder.  Two rules keep that harmless.
# Restoration matches whole known tokens only, never an index parsed out of the
# text, so ``\x00ESC12\x00`` means nothing here and is left alone instead of
# indexing off the end of ``_ESCAPABLE_CHARS``.  And every NUL arriving in the
# text is itself replaced by a token first, so a literal ``\x00ESC7\x00`` is
# not mistaken for the placeholder of ``\-``: both tokens are restored in one
# left-to-right pass, where the NUL token consumes the NUL that would otherwise
# open an escape placeholder, and the field reads back byte for byte.
_NUL_PLACEHOLDER = "\x00NUL\x00"
_PLACEHOLDER_RESTORATIONS: dict[str, str] = {
    _NUL_PLACEHOLDER: "\x00",
    **{placeholder: "\\" + char for char, placeholder in _ESCAPE_PLACEHOLDERS.items()},
}
# One literal prefix and one alternation of token bodies, so marker-free text
# costs a single scan for NUL rather than one attempt per token.
_PLACEHOLDER_PATTERN = re.compile(
    "\x00(?:"
    + "|".join(re.escape(token[1:-1]) for token in _PLACEHOLDER_RESTORATIONS)
    + ")\x00"
)

# A URL is a single token that has to survive the read path byte for byte: a
# marker converted inside one produces a dead link, which is the reason this
# module bounds inline markup at all.  Spans delimited inside such a token are
# left alone, so both ``.../Example+Meeting+Notes`` and ``?q=+foo+`` keep
# their plus signs.  A trailing marker, sentence punctuation or placeholder is
# left outside the token, so markup *wrapping* a whole URL still converts, as
# in ``+https://example.com/a+.``
#
# Only a token with an explicit scheme is recognised, and ``_sub_outside_urls``
# short-circuits on text with no ``://`` at all, so a bare host is not covered:
# ``www.example.com/a_b_c`` still italicises and ``example.com/~user/~backup``
# still subscripts, both of them dead links.  That is unchanged behaviour, not
# something the flanking rules address -- a boundary rule cannot tell a host
# name from a sentence, and widening the guard to bare hosts would have to
# guess at where prose ends.
_URL_TOKEN_PATTERN = re.compile(
    r"(?:https?|ftp)://\S*[^\s+*_~^?.,;:!\x00]", re.IGNORECASE
)

# Bold takes the space-side half of the flanking rule and nothing else: the
# opening ``*`` hugs its content and so does the closing one.  That is what
# keeps arithmetic prose such as ``area = 5 * 3 and volume = 2 * 4`` from
# reading back as ``5 ** 3``.  It deliberately gets no word-character rule,
# unlike ``+`` or ``??``: Jira does render intraword bold, so ``a*b*c`` has to
# keep converting to ``a**b**c``.
#
# Italic pairing stays fully unbounded, because Jira's renderer italicises even
# intraword underscores -- ``a_b.py and c_d.py`` really is rendered with italics
# in Jira, and the write path escapes intraword underscore runs for exactly that
# reason (see ``escape_intraword_underscore_runs``).  Bounding the read side
# would break fidelity with Jira's renderer rather than restore it.
#
# Neither marker may reach into a scheme-ful URL; ``_sub_outside_urls``
# enforces that, with the limits noted on ``_URL_TOKEN_PATTERN``.
_BOLD_ITALIC_PATTERN = re.compile(r"\*(?!\s)(.*?)(?<!\s)\*|_(.*?)_")


def _protect_escaped_chars(text: str) -> str:
    r"""Replace every ``\c`` escape with a per-character placeholder.

    Any NUL already in the text is replaced first, so a field that happens to
    carry a placeholder-shaped byte sequence cannot be confused with a
    placeholder this function wrote (see ``_NUL_PLACEHOLDER``).  The code and
    inline-code placeholders that ``_extract_blocks`` inserted upstream are
    built from NUL as well, so they are tokenised here too and come back intact
    in ``_restore_escaped_chars``, which runs before the blocks are restored.

    Args:
        text: Text in Jira wiki markup, straight from the field.

    Returns:
        Text with every escape, and every NUL, replaced by a placeholder.
    """
    text = text.replace("\x00", _NUL_PLACEHOLDER)
    return _ESCAPED_CHAR_PATTERN.sub(
        lambda match: _ESCAPE_PLACEHOLDERS[match.group(1)], text
    )


def _restore_escaped_chars(text: str) -> str:
    """Turn escape placeholders back into markdown escapes, NUL tokens into NUL.

    Only whole tokens this module writes are restored; a placeholder-shaped
    sequence that came in with the text matches none of them and is left as it
    was.

    Args:
        text: Text whose escapes are still held as placeholders.

    Returns:
        Text with escapes back as ``\\c`` and any NUL back as itself.
    """
    if "\x00" not in text:
        return text
    return _PLACEHOLDER_PATTERN.sub(
        lambda match: _PLACEHOLDER_RESTORATIONS[match.group(0)], text
    )


def _sub_outside_urls(
    pattern: re.Pattern[str],
    repl: Callable[[re.Match[str]], str],
    text: str,
    *,
    marker_length: int = 1,
) -> str:
    """Substitute ``pattern`` except where a delimiter sits inside a URL token.

    A URL token means one with an explicit scheme; a scheme-less
    ``www.example.com/a_b_c`` is not protected (see ``_URL_TOKEN_PATTERN``).

    Args:
        pattern: Compiled pattern whose match starts and ends with a marker.
        repl: Replacement callback, applied only to matches clear of a URL.
        text: Text to substitute in.
        marker_length: Length of the closing marker, used to locate it.

    Returns:
        Text with every match delimited outside a scheme-ful URL replaced.
    """
    # Fast path: no scheme, so no URL to protect.  Worth the check because
    # this runs once per marker per field on every issue read.
    if "://" not in text:
        return pattern.sub(repl, text)
    spans = [match.span() for match in _URL_TOKEN_PATTERN.finditer(text)]
    if not spans:
        return pattern.sub(repl, text)
    starts = [start for start, _ in spans]
    ends = [end for _, end in spans]

    def _inside_url(position: int) -> bool:
        # finditer yields non-overlapping spans in order, so the only span
        # that can hold the position is the last one starting at or before it.
        index = bisect_right(starts, position) - 1
        return index >= 0 and position < ends[index]

    def _guarded(match: re.Match[str]) -> str:
        if _inside_url(match.start()) or _inside_url(match.end() - marker_length):
            return match.group(0)
        return repl(match)

    return pattern.sub(_guarded, text)


def _inline_markup_pattern(marker: str, *, intraword: bool) -> re.Pattern[str]:
    """Build the pattern that pairs one Jira inline marker.

    Two span shapes count as markup, and neither ever crosses a line break:

    * A *tight* span, whose content holds no whitespace -- ``+ok+``,
      ``(+ok+)``, ``H~2~O``.  With ``intraword`` set the span may also sit
      inside a word, because that is where superscript and subscript nearly
      always appear (``H~2~O``, ``CO~2~``, ``1^st^``, ``2^10^``) and because
      this library writes exactly that: ``markdown_to_jira`` turns
      ``<sub>2</sub>`` into ``~2~`` with no boundary logic, so refusing to
      read it back would mean writing markup we cannot parse.  It stays off
      for ``+`` and ``??``, where the same shape is overwhelmingly not markup:
      a Confluence page path or a URL query string joins words with ``+``, and
      ``a??b??c`` is null-coalescing code rather than a citation.
    * A *loose* span, whose content holds whitespace -- ``+two words+``.  It
      must open at the start of a line or after whitespace, so prose whose
      markers merely hug punctuation keeps them: ``[+] done and [+] pending``,
      ``see "~/tmp" and "~/var"``, ``(+/-) (+/-)``.

    Either way the opening marker hugs its content, the closing marker hugs
    its content and is not followed by a word character, and the content never
    contains the marker.  Prose such as ``x^2 plus y^2``, ``date + 4 weeks``,
    ``12:30~13:00 and 14:00~15:00`` or ``cost $5+$6 and $7+$8`` therefore
    survives untouched instead of losing characters to a stray second marker.

    What that costs, in full -- every one of these is markup Jira renders and
    this function declines, and every one of them leaves its markers visible
    rather than eating text:

    * The word-character condition on ``+`` and ``??`` is a Unicode one, so in
      a script written without spaces between words every span is intraword
      and neither marker converts *at all*.  ``これは+挿入+です`` and
      ``これは??引用??です`` come back unchanged, and there is no
      whitespace-delimited alternative in that prose to fall back on, so for
      those languages the loss is total rather than merely intraword; the same
      goes for accented Latin, where ``café+ins+naïve`` keeps its markers.
      ``^`` and ``~`` are unaffected, since their intraword arm has no
      word-character condition (``你好~sub~世界`` still converts).
    * A loose span must open at the start of a line or after whitespace, which
      no punctuation satisfies: ``(+two words+)``, a span wrapped in a macro
      (``{color:red}+two words+{color}``), a span nested inside emphasis
      (``*+ins bold+*``) and a span in an unpadded table cell (``|+two ok+|``)
      are all left alone.  Only the whitespace-padded forms (``| cell +ins+ |``)
      and single tokens (``|+ok+|``) convert.
    * The closing marker may not be followed by a word character, so a span
      glued to the next word -- ``+word+s`` -- does not convert either.  That
      is the symmetric price of protecting ``?q=+foo+bar``; a following
      apostrophe or punctuation is not a word character, so ``the +new+'s``
      still converts.

    And what it does *not* fix, for ``^`` and ``~``: their tight arm asks only
    that the content hold no whitespace, so a pair whose neighbours are
    punctuation still pairs across prose exactly as before -- ``x^2,y^2``,
    ``12:30~13:00,14:00~15:00``, ``9:00~12:00、13:00~17:00`` and ``1.0~rc1~2``
    all lose characters.  Only the space-separated forms are protected.  The
    corruption class is narrowed for these two markers, not closed: nothing
    distinguishes ``~13:00,14:00~`` from ``~2~`` in ``H~2~O``, which Jira
    renders and this library writes.

    Args:
        marker: Literal wiki marker delimiting the span, e.g. ``"+"``.
        intraword: Whether a tight span may sit inside a word.

    Returns:
        Compiled pattern whose first or second group holds the span content.
    """
    esc = re.escape(marker)
    if len(marker) == 1:
        # "Anything but the marker" as a character class, which the regex
        # engine walks far faster than a lookahead per character.
        tight_content = rf"[^\s{esc}]+"
        loose_content = rf"[^\s{esc}][^\n{esc}]*"
    else:
        not_marker = f"(?!{esc})"
        tight_content = rf"(?:{not_marker}\S)+?"
        loose_content = rf"{not_marker}\S(?:{not_marker}[^\n])*?"
    # Every branch opens with the marker itself rather than a lookbehind, so
    # the engine can scan straight to the next marker instead of testing every
    # position; each boundary rule is then a lookbehind spanning the character
    # before the marker plus the marker.  Leading with the lookbehind costs
    # ~70x on marker-free prose, which is most of what a read path sees.
    if intraword:
        tight = rf"{esc}(?<!\\{esc})({tight_content}){esc}"
    else:
        tight = rf"{esc}(?<![\w\\]{esc})({tight_content}){esc}(?!\w)"
    # "(?<![^\s]MARKER)" is "the marker starts the text or follows whitespace".
    loose = rf"{esc}(?<![^\s]{esc})({loose_content})(?<!\s){esc}(?!\w)"
    return re.compile(f"(?:{tight}|{loose})")


# Marker, tag and intraword rule for every inline span the read path converts.
# Strikethrough (``-text-``) is deliberately absent: no flanking rule can tell
# markup from prose on hyphens, where "rows 3-5 and 7-9" looks exactly like a
# span.
_INLINE_MARKUP_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("??", _inline_markup_pattern("??", intraword=False), "cite"),
    ("+", _inline_markup_pattern("+", intraword=False), "ins"),
    ("^", _inline_markup_pattern("^", intraword=True), "sup"),
    ("~", _inline_markup_pattern("~", intraword=True), "sub"),
)


def _convert_inline_markup(text: str) -> str:
    """Convert Jira citation, inserted, superscript and subscript spans.

    Args:
        text: Text in Jira wiki markup, with escapes already protected.

    Returns:
        Text with every paired marker replaced by its HTML tag.
    """
    for marker, pattern, tag in _INLINE_MARKUP_RULES:

        def _wrap(match: re.Match[str], tag: str = tag) -> str:
            content = match.group(1)
            if content is None:
                content = match.group(2)
            return f"<{tag}>{content}</{tag}>"

        text = _sub_outside_urls(pattern, _wrap, text, marker_length=len(marker))
    return text


def _wrap_bold_or_italic(match: re.Match[str]) -> str:
    """Convert one paired ``*`` or ``_`` span to its markdown equivalent.

    Args:
        match: Match from ``_BOLD_ITALIC_PATTERN``; group 1 holds the content
            of a bold span, group 2 the content of an italic one.

    Returns:
        The content wrapped in ``**`` for bold, or in ``*`` for italic.
    """
    bold = match.group(1)
    if bold is not None:
        return f"**{bold}**"
    return f"*{match.group(2)}*"


# Lazy and per block: with a greedy ``([\s\S]*)`` the first ``{quote}`` pairs
# with the *last* closing tag in the field, so two quotes with prose between
# them -- which is exactly what Jira emits for two blockquote nodes -- come back
# as one blockquote that swallows the prose and carries literal ``{quote}``
# tokens inside it.  No ``re.MULTILINE``: the pattern has no ``^`` or ``$`` for
# the flag to change.
_QUOTE_BLOCK_PATTERN = re.compile(r"\{quote\}([\s\S]*?)\{quote\}")

# Jira Cloud spells a panel's kind as a background colour and never emits
# ``title=``; these five colours are the whole vocabulary its editor produces.
# Mapping them back is what keeps a warning panel from reading as an ordinary
# paragraph -- the panel's entire meaning lives in this one hex value.  The
# fence spellings are the ones ``markdown_to_adf`` reads back as a real ADF
# ``panel``, so a colour panel survives a read/write cycle.
_PANEL_BG_COLOR_TYPES = {
    "#deebff": "info",
    "#eae6ff": "note",
    "#fffae6": "warning",
    "#e3fcef": "success",
    "#ffebe6": "error",
}
# Matched on the parameter's value, not on the whole parameter string: Jira puts
# ``borderStyle`` or ``title`` alongside the colour, in either order.  Anchoring
# on ``|`` (or the start) keeps a ``borderColor`` from being read as ``bgColor``
# and a ``subtitle`` from being read as ``title``.
_PANEL_BG_COLOR_PATTERN = re.compile(
    r"(?:^|\|)\s*bgColor\s*=\s*([^|}]+)", re.IGNORECASE
)
_MACRO_TITLE_PATTERN = re.compile(r"(?:^|\|)\s*title\s*=\s*([^|}]+)", re.IGNORECASE)

# The block macros a Server/DC author writes by hand, and the panel type each
# one carries.  ``{tip}`` has no counterpart in ADF, where the closest panel
# type is ``success``; the read side therefore never emits ``:::tip``.
_MACRO_PANEL_TYPES = {
    "info": "info",
    "note": "note",
    "warning": "warning",
    "tip": "success",
}
# ``{panel}`` and ``{expand}`` are handled separately: a panel takes its type
# from ``bgColor``, an expand takes a title into the fence itself.
_BLOCK_MACRO_TAGS = ("panel", "expand", *_MACRO_PANEL_TYPES)
_BLOCK_MACRO_PATTERN = re.compile(
    r"\{(" + "|".join(_BLOCK_MACRO_TAGS) + r")(?::([^}\n]*))?\}", re.IGNORECASE
)

# A code block is pulled out to a placeholder before the ``{quote}`` handler
# runs, so the handler prefixes the placeholder's line and cannot reach the
# lines it stands for.  This finds such a line to carry the prefix inwards.
_QUOTED_CODEBLOCK_PATTERN = re.compile(
    r"^((?:> )+)(?=\x00CODEBLOCK(\d+)\x00)", re.MULTILINE
)

# The write direction of the fences above, for Server/DC -- Cloud writes ADF.
# Without it a description read and written back on Server puts the fence into
# the field as literal text, where before the read path spelled panels this way
# it at least came back as prose that renders.  The colours are the read map
# inverted, so the two directions cannot drift apart.
_PANEL_TYPE_BG_COLORS = {
    panel_type: bg_color for bg_color, panel_type in _PANEL_BG_COLOR_TYPES.items()
}
# ``:::tip`` is accepted on the write side only, as an alias for success, so
# hand-authored markdown is tolerated; the read path never emits it.
_FENCE_PANEL_ALIASES = {"tip": "success"}
# The fence form the read path emits, and nothing looser.  A whole line, so
# prose that merely opens with three colons is left alone; the kind is optional
# so one pattern also matches the closing fence.  Trailing text is captured
# rather than allowed, because a panel fence carries none -- that is what
# ``markdown_to_adf`` accepts too, and it keeps ``::: see below`` prose.
_MD_FENCE_PATTERN = re.compile(r"^:::(?:([A-Za-z]+)(?:[ \t]+(.*?))?)?[ \t]*$")


def _convert_quote_block(match: re.Match[str]) -> str:
    """Convert one ``{quote}`` block to ``> ``-prefixed markdown lines.

    Jira wraps the quoted text in newlines of its own, so exactly one leading
    and one trailing newline is dropped: keeping them would open and close the
    blockquote with an empty ``> `` line, which a markdown reader shows as a
    blank quoted paragraph.  An interior blank line keeps its ``> ``, since
    that is what holds one blockquote together across two paragraphs.

    Args:
        match: Match from ``_QUOTE_BLOCK_PATTERN``; group 1 holds the quoted
            text.

    Returns:
        The quoted text with every line prefixed by ``"> "``.
    """
    content = match.group(1).removeprefix("\n").removesuffix("\n")
    return "\n".join(f"> {line}" for line in content.split("\n"))


def _panel_type_from_params(params: str | None) -> str:
    """Map a wiki panel's ``bgColor`` to the markdown fence's panel type.

    Args:
        params: Parameter string of a ``{panel:...}`` tag, or None for a bare
            ``{panel}``.

    Returns:
        One of the five fence panel types; ``"info"`` when the panel carries no
        colour, an unrecognised one, or only a title.
    """
    if params:
        match = _PANEL_BG_COLOR_PATTERN.search(params)
        if match:
            panel_type = _PANEL_BG_COLOR_TYPES.get(match.group(1).strip().lower())
            if panel_type:
                return panel_type
    return "info"


def _macro_title(params: str | None) -> str:
    """Read the ``title=`` parameter of a block macro.

    Args:
        params: Parameter string of the macro's opening tag, or None.

    Returns:
        The title, stripped, or ``""`` when the macro carries none.
    """
    if not params:
        return ""
    match = _MACRO_TITLE_PATTERN.search(params)
    return match.group(1).strip() if match else ""


def _expand_title(params: str | None) -> str:
    """Read the title of an ``{expand}`` macro, in either spelling.

    ``{expand:Details}`` is the common hand-written form and ``{expand:title=Details}``
    the explicit one; a parameter string that assigns something else carries no
    title.

    Args:
        params: Parameter string of the ``{expand}`` tag, or None.

    Returns:
        The title, stripped, or ``""`` when the macro carries none.
    """
    if not params:
        return ""
    title = _macro_title(params)
    if title:
        return title
    return "" if "=" in params else params.strip()


def _macro_open_fence(tag: str, params: str | None) -> str:
    """Build the opening ``:::`` fence for one block macro.

    Args:
        tag: Lower-cased macro tag, e.g. ``"panel"`` or ``"warning"``.
        params: Parameter string of the opening tag, or None.

    Returns:
        The fence line, followed by the title as a bold line when the macro has
        one.  ADF has no title on a panel, so a wiki title has to become
        content: a bold first paragraph inside the fence is what round-trips.
    """
    if tag == "expand":
        title = _expand_title(params)
        return f":::expand {title}" if title else ":::expand"
    panel_type = _MACRO_PANEL_TYPES.get(tag) or _panel_type_from_params(params)
    title = _macro_title(params)
    if title:
        return f":::{panel_type}\n**{title}**\n"
    return f":::{panel_type}"


def _pop_innermost_open(
    matches: list[re.Match[str]], open_stack: list[int], tag: str
) -> int | None:
    """Take the innermost still-open macro with ``tag`` off ``open_stack``.

    Any macro opened after it is abandoned, so interleaved tags such as
    ``{info}a{note}b{info}`` close the info and leave the ``{note}`` token
    literal instead of emitting a fence closed by another macro's tag.

    Args:
        matches: Every macro token in the text, in order.
        open_stack: Indices into ``matches`` of the macros still open,
            outermost first; mutated in place.
        tag: Lower-cased tag of the closing token.

    Returns:
        Index of the matching opener, or None when no macro of that tag is
        open.
    """
    for depth in range(len(open_stack) - 1, -1, -1):
        if matches[open_stack[depth]].group(1).lower() == tag:
            opener = open_stack[depth]
            del open_stack[depth:]
            return opener
    return None


def _convert_block_macros(text: str) -> str:
    """Convert Jira's block macros to the ``:::`` fenced markdown form.

    Every macro handled here -- ``{panel}``, ``{info}``, ``{note}``,
    ``{warning}``, ``{tip}`` and ``{expand}`` -- closes with the *same* bare tag
    it opens with, so nothing in a single token says whether it opens or closes.
    A lazy per-tag regex gets that wrong as soon as two macros of one kind nest:
    in ``{expand:A}outer {expand:B}inner{expand}{expand}`` it pairs
    ``{expand:A}`` with the first *bare* ``{expand}``, swallowing ``{expand:B}``
    into the content and leaving the trailing token as literal text.  The tokens
    are therefore paired by a scan over two rules:

    * a token that carries parameters is always an opener -- ``{expand:B}``
      cannot close anything;
    * a bare token closes the innermost open macro of the same tag, and opens
      one when no macro of that tag is open, which is how Jira's own renderer
      treats a repeated bare tag.

    A token left unpaired converts to nothing and is emitted verbatim, so a
    lone ``{info}`` in prose stays prose rather than opening a fence that never
    closes.

    Args:
        text: Text in Jira wiki markup, with code blocks already extracted.

    Returns:
        Text with every paired macro replaced by a ``:::`` fence on its own
        line.
    """
    matches = list(_BLOCK_MACRO_PATTERN.finditer(text))
    if not matches:
        return text

    fences: dict[int, str] = {}
    open_stack: list[int] = []
    for index, match in enumerate(matches):
        tag = match.group(1).lower()
        if match.group(2) is None:
            opener = _pop_innermost_open(matches, open_stack, tag)
            if opener is not None:
                fences[opener] = _macro_open_fence(tag, matches[opener].group(2))
                fences[index] = ":::"
                continue
        open_stack.append(index)

    # A fence has to own its line, and one newline is dropped on each side of it
    # so that a macro Jira already put on its own line does not gain a blank
    # one, while an inline ``{info}body{info}`` still lands on three lines.
    parts: list[str] = []
    position = 0
    after_fence = False
    for index, match in enumerate(matches):
        segment = text[position : match.start()]
        position = match.end()
        if after_fence:
            segment = segment.removeprefix("\n")
        fence = fences.get(index)
        if fence is None:
            parts.append(segment + match.group(0))
            after_fence = False
            continue
        parts.append(segment.removesuffix("\n"))
        parts.append(f"\n{fence}\n")
        after_fence = True
    tail = text[position:]
    parts.append(tail.removeprefix("\n") if after_fence else tail)
    return "".join(parts)


def _carry_quote_prefix_into_code_blocks(text: str, code_blocks: list[str]) -> None:
    """Prefix a code block sitting inside ``{quote}`` with the quote marker.

    The ``{quote}`` handler prefixes the placeholder's line with ``"> "`` but
    cannot reach the lines the placeholder stands for, so restoring the block
    as-is ends the blockquote at the opening fence and drops the code out of
    the quote -- even though Jira accepts a ``codeBlock`` inside a
    ``blockquote``.  Each placeholder occurs exactly once, so the stored block
    is rewritten in place with the prefix its line carries.

    Args:
        text: Text with the code placeholders back in place, i.e. after
            ``_restore_escaped_chars`` and before ``_restore_blocks``.
        code_blocks: Stored code blocks, rewritten in place.
    """
    if not code_blocks:
        return
    for match in _QUOTED_CODEBLOCK_PATTERN.finditer(text):
        index = int(match.group(2))
        # A field can arrive holding a placeholder-shaped sequence of its own
        # (see _NUL_PLACEHOLDER), so the index is not trusted to be one of ours.
        if index < len(code_blocks):
            code_blocks[index] = code_blocks[index].replace("\n", f"\n{match.group(1)}")


def _fence_macros(kind: str, title: str | None) -> tuple[str, str] | None:
    """Build the wiki opening and closing tags for one ``:::`` fence.

    Args:
        kind: Lower-cased word after the colons, e.g. ``"warning"``.
        title: Text following the kind on the fence line, or None when there is
            none.

    Returns:
        The opening and closing tag, or None when the line is not a fence this
        writes: an unknown kind, or a panel kind carrying trailing text.
    """
    if kind == "expand":
        return (f"{{expand:title={title}}}" if title else "{expand}"), "{expand}"
    if title is not None:
        return None
    bg_color = _PANEL_TYPE_BG_COLORS.get(_FENCE_PANEL_ALIASES.get(kind, kind))
    if bg_color is None:
        return None
    return f"{{panel:bgColor={bg_color}}}", "{panel}"


def _convert_fenced_blocks(text: str) -> str:
    """Convert ``:::`` panel and expand fences to the wiki macros they came from.

    The opening and closing fences are different tokens, so unlike the wiki
    macros they pair by nesting with no ambiguity: a closing fence closes the
    innermost fence still open.  A fence left unclosed is written out verbatim
    rather than as a macro, since half a macro reads worse in the field than the
    fence a human wrote.

    Args:
        text: Text in markdown, with code blocks already extracted -- a fence
            inside a code block must stay literal, and extraction is what keeps
            it out of reach here.

    Returns:
        Text with every paired fence replaced by its wiki tag.
    """
    if ":::" not in text:
        return text
    lines = text.split("\n")
    macros: dict[int, str] = {}
    open_stack: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = _MD_FENCE_PATTERN.match(line)
        if match is None:
            continue
        kind = match.group(1)
        if kind is None:
            # A closing fence with nothing open is prose, not a fence.
            if open_stack:
                open_index, open_tag, close_tag = open_stack.pop()
                macros[open_index] = open_tag
                macros[index] = close_tag
            continue
        tags = _fence_macros(kind.lower(), match.group(2))
        if tags is not None:
            open_stack.append((index, tags[0], tags[1]))
    for index, macro in macros.items():
        lines[index] = macro
    return "\n".join(lines)


class JiraPreprocessor(BasePreprocessor):
    """Handles text preprocessing for Jira content."""

    # Step 1: Valid JIRA languages (official list)
    # Source: https://jira.atlassian.com/browse/JRASERVER-21067 (JIRA 7.5.0+)
    # and JIRA v9.12.12 release notes
    # Official documentation: https://jira.atlassian.com/secure/WikiRendererHelpAction.jspa
    VALID_JIRA_LANGUAGES = {
        # Core languages from JIRA 7.5.0+
        "actionscript",
        "actionscript3",
        "ada",
        "applescript",
        "bash",
        "sh",  # alias for bash
        "c",
        "c#",
        "csharp",  # alias for c#
        "cs",  # alias for c#
        "c++",
        "cpp",  # alias for c++
        "css",
        "sass",  # CSS preprocessor
        "less",  # CSS preprocessor
        "coldfusion",
        "delphi",
        "diff",
        "patch",  # alias for diff
        "erlang",
        "erl",  # alias for erlang
        "go",
        "groovy",
        "haskell",
        "html",
        "xml",
        "java",
        "javafx",
        "javascript",
        "js",  # alias for javascript
        "json",
        "lua",
        "nyan",
        "objc",
        "objective-c",  # alias for objc
        "perl",
        "php",
        "powershell",
        "ps1",  # alias for powershell
        "python",
        "py",  # alias for python
        "r",
        "rainbow",
        "ruby",
        "rb",  # alias for ruby
        "scala",
        "sql",
        "swift",
        "visualbasic",
        "vb",  # alias for visualbasic
        "yaml",
        "yml",  # alias for yaml
        "none",  # plain text, no highlighting
    }

    # Step 2: Mapping for unsupported languages to closest valid JIRA alternative
    # Only map to actual JIRA languages; unmapped languages will return None → {code}
    LANGUAGE_MAPPING = {
        # Dockerfile → bash (similar shell syntax)
        "dockerfile": "bash",
        "docker": "bash",
        # TypeScript → javascript
        "typescript": "javascript",
        "ts": "javascript",
        "tsx": "javascript",
        # JSX/React → javascript
        "jsx": "javascript",
        # Kotlin → java (JVM-based language)
        "kotlin": "java",
        "kt": "java",
        # Build files → bash
        "makefile": "bash",
        "make": "bash",
        "cmake": "bash",
    }

    def __init__(
        self, base_url: str = "", disable_translation: bool = False, **kwargs: Any
    ) -> None:
        """
        Initialize the Jira text preprocessor.

        Args:
            base_url: Base URL for Jira API
            disable_translation: If True, disable markup translation between formats
            **kwargs: Additional arguments for the base class
        """
        super().__init__(base_url=base_url, **kwargs)
        self.disable_translation = disable_translation

    def clean_jira_text(self, text: str) -> str:
        """
        Clean Jira text content by:
        1. Processing user mentions and links
        2. Converting Jira markup to markdown (if translation enabled)
        3. Converting HTML/wiki markup to markdown (if translation enabled)
        """
        if not text:
            return ""

        # Process user mentions
        mention_pattern = r"\[~accountid:(.*?)\]"
        text = self._process_mentions(text, mention_pattern)

        # Process Jira smart links
        text = self._process_smart_links(text)

        # Convert markup only if translation is enabled
        if not self.disable_translation:
            # First convert any Jira markup to Markdown
            text = self.jira_to_markdown(text)

            # Then convert any remaining HTML to markdown
            text = self._convert_html_to_markdown(text)

        return text.strip()

    def _process_mentions(self, text: str, pattern: str) -> str:
        """
        Process user mentions in text.

        Args:
            text: The text containing mentions
            pattern: Regular expression pattern to match mentions

        Returns:
            Text with mentions replaced with display names
        """
        mentions = re.findall(pattern, text)
        for account_id in mentions:
            try:
                # Note: This is a placeholder - actual user fetching should be injected
                display_name = f"User:{account_id}"
                text = text.replace(f"[~accountid:{account_id}]", display_name)
            except Exception as e:
                logger.error(f"Error processing mention for {account_id}: {str(e)}")
        return text

    def _process_smart_links(self, text: str) -> str:
        """Process Jira/Confluence smart links."""
        # Pattern matches: [text|url|smart-link]
        link_pattern = r"\[(.*?)\|(.*?)\|smart-link\]"
        matches = re.finditer(link_pattern, text)

        for match in matches:
            full_match = match.group(0)
            link_text = match.group(1)
            link_url = match.group(2)

            # Extract issue key if it's a Jira issue link
            issue_key_match = re.search(
                rf"browse/({_ISSUE_KEY_PATTERN})(?=$|[/?#])", link_url
            )
            # Check if it's a Confluence wiki link
            confluence_match = re.search(
                r"wiki/spaces/.+?/pages/\d+/(.+?)(?:\?|$)", link_url
            )

            if issue_key_match:
                issue_key = issue_key_match.group(1)
                clean_url = f"{self.base_url}/browse/{issue_key}"
                text = text.replace(full_match, f"[{issue_key}]({clean_url})")
            elif confluence_match:
                url_title = confluence_match.group(1)
                readable_title = url_title.replace("+", " ")
                readable_title = re.sub(
                    rf"^{_ISSUE_KEY_PATTERN}\s+", "", readable_title
                )
                text = text.replace(full_match, f"[{readable_title}]({link_url})")
            else:
                clean_url = link_url.split("?")[0]
                text = text.replace(full_match, f"[{link_text}]({clean_url})")

        return text

    def jira_to_markdown(self, input_text: str) -> str:
        """
        Convert Jira markup to Markdown format.

        Args:
            input_text: Text in Jira markup format

        Returns:
            Text in Markdown format (or original text if translation disabled)
        """
        if not input_text:
            return ""

        if self.disable_translation:
            return input_text

        output = input_text

        # Protect code/noformat/inline-code blocks from downstream
        # transformations by replacing them with placeholders.
        #
        # A {code} block inside {quote} is extracted before the
        # {quote} handler runs, so the handler prefixes only the
        # placeholder's line and the restored fence would end the
        # blockquote at its first line.  Jira accepts a codeBlock
        # inside a blockquote, so the prefix is carried into the
        # block at restore time instead -- see
        # _carry_quote_prefix_into_code_blocks.
        code_blocks: list[str] = []
        inline_codes: list[str] = []

        def _jira_code_to_md(match: re.Match[str]) -> str:
            lang = match.group(1) or ""
            content = match.group(2)
            return f"```{lang}\n{content}\n```"

        output = _extract_blocks(
            output,
            r"\{code(?::([a-z]+))?\}([\s\S]*?)\{code\}",
            _jira_code_to_md,
            code_blocks,
            "CODEBLOCK",
            flags=re.MULTILINE,
        )
        output = _extract_blocks(
            output,
            r"\{noformat\}([\s\S]*?)\{noformat\}",
            lambda m: f"```\n{m.group(1)}\n```",
            code_blocks,
            "CODEBLOCK",
        )
        output = _extract_blocks(
            output,
            r"\{\{([^}]+)\}\}",
            lambda m: f"`{m.group(1)}`",
            inline_codes,
            "INLINECODE",
        )

        # Protect backslash-escaped characters the same way.  Jira emits them
        # when the character is literal, so they must not open or close a
        # markup span; they are restored as markdown escapes once every
        # conversion below has run.
        output = _protect_escaped_chars(output)

        # Block quotes
        output = re.sub(r"^bq\.(.*?)$", r"> \1\n", output, flags=re.MULTILINE)

        # Text formatting (bold, italic).  Bold requires a non-space on the
        # inside of each marker; italic pairing stays unbounded, matching
        # Jira's own renderer.  Both stop at the boundary of a scheme-ful URL,
        # so a link with underscores in its path stays clickable.  See
        # _BOLD_ITALIC_PATTERN and _URL_TOKEN_PATTERN.
        #
        # A delimiter preceded by a backslash is a wiki escape, not markup, and
        # must neither open nor close a span (issue #1610 upstream, e.g. the
        # intraword ``\_`` markdown_to_jira writes).  No lookbehind is needed
        # here: _protect_escaped_chars above has already replaced every ``\c``
        # escape with a placeholder, so the pattern never sees the delimiter.
        output = _sub_outside_urls(_BOLD_ITALIC_PATTERN, _wrap_bold_or_italic, output)

        # Multi-level numbered list
        output = re.sub(
            r"^((?:#|-|\+|\*)+) (.*)$",
            lambda match: self._convert_jira_list_to_markdown(match),
            output,
            flags=re.MULTILINE,
        )

        # Headers
        output = re.sub(
            r"^h([0-6])\.(.*)$",
            lambda match: "#" * int(match.group(1)) + match.group(2),
            output,
            flags=re.MULTILINE,
        )

        # Citation, inserted text, superscript and subscript.  Strikethrough
        # (-text-) is deliberately left alone, see _INLINE_MARKUP_RULES.
        output = _convert_inline_markup(output)

        # Quote blocks, one block at a time.  See _QUOTE_BLOCK_PATTERN.
        output = _QUOTE_BLOCK_PATTERN.sub(_convert_quote_block, output)

        # Panels and the {info}/{note}/{warning}/{tip}/{expand} macros, as :::
        # fences -- the spelling markdown_to_adf reads back as a real ADF panel.
        output = _convert_block_macros(output)

        # Images with alt text
        output = re.sub(
            r"!([^|\n\s]+)\|([^\n!]*)alt=([^\n!\,]+?)"
            r"(,([^\n!]*))?!",
            r"![\3](\1)",
            output,
        )

        # Images with other parameters (ignore them)
        output = re.sub(r"!([^|\n\s]+)\|([^\n!]*)!", r"![](\1)", output)

        # Images without parameters
        output = re.sub(r"!([^\n\s!]+)!", r"![](\1)", output)

        # Links
        output = re.sub(r"\[([^|]+)\|(.+?)\]", r"[\1](\2)", output)
        output = re.sub(r"\[(.+?)\]([^\(])", r"\1\2", output)

        # Colored text
        output = re.sub(
            r"\{color:([^}]+)\}([\s\S]*?)\{color\}",
            r"<span style=\"color:\1\">\2</span>",
            output,
            flags=re.MULTILINE,
        )

        # Convert Jira table headers (||) to markdown table format
        lines = output.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]

            if "||" in line:
                # Replace Jira table headers
                lines[i] = lines[i].replace("||", "|")

                # Add a separator line for markdown tables
                header_cells = lines[i].count("|") - 1
                if header_cells > 0:
                    separator_line = "|" + "---|" * header_cells
                    lines.insert(i + 1, separator_line)
                    i += 1

            i += 1

        # Rejoin the lines
        output = "\n".join(lines)

        # Restore escapes as markdown escapes, then code/inline code
        output = _restore_escaped_chars(output)
        _carry_quote_prefix_into_code_blocks(output, code_blocks)
        output = _restore_blocks(output, code_blocks, "CODEBLOCK")
        output = _restore_blocks(output, inline_codes, "INLINECODE")

        return output

    def _normalize_code_language(self, lang: str | None) -> str | None:
        """
        Normalize and map markdown code language to JIRA-supported language.

        Step 3: Default handling - unmapped languages return None for plain {code}

        Args:
            lang: Language identifier from markdown code block

        Returns:
            Valid JIRA language string, or None for plain {code} block
        """
        if not lang:
            return None

        lang_lower = lang.lower()

        # Step 1: Check if already valid JIRA language
        if lang_lower in self.VALID_JIRA_LANGUAGES:
            return lang_lower

        # Step 2: Check language mapping
        if lang_lower in self.LANGUAGE_MAPPING:
            return self.LANGUAGE_MAPPING[lang_lower]

        # Step 3: Default - unmapped language returns None for plain {code}
        return None

    def markdown_to_jira(self, input_text: str) -> str:
        """
        Convert Markdown syntax to Jira markup syntax.

        Args:
            input_text: Text in Markdown format

        Returns:
            Text in Jira markup format (or original text if translation disabled)
        """
        if not input_text:
            return ""

        if self.disable_translation:
            return input_text

        code_blocks: list[str] = []
        inline_codes: list[str] = []

        def _md_code_to_jira(match: re.Match[str]) -> str:
            syntax = match.group(1) or ""
            content = match.group(2)
            jira_lang = self._normalize_code_language(syntax)
            code = "{code"
            if jira_lang:
                code += ":" + jira_lang
            code += "}" + content + "{code}"
            return code

        def _md_inline_to_jira(
            match: re.Match[str],
        ) -> str:
            return "{{" + match.group(1) + "}}"

        # Extract code blocks and inline code before
        # any other transformations.
        output = _extract_blocks(
            input_text,
            r"```(\w*)\n([\s\S]+?)```",
            _md_code_to_jira,
            code_blocks,
            "CODEBLOCK",
        )
        output = _extract_blocks(
            output,
            r"`([^`]+)`",
            _md_inline_to_jira,
            inline_codes,
            "INLINECODE",
        )

        # Panel and expand fences, back to their wiki macros.  After the code
        # extraction, so a fence inside a code block stays literal, and before
        # every other pass, so a fence's content is converted like any other
        # markdown.
        output = _convert_fenced_blocks(output)

        # Headers with = or - underlines. A setext heading needs actual text on
        # the line above the underline, so the lookahead keeps a blank line from
        # matching: `\n\n----\n` is a horizontal rule, which is already valid
        # Jira markup, not an empty `h2.` (issue #1587).
        output = re.sub(
            r"^(?=[^\n]*\S)(.*?)\n([=-])+$",
            lambda match: f"h{1 if match.group(2)[0] == '=' else 2}. {match.group(1)}",
            output,
            flags=re.MULTILINE,
        )

        # Headers with # prefix - require space after #
        # to distinguish from Jira lists (issue #786)
        output = re.sub(
            r"^([#]+) (.*)$",
            lambda match: f"h{len(match.group(1))}. " + match.group(2),
            output,
            flags=re.MULTILINE,
        )

        markdown_url_targets: list[str] = []

        def store_markdown_url_target(target: str) -> str:
            placeholder = f"\x00MARKDOWNURL{len(markdown_url_targets)}\x00"
            markdown_url_targets.append(target)
            return placeholder

        def protect_markdown_link_target(match: re.Match[str]) -> str:
            return (
                match.group(1)
                + store_markdown_url_target(match.group(2))
                + match.group(3)
            )

        def protect_markdown_autolink_target(match: re.Match[str]) -> str:
            return "<" + store_markdown_url_target(match.group(1)) + ">"

        output = re.sub(
            r"(!?\[[^\]\n]*\]\()([^)]+)(\))",
            protect_markdown_link_target,
            output,
        )
        output = re.sub(
            r"<((?:[A-Za-z][A-Za-z0-9+.-]*:[^>\s]+|[^<>\s@]+@[^<>\s@]+))>",
            protect_markdown_autolink_target,
            output,
        )

        # Bold and italic - skip lines starting with
        # asterisks+space (Jira list syntax, issue #786)
        def escape_intraword_underscore_runs(match: re.Match[str]) -> str:
            return r"\_" * len(match.group(0))

        def convert_bold_italic_line(line: str) -> str:
            # CommonMark treats underscores between two word characters as
            # literal text, not emphasis. The Jira wiki renderer does not:
            # it italicizes any ``_word_`` span, so identifiers such as
            # snake_case, customfield_10101 or foo_bar_baz would render with
            # spurious italics (and adjacent identifiers can pair into a
            # cross-token italic span). Escape intraword underscore runs as
            # ``\_`` so Jira renders them literally; genuine word-boundary
            # ``_emphasis_``/``__strong__`` is left intact for conversion below.
            line = re.sub(
                r"(?<=[^\W_])_+(?=[^\W_])",
                escape_intraword_underscore_runs,
                line,
            )
            if re.match(r"^[*_]+\s", line):
                return line
            return re.sub(
                r"([*_]+)(.*?)\1",
                lambda m: (
                    ("_" if len(m.group(1)) == 1 else "*")
                    + m.group(2)
                    + ("_" if len(m.group(1)) == 1 else "*")
                ),
                line,
            )

        lines = output.split("\n")
        output = "\n".join(convert_bold_italic_line(line) for line in lines)
        output = _restore_blocks(output, markdown_url_targets, "MARKDOWNURL")

        # Multi-level bulleted list
        def bulleted_list_fn(match: re.Match[str]) -> str:
            ident = len(match.group(1)) if match.group(1) else 0
            level = ident // 2 + 1
            return str("*" * level + " " + match.group(2))

        output = re.sub(
            r"^(\s+)?[-+*] (.*)$",
            bulleted_list_fn,
            output,
            flags=re.MULTILINE,
        )

        # Multi-level numbered list
        def numbered_list_fn(
            match: re.Match[str],
        ) -> str:
            ident = len(match.group(1)) if match.group(1) else 0
            level = ident // 2 + 1
            return str("#" * level + " " + match.group(2))

        output = re.sub(
            r"^(\s+)?\d+\. (.*)$",
            numbered_list_fn,
            output,
            flags=re.MULTILINE,
        )

        # HTML formatting tags to Jira markup
        tag_map = {
            "cite": "??",
            "del": "-",
            "ins": "+",
            "sup": "^",
            "sub": "~",
        }

        for tag, replacement in tag_map.items():
            output = re.sub(
                rf"<{tag}>(.*?)<\/{tag}>",
                rf"{replacement}\1{replacement}",
                output,
            )

        # Colored text
        output = re.sub(
            r"<span style=\"color:(#[^\"]+)\">"
            r"([\s\S]*?)</span>",
            r"{color:\1}\2{color}",
            output,
            flags=re.MULTILINE,
        )

        # Strikethrough
        output = re.sub(r"~~(.*?)~~", r"-\1-", output)

        # Images without alt text
        output = re.sub(r"!\[\]\(([^)\n\s]+)\)", r"!\1!", output)

        # Images with alt text
        output = re.sub(
            r"!\[([^\]\n]+)\]\(([^)\n\s]+)\)",
            r"!\2|alt=\1!",
            output,
        )

        # Links
        output = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"[\1|\2]", output)
        output = re.sub(r"<([^>]+)>", r"[\1]", output)

        # Convert markdown tables to Jira table format
        # Issue #1343: parse full table blocks (header + separator + data rows),
        # strip whitespace from each cell, convert header to ||cell|| and data
        # rows to |cell|.
        lines = output.split("\n")
        i = 0
        while i < len(lines):
            # Look for a header row followed by a markdown separator line
            if (
                i < len(lines) - 1
                and re.match(r"^\|[-\s|:]+\|$", lines[i + 1])
                and re.match(r"^\|.*\|$", lines[i])
            ):
                # Header row
                header_cells = [cell.strip() for cell in lines[i].split("|")[1:-1]]
                lines[i] = "||" + "||".join(header_cells) + "||"
                lines.pop(i + 1)  # drop separator

                # Consume data rows while they look like table rows
                while i < len(lines) and re.match(
                    r"^\|.*\|$", lines[i + 1] if i + 1 < len(lines) else ""
                ):
                    data_cells = [
                        cell.strip() for cell in lines[i + 1].split("|")[1:-1]
                    ]
                    lines[i + 1] = "|" + "|".join(data_cells) + "|"
                    i += 1
            i += 1

        # Rejoin the lines
        output = "\n".join(lines)

        # Restore code blocks and inline code
        output = _restore_blocks(output, code_blocks, "CODEBLOCK")
        output = _restore_blocks(output, inline_codes, "INLINECODE")

        return output

    def _convert_jira_list_to_markdown(self, match: re.Match) -> str:
        """
        Helper method to convert Jira lists to Markdown format.

        Args:
            match: Regex match object containing the Jira list markup

        Returns:
            Markdown-formatted list item
        """
        jira_bullets = match.group(1)
        content = match.group(2)

        # Calculate indentation level based on number of symbols
        indent_level = len(jira_bullets) - 1
        indent = " " * (indent_level * 2)

        # Determine the marker based on the last character
        last_char = jira_bullets[-1]
        prefix = "1." if last_char == "#" else "-"

        return f"{indent}{prefix} {content}"
