#!/usr/bin/env python3
"""Launcher for the mcp-atlassian Claude Code plugin.

Resolves where the server lives and which credentials it gets, then runs it.
Three things make this a script rather than a plain command in ``.mcp.json``:

1. An unfilled optional ``userConfig`` field does not always arrive as an unset
   variable, so empty and un-substituted values are cleared here. Passing
   ``JIRA_URL=""`` would otherwise look to the server like a configured Jira.
2. Credentials can come from a file instead of the plugin's settings, which is
   what lets one machine serve several Atlassian sites by directory.
3. ``uv`` is the plugin's one system dependency, and a missing one has to say so
   rather than fail as "command not found".

Stdlib only, so it can run under ``uv run --no-project`` without resolving the
server's dependency set first.
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

# Every variable the server reads for authentication. Cleared when the value is
# unusable, and settable from the credentials file.
CREDENTIAL_VARS = (
    "JIRA_URL",
    "JIRA_USERNAME",
    "JIRA_API_TOKEN",
    "JIRA_PERSONAL_TOKEN",
    "CONFLUENCE_URL",
    "CONFLUENCE_USERNAME",
    "CONFLUENCE_API_TOKEN",
    "CONFLUENCE_PERSONAL_TOKEN",
)
# One of these has to be set, or the server has no site to talk to.
SITE_VARS = ("JIRA_URL", "CONFLUENCE_URL")
# Values that must never be echoed by --debug-env.
SECRET_VARS = tuple(v for v in CREDENTIAL_VARS if "TOKEN" in v)

DEFAULT_SECTION = "default"


def die(message: str) -> NoReturn:
    """Exit with a message that names the fix, not just the symptom."""
    print(f"mcp-atlassian: {message}", file=sys.stderr)
    raise SystemExit(1)


def usable(value: str | None) -> bool:
    """Whether a value is a real value.

    An option the user left empty can arrive as ``""`` or as the literal
    ``${user_config.x}`` when substitution did not fire. Neither is a value.
    """
    return bool(value) and not value.lstrip().startswith("${")


def unquote(value: str) -> str:
    """Strip one matching pair of surrounding quotes from a config value.

    The credentials file is INI, where a value is literal to end of line, so
    quotes are part of it: ``JIRA_URL = "https://x"`` yields a URL *containing*
    the quote characters and an authentication failure that names nothing. The
    file's ``[section]`` + ``key = value`` shape reads as TOML, where quoting a
    string is required, so writing them is the natural mistake rather than an
    exotic one. Accepting both spellings costs one function.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def plugin_root() -> Path:
    """The repository root, derived from this file's location.

    This file sits at ``<root>/plugin/scripts/``. Deriving the root beats
    reading ``${CLAUDE_PLUGIN_ROOT}``: that variable is expanded only when
    Claude Code launches this as a plugin server, the same ``.mcp.json`` read as
    *project* config hands over the literal string, and running this by hand
    sets nothing at all.
    """
    return Path(__file__).resolve().parent.parent.parent


def resolve_source(root: Path) -> Path:
    """Which tree to run the server from.

    The plugin ships the whole repository, so its own root is a usable checkout.
    An explicit override points at a working tree instead, which is how
    converter changes are exercised live.
    """
    for name in ("MCP_ATLASSIAN_SRC", "CLAUDE_PLUGIN_OPTION_SERVER_SOURCE"):
        value = os.environ.get(name)
        if usable(value):
            return Path(value).expanduser().resolve()  # type: ignore[arg-type]
    return root


def clear_unusable(env: dict[str, str]) -> None:
    """Drop credential variables whose values are not values."""
    for name in CREDENTIAL_VARS:
        if name in env and not usable(env[name]):
            del env[name]


def config_path() -> Path:
    """Where the credentials file lives."""
    override = os.environ.get("MCP_ATLASSIAN_CONFIG")
    if usable(override):
        return Path(override).expanduser()  # type: ignore[arg-type]
    return Path.home() / ".config" / "mcp-atlassian" / "credentials.ini"


def _sections_for(parser: configparser.ConfigParser, cwd: Path) -> list[str]:
    """Every directory section containing ``cwd``, least specific first.

    Section names other than ``[default]`` are directory paths. They compose
    along the path rather than only the closest one applying: a section for a
    nested directory inherits the enclosing one and overrides just the keys it
    names. Applying only the closest match would silently pair a nested
    section's URL with the *default* token, which authenticates to the wrong
    site with a credential that may well be valid somewhere.

    A name that cannot be resolved to a path is skipped rather than raising --
    this runs on every server start.
    """
    matches: list[tuple[int, str]] = []
    for name in parser.sections():
        if name == DEFAULT_SECTION:
            continue
        try:
            candidate = Path(name).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if cwd == candidate or candidate in cwd.parents:
            matches.append((len(candidate.parts), name))
    return [name for _, name in sorted(matches)]


