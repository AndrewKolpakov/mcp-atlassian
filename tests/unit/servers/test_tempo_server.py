"""Tests for the Tempo Cloud tools, exercised through the public MCP client."""

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import Client

from mcp_atlassian.jira import JiraFetcher
from mcp_atlassian.tempo import TempoConfig, TempoFetcher

TEMPO_TOOLS = {
    "tempo_search_worklogs",
    "tempo_get_worklog",
    "tempo_create_worklog",
    "tempo_update_worklog",
    "tempo_get_work_attributes",
    "tempo_get_timesheet_approval",
    "tempo_search_accounts",
    "tempo_get_teams",
    "tempo_get_team_members",
}
TEMPO_WRITE_TOOLS = {"tempo_create_worklog", "tempo_update_worklog"}

CATALOG = [
    {"key": "_Account_", "name": "Account", "type": "ACCOUNT", "required": True},
    {
        "key": "_Role_",
        "name": "Role",
        "type": "STATIC_LIST",
        "required": False,
        "values": ["dev-id", "qa-id"],
        "names": {"dev-id": "Developer", "qa-id": "QA"},
    },
    {"key": "_Note_", "name": "Note", "type": "INPUT_FIELD", "required": False},
]


def _stored_worklog(**overrides: Any) -> dict[str, Any]:
    worklog = {
        "tempoWorklogId": 501,
        "issue": {"id": 10001},
        "author": {"accountId": "me-id"},
        "startDate": "2026-11-02",
        "startTime": "09:00:00",
        "timeSpentSeconds": 3600,
        "billableSeconds": 3600,
        "description": "Work",
        "attributes": {
            "values": [
                {"key": "_Account_", "value": "ACC"},
                {"key": "_Role_", "value": "dev-id"},
            ]
        },
    }
    worklog.update(overrides)
    return worklog


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    jira_url: str = "https://example.atlassian.net",
    tempo_token: str | None = "tempo-token",
    read_only: bool = False,
) -> None:
    monkeypatch.setenv("JIRA_URL", jira_url)
    monkeypatch.setenv("JIRA_USERNAME", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")
    monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "jira-pat")
    monkeypatch.delenv("CONFLUENCE_URL", raising=False)
    monkeypatch.setenv("TOOLSETS", "all")
    monkeypatch.delenv("ENABLED_TOOLS", raising=False)
    monkeypatch.setenv("READ_ONLY_MODE", "true" if read_only else "false")
    monkeypatch.setenv("ATLASSIAN_OAUTH_PROXY_ENABLE", "false")
    monkeypatch.setenv("FASTMCP_HOME", str(tmp_path / "fastmcp-home"))
    if tempo_token is None:
        monkeypatch.delenv("TEMPO_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("TEMPO_API_TOKEN", tempo_token)
    monkeypatch.delenv("TEMPO_URL", raising=False)


def _main_mcp() -> Any:
    return importlib.import_module("mcp_atlassian.servers.main").main_mcp


@pytest.fixture
def jira() -> MagicMock:
    fetcher = MagicMock(spec=JiraFetcher)
    fetcher.jira = MagicMock()
    fetcher.get_current_user_account_id.return_value = "me-id"

    def post(path: str, json: dict[str, Any]) -> dict[str, Any]:
        assert path == "rest/api/3/issue/bulkfetch"
        known = {
            "PROJ-1": ("10001", "PROJ-1"),
            "10001": ("10001", "PROJ-1"),
            "10002": ("10002", "PROJ-2"),
        }
        issues = [
            {"id": known[k][0], "key": known[k][1], "fields": {"summary": f"S {k}"}}
            for k in json["issueIdsOrKeys"]
            if k in known
        ]
        return {"issues": issues}

    def get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if path.startswith("rest/api/3/user/bulk?"):
            # One accountId parameter per id, never a stringified list.
            requested = parse_qs(urlsplit(path).query)["accountId"]
            assert params is None
            return {
                "values": [
                    {"accountId": a, "displayName": "Me Myself"}
                    for a in requested
                    if a == "me-id"
                ]
            }
        if path == "rest/api/3/issue/OLD-7":  # moved issue: old key redirects
            return {"id": "10002", "key": "PROJ-2"}
        raise RuntimeError(f"404 {path}")

    fetcher.jira.post.side_effect = post
    fetcher.jira.get.side_effect = get
    fetcher.get_project.side_effect = (
        lambda key: {"id": "200"} if key == "PROJ" else None
    )
    return fetcher


@pytest.fixture
def tempo() -> MagicMock:
    fetcher = MagicMock(spec=TempoFetcher)
    fetcher.get_work_attributes.return_value = CATALOG
    fetcher.create_worklog.side_effect = lambda payload: _stored_worklog(
        timeSpentSeconds=payload["timeSpentSeconds"]
    )
    fetcher.get_worklog.return_value = _stored_worklog()
    fetcher.update_worklog.side_effect = lambda worklog_id, payload: _stored_worklog(
        timeSpentSeconds=payload["timeSpentSeconds"]
    )
    fetcher.search_worklogs.return_value = ([_stored_worklog()], False)
    return fetcher


async def _call(
    name: str, arguments: dict[str, Any], jira: MagicMock, tempo: MagicMock
) -> Any:
    with (
        patch(
            "mcp_atlassian.servers.tempo.get_jira_fetcher",
            new_callable=AsyncMock,
            return_value=jira,
        ),
        patch(
            "mcp_atlassian.servers.tempo.get_tempo_fetcher",
            new_callable=AsyncMock,
            return_value=tempo,
        ),
    ):
        async with Client(_main_mcp()) as client:
            return await client.call_tool_mcp(name, arguments)


def _payload(result: Any) -> Any:
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


def _error(result: Any) -> str:
    assert result.isError
    return " ".join(getattr(c, "text", "") for c in result.content)


# ---------------------------------------------------------------------------
# Availability and policy
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tempo_tools_listed_with_token_and_cloud_jira(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    async with Client(_main_mcp()) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert TEMPO_TOOLS <= names


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param({"tempo_token": None}, id="no-token"),
        pytest.param({"jira_url": "https://jira.example.test"}, id="server-dc-jira"),
    ],
)
@pytest.mark.anyio
async def test_tempo_tools_hidden_without_usable_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup: dict[str, Any]
) -> None:
    _configure(monkeypatch, tmp_path, **setup)
    async with Client(_main_mcp()) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert not names & TEMPO_TOOLS
    assert "jira_get_issue" in names


