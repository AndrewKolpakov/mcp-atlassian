---
name: jira-content
description: Rules for moving formatted content through the atlassian MCP server (mcp__plugin_mcp-atlassian_atlassian__jira_*/confluence_* when it comes from this plugin, mcp__atlassian__jira_*/confluence_* when the server is registered directly). Read BEFORE writing or updating a Jira issue description, comment, or Confluence body, and before parsing what a read returned — every body goes to a file and through plugin/scripts/check_draft.py first, never composed inside the tool call. Also use when formatting came out wrong in the browser, when a read looks empty or mangled, when a write 400s with INVALID_INPUT or the converter refuses it with AdfMarkConflictError, when creating or removing issue links (the inward/outward parameters do not read the way they sound), or to work out whether this session's server reads Jira Cloud over v2 or v3.
---

**Author in Markdown, in both directions.** Which rules apply beyond that depends on
one thing: whether this session's server reads Jira Cloud over **v3** (returns the
stored ADF) or **v2** (returns Atlassian's wiki-markup *rendering* of it). Settle that
first — it decides whether a read is evidence.

## Which read path is this session on

The server's behaviour is whatever code its process loaded at start-up, and this plugin
can serve two different trees:

- **The copy the plugin shipped** (the default). `plugin/scripts/mcp_atlassian_launcher.py`
  runs `uv run --frozen --project <plugin root>`, so the converter is pinned to the
  plugin version — stable, and it only changes when the plugin updates.
- **A working tree you point it at**, via the *Run from this checkout instead* option or
  `MCP_ATLASSIAN_SRC`. Then behaviour is whatever is checked out *and* whatever was
  loaded when the process started, which is the case that bites.

Pinning down what the running process actually has:

- `/mcp` → "Reconnected to atlassian" **does restart the process**: a new
  `mcp-atlassian` PID appears, parented by this session's `claude`, with a start time
  after the last source edit.
- Confirm by hand with `git -C <checkout> status -sb` against
  `ps -eo pid,ppid,lstart,command | grep mcp-atlassian`: a process older than your edits
  still has the old modules.
- **One-read discriminator**: read an issue whose description has a coloured panel. A
  v3 read returns `:::info` / `:::warning` fences; a v2 read returns anonymous
  blank-separated paragraphs with the colour gone.

- **Cheaper discriminator when no panel is at hand**: look at how a link came back. v3
  emits Markdown — `[ABC-123](https://…/browse/ABC-123)`; v2 emits wiki —
  `[ABC-123|https://…]`.

The v3 read path is in every version of this plugin — `adf_to_markdown` in
`src/mcp_atlassian/models/jira/adf.py`. So on Cloud a read is evidence by default. When
you are serving a working tree, still confirm the *running process* is newer than your
last source edit, and re-check if the checkout moves.

Server/DC has no v3 API and is always read over v2/wiki, so the v2 half of this page
never stops applying there.

## Before you write (both paths)

### The converter refuses this construct

`markdown_to_adf` raises `AdfMarkConflictError` rather than emitting a document Jira
will not store, and the message names **every** offending construct at once — line
number, the line as written, and the line rewritten so it passes:

```
Jira would refuse this write and store nothing: 2 constructs in this body put a
`code` span inside another mark, which ADF permits only for links.

  line 3: code span `plans/foo.md` also carries: strong
      written: - **T1 of `plans/foo.md`** (tail)
      fix to:  - **T1 of** `plans/foo.md` (tail)
  line 5: code span `run_id` also carries: em
      ...
```

Three things follow, and the third is the one worth remembering:

- **No opaque 400 for this construct.** `INVALID_INPUT` naming nothing still happens
  for other rejections — the nestings further down — but not for this one.
- **`jira_batch_create_issues` validates every body before creating any issue**, and
  refuses the whole batch with every offence across every issue named. A bad body in
  the second issue does not leave the first and third created and return a short list
  with no error.
- **The list is the point.** Fixing the first offence and resending costs a refusal per
  defect. Everything below — the file-first procedure, the gate, the hook — exists to put
  the whole list in front of you once, before you send.

### A status lozenge is authorable and round-trips

On the **Cloud/ADF path** a status lozenge survives a read → edit → write circle: the
writer reads the `{status:…}` spelling and the reader emits it. Write path:

| written in Markdown | ADF |
| --- | --- |
| `{status:color=green\|title=Done}` | `status`, green |
| `{status:title=Done}` | `status`, **neutral** — colour may be omitted |
| `{status:color=GREEN\|title=Done}` | `status`, green — colour is case-insensitive |
| `{status:color=lime\|title=Done}` | `status`, **neutral** — an unknown colour is silently coerced, not refused |
| `{status:colour=green\|title=Done}` | **plain text** — the British spelling is not read |
| `{status:color=green\|title=}` | **plain text** — an empty title is not read |

Colours Jira takes: `neutral`, `purple`, `blue`, `red`, `yellow`, `green`.

Reading it back, three things are deliberate and must not be "fixed":

- **A lozenge in a table cell comes back as `{status:title=…}`, colour dropped.** The
  colour form separates its fields with `|`, which is the cell separator; the cell
  escapes it to `\|`, which the writer does not read. Dropping the colour keeps the
  node — the old `\[TEXT\]` spelling kept nothing.
- **A title holding `}`, a newline, or nothing reads back as `\[TEXT\]`** and does not
  survive a further write. The token cannot carry those, and a lost lozenge is
  preferable to a mislabelled one.
- **Prose that merely contains the token comes back escaped: `\{status:title=Done}`.**
  That backslash is load-bearing. Strip it and the next write turns the author's
  sentence into a real lozenge.

On the **Server/DC wiki path** this library does not convert the construct in either
direction — `markdown_to_jira` and `jira_to_markdown` both pass `{status:…}` through
verbatim. Whether Jira's own wiki renderer draws a lozenge from it was **not** measured;
do not promise a Server/DC user that it renders.

### Compose in a file, gate the file, send that file — this part is not optional

**The one construct that rejects a whole write is not caught by knowing about it.** It
is caught by running the gate on the bytes that get sent. Every recorded occurrence has
the same shape: a body typed straight into the tool call was refused, and the same text
written to a file failed the gate immediately — including in sessions where the author
had just read this page, and including a batch of parallel creates that shared one
offending lead-in and were refused together.

So the failure mode is not ignorance of the rule and not a weak check. It is that
**nothing binds the checked bytes to the sent bytes.** Bind them by hand:

```bash
D=$(mktemp -d)/body.md
cat > "$D" <<'EOF'
**Wave 0 delivered** — commits `f09fdb1` and `b0d69e6` on `main`.
EOF
uv run --frozen --project "$CLAUDE_PLUGIN_ROOT" \
  python "$CLAUDE_PLUGIN_ROOT"/plugin/scripts/check_draft.py "$D"   # exit 0 = safe to send
```

Then send **that file's contents, unedited**. Three rules make it work:

1. **Never compose a description, comment or Confluence body inside the tool call.**
   A body that exists only as a tool argument was never gated, and it is the only kind
   that has ever been refused.
2. **Any edit after a check voids the check.** Re-run the script on the same path.
   Checking draft A and sending a re-worded B is what 16:25 was.
3. **Gate every draft before a fan-out of parallel creates.** One bad shared template
   fails every call in the batch at once.

`plugin/scripts/check_draft.py` is the whole gate: it runs the real converter's own
`find_mark_conflicts` from the checkout the MCP server runs, so it inspects the document
Jira will actually see, and for each offence it prints the line number, the line as
written and the line rewritten so it passes. Exit `1` is a refusal waiting to happen;
exit `2` is an environment problem and **not** a pass.
It also warns about the two defects that **succeed** and post mangled, which no ADF
check can see because the document is perfectly valid: a bold marker left unclosed on its
line (this converter stores literal asterisks rather than carrying the run across the
break — hard-wrapping prose is what puts a long bold lead-in across one), and a pipe
inside a code span on a table row (the cell splits). Those exit `0`; read the response
body back after the write, which is the only thing that surfaces them.

**Do not hand-write the grep instead.** The obvious one-liner — extract `**…**` spans
with a negated character class, then look for a backtick — cannot cross an asterisk, so
it reports clean on
`` - **The `aws-actions/*` namespace is not mirrored** `` — measured, where it
did exactly that and only the ADF walker caught it. The script's own scan allows an
asterisk inside a code span for that reason.

**Never wiki markup.** Wiki tokens land as literal visible text — `h2.`, `{{code}}`,
`{panel}`, `{quote}` all show up in the browser as the raw string. Exactly two are
translated: wiki tables (`||h1||h2||`) and `{expand:Title}…{expand}`.

The trap is **mixing dialects**: a list written with wiki's `#` marker becomes a run of
Markdown H1 headings (because `#` *is* Markdown), while `h2.` in the same description
stays literal.

Vocabulary, measured end to end (written to a real Cloud issue, verified in the
browser, read back byte-identical over v3):

| write this | you get | round-trips on v3 |
|---|---|---|
| `##` | `heading` | yes |
| backticks | `code` mark | yes |
| `**b**` / `*i*` / `***bi***` | `strong` / `em` / both | yes |
| `~~s~~` | `strike` | yes |
| `-` / `1.` | `bulletList` / `orderedList` | yes |
| `5.` as the first marker | `orderedList` starting at 5 (`order` attr) | yes |
| two-space-indented `-` | nested list | yes |
| indented block under an item | multi-block `listItem` | yes |
| `- [ ]` / `- [x]` | `taskList` with `state` | yes, one level only |
| `> ` lines | `blockquote`, may hold paragraphs, a list, a fence | yes |
| `\|a\|b\|` + `\|---\|` | `table` | yes |
| fenced block | `codeBlock` | yes |
| ```` ```` ```` fence | `codeBlock` whose body contains a ``` line | yes |
| `:::info\|note\|warning\|success\|error\|tip` | `panel` with that `panelType` | yes |
| `:::expand Title` | `expand` | yes on v3, never on v2 |
| bare in-project issue key | `link` to `/browse/KEY` | yes |

**Escapes work, and they are how you write literal markup.** A backslash before
punctuation is consumed on the way in, so `\- x`, `1\. x`, `\| x`, `\::: x`, `\## x`,
`2 \* 3`, `C:\\temp` and a `\]` inside a link label all store the literal text — all
verified rendering as plain text in the browser. Non-punctuation is untouched, so
`C:\temp` keeps its backslash the way CommonMark says.

Avoid:

- **A code span inside bold — this one REJECTS THE WHOLE WRITE.** `` **`x`** `` makes the
  converter emit one text node carrying `marks: [code, strong]`, and the ADF schema lets
  `code` share a node with `link` and nothing else, so Jira refuses the document — no
  field is written, not even the valid parts. Measured on Cloud both as a whole
  description and as an isolated paragraph. The construct is fatal, not untidy: the
  write does not store the marks and fail to round-trip, it stores nothing.

  **The shape it arrives in is a bold lead-in around a backticked path or identifier**,
  not a deliberate `` **`x`** ``: writing ``**T1 of `plans/foo.md`** (…)`` feels like
  emphasis-plus-filename, and it is this whole class that rejects. The fix is to close
  the emphasis before the code span and reopen it after:
  ``**T1 of** `plans/foo.md` (…)``. **The converter refuses it for you** and prints
  that rewrite for every offending line, so it is a build-time error rather than a 400
  you have to bisect. The gate that catches these before the call is in **the section
  above** — run
  `plugin/scripts/check_draft.py` rather than re-deriving a check here.

- **Wiki links** `[KEY|url]` — prefer a bare issue key; a wiki link reads back as
  `[KEY](url)|url` with the URL duplicated.
- **Key-shaped tokens in prose.** Every `[A-Z][A-Z0-9_]+-\d+` is auto-linked to
  `{base_url}/browse/{KEY}` whether or not that project exists, so "PR-1 … PR-17"
  becomes 17 dead links. `JIRA_PROJECTS_FILTER` would scope it, but the launcher exports
  no such variable for any workspace. **A code span suppresses it** — measured in the
  same write: backticked `PRI-2`/`UA-9`/`CLS-3` stayed code, while the one bare `PRI-2`
  came back as `[**PRI-2**](…/browse/PRI-2)`, a dead link that also split the bold run
  around it. So backtick the token — but never inside a bold run, per the bullet above.
  Otherwise spell it out, or accept the links.
- **A URL containing `)`** — the href parser stops at the first one.

## When a write is refused

Two different messages, one cause class — a document the ADF schema rejects:
`The field value is not valid Atlassian Document Format (ADF) content.` (seen from
`jira_create_issue`) and `Failed to update issue KEY: INVALID_INPUT` (seen from
`jira_update_issue`). Neither names the offending construct or field.

**A refused write changes nothing**, which makes the target field a safe probe: send the
suspect construct alone to an issue you own, and on rejection the stored description is
still whatever it was. Verified — the probe above left a 5 KB description untouched
(`operations_performed: []`).

**Bisect locally instead of by trial and error.** For the routine pre-send case use
`plugin/scripts/check_draft.py`, which is this walker plus the span scan; what follows is the
same thing unrolled, for when you are narrowing an unfamiliar construct. The converter is
importable from whichever tree the server is running -- the plugin's own root, or the
checkout `MCP_ATLASSIAN_SRC` points at -- so the document Jira will see can be inspected
before sending:

```bash
cd "${MCP_ATLASSIAN_SRC:-$CLAUDE_PLUGIN_ROOT}" && uv run --frozen python - <<'PY'
import sys; sys.path.insert(0, "src")
# find_mark_conflicts REPORTS; markdown_to_adf RAISES on the same finding. Use the
# reporting one when you want the list rather than the first stop.
from mcp_atlassian.models.jira.adf import _markdown_to_adf_unchecked, find_mark_conflicts
text = open("draft.md").read()
for c in find_mark_conflicts(text):
    print(c.describe())
def empty(node, path="doc"):                                 # also invalid ADF
    if isinstance(node, dict):
        if node.get("type") == "text" and not node.get("text"):
            yield path
        for n, ch in enumerate(node.get("content") or []):
            yield from empty(ch, f"{path}/{node.get('type')}[{n}]")
print(list(empty(_markdown_to_adf_unchecked(text))) or "no empty text nodes")
PY
```

Empty-content checks on `listItem`/`table`/`panel`/`blockquote` are worth adding to the
same walker. What neither can see is a *nesting* Jira rejects (the section below) —
those produce structurally fine nodes and still 400, so isolate those by probing.

## The payload is guarded automatically

The procedure above is a manual binding, and a manual binding can be skipped — which is
how every recorded refusal happened. A `PreToolUse` hook validates the **tool input**,
so the bytes actually being sent are the bytes checked:

- `plugin/hooks.json` → `hooks.PreToolUse`. Its matcher lists full tool names, both the
  plugin-scoped and the directly-registered spelling, because a matcher takes a literal
  `|` list and not a pattern. The real decision is made inside the guard, which matches
  `jira_create_issue`, `jira_update_issue`, `jira_add_comment`, `jira_edit_comment`,
  `jira_batch_create_issues` **by suffix** — so renaming the plugin or the server cannot
  silently switch the guard off.
- `plugin/scripts/jira_adf_guard.py` — the check, and the hook's entry point; there
  is no wrapper, and it never exits non-zero, so its own failure cannot block a write.
  It reads `description`, `comment` and `body`, and decodes the JSON-string arguments
  `fields`, `additional_fields` and `issues` to reach the fields inside them. A fatal
  construct is answered with `permissionDecision: deny` naming the offending span.
- Proven live, both halves: a fatal body was refused before leaving this machine, and a
  clean body reached Jira and failed only on the nonexistent issue it named. ~0.7 s per
  write.

**Three scope decisions in it, each measured rather than assumed:**

- **Jira only.** The refusal is an ADF property; Confluence bodies go markdown →
  storage (XHTML) with no ADF anywhere under `confluence/` in the server's source, and
  `<strong><code>` is legal there. Guarding them would deny writes that succeed.
- **`summary` is not checked.** It is a plain string field, so the construct is harmless
  there and a deny would be wrong.
- **The emphasis map is narrower than "any emphasis plus a code span."** `**x**`,
  `***x***` and `~~x~~` around a code span are fatal *and* survive an asterisk inside the
  span; `*x*` is fatal only *without* one, because a single `*` delimiter is terminated by
  it; `__x__` and `_x_` are not fatal at all — this converter does not parse underscore
  emphasis; a code span in a link label is the one allowed pairing; and a run spanning a
  line break is not emphasis here.
  **That map decides nothing.** The guard calls `find_mark_conflicts` — the server's
  own check — so its verdict cannot disagree with the write's, and its deny message is
  the same list of lines and rewrites. The map above survives for one case: the checkout
  being unreachable. So there is no reason to distrust the hook on the grounds that was
  once the obvious one — that it was a *second*
  definition of what Jira refuses, free to drift from the first.

**A body that DOCUMENTS this defect is not the defect, and getting that wrong was the
guard's own first bug.** Quoting the construct in a fence or in a ``…`` span is a
`codeBlock` or a code-only mark — Jira accepts it — while a raw-text scan flags it. So the
converter's verdict is the **authority** wherever it ran, and the span scan only names a
defect the converter already confirmed. Proven live in both directions: the construct
applied is refused, the same construct quoted reaches Jira. Only the degraded path (no
converter) decides on the scan alone, and there fenced blocks and ``…`` spans are blanked
first; a real `**a ``b`` c**` is the one shape it can then miss, which costs a Jira
refusal rather than a false deny. `plugin/scripts/check_draft.py` carries the identical rule for
the same reason.

**What it still buys, now that the converter refuses the same body.** Only *when*: the
refusal arrives before any network call, and before `jira_batch_create_issues` has looked
at a single issue. The write fails either way and with the same list — the hook makes it
fail earlier and for free.

**It does not replace the file-first procedure.** The hook catches the fatal construct;
the two defects that *succeed* and post mangled — a bold marker left unclosed on its line,
a pipe inside a code span on a table row — are valid ADF and invisible to it. Those need
`plugin/scripts/check_draft.py` and a read-back. The hook is a floor, not the method.

**If it ever misfires**, the escape hatch is `/hooks` (review or disable), or disabling
the plugin; the whole check is two files under `plugin/scripts/`. The guard is written to fail open: a missing
converter checkout falls back to the span scan, an unparseable body is left alone, and any
internal failure exits without a decision — it denies only on a positive identification.

## Nesting Jira rejects, measured one construct at a time

An opaque `400 {"errorMessages":["INVALID_INPUT"],"errors":{}}` naming no field:

- `panel` containing `blockquote` → **REJECTED**
- `panel` containing `panel` → **REJECTED**
- `panel` containing `paragraph` / `heading` / `bulletList` / `codeBlock` → accepted
- `blockquote` containing `codeBlock` → accepted
- `blockquote` containing `bulletList` → **accepted** (measured; renders as a list
  inside the quote bar)

A document mixing several constructs is rejected as a whole, so isolate before
concluding anything.

## After you write

**On the v3 read path a read is evidence.** The reader escapes anything that would
re-parse as markup, so a paragraph holding the literal text `## x` comes back as
`\## x` and a real heading comes back as `## x`, so the two are distinguishable.
Measured: a 918-byte document covering the whole table above read back byte-identical
to what was written.

**On the v2 read path a read proves nothing.** A real `heading` and a paragraph
containing `"h2. X"` both read back as `## X`. And `expand=renderedFields` is worse
than useless there — it *manufactures* markup that is not in the document, returning
`<h2>` and `<tt>` for literal `h2.` text, so two independent reads can both say
"formatted correctly" while the UI shows raw strings.

**A browser screenshot is still the only proof of rendering**, on either path — it is
what caught that panel colours were surviving as `bgColor` in the first place.

## What a v3 read still loses

Not defects; Markdown has no spelling for these. `underline`, coloured text and
sub/superscript become plain text. A `hardBreak` becomes a newline that a rewrite
stores as two paragraphs. Media becomes a `(media: name)` marker — the write path
re-attaches the real nodes rather than rebuilding them. A nested `taskList` flattens to
one level. A `nestedExpand` returns as an `expand`. A table whose first row is not a
header gains one, and a cell holding several blocks is flattened onto the row's line.

A `status` lozenge is **not** in this list — it round-trips. See *A status lozenge is
authorable and round-trips* under **Before you write**.

## Reading: the response is the server's own model, not Jira REST

`to_simplified_dict()` emits `id`, `key`, `summary`, `url`, `browse_url`, `status`,
`issue_type`, `priority` … at the **top level** — there is no `{"fields": {...}}`
wrapper, and `issuetype` is renamed **`issue_type`**. An empty field is omitted
entirely, so a description-less issue simply has no `description` key. `jira_search`
wraps rows as `{"issues": [...]}`. Parsing `issue["fields"]["summary"]` silently yields
nothing for every row, which reads exactly like "the query matched but returned empty".
The `fields=` argument is a filter over these flattened names, not a REST field spec.

`jira_update_issue`'s `fields` argument is a **JSON string**, not an object — passing a
dict fails Pydantic validation before any Jira call is made.

## Issue links: the parameter names do not say which way the link points

`jira_create_issue_link(link_type, inward_issue_key, outward_issue_key)`. For `link_type: 'Blocks'`
(pass the type name, not one of its descriptions), measured against Jira Cloud:

- **`inward_issue_key` is the blocking issue; `outward_issue_key` is the blocked one.**
  `inward=A, outward=B` produces "B is blocked by A".

**The reversed call also succeeds, and nothing in the response says so.** Both orders return
`success: true` with `Link created between A and B` — a message that names the two issues and not
the direction. So a whole batch can be created backwards without a single error, and the mistake
surfaces only when someone reads the board.

**How to check, in one read.** Fetch the *blocked* issue with `fields=issuelinks`. For a read issue
X, each element carries exactly one neighbour and the side it sits on is the meaning:

| Element holds | Reads as | UI heading |
|---|---|---|
| `inward_issue: Y` | X is blocked by Y | "is blocked by" |
| `outward_issue: Z` | X blocks Z | "blocks" |

So the blocking issue must appear under `inward_issue` on the blocked issue. If it appears under
`outward_issue`, the link was created the wrong way round.

**Cheaper still when the project already has correct links**: read one and copy its shape. An
existing link is a fact about this instance; reasoning about what "inward" ought to mean is not.

**Undo:** `jira_remove_issue_link(link_id)`. The id is **not** returned by the create call — read
either linked issue and take `issuelinks[].id`. Removing and recreating is the only fix; there is no
direction flip.

## Full reference

`plugin/scripts/check_draft.py` — the pre-send gate. Takes draft files, exits `1` on a
construct that would be refused whole. Run it on every body before every write.

`references/adf-wiki-mapping.md` — the measured ADF→wiki table (panel types are
`bgColor` hex values), what v3 returns instead, constructs with no ADF equivalent, and
the recipe for verifying a converter change end to end.

Fork-specific state (which fixes are on `main`, what is unpushed, the repo's real
gates) lives in that repository's own memory, not here.
