# Measured ADF↔wiki behaviour on Jira Cloud

Measured against a real Jira Cloud site (`JiraConfig.is_cloud` is True — check that
`JIRA_URL` is the `atlassian.net` host and not an internal alias, or the detection is
wrong) by writing ADF through `/rest/api/3` and reading it back through `/rest/api/2`.

**Scope note.** A v2 read is what **Server/DC** gets, and what a session whose MCP
process still has older modules loaded gets (`Jira()` constructed with no
`api_version` defaults to `"2"`). On Cloud the read goes over `/rest/api/3` and returns
the stored ADF, which `adf_to_markdown` renders directly. This table therefore
describes what Atlassian's own ADF→wiki renderer does — worth knowing, because it is
what the v2 half of the world sees, and because it is the reason the Cloud read path
uses v3 at all.

## ADF written → wiki the v2 read returns

**A panel's kind is a background colour on the wire. Jira Cloud never emits `title=`.**

| ADF written | wiki returned |
|---|---|
| `panel` / `panelType: info` | `{panel:bgColor=#deebff}` |
| `panel` / `note` | `{panel:bgColor=#eae6ff}` |
| `panel` / `warning` | `{panel:bgColor=#fffae6}` |
| `panel` / `success` | `{panel:bgColor=#e3fcef}` |
| `panel` / `error` | `{panel:bgColor=#ffebe6}` |
| `panel` whose first child is a bold paragraph | `{panel:bgColor=…}` + `*Titled*` — ADF panels have no title attribute, so a wiki panel title has to become a bold first paragraph inside |
| `blockquote` with two paragraphs | `{quote}line one\n\nline two{quote}` — **no padding newlines** next to the braces |
| two `blockquote`s with prose between | `{quote}first{quote}\n\nprose\n\n{quote}second{quote}` |
| `blockquote` + `codeBlock` | `{quote}q\n\n{code:python}x = 1{code}{quote}` |
| `expand` | **the node is lost** — `*title*` followed by the inlined content |

## Which nestings Jira accepts, tested one at a time

A document mixing several constructs is rejected as a whole with
`400 {"errorMessages":["INVALID_INPUT"],"errors":{}}` — opaque, naming no field — so
isolate before concluding anything.

- `panel` containing `blockquote` → **REJECTED 400**
- `panel` containing `panel` → **REJECTED 400**
- `panel` containing `paragraph` / `heading` / `bulletList` / `codeBlock` → accepted
- `blockquote` containing `codeBlock` → accepted
- `blockquote` containing `bulletList` → accepted (measured: the write returned 204 and
  the browser shows a bulleted list inside the quote bar). The ADF schema declares `bulletList` and
  `orderedList` as blockquote children, and Jira honours the schema here.

Consequence for any converter: a wiki panel *can* hold a quote, so a reader that spells
panels as something the writer nests must flatten the quote — otherwise a
read-modify-write turns a lossy read into a hard 400, which is strictly worse.

## `expand` is write-only *on the v2 read path*

The node survives the write, but Jira's own ADF→wiki flattens it, so over v2 it can
never round-trip and `{expand}` only ever reaches the read path from wiki-authored
content. That is Jira's behaviour, not a converter defect — nothing in the converter
can fix it.

Over v3 the node comes back as itself: measured — `:::expand Hidden details` was
written, rendered as a collapsed section in the browser, and read back as
`:::expand Hidden details`. This is the single sharpest reason the read path moved.

## Read → edit → write is safe on the core vocabulary

Measured by piping `clean_jira_text` output straight into `markdown_to_adf` and
asserting ADF node types. All of these survive the full circle:

`h2.`→`##`→`heading`, `{{x}}`→backticks→`code`, wiki `*b*`→`**b**`→`strong`,
wiki `_i_`→`*i*`→`em`, `*`-bullets→`-`→`bulletList`, `#`-numbered→`1.`→`orderedList`,
`[t|u]`→`[t](u)`→`link`, `||h||`→md table→`table`, `{code}`→fenced→`codeBlock`,
`{panel:bgColor=…}`→`:::info`→`panel` with the right `panelType`, and two `{quote}`
blocks with prose between them stay two quotes.

## Constructs with no ADF equivalent

`{color}`, `-strike-` (deliberately unconverted — hyphen false positives), `+ins+`,
`^sup^`, `~sub~`.

One live parser gap: `markdown_to_adf` splits a blockquote on a bare `>` line
(`> a` / `>` / `> b` → blockquote, paragraph, blockquote) instead of keeping one quote
with two paragraphs.

## Verifying a converter change end to end

**`/mcp` restarts the server process.** Measured: after `/mcp` reports "Reconnected to
atlassian", a new `mcp-atlassian` PID exists, parented by this session's `claude`,
started after the last source edit — and the tool layer serves the new code. So a
converter change can be verified end to end from inside a session, not only at library
level.

So the end-to-end recipe is: edit → `/mcp` → confirm the new PID's start time → write a
probe document → read it back → screenshot the browser. What that catches and a library
probe does not: the tool layer, transport version, and Jira's own acceptance of the ADF.

To test a checkout *other* than the launcher's, drive a fresh subprocess:

```
uv run --frozen --project <checkout> mcp-atlassian     # stdio
# then: initialize → notifications/initialized → tools/call
```

That exercises the tool layer, which is where a bug like "parameter forwarded to the
API but never read back off the response" hides — `renderedFields` was exactly that
shape, and `properties` still is.

Gotchas:

- `jira_update_issue`'s `fields` argument is a **JSON string**, not an object.
- The launcher picks credentials from a `$PWD` prefix case; to talk to a checkout other
  than the launcher's, pass the credentials yourself rather than relying on `$PWD`.
- A library-level probe is enough for the converter itself:
  `JiraPreprocessor(base_url=…).clean_jira_text(<wiki>)` for the read direction,
  `markdown_to_adf(<markdown>, <base_url>, known_project_keys=…)` for the write
  direction, asserting node **types**, never rendered text.

## Jira's own doing, not the server's

The link-type `name` is `Relates` while the inward/outward *labels* read "relates to" —
call `jira_get_link_types(name_filter=…)` before linking.

## The probe document

A 918-byte Markdown document covering heading, all inline marks, five escaped
"looks like markup but is prose" paragraphs, three panel types (one titled), a nested
bullet, a multi-block list item, an ordered list starting at 5, a task list, a
blockquote holding two paragraphs + a list + a bash fence, an expand, a table with an
escaped pipe, a four-backtick fence wrapping a ``` line, a rule.

It is kept verbatim in `references/probe-document.md`. Written to a real issue,
verified in the browser, read back **byte-identical** over v3, then the description was
cleared. Reuse that file rather than inventing a
new document — every line in it is there because some converter defect once hid behind
that construct.