@pytest.mark.anyio
@pytest.mark.security_regression
async def test_read_only_mode_hides_and_blocks_tempo_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    _configure(monkeypatch, tmp_path, read_only=True)
    async with Client(_main_mcp()) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert not names & TEMPO_WRITE_TOOLS
    assert "tempo_search_worklogs" in names

    result = await _call(
        "tempo_create_worklog",
        {"issue_key": "PROJ-1", "time_spent": "1h", "date": "2026-11-02"},
        jira,
        tempo,
    )
    assert "Unknown tool" in _error(result)
    tempo.create_worklog.assert_not_called()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_worklog_builds_tempo_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    _configure(monkeypatch, tmp_path)
    result = await _call(
        "tempo_create_worklog",
        {
            "issue_key": "PROJ-1",
            "time_spent": "1h 30m",
            "date": "2026-11-02",
            "start_time": "09:15",
            "description": "Pairing",
            "account_key": "ACC",
            "attributes": json.dumps({"Role": "QA", "_Note_": "n"}),
        },
        jira,
        tempo,
    )
    body = _payload(result)
    payload = tempo.create_worklog.call_args.args[0]
    assert payload == {
        "startDate": "2026-11-02",
        "timeSpentSeconds": 5400,
        "startTime": "09:15:00",
        "description": "Pairing",
        "issueId": 10001,
        "authorAccountId": "me-id",
        "attributes": [
            {"key": "_Role_", "value": "qa-id"},
            {"key": "_Note_", "value": "n"},
            {"key": "_Account_", "value": "ACC"},
        ],
    }
    assert "remainingEstimateSeconds" not in payload
    assert "billableSeconds" not in payload
    assert body["worklog"]["issue"]["key"] == "PROJ-1"
    assert body["worklog"]["author"] == "Me Myself"
    assert "warning" not in body


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param({"time_spent": "1d"}, "days or weeks", id="days"),
        pytest.param({"time_spent": "soon"}, "Cannot parse duration", id="garbage"),
        pytest.param({"date": "2026-13-01"}, "YYYY-MM-DD", id="bad-date"),
        pytest.param(
            {"attributes": '{"Colour": "red"}'}, "Unknown work attribute", id="attr"
        ),
        pytest.param(
            {"attributes": '{"_Role_": "Manager"}'}, "not an option", id="option"
        ),
        pytest.param({"attributes": "[1]"}, "JSON object", id="not-object"),
        pytest.param({"issue_key": "NOPE-9"}, "not found or not visible", id="issue"),
        pytest.param(
            {"issue_key": "PROJ-1/transitions"}, "PROJ-1/transitions", id="path"
        ),
    ],
)
@pytest.mark.anyio
async def test_create_worklog_refuses_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
    arguments: dict[str, Any],
    message: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    base = {"issue_key": "PROJ-1", "time_spent": "1h", "date": "2026-11-02"}
    result = await _call("tempo_create_worklog", {**base, **arguments}, jira, tempo)
    assert message in _error(result)
    tempo.create_worklog.assert_not_called()


