"""Tempo Cloud FastMCP server instance and tool definitions.

Tempo v4 speaks numeric Jira ids (issues, projects) and Atlassian account ids
(people). The tools take issue and project keys and translate them through
Jira, and translate ids back to keys and display names on the way out, so a
caller never has to handle Tempo's ids except a worklog's own id.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import urlencode

from fastmcp import Context
from pydantic import Field

from mcp_atlassian.jira import JiraFetcher
from mcp_atlassian.models.tempo import TempoWorklog, simplify_work_attribute
from mcp_atlassian.servers.dependencies import get_jira_fetcher, get_tempo_fetcher
from mcp_atlassian.servers.error_handling import ErrorPreservingFastMCP
from mcp_atlassian.servers.jira import ISSUE_KEY_PATTERN, PROJECT_KEY_PATTERN
from mcp_atlassian.tempo import TempoFetcher
from mcp_atlassian.tempo.utils import (
    format_duration,
    parse_date,
    parse_duration,
    parse_start_time,
)
from mcp_atlassian.utils.decorators import check_write_access

logger = logging.getLogger(__name__)

tempo_mcp = ErrorPreservingFastMCP(
    name="Tempo MCP Service",
    instructions=(
        "Provides tools for Tempo Timesheets on Jira Cloud: worklogs with work "
        "attributes, accounts, teams and timesheet approval status."
    ),
)

# Jira's bulk endpoints take at most 100 issues / 200 users per call.
_ISSUE_BATCH = 100
_USER_BATCH = 200
_MAX_RESULTS = 5000


def _split_csv(value: str | None) -> list[str]:
    """Split a comma-separated parameter into trimmed, non-empty items."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _validated_keys(value: str | None, pattern: str, what: str) -> list[str]:
    """Split comma-separated keys, upper-case them, and check each one.

    The keys end up in Jira URL paths, so anything that is not a key is
    refused before a request is made.

    Raises:
        ValueError: If an item is not a valid key.
    """
    keys = [key.upper() for key in _split_csv(value)]
    invalid = [key for key in keys if not re.match(pattern, key)]
    if invalid:
        raise ValueError(f"Invalid {what}: {', '.join(invalid)}")
    return keys


def _dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Jira lookups (blocking; run in a worker thread)
# ---------------------------------------------------------------------------


def _bulk_fetch_issues(
    jira: JiraFetcher, ids_or_keys: list[str]
) -> list[dict[str, Any]]:
    """Fetch issues by id or key through ``POST /rest/api/3/issue/bulkfetch``.

    Issues that do not exist or are not visible are left out rather than
    failing the batch.
    """
    issues: list[dict[str, Any]] = []
    for start in range(0, len(ids_or_keys), _ISSUE_BATCH):
        batch = ids_or_keys[start : start + _ISSUE_BATCH]
        response = jira.jira.post(
            "rest/api/3/issue/bulkfetch",
            json={"issueIdsOrKeys": batch, "fields": ["summary"]},
        )
        if isinstance(response, dict):
            issues.extend(response.get("issues") or [])
    return issues


def _issue_ids_for_keys(jira: JiraFetcher, keys: list[str]) -> dict[str, int]:
    """Map issue keys to numeric ids.

    The bulk endpoint answers with each issue's current key, so a key from
    before a move does not match its result; those are looked up one by one,
    which follows Jira's redirect from the old key.

    Raises:
        ValueError: If any key does not resolve to a visible issue.
    """
    if not keys:
        return {}
    issues = _bulk_fetch_issues(jira, keys)
    found = {issue["key"]: int(issue["id"]) for issue in issues}
    missing = []
    for key in keys:
        if key in found:
            continue
        try:
            issue = jira.jira.get(
                f"rest/api/3/issue/{key}", params={"fields": "summary"}
            )
        except Exception:  # noqa: BLE001 - reported below as not found
            issue = None
        if isinstance(issue, dict) and issue.get("id"):
            found[key] = int(issue["id"])
        else:
            missing.append(key)
    if missing:
        raise ValueError(
            f"Jira issue(s) not found or not visible: {', '.join(missing)}"
        )
    return found


