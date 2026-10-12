# mcp-atlassian as a Claude Code plugin

This repository is both the MCP server and its own single-entry plugin
marketplace. It is a fork of [sooperset/mcp-atlassian](https://github.com/sooperset/mcp-atlassian);
the converter work that makes formatted content survive a read/edit/write cycle
is what the fork adds. Installing the plugin gives you three things:

- **the MCP server** — 98 Jira and Confluence tools;
- **the `jira-content` skill** — the rules for moving formatted content through
  it, and the measured ADF↔wiki behaviour behind them;
- **an ADF write guard** — a `PreToolUse` hook that refuses a body Jira would
  reject, before it leaves your machine.

## Requirements

[`uv`](https://docs.astral.sh/uv/) on `PATH`. It is the only system dependency:
it brings its own Python and installs the server's dependencies from `uv.lock`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
brew install uv                                   # macOS
winget install astral-sh.uv                       # Windows
```

Everything the plugin runs is Python invoked through `uv`, so there is no shell
dependency and Windows needs no WSL or Git Bash.

## Install

```bash
claude plugin marketplace add AndrewKolpakov/mcp-atlassian
claude plugin install mcp-atlassian@local-atlassian
```

Then configure credentials, either way below.

## Credentials

Nothing secret is stored in this repository or in the plugin. Pick one of two
routes; if both are present, **the file wins**.

### Route 1 — the plugin's own options

```
/plugin configure mcp-atlassian@local-atlassian
```

Fill the Jira and/or Confluence URL, account email and API token. Tokens are
marked sensitive, so they go to the OS keychain rather than `settings.json`.
Leave a group empty to disable that product — an unfilled option is discarded by
the launcher rather than passed on as an empty setting, so a Jira-only
configuration registers Jira tools only.

Create an Atlassian Cloud API token at
<https://id.atlassian.com/manage-profile/security/api-tokens>. On Server/DC use a
personal access token instead: set `JIRA_PERSONAL_TOKEN` /
`CONFLUENCE_PERSONAL_TOKEN` through route 2.

### Route 2 — a credentials file

`~/.config/mcp-atlassian/credentials.ini`, or wherever `MCP_ATLASSIAN_CONFIG`
points.

**It is an INI file, not TOML**, even though `[section]` + `key = value` reads
that way — `JIRA_URL = https://…` is a syntax error in TOML, which requires
strings to be quoted. INI is deliberate: `tomllib` is stdlib only from Python
3.11, this project supports 3.10, and the launcher stays dependency-free so it
can run under `uv run --no-project`. Values therefore need no quotes; a matching
pair around one is stripped, so the TOML habit is harmless rather than a silent
authentication failure.

```ini
[default]
JIRA_URL = https://your-company.atlassian.net
JIRA_USERNAME = you@example.com
JIRA_API_TOKEN = ...
CONFLUENCE_URL = https://your-company.atlassian.net/wiki
CONFLUENCE_USERNAME = you@example.com
CONFLUENCE_API_TOKEN = ...
```

One machine can serve several Atlassian sites: name a section after a directory
and it applies whenever Claude Code was started inside it.

```ini
[default]
JIRA_USERNAME = you@example.com

[~/work/acme]
JIRA_URL = https://acme.atlassian.net
JIRA_USERNAME = you@acme.com
JIRA_API_TOKEN = ...

[~/work/globex]
JIRA_URL = https://globex.atlassian.net
JIRA_USERNAME = you@globex.com
JIRA_API_TOKEN = ...
```

Sections **layer from least to most specific**: `[default]`, then each enclosing
directory, then the directory itself. So `[~/work/acme/api]` inherits
`[~/work/acme]`'s token and overrides only what it names — it never falls back
past its parent to `[default]`, which would authenticate to one site with
another site's credential.

The same file also configures the companion
[bitbucket-mcp plugin](https://github.com/AndrewKolpakov/bitbucket-mcp), which
reads `BITBUCKET_WORKSPACE`, `BITBUCKET_USERNAME`, `BITBUCKET_PASSWORD`,
`BITBUCKET_TOKEN` and `BITBUCKET_URL` from the same sections, layered by the same
rules. This launcher ignores those keys, and that one ignores the Jira and
Confluence ones.

Every variable the server understands — proxies, SSL, toolset filtering,
read-only mode — is listed in [`.env.example`](../.env.example). Set those in the
environment; this file carries the credential variables.

#### Running the server only in certain directories

Omit `[default]` entirely and the server starts only where a section matches:

```ini
[~/work/acme]
JIRA_URL = https://acme.atlassian.net
JIRA_USERNAME = you@acme.com
JIRA_API_TOKEN = ...
```

Inside `~/work/acme` the server starts; anywhere else no credential resolves, the
launcher stops with `no Atlassian site configured`, and no Jira or Confluence
tools reach the session. Useful when the client's Atlassian data should not be
one stray directory away.

**This only holds while the plugin's own options are empty.** An option you filled
in supplies a URL everywhere, which satisfies the same check and starts the server
outside the listed directories too. Pick one route or the other, not both.

`--dry-run` (see Troubleshooting) reports which sections applied, so the gating
can be checked per directory without starting anything.

## Tool names

Tools arrive under the plugin-scoped prefix:

```
mcp__plugin_mcp-atlassian_atlassian__jira_get_issue
mcp__plugin_mcp-atlassian_atlassian__confluence_get_page
```

If you also have the server registered directly (`claude mcp add atlassian …`),
its tools keep the shorter `mcp__atlassian__…` prefix — and you will have two of
every tool. Remove one:

```bash
claude mcp remove atlassian -s user
```

## Running your own working tree

By default the server runs the copy the plugin shipped, pinned to the plugin
version. To serve a clone instead — the converter dev loop — either set the
*Run from this checkout instead* option, or:

```bash
export MCP_ATLASSIAN_SRC=~/src/mcp-atlassian
```

`MCP_ATLASSIAN_SRC` also governs the guard and `plugin/scripts/check_draft.py`,
so all three exercise the same tree. Restart the server with `/mcp` after
changing source: a process started before your edit still holds the old modules.

## Troubleshooting

```bash
uv run --no-project python plugin/scripts/mcp_atlassian_launcher.py --dry-run
```

Prints the resolved roots, which credentials file and which sections were used,
and which variables are set — never a token value — then stops without starting
the server. Drop `--dry-run` (or set `MCP_ATLASSIAN_DEBUG_ENV=1`) to print the
same report and then serve on stdio.

| symptom | cause |
| --- | --- |
| `uv is required and was not found` | install `uv`, see Requirements |
| `no Atlassian site configured` | neither route above supplied a `JIRA_URL` or `CONFLUENCE_URL` |
| `no Python project at …` | the *Run from this checkout instead* option points somewhere that is not a clone; clear it |
| credentials ignored | the file is INI, not shell — check the section names with `--dry-run`, which reports exactly which sections applied |
| two of every Jira tool | the server is registered directly as well as via the plugin, see Tool names |

## Notes for maintainers

- **The server config has to live in the repository-root `.mcp.json`.** On Claude
  Code 2.1.252 the `mcpServers` field of `plugin.json` is not honoured — measured
  in all three documented shapes (a path to a `{"mcpServers": …}` file, a path to
  a bare map, and an inline map); each registers zero servers, while `skills` as
  a manifest path registers normally.
- **That root file is also read as *project* MCP config** by anyone who opens
  this repository in Claude Code, and `${CLAUDE_PLUGIN_ROOT}` is not expanded in
  that reading. Every script here therefore derives the repository root from its
  own location, and treats a value that still looks like `${…}` as absent. Decline
  the project-scope prompt when working in the repo; use the plugin, or your own
  registration.
- `claude plugin validate --strict` reports one warning, deliberately: the
  repository's `CLAUDE.md` is contributor context and is not loaded for plugin
  users. Non-strict validation is clean.
- **Nothing here is a shell script.** The launcher, the write guard and the
  pre-send gate are Python, invoked through `uv`, because a `.sh` entry point
  would have made a POSIX shell a second, undeclared dependency and put Windows
  behind WSL or Git Bash. The launcher is stdlib-only so it can run under
  `uv run --no-project`; the guard and the gate run under
  `uv run --frozen --project <root>` because they import the converter.