@pytest.mark.anyio
async def test_create_worklog_resolves_a_moved_issue_by_its_old_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    _configure(monkeypatch, tmp_path)
    _payload(
        await _call(
            "tempo_create_worklog",
            {"issue_key": "OLD-7", "time_spent": "1h", "date": "2026-11-02"},
            jira,
            tempo,
        )
    )
    assert tempo.create_worklog.call_args.args[0]["issueId"] == 10002


@pytest.mark.anyio
async def test_create_worklog_warns_when_tempo_stores_another_author(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    _configure(monkeypatch, tmp_path)
    tempo.create_worklog.side_effect = lambda payload: _stored_worklog(
        author={"accountId": "someone-else"}
    )
    body = _payload(
        await _call(
            "tempo_create_worklog",
            {"issue_key": "PROJ-1", "time_spent": "1h", "date": "2026-11-02"},
            jira,
            tempo,
        )
    )
    assert "different people" in body["warning"]


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_update_worklog_merges_onto_current_worklog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    """Tempo's PUT replaces the worklog: unchanged fields must be sent back."""
    _configure(monkeypatch, tmp_path)
    _payload(
        await _call(
            "tempo_update_worklog", {"worklog_id": 501, "time_spent": "2h"}, jira, tempo
        )
    )
    worklog_id, payload = tempo.update_worklog.call_args.args
    assert worklog_id == 501
    assert payload == {
        "authorAccountId": "me-id",
        "startDate": "2026-11-02",
        "timeSpentSeconds": 7200,
        "startTime": "09:00:00",
        "description": "Work",
        "billableSeconds": 7200,
        "attributes": [
            {"key": "_Account_", "value": "ACC"},
            {"key": "_Role_", "value": "dev-id"},
        ],
    }


@pytest.mark.anyio
async def test_update_worklog_keeps_partial_billable_and_removes_null_attribute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    _configure(monkeypatch, tmp_path)
    tempo.get_worklog.return_value = _stored_worklog(billableSeconds=1800)
    _payload(
        await _call(
            "tempo_update_worklog",
            {
                "worklog_id": 501,
                "time_spent": "2h",
                "attributes": '{"_Role_": null}',
                "remaining_estimate": "3h",
            },
            jira,
            tempo,
        )
    )
    payload = tempo.update_worklog.call_args.args[1]
    assert payload["billableSeconds"] == 1800
    assert payload["attributes"] == [{"key": "_Account_", "value": "ACC"}]
    assert payload["remainingEstimateSeconds"] == 10800


@pytest.mark.anyio
async def test_update_worklog_requires_a_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    _configure(monkeypatch, tmp_path)
    result = await _call("tempo_update_worklog", {"worklog_id": 501}, jira, tempo)
    assert "Nothing to update" in _error(result)
    tempo.update_worklog.assert_not_called()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "authors", "issue_ids", "project_ids"),
    [
        pytest.param({}, ["me-id"], None, None, id="default-me"),
        pytest.param({"issue_keys": "PROJ-1"}, None, [10001], None, id="issue-all"),
        pytest.param({"project_keys": "proj"}, None, None, [200], id="project-all"),
        pytest.param(
            {"issue_keys": "PROJ-1", "authors": "me"}, ["me-id"], [10001], None, id="me"
        ),
        pytest.param({"authors": "a1, a2"}, ["a1", "a2"], None, None, id="ids"),
    ],
)
@pytest.mark.anyio
async def test_search_worklogs_filters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
    arguments: dict[str, Any],
    authors: list[str] | None,
    issue_ids: list[int] | None,
    project_ids: list[int] | None,
) -> None:
    _configure(monkeypatch, tmp_path)
    body = _payload(
        await _call(
            "tempo_search_worklogs",
            {"date_from": "2026-11-01", "date_to": "2026-11-30", **arguments},
            jira,
            tempo,
        )
    )
    kwargs = tempo.search_worklogs.call_args.kwargs
    assert kwargs["author_account_ids"] == authors
    assert kwargs["issue_ids"] == issue_ids
    assert kwargs["project_ids"] == project_ids
    assert body["count"] == 1
    assert body["total_time_spent"] == "1h"
    assert body["worklogs"][0]["issue"] == {
        "id": 10001,
        "key": "PROJ-1",
        "summary": "S 10001",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"issue_keys": "PROJ-1,../../myself"}, id="issue-path"),
        pytest.param({"project_keys": "PROJ,a/b"}, id="project-path"),
    ],
)
@pytest.mark.anyio
@pytest.mark.security_regression
async def test_search_worklogs_refuses_non_keys_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
    arguments: dict[str, Any],
) -> None:
    """Keys are interpolated into Jira URL paths, so non-keys never get there."""
    _configure(monkeypatch, tmp_path)
    result = await _call(
        "tempo_search_worklogs",
        {"date_from": "2026-11-01", "date_to": "2026-11-30", **arguments},
        jira,
        tempo,
    )
    assert "Invalid" in _error(result)
    jira.jira.get.assert_not_called()
    jira.jira.post.assert_not_called()
    jira.get_project.assert_not_called()
    tempo.search_worklogs.assert_not_called()


