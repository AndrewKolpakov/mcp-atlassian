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
        with pytest.raises(MCPAtlassianAuthenticationError, match="region"):
            tempo.get_worklog(1)

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

    def test_approval_account_id_is_path_escaped(self, tempo: TempoFetcher) -> None:
        tempo.session.request.return_value = _response(200, {})
        tempo.get_timesheet_approval("557058:abc/../x", "2026-11-01", "2026-11-30")
        url = tempo.session.request.call_args.args[1]
        assert url.endswith("/timesheet-approvals/user/557058%3Aabc%2F..%2Fx")