def apply_config(env: dict[str, str], path: Path, cwd: Path) -> str:
    """Apply the credentials file, and report which section was used.

    ``[default]`` is the base and directory sections layer over it from least
    to most specific, so a shared account email can be written once and only the
    differing values repeated. The file has the last word over the plugin's own
    options, which is what makes it a usable escape hatch.
    """
    if not path.is_file():
        return "absent"
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error) as exc:
        die(
            f"cannot read the credentials file {path}: {exc}\n"
            "    Fix the file or remove it; the plugin's own options work "
            "without it."
        )
    used = []
    for name in [DEFAULT_SECTION, *_sections_for(parser, cwd)]:
        if not parser.has_section(name):
            continue
        used.append(name)
        for key, value in parser.items(name):
            key = key.upper()
            value = unquote(value.strip())
            if key in CREDENTIAL_VARS and usable(value):
                env[key] = value
    return " + ".join(used) if used else "no matching section"


def debug_dump(
    root: Path, src: Path, config: Path, section: str, env: dict[str, str]
) -> None:
    """Report what was resolved, never a secret."""
    lines = [
        f"plugin root = {root}",
        f"server source = {src}",
        f"credentials file = {config} ({section})",
        f"cwd = {Path.cwd()}",
    ]
    for name in CREDENTIAL_VARS:
        if name in SECRET_VARS:
            lines.append(
                f"{name} = {'<set, value hidden>' if env.get(name) else '<unset>'}"
            )
        else:
            lines.append(f"{name} = {env.get(name, '<unset>')}")
    for line in lines:
        print(f"mcp-atlassian: {line}", file=sys.stderr)


def main(argv: list[str]) -> int:
    # --dry-run resolves everything, reports it, and stops short of the server.
    # Without it the only way to see what was resolved is to start a server that
    # then waits on stdin forever.
    dry_run = "--dry-run" in argv
    argv = [arg for arg in argv if arg != "--dry-run"]

    root = plugin_root()
    src = resolve_source(root)
    env = dict(os.environ)
    clear_unusable(env)
    config = config_path()
    section = apply_config(env, config, Path.cwd().resolve())

    if dry_run or os.environ.get("MCP_ATLASSIAN_DEBUG_ENV"):
        debug_dump(root, src, config, section, env)

    if shutil.which("uv") is None:
        die(
            "uv is required and was not found on PATH.\n"
            "    Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh\n"
            "    or:               brew install uv\n"
            "    On Windows:       winget install astral-sh.uv\n"
            "    uv is this plugin's only system dependency; it manages the "
            "Python side."
        )
    if not (src / "pyproject.toml").is_file():
        die(
            f"no Python project at {src} (looked for pyproject.toml).\n"
            "    Point MCP_ATLASSIAN_SRC at a clone of the repository, or clear "
            "the\n    'Run from this checkout instead' plugin option to use the "
            "bundled copy."
        )
    if not any(env.get(name) for name in SITE_VARS):
        die(
            "no Atlassian site configured — set at least JIRA_URL or "
            "CONFLUENCE_URL.\n"
            "    Either fill the plugin's options (/plugin, then configure "
            "mcp-atlassian),\n"
            f"    or create {config}. plugin/README.md has both recipes, and "
            "the full\n    variable list is in .env.example."
        )

    # --frozen installs from uv.lock and never re-resolves, so a launch cannot
    # silently drift from the locked dependency set; it fails loudly instead.
    # --project keeps the working directory intact, which the directory-matching
    # above depends on.
    if dry_run:
        print("mcp-atlassian: --dry-run, not starting the server", file=sys.stderr)
        return 0

    command = ["uv", "run", "--frozen", "--project", str(src), "mcp-atlassian", *argv]
    # subprocess rather than os.execvp: exec semantics differ on Windows, and
    # this server speaks MCP over inherited stdio, which a re-spawned parent
    # would break. Streams are inherited, so the pipe is untouched.
    try:
        # noqa justification: argv-list form, never a shell, so there is no
        # injection surface. The only variable parts are a filesystem path the
        # operator configured and the server's own arguments.
        return subprocess.run(  # noqa: S603 - argv list, shell=False, operator-supplied path
            command, env=env, check=False
        ).returncode
    except OSError as exc:  # uv vanished between the which() and here
        die(f"could not start the server: {exc}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