def _issue_refs_for_ids(jira: JiraFetcher, ids: set[int]) -> dict[int, dict[str, str]]:
    """Map numeric issue ids to ``{"key", "summary"}`` (best effort)."""
    if not ids:
        return {}
    refs: dict[int, dict[str, str]] = {}
    try:
        for issue in _bulk_fetch_issues(jira, [str(i) for i in sorted(ids)]):
            refs[int(issue["id"])] = {
                "key": issue["key"],
                "summary": (issue.get("fields") or {}).get("summary", ""),
            }
    except Exception as e:  # noqa: BLE001 - enrichment must not fail a read
        logger.warning(f"Could not resolve Tempo issue ids to keys: {e}")
    return refs


def _project_ids_for_keys(jira: JiraFetcher, keys: list[str]) -> list[int]:
    """Map project keys to numeric ids.

    Raises:
        ValueError: If a project does not exist or is not visible.
    """
    ids = []
    for key in keys:
        project = jira.get_project(key)
        if not project or "id" not in project:
            raise ValueError(f"Jira project not found or not visible: {key}")
        ids.append(int(project["id"]))
    return ids


def _display_names(jira: JiraFetcher, account_ids: set[str]) -> dict[str, str]:
    """Map Atlassian account ids to display names (best effort)."""
    names: dict[str, str] = {}
    ordered = sorted(account_ids)
    try:
        for start in range(0, len(ordered), _USER_BATCH):
            batch = ordered[start : start + _USER_BATCH]
            # The query goes into the path: atlassian-python-api urlencodes
            # params without doseq, which sends a list as the literal text
            # "['id']" instead of one accountId parameter per id.
            query = urlencode(
                [("accountId", a) for a in batch] + [("maxResults", _USER_BATCH)]
            )
            response = jira.jira.get(f"rest/api/3/user/bulk?{query}")
            for user in (response or {}).get("values") or []:
                if user.get("accountId"):
                    names[user["accountId"]] = user.get("displayName", "")
    except Exception as e:  # noqa: BLE001 - enrichment must not fail a read
        logger.warning(f"Could not resolve account ids to display names: {e}")
    return names


