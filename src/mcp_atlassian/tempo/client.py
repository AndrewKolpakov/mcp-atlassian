"""Base client for the Tempo Cloud REST API (v4)."""

import logging
from typing import Any

import requests

from ..exceptions import MCPAtlassianAuthenticationError
from ..utils.user_agent import get_default_user_agent
from .config import TempoConfig

logger = logging.getLogger("mcp-atlassian.tempo.client")

API_PREFIX = "/4"
# Tempo accepts up to 5000 records per call; smaller pages keep a single
# response, and the memory it costs, bounded.
PAGE_SIZE = 1000
# Tempo's 401 text when the token is accepted but its own call to Jira, made
# as the token's owner, fails ("We had a problem trying to communicate with
# Atlassian ..."). Endpoints that need no Jira permission check still work.
_ATLASSIAN_LINK_FAILURE = "communicate with atlassian"


class TempoApiError(ValueError):
    """Raised when Tempo answers a request with an error status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _error_messages(response: requests.Response) -> str:
    """Extract Tempo's error messages from a response body.

    Tempo answers ``{"errors": [{"message": "..."}]}``; anything else falls
    back to the raw body, truncated.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list):
            messages = [
                str(error.get("message", error))
                if isinstance(error, dict)
                else str(error)
                for error in errors
            ]
            return "; ".join(messages)
        if "message" in body:
            return str(body["message"])
    return str(body)[:500]


class TempoClient:
    """Base client for Tempo Cloud API interactions."""

    config: TempoConfig

    def __init__(self, config: TempoConfig | None = None) -> None:
        """Initialize the Tempo client.

        Args:
            config: Optional configuration object (otherwise uses env vars).

        Raises:
            ValueError: If the configuration is invalid.
        """
        self.config = config or TempoConfig.from_env()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.config.api_token}",
                "Accept": "application/json",
                "User-Agent": get_default_user_agent(),
            }
        )
        self.session.verify = self.config.ssl_verify

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Send a request to the Tempo API and return the decoded JSON body.

        Args:
            method: HTTP method.
            path: Path below the API version prefix, e.g. ``/worklogs/search``.
            params: Optional query parameters.
            json: Optional JSON body.

        Returns:
            The decoded JSON body, or None for an empty response.

        Raises:
            MCPAtlassianAuthenticationError: On 401.
            TempoApiError: On any other error status.
        """
        url = f"{self.config.url}{API_PREFIX}{path}"
        response = self.session.request(
            method,
            url,
            params=params,
            json=json,
            timeout=self.config.timeout,
        )
        if response.status_code == 401:
            detail = _error_messages(response)
            msg = f"Tempo answered 401 on {method} {path}"
            msg += f": {detail}" if detail else "."
            if _ATLASSIAN_LINK_FAILURE in detail.lower():
                # The token was accepted: Tempo failed calling Jira as its
                # owner, which no token or region change fixes.
                msg += (
                    " The token is valid, but Tempo could not reach Jira on "
                    "its owner's behalf. Open Tempo in Jira as that user and "
                    "grant any access it asks for, or create a new token; if "
                    "that does not help, it is for Tempo support."
                )
            else:
                msg += (
                    " Check TEMPO_API_TOKEN, that it has not expired, and that "
                    "TEMPO_URL points at the region your Tempo data lives in "
                    f"(currently {self.config.url})."
                )
            raise MCPAtlassianAuthenticationError(msg)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            hint = f" Retry after {retry_after}s." if retry_after else ""
            msg = f"Tempo rate limit reached (429).{hint}"
            raise TempoApiError(429, msg)
        if not response.ok:
            detail = _error_messages(response)
            msg = (
                f"Tempo API error {response.status_code} on {method} {path}: "
                f"{detail or '(no details)'}"
            )
            if response.status_code == 403:
                msg += (
                    " (The token may lack the scope or permission for this, or "
                    f"TEMPO_URL {self.config.url} may be the wrong region.)"
                )
            raise TempoApiError(response.status_code, msg)
        if not response.content:
            return None
        return response.json()

    def _paginate(
        self,
        method: str,
        path: str,
        limit: int,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Collect up to ``limit`` results from a paged Tempo endpoint.

        Args:
            method: HTTP method (search endpoints are POST).
            path: Path below the API version prefix.
            limit: Maximum number of results to return.
            params: Optional query parameters (offset/limit are managed here).
            json: Optional JSON body, re-sent with every page.

        Returns:
            The results, and whether more were available beyond ``limit``.
        """
        results: list[dict[str, Any]] = []
        offset = 0
        while len(results) < limit:
            page_params = dict(params or {})
            page_params["offset"] = offset
            page_params["limit"] = min(PAGE_SIZE, limit - len(results))
            body = self._request(method, path, params=page_params, json=json) or {}
            page = body.get("results") or []
            results.extend(page)
            metadata = body.get("metadata") or {}
            if not page or not metadata.get("next"):
                return results, False
            offset += len(page)
        return results, True