@pytest.mark.anyio
async def test_search_worklogs_rejects_reversed_period(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    _configure(monkeypatch, tmp_path)
    result = await _call(
        "tempo_search_worklogs",
        {"date_from": "2026-11-30", "date_to": "2026-11-01"},
        jira,
        tempo,
    )
    assert "before date_from" in _error(result)


@pytest.mark.anyio
async def test_get_work_attributes_lists_static_list_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    _configure(monkeypatch, tmp_path)
    body = _payload(
        await _call("tempo_get_work_attributes", {"search": "role"}, jira, tempo)
    )
    assert body == [
        {
            "key": "_Role_",
            "name": "Role",
            "type": "STATIC_LIST",
            "required": False,
            "options": [
                {"value": "dev-id", "name": "Developer"},
                {"value": "qa-id", "name": "QA"},
            ],
        }
    ]


@pytest.mark.anyio
async def test_get_timesheet_approval_looks_up_the_period_of_a_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    """Tempo answers only for its own period, so a mid-period day must work."""
    _configure(monkeypatch, tmp_path)
    tempo.get_period.return_value = {"from": "2026-11-01", "to": "2026-11-30"}
    tempo.get_timesheet_approval.return_value = {
        "period": {"from": "2026-11-01", "to": "2026-11-30"},
        "status": {"key": "OPEN"},
        "requiredSeconds": 604800,
        "timeSpentSeconds": 315000,
    }
    body = _payload(
        await _call("tempo_get_timesheet_approval", {"date": "2026-11-17"}, jira, tempo)
    )
    tempo.get_period.assert_called_once_with("2026-11-17")
    tempo.get_timesheet_approval.assert_called_once_with(
        "me-id", "2026-11-01", "2026-11-30"
    )
    assert body["status"] == "OPEN"
    assert body["name"] == "Me Myself"
    assert body["period"] == {"from": "2026-11-01", "to": "2026-11-30"}
    assert body["required"] == "168h"
    assert body["time_spent"] == "87h 30m"


@pytest.mark.anyio
async def test_get_timesheet_approval_defaults_to_today(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jira: MagicMock,
    tempo: MagicMock,
) -> None:
    _configure(monkeypatch, tmp_path)
    tempo.get_period.return_value = {"from": "2026-11-01", "to": "2026-11-30"}
    tempo.get_timesheet_approval.return_value = {"status": {"key": "OPEN"}}
    _payload(await _call("tempo_get_timesheet_approval", {}, jira, tempo))
    tempo.get_period.assert_called_once_with(
        datetime.now(timezone.utc).astimezone().date().isoformat()
    )


# ---------------------------------------------------------------------------
# get_tempo_fetcher
# ---------------------------------------------------------------------------


def _ctx_with(config: TempoConfig | None) -> MagicMock:
    from mcp_atlassian.servers.context import MainAppContext

    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "app_lifespan_context": MainAppContext(full_tempo_config=config)
    }
    return ctx


