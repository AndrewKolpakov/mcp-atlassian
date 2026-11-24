"""Tests for the Tempo Cloud client and its configuration."""

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.tempo import TempoApiError, TempoConfig, TempoFetcher


def _response(status: int, body: Any = None, headers: dict | None = None) -> MagicMock:
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.ok = status < 400
    response.headers = headers or {}
    if body is None:
        response.content = b""
        response.json.side_effect = ValueError("no body")
        response.text = ""
    else:
        response.content = b"x"
        response.json.return_value = body
        response.text = str(body)
    return response


@pytest.fixture
def tempo() -> TempoFetcher:
    fetcher = TempoFetcher(TempoConfig(api_token="secret-token"))
    fetcher.session = MagicMock()
    return fetcher


class TestTempoConfig:
    def test_from_env_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPO_API_TOKEN", " tok ")
        monkeypatch.delenv("TEMPO_URL", raising=False)
        config = TempoConfig.from_env()
        assert config.api_token == "tok"
        assert config.url == "https://api.tempo.io"
        assert config.ssl_verify is True

    def test_from_env_regional_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPO_API_TOKEN", "tok")
        monkeypatch.setenv("TEMPO_URL", "https://api.eu.tempo.io/")
        assert TempoConfig.from_env().url == "https://api.eu.tempo.io"

    def test_from_env_requires_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEMPO_API_TOKEN", raising=False)
        with pytest.raises(ValueError, match="TEMPO_API_TOKEN"):
            TempoConfig.from_env()

    def test_from_env_refuses_plain_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bearer token must never travel over plain HTTP."""
        monkeypatch.setenv("TEMPO_API_TOKEN", "tok")
        monkeypatch.setenv("TEMPO_URL", "http://api.tempo.io")
        with pytest.raises(ValueError, match="https"):
            TempoConfig.from_env()

    def test_from_env_tempo_jira_site(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPO_API_TOKEN", "tempo-secret")
        monkeypatch.setenv("TEMPO_JIRA_URL", "https://tempo-site.atlassian.net/")
        monkeypatch.setenv("TEMPO_JIRA_USERNAME", "me@example.com")
        monkeypatch.setenv("TEMPO_JIRA_API_TOKEN", "jira-secret")
        config = TempoConfig.from_env()
        assert config.has_own_jira
        assert config.jira_url == "https://tempo-site.atlassian.net"
        assert "jira-secret" not in repr(config)
        assert "tempo-secret" not in repr(config)

    def test_from_env_without_tempo_jira_site(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPO_API_TOKEN", "tok")
        for name in ("TEMPO_JIRA_URL", "TEMPO_JIRA_USERNAME", "TEMPO_JIRA_API_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        assert not TempoConfig.from_env().has_own_jira

    @pytest.mark.parametrize(
        ("env", "message"),
        [
            pytest.param(
                {"TEMPO_JIRA_URL": "https://tempo-site.atlassian.net"},
                "TEMPO_JIRA_USERNAME, TEMPO_JIRA_API_TOKEN",
                id="incomplete",
            ),
            pytest.param(
                {
                    "TEMPO_JIRA_URL": "https://jira.example.test",
                    "TEMPO_JIRA_USERNAME": "me",
                    "TEMPO_JIRA_API_TOKEN": "t",
                },
                "Jira Cloud",
                id="server-dc",
            ),
            pytest.param(
                {
                    "TEMPO_JIRA_URL": "http://tempo-site.atlassian.net",
                    "TEMPO_JIRA_USERNAME": "me",
                    "TEMPO_JIRA_API_TOKEN": "t",
                },
                "https",
                id="plain-http",
            ),
        ],
    )
    def test_from_env_refuses_bad_tempo_jira_site(
        self, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
    ) -> None:
        monkeypatch.setenv("TEMPO_API_TOKEN", "tok")
        for name in ("TEMPO_JIRA_URL", "TEMPO_JIRA_USERNAME", "TEMPO_JIRA_API_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=message):
            TempoConfig.from_env()

    def test_config_is_hashable(self) -> None:
        assert hash(TempoConfig(api_token="a")) == hash(TempoConfig(api_token="a"))


class TestTempoClient:
    def test_session_sends_bearer_token(self) -> None:
        fetcher = TempoFetcher(TempoConfig(api_token="secret-token"))
        assert fetcher.session.headers["Authorization"] == "Bearer secret-token"

    def test_request_targets_v4_under_configured_host(
        self, tempo: TempoFetcher
    ) -> None:
        tempo.session.request.return_value = _response(200, {"tempoWorklogId": 7})
        assert tempo.get_worklog(7) == {"tempoWorklogId": 7}
        args, kwargs = tempo.session.request.call_args
        assert args == ("GET", "https://api.tempo.io/4/worklogs/7")

    def test_401_names_token_and_region(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(401, {"errors": []})
        with pytest.raises(MCPAtlassianAuthenticationError, match="region") as exc:
            tempo.get_worklog(1)
        assert "GET /worklogs/1." in str(exc.value)

    def test_401_keeps_tempo_text_and_names_the_jira_link(
        self, tempo: TempoFetcher
    ) -> None:
        """A 401 Tempo raises while calling Jira is not a bad token or region."""
        text = (
            "We had a problem trying to communicate with Atlassian. Please "
            "try again later."
        )
        tempo.session.request.return_value = _response(
            401, {"errors": [{"message": text}]}
        )
        with pytest.raises(MCPAtlassianAuthenticationError) as exc:
            tempo.search_worklogs()
        message = str(exc.value)
        assert text in message
        assert "token is valid" in message
        assert "TEMPO_URL" not in message

    def test_429_reports_retry_after(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(
            429, {}, headers={"Retry-After": "30"}
        )
        with pytest.raises(TempoApiError, match="30s") as excinfo:
            tempo.get_worklog(1)
        assert excinfo.value.status_code == 429

    def test_error_body_messages_are_surfaced(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(
            400,
            {"errors": [{"message": "Worklog must have an account"}, {"message": "x"}]},
        )
        with pytest.raises(TempoApiError, match="must have an account; x"):
            tempo.create_worklog({})

    def test_403_hints_scope_and_region(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(403, {"message": "Forbidden"})
        with pytest.raises(TempoApiError, match="wrong region"):
            tempo.get_worklog(1)

    def test_token_never_appears_in_errors(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(403, {"message": "Forbidden"})
        with pytest.raises(TempoApiError) as excinfo:
            tempo.get_worklog(1)
        assert "secret-token" not in str(excinfo.value)

    def test_pagination_follows_next_and_reports_more(
        self, tempo: TempoFetcher
    ) -> None:
        tempo.session.request.side_effect = [
            _response(
                200, {"results": [{"i": 1}, {"i": 2}], "metadata": {"next": "u"}}
            ),
            _response(200, {"results": [{"i": 3}], "metadata": {"next": "u"}}),
        ]
        results, has_more = tempo._paginate(
            "POST", "/worklogs/search", limit=3, json={}
        )
        assert [r["i"] for r in results] == [1, 2, 3]
        assert has_more is True
        offsets = [
            c.kwargs["params"]["offset"] for c in tempo.session.request.call_args_list
        ]
        assert offsets == [0, 2]
        assert tempo.session.request.call_args_list[1].kwargs["params"]["limit"] == 1

    def test_pagination_stops_without_next(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(
            200, {"results": [{"i": 1}], "metadata": {}}
        )
        results, has_more = tempo._paginate("GET", "/teams", limit=50)
        assert len(results) == 1
        assert has_more is False
        assert tempo.session.request.call_count == 1

    def test_search_worklogs_builds_body(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(
            200, {"results": [], "metadata": {}}
        )
        tempo.search_worklogs(
            author_account_ids=["me"],
            issue_ids=[10001],
            date_from="2026-11-01",
            date_to="2026-11-30",
        )
        kwargs = tempo.session.request.call_args.kwargs
        assert kwargs["json"] == {
            "authorIds": ["me"],
            "issueIds": [10001],
            "from": "2026-11-01",
            "to": "2026-11-30",
        }

    def test_work_attribute_catalog_is_cached(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(
            200, {"results": [{"key": "_A_"}], "metadata": {}}
        )
        tempo.get_work_attributes()
        tempo.get_work_attributes()
        assert tempo.session.request.call_count == 1
        tempo.get_work_attributes(refresh=True)
        assert tempo.session.request.call_count == 2

    def test_get_period_returns_the_period_containing_the_day(
        self, tempo: TempoFetcher
    ) -> None:
        tempo.session.request.return_value = _response(
            200, {"periods": [{"from": "2026-11-01", "to": "2026-11-30"}]}
        )
        assert tempo.get_period("2026-11-17") == {
            "from": "2026-11-01",
            "to": "2026-11-30",
        }
        kwargs = tempo.session.request.call_args.kwargs
        assert kwargs["params"] == {"from": "2026-11-17", "to": "2026-11-17"}

    def test_get_period_refuses_when_tempo_has_none(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(200, {"periods": []})
        with pytest.raises(ValueError, match="no timesheet period"):
            tempo.get_period("2026-11-17")

    def test_approval_account_id_is_path_escaped(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(200, {})
        tempo.get_timesheet_approval("557058:abc/../x", "2026-11-01", "2026-11-30")
        url = tempo.session.request.call_args.args[1]
        assert url.endswith("/timesheet-approvals/user/557058%3Aabc%2F..%2Fx")