def _simplify_worklogs(
    jira: JiraFetcher, raw: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Convert raw Tempo worklogs, adding issue keys and author names."""
    issue_ids = {
        w["issue"]["id"] for w in raw if (w.get("issue") or {}).get("id") is not None
    }
    authors = {
        w["author"]["accountId"]
        for w in raw
        if (w.get("author") or {}).get("accountId")
    }
    issues = _issue_refs_for_ids(jira, issue_ids)
    names = _display_names(jira, authors)
    worklogs = []
    for item in raw:
        simplified = TempoWorklog.from_api_response(
            item, issues=issues
        ).to_simplified_dict()
        author_name = names.get(simplified.get("author_account_id", ""))
        if author_name:
            simplified["author"] = author_name
        worklogs.append(simplified)
    return worklogs


# ---------------------------------------------------------------------------
# Work attribute validation
# ---------------------------------------------------------------------------


def _parse_attributes_param(attributes: str | None) -> dict[str, str | None]:
    """Parse the ``attributes`` tool parameter, a JSON object of key -> value."""
    if not attributes:
        return {}
    try:
        parsed = json.loads(attributes)
    except json.JSONDecodeError as e:
        raise ValueError(f"attributes must be a JSON object: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError('attributes must be a JSON object, e.g. {"_Role_": "Dev"}')
    result: dict[str, str | None] = {}
    for key, value in parsed.items():
        if value is None:
            result[str(key)] = None
        elif isinstance(value, bool):
            result[str(key)] = "true" if value else "false"
        else:
            result[str(key)] = str(value)
    return result


def _resolve_attributes(
    tempo: TempoFetcher,
    requested: dict[str, str | None],
    account_key: str | None,
) -> dict[str, str | None]:
    """Validate requested work attribute values against Tempo's catalog.

    A key may be given as the attribute's key or its display name; a
    STATIC_LIST value may be given as its stored value or its label.
    ``account_key`` fills the catalog's ACCOUNT attribute.

    Returns:
        Attribute key -> stored value (None means "remove").

    Raises:
        ValueError: On an unknown attribute or a value not in its list.
    """
    if not requested and account_key is None:
        return {}
    catalog = tempo.get_work_attributes()
    by_key = {a["key"]: a for a in catalog if a.get("key")}
    by_name = {str(a.get("name", "")).casefold(): a for a in catalog if a.get("key")}

    resolved: dict[str, str | None] = {}
    for given_key, value in requested.items():
        attribute = by_key.get(given_key) or by_name.get(given_key.casefold())
        if attribute is None:
            known = ", ".join(f"{a['key']} ({a.get('name')})" for a in by_key.values())
            raise ValueError(
                f"Unknown work attribute {given_key!r}. Known: {known or 'none'}"
            )
        key = attribute["key"]
        if value is not None and attribute.get("type") == "STATIC_LIST":
            options = attribute.get("values") or []
            labels = attribute.get("names") or {}
            if value not in options:
                matches = [
                    v
                    for v in options
                    if str(labels.get(v, "")).casefold() == value.casefold()
                ]
                if len(matches) != 1:
                    allowed = ", ".join(f"{labels.get(v, v)}" for v in options)
                    raise ValueError(
                        f"{value!r} is not an option of work attribute {key}. "
                        f"Options: {allowed}"
                    )
                value = matches[0]
        resolved[key] = value

    if account_key is not None:
        account_attributes = [a for a in catalog if a.get("type") == "ACCOUNT"]
        if not account_attributes:
            raise ValueError(
                "account_key was given, but Tempo has no ACCOUNT work attribute "
                "configured."
            )
        resolved[account_attributes[0]["key"]] = account_key
    return resolved


def _attributes_payload(values: dict[str, str | None]) -> list[dict[str, str]]:
    return [
        {"key": key, "value": value}
        for key, value in values.items()
        if value is not None and value != ""
    ]


def _check_author(created: dict[str, Any], expected_account_id: str) -> str | None:
    """Return a warning when Tempo stored a different author than was sent."""
    stored = (created.get("author") or {}).get("accountId")
    if stored and stored != expected_account_id:
        return (
            f"Tempo stored author {stored}, not the Jira user {expected_account_id}: "
            "the Tempo token and the Jira credentials belong to different people."
        )
    return None


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@tempo_mcp.tool(
    tags={"tempo", "read", "toolset:tempo_worklogs"},
    annotations={"title": "Search Tempo Worklogs", "readOnlyHint": True},
)
async def search_worklogs(
    ctx: Context,
    date_from: Annotated[
        str, Field(description="First day to include, YYYY-MM-DD (inclusive)")
    ],
    date_to: Annotated[
        str, Field(description="Last day to include, YYYY-MM-DD (inclusive)")
    ],
    issue_keys: Annotated[
        str | None,
        Field(
            description="(Optional) Comma-separated issue keys, e.g. 'PROJ-1,PROJ-2'"
        ),
    ] = None,
    project_keys: Annotated[
        str | None,
        Field(description="(Optional) Comma-separated project keys, e.g. 'PROJ,OPS'"),
    ] = None,
    authors: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Whose worklogs: 'me', 'all', or comma-separated "
                "Atlassian account ids. Default: 'me' when no issue or project "
                "filter is given, otherwise 'all'."
            )
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description="Maximum number of worklogs to return", ge=1, le=_MAX_RESULTS
        ),
    ] = 500,
) -> str:
    """Search Tempo worklogs for a period, by issue, project and author.

    Returns worklogs with issue keys, author names, work attribute values and
    a total, ordered by start date.

    Args:
        ctx: The FastMCP context.
        date_from: First day (inclusive).
        date_to: Last day (inclusive).
        issue_keys: Optional issue keys.
        project_keys: Optional project keys.
        authors: 'me', 'all' or account ids.
        limit: Maximum worklogs to return.

    Returns:
        JSON with the worklogs, their count, total time and whether more exist.
    """
    start = parse_date(date_from, "date_from")
    end = parse_date(date_to, "date_to")
    if end < start:
        raise ValueError("date_to must not be before date_from")
    issue_key_list = _validated_keys(issue_keys, ISSUE_KEY_PATTERN, "issue key(s)")
    project_key_list = _validated_keys(
        project_keys, PROJECT_KEY_PATTERN, "project key(s)"
    )
    author_mode = (authors or "").strip()
    if not author_mode:
        author_mode = "all" if (issue_key_list or project_key_list) else "me"

    jira = await get_jira_fetcher(ctx)
    tempo = await get_tempo_fetcher(ctx)

    def run() -> dict[str, Any]:
        if author_mode.lower() == "me":
            author_ids: list[str] | None = [jira.get_current_user_account_id()]
        elif author_mode.lower() == "all":
            author_ids = None
        else:
            author_ids = _split_csv(author_mode)
        issue_ids = list(_issue_ids_for_keys(jira, issue_key_list).values())
        project_ids = _project_ids_for_keys(jira, project_key_list)
        raw, has_more = tempo.search_worklogs(
            author_account_ids=author_ids,
            issue_ids=issue_ids or None,
            project_ids=project_ids or None,
            date_from=start,
            date_to=end,
            limit=limit,
        )
        worklogs = _simplify_worklogs(jira, raw)
        total = sum(w.get("time_spent_seconds", 0) for w in worklogs)
        return {
            "count": len(worklogs),
            "total_time_spent": format_duration(total),
            "total_time_spent_seconds": total,
            "has_more": has_more,
            "worklogs": worklogs,
        }

    return _dumps(await asyncio.to_thread(run))


@tempo_mcp.tool(
    tags={"tempo", "read", "toolset:tempo_worklogs"},
    annotations={"title": "Get Tempo Worklog", "readOnlyHint": True},
)
async def get_worklog(
    ctx: Context,
    worklog_id: Annotated[int, Field(description="Tempo worklog id", ge=1)],
) -> str:
    """Get one Tempo worklog by its id.

    Args:
        ctx: The FastMCP context.
        worklog_id: The Tempo worklog id.

    Returns:
        JSON with the worklog.
    """
    jira = await get_jira_fetcher(ctx)
    tempo = await get_tempo_fetcher(ctx)

    def run() -> dict[str, Any]:
        return _simplify_worklogs(jira, [tempo.get_worklog(worklog_id)])[0]

    return _dumps(await asyncio.to_thread(run))


@tempo_mcp.tool(
    tags={"tempo", "read", "toolset:tempo_worklogs"},
    annotations={"title": "Get Tempo Work Attributes", "readOnlyHint": True},
)
async def get_work_attributes(
    ctx: Context,
    search: Annotated[
        str | None,
        Field(description="(Optional) Only attributes whose key or name contains this"),
    ] = None,
) -> str:
    """List the work attributes worklogs can carry, with their allowed options.

    Use the keys (or names) and option values (or labels) from here in the
    'attributes' parameter of tempo_create_worklog / tempo_update_worklog.

    Args:
        ctx: The FastMCP context.
        search: Optional case-insensitive key/name filter.

    Returns:
        JSON list of attributes: key, name, type, required, and options for
        STATIC_LIST attributes.
    """
    tempo = await get_tempo_fetcher(ctx)
    catalog = await asyncio.to_thread(tempo.get_work_attributes, refresh=True)
    attributes = [simplify_work_attribute(a) for a in catalog]
    if search:
        needle = search.casefold()
        attributes = [
            a
            for a in attributes
            if needle in str(a.get("key", "")).casefold()
            or needle in str(a.get("name", "")).casefold()
        ]
    return _dumps(attributes)


@tempo_mcp.tool(
    tags={"tempo", "read", "toolset:tempo_accounts"},
    annotations={"title": "Search Tempo Accounts", "readOnlyHint": True},
)
async def search_accounts(
    ctx: Context,
    keys: Annotated[
        str | None,
        Field(description="(Optional) Comma-separated account keys"),
    ] = None,
    statuses: Annotated[
        str,
        Field(
            description=(
                "Comma-separated statuses: OPEN, CLOSED, ARCHIVED. Default OPEN; "
                "pass 'all' for every status."
            )
        ),
    ] = "OPEN",
    limit: Annotated[
        int, Field(description="Maximum number of accounts", ge=1, le=_MAX_RESULTS)
    ] = 200,
) -> str:
    """Search Tempo accounts (the billing accounts worklogs are logged against).

    Args:
        ctx: The FastMCP context.
        keys: Optional account keys.
        statuses: Account statuses to include.
        limit: Maximum accounts to return.

    Returns:
        JSON with the accounts: key, name, status, customer, category, lead.
    """
    status_list = [s.upper() for s in _split_csv(statuses)]
    if [s.lower() for s in status_list] == ["all"]:
        status_list = []
    invalid = set(status_list) - {"OPEN", "CLOSED", "ARCHIVED"}
    if invalid:
        raise ValueError(f"Unknown account status(es): {', '.join(sorted(invalid))}")
    tempo = await get_tempo_fetcher(ctx)
    raw, has_more = await asyncio.to_thread(
        tempo.search_accounts,
        keys=_split_csv(keys) or None,
        statuses=status_list or None,
        limit=limit,
    )
    accounts = []
    for item in raw:
        account = {
            "id": item.get("id"),
            "key": item.get("key"),
            "name": item.get("name"),
            "status": item.get("status"),
            "global": item.get("global"),
            "customer": (item.get("customer") or {}).get("name"),
            "category": (item.get("category") or {}).get("name"),
            "lead_account_id": (item.get("lead") or {}).get("accountId"),
        }
        accounts.append({k: v for k, v in account.items() if v is not None})
    return _dumps({"count": len(accounts), "has_more": has_more, "accounts": accounts})


@tempo_mcp.tool(
    tags={"tempo", "read", "toolset:tempo_teams"},
    annotations={"title": "Get Tempo Teams", "readOnlyHint": True},
)
async def get_teams(
    ctx: Context,
    name: Annotated[
        str | None, Field(description="(Optional) Team name filter")
    ] = None,
    limit: Annotated[
        int, Field(description="Maximum number of teams", ge=1, le=_MAX_RESULTS)
    ] = 200,
) -> str:
    """List Tempo teams.

    Args:
        ctx: The FastMCP context.
        name: Optional team name filter.
        limit: Maximum teams to return.

    Returns:
        JSON with the teams: id, name, summary, status, lead.
    """
    tempo = await get_tempo_fetcher(ctx)
    raw, has_more = await asyncio.to_thread(tempo.get_teams, name=name, limit=limit)
    teams = []
    for item in raw:
        team = {
            "id": item.get("id"),
            "name": item.get("name"),
            "summary": item.get("summary") or None,
            "status": item.get("status"),
            "lead_account_id": (item.get("lead") or {}).get("accountId"),
        }
        teams.append({k: v for k, v in team.items() if v is not None})
    return _dumps({"count": len(teams), "has_more": has_more, "teams": teams})


@tempo_mcp.tool(
    tags={"tempo", "read", "toolset:tempo_teams"},
    annotations={"title": "Get Tempo Team Members", "readOnlyHint": True},
)
async def get_team_members(
    ctx: Context,
    team_id: Annotated[
        int, Field(description="Tempo team id (from tempo_get_teams)", ge=1)
    ],
) -> str:
    """List the members of a Tempo team with their names and active role.

    Args:
        ctx: The FastMCP context.
        team_id: The Tempo team id.

    Returns:
        JSON with the members: account id, display name, role, commitment.
    """
    jira = await get_jira_fetcher(ctx)
    tempo = await get_tempo_fetcher(ctx)

    def run() -> dict[str, Any]:
        raw, has_more = tempo.get_team_members(team_id)
        account_ids = {
            (m.get("member") or {}).get("accountId")
            for m in raw
            if (m.get("member") or {}).get("accountId")
        }
        names = _display_names(jira, account_ids)
        members = []
        for item in raw:
            account_id = (item.get("member") or {}).get("accountId")
            active = (item.get("memberships") or {}).get("active") or {}
            member = {
                "account_id": account_id,
                "name": names.get(account_id or ""),
                "role": (active.get("role") or {}).get("name"),
                "commitment_percent": active.get("commitmentPercent"),
                "from": active.get("from"),
                "to": active.get("to"),
            }
            members.append({k: v for k, v in member.items() if v is not None})
        return {"count": len(members), "has_more": has_more, "members": members}

    return _dumps(await asyncio.to_thread(run))


@tempo_mcp.tool(
    tags={"tempo", "read", "toolset:tempo_worklogs"},
    annotations={"title": "Get Tempo Timesheet Approval", "readOnlyHint": True},
)
async def get_timesheet_approval(
    ctx: Context,
    date: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Any day in the period, YYYY-MM-DD; default: today. "
                "The period itself (a month, a week, ...) is Tempo's own."
            )
        ),
    ] = None,
    account_id: Annotated[
        str | None,
        Field(description="(Optional) Atlassian account id; default: the current user"),
    ] = None,
) -> str:
    """Get the approval status of the timesheet period containing a day (read only).

    Tempo approves timesheets per period, with boundaries the site sets, so
    the period is looked up from the day rather than given.

    Args:
        ctx: The FastMCP context.
        date: Any day in the period; defaults to today.
        account_id: Whose timesheet; defaults to the current user.

    Returns:
        JSON with the period, status (OPEN, IN_REVIEW, APPROVED), required
        and logged time, and the reviewer.
    """
    # Today in the server's local time zone: the user's day, not UTC's.
    today = datetime.now(timezone.utc).astimezone().date()
    day = parse_date(date, "date") if date else today.isoformat()
    jira = await get_jira_fetcher(ctx)
    tempo = await get_tempo_fetcher(ctx)

    def run() -> dict[str, Any]:
        owner = account_id or jira.get_current_user_account_id()
        period = tempo.get_period(day)
        raw = tempo.get_timesheet_approval(owner, period["from"], period["to"])
        status = raw.get("status") or {}
        reviewer = (raw.get("reviewer") or {}).get("accountId")
        names = _display_names(jira, {a for a in (owner, reviewer) if a})
        result = {
            "account_id": owner,
            "name": names.get(owner),
            "period": raw.get("period") or period,
            "status": status.get("key"),
            "status_updated": status.get("updatedAt"),
            "status_comment": status.get("comment") or None,
            "required": format_duration(raw.get("requiredSeconds")),
            "time_spent": format_duration(raw.get("timeSpentSeconds")),
            "reviewer_account_id": reviewer,
            "reviewer": names.get(reviewer or ""),
        }
        return {k: v for k, v in result.items() if v is not None}

    return _dumps(await asyncio.to_thread(run))


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@tempo_mcp.tool(
    tags={"tempo", "write", "toolset:tempo_worklogs"},
    annotations={"title": "Create Tempo Worklog", "destructiveHint": False},
)
@check_write_access
async def create_worklog(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(description="Jira issue key, e.g. 'PROJ-123'", pattern=ISSUE_KEY_PATTERN),
    ],
    time_spent: Annotated[
        str, Field(description="Time spent, e.g. '1h 30m', '45m', '1.5h' (no days)")
    ],
    date: Annotated[str, Field(description="Day the work was done, YYYY-MM-DD")],
    start_time: Annotated[
        str | None,
        Field(description="(Optional) Local start time, HH:MM or HH:MM:SS"),
    ] = None,
    description: Annotated[
        str | None, Field(description="(Optional) Worklog description (plain text)")
    ] = None,
    billable_time: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Billable time, e.g. '1h'. Omit to let Tempo apply "
                "its default for the account."
            )
        ),
    ] = None,
    account_key: Annotated[
        str | None,
        Field(description="(Optional) Tempo account key to log against"),
    ] = None,
    attributes: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Work attribute values as a JSON object, keyed by "
                'attribute key or name, e.g. \'{"_Role_": "Developer"}\'. '
                "See tempo_get_work_attributes."
            )
        ),
    ] = None,
    remaining_estimate: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) New remaining estimate for the issue, e.g. '2h'. "
                "Omit to leave the estimate to Tempo/Jira."
            )
        ),
    ] = None,
) -> str:
    """Log time to a Jira issue in Tempo, as the current user.

    This writes to the user's own timesheet: call it only when the user
    explicitly asks to log time and has stated the amount. There is no delete
    tool, so a wrong entry has to be removed by hand in Tempo.

    The author is the Jira user these credentials belong to; the Tempo token
    must belong to the same person.

    Args:
        ctx: The FastMCP context.
        issue_key: The issue to log against.
        time_spent: Duration.
        date: Day of the work.
        start_time: Optional local start time.
        description: Optional description.
        billable_time: Optional billable duration.
        account_key: Optional Tempo account key.
        attributes: Optional work attribute values (JSON object).
        remaining_estimate: Optional new remaining estimate.

    Returns:
        JSON with the created worklog.
    """
    payload: dict[str, Any] = {
        "startDate": parse_date(date, "date"),
        "timeSpentSeconds": parse_duration(time_spent),
    }
    if start_time:
        payload["startTime"] = parse_start_time(start_time)
    if description:
        payload["description"] = description
    if billable_time:
        payload["billableSeconds"] = parse_duration(billable_time)
    if remaining_estimate:
        payload["remainingEstimateSeconds"] = parse_duration(remaining_estimate)
    requested_attributes = _parse_attributes_param(attributes)
    key = issue_key

    jira = await get_jira_fetcher(ctx)
    tempo = await get_tempo_fetcher(ctx)

    def run() -> dict[str, Any]:
        resolved = _resolve_attributes(tempo, requested_attributes, account_key)
        if resolved:
            payload["attributes"] = _attributes_payload(resolved)
        payload["issueId"] = _issue_ids_for_keys(jira, [key])[key]
        author = jira.get_current_user_account_id()
        payload["authorAccountId"] = author
        created = tempo.create_worklog(payload)
        result: dict[str, Any] = {
            "message": f"Worklog created on {key}",
            "worklog": _simplify_worklogs(jira, [created])[0],
        }
        warning = _check_author(created, author)
        if warning:
            result["warning"] = warning
        return result

    return _dumps(await asyncio.to_thread(run))


@tempo_mcp.tool(
    tags={"tempo", "write", "toolset:tempo_worklogs"},
    annotations={"title": "Update Tempo Worklog", "destructiveHint": True},
)
@check_write_access
async def update_worklog(
    ctx: Context,
    worklog_id: Annotated[int, Field(description="Tempo worklog id", ge=1)],
    time_spent: Annotated[
        str | None, Field(description="(Optional) New time spent, e.g. '2h'")
    ] = None,
    date: Annotated[
        str | None, Field(description="(Optional) New day, YYYY-MM-DD")
    ] = None,
    start_time: Annotated[
        str | None, Field(description="(Optional) New local start time, HH:MM")
    ] = None,
    description: Annotated[
        str | None, Field(description="(Optional) New description (plain text)")
    ] = None,
    billable_time: Annotated[
        str | None, Field(description="(Optional) New billable time, e.g. '1h'")
    ] = None,
    account_key: Annotated[
        str | None, Field(description="(Optional) New Tempo account key")
    ] = None,
    attributes: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Work attribute values to change, as a JSON object; "
                "a null value removes that attribute. Others are kept."
            )
        ),
    ] = None,
    remaining_estimate: Annotated[
        str | None,
        Field(description="(Optional) New remaining estimate for the issue, e.g. '2h'"),
    ] = None,
) -> str:
    """Change an existing Tempo worklog; fields not given are kept.

    Call it only when the user explicitly asks to change that worklog.

    Tempo replaces a worklog as a whole, so this reads the current worklog
    and sends it back with only the given fields changed. When time_spent
    changes and billable_time is not given, a fully billable worklog stays
    fully billable; otherwise the billable time is kept as it was.

    Args:
        ctx: The FastMCP context.
        worklog_id: The Tempo worklog id.
        time_spent: Optional new duration.
        date: Optional new day.
        start_time: Optional new local start time.
        description: Optional new description.
        billable_time: Optional new billable duration.
        account_key: Optional new Tempo account key.
        attributes: Optional attribute changes (JSON object).
        remaining_estimate: Optional new remaining estimate.

    Returns:
        JSON with the updated worklog.
    """
    new_seconds = parse_duration(time_spent) if time_spent else None
    new_date = parse_date(date, "date") if date else None
    new_start = parse_start_time(start_time) if start_time else None
    new_billable = parse_duration(billable_time) if billable_time else None
    new_remaining = parse_duration(remaining_estimate) if remaining_estimate else None
    requested_attributes = _parse_attributes_param(attributes)
    if (
        not any(
            v is not None
            for v in (
                new_seconds,
                new_date,
                new_start,
                description,
                new_billable,
                new_remaining,
                account_key,
            )
        )
        and not requested_attributes
    ):
        raise ValueError("Nothing to update: give at least one field to change.")

    jira = await get_jira_fetcher(ctx)
    tempo = await get_tempo_fetcher(ctx)

    def run() -> dict[str, Any]:
        current = tempo.get_worklog(worklog_id)
        old_seconds = current.get("timeSpentSeconds") or 0
        old_billable = current.get("billableSeconds")
        payload: dict[str, Any] = {
            "authorAccountId": (current.get("author") or {}).get("accountId"),
            "startDate": new_date or current.get("startDate"),
            "timeSpentSeconds": new_seconds or old_seconds,
        }
        start = new_start or current.get("startTime")
        if start:
            payload["startTime"] = start
        text = description if description is not None else current.get("description")
        if text:
            payload["description"] = text
        if new_billable is not None:
            payload["billableSeconds"] = new_billable
        elif old_billable is not None:
            fully_billable = old_billable == old_seconds
            payload["billableSeconds"] = (
                payload["timeSpentSeconds"] if fully_billable else old_billable
            )
        if new_remaining is not None:
            payload["remainingEstimateSeconds"] = new_remaining

        values = {
            v["key"]: v.get("value")
            for v in ((current.get("attributes") or {}).get("values") or [])
            if "key" in v
        }
        values.update(_resolve_attributes(tempo, requested_attributes, account_key))
        attribute_list = _attributes_payload(values)
        if attribute_list:
            payload["attributes"] = attribute_list

        updated = tempo.update_worklog(worklog_id, payload)
        return {
            "message": f"Worklog {worklog_id} updated",
            "worklog": _simplify_worklogs(jira, [updated])[0],
        }

    return _dumps(await asyncio.to_thread(run))