@pytest.mark.anyio
async def test_get_tempo_fetcher_reuses_one_fetcher_in_stdio() -> None:
    from mcp_atlassian.servers.dependencies import get_tempo_fetcher

    ctx = _ctx_with(TempoConfig(api_token="reuse-token"))
    with patch(
        "mcp_atlassian.servers.dependencies.get_http_request",
        side_effect=RuntimeError("no http"),
    ):
        first = await get_tempo_fetcher(ctx)
        second = await get_tempo_fetcher(ctx)
    assert first is second


@pytest.mark.anyio
async def test_get_tempo_fetcher_requires_configuration() -> None:
    from mcp_atlassian.servers.dependencies import get_tempo_fetcher

    with pytest.raises(ValueError, match="TEMPO_API_TOKEN"):
        await get_tempo_fetcher(_ctx_with(None))


@pytest.mark.parametrize(
    ("auth_type", "allow_fallback", "message"),
    [
        pytest.param("oauth", "true", "per-user", id="per-user-oauth"),
        pytest.param("basic", "true", "per-user", id="per-user-basic"),
        pytest.param(None, None, "ALLOW_GLOBAL_CRED_FALLBACK", id="anonymous-http"),
    ],
)
@pytest.mark.anyio
@pytest.mark.security_regression
async def test_get_tempo_fetcher_refuses_shared_token_over_http(
    monkeypatch: pytest.MonkeyPatch,
    auth_type: str | None,
    allow_fallback: str | None,
    message: str,
) -> None:
    """The operator's Tempo token must not act for other HTTP callers."""
    from mcp_atlassian.servers.dependencies import get_tempo_fetcher

    if allow_fallback is None:
        monkeypatch.delenv("ALLOW_GLOBAL_CRED_FALLBACK", raising=False)
    else:
        monkeypatch.setenv("ALLOW_GLOBAL_CRED_FALLBACK", allow_fallback)
    request = MagicMock()
    request.state.user_atlassian_auth_type = auth_type
    with patch(
        "mcp_atlassian.servers.dependencies.get_http_request", return_value=request
    ):
        with pytest.raises(ValueError, match=message):
            await get_tempo_fetcher(_ctx_with(TempoConfig(api_token="t")))


@pytest.mark.anyio
async def test_get_tempo_fetcher_allows_opted_in_single_user_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_atlassian.servers.dependencies import get_tempo_fetcher

    monkeypatch.setenv("ALLOW_GLOBAL_CRED_FALLBACK", "true")
    request = MagicMock()
    request.state.user_atlassian_auth_type = None
    with patch(
        "mcp_atlassian.servers.dependencies.get_http_request", return_value=request
    ):
        fetcher = await get_tempo_fetcher(_ctx_with(TempoConfig(api_token="t2")))
    assert isinstance(fetcher, TempoFetcher)
