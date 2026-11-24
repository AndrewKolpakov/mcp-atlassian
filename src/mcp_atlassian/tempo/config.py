"""Configuration module for the Tempo Cloud REST API."""

import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..utils.env import is_env_ssl_verify
from ..utils.urls import is_atlassian_cloud_url

logger = logging.getLogger("mcp-atlassian.tempo.config")

DEFAULT_TEMPO_URL = "https://api.tempo.io"
DEFAULT_TIMEOUT = 75


@dataclass(frozen=True)
class TempoConfig:
    """Tempo Cloud API configuration.

    Tempo Cloud is a separate service from Jira: it has its own host and its
    own bearer token (Tempo > Settings > Data Access > API integration), and
    the token acts with the permissions of the user who created it.

    The regional hosts (``https://api.eu.tempo.io``, ``https://api.us.tempo.io``)
    answer an authorization error when the data lives in the other region, so
    a 401 against a regional ``TEMPO_URL`` can mean "wrong region" as much as
    "wrong token".

    Tempo belongs to one Jira Cloud site, and its tools resolve issue keys and
    people through that site. By default that is ``JIRA_URL``; the
    ``TEMPO_JIRA_*`` variables name a different site, so the Jira tools can
    work on one site while time is logged in another site's Tempo.
    """

    api_token: str = field(repr=False)
    url: str = DEFAULT_TEMPO_URL
    ssl_verify: bool = True
    timeout: int = DEFAULT_TIMEOUT
    jira_url: str | None = None
    jira_username: str | None = None
    jira_api_token: str | None = field(default=None, repr=False)

    @property
    def has_own_jira(self) -> bool:
        """Whether Tempo resolves keys through its own Jira site."""
        return self.jira_url is not None

    @classmethod
    def from_env(cls) -> "TempoConfig":
        """Create the configuration from environment variables.

        Returns:
            TempoConfig with values from the environment.

        Raises:
            ValueError: If TEMPO_API_TOKEN is missing, TEMPO_URL is not an
                https URL, or the TEMPO_JIRA_* variables are incomplete or do
                not name a Jira Cloud site.
        """
        api_token = (os.getenv("TEMPO_API_TOKEN") or "").strip()
        if not api_token:
            raise ValueError("Missing required TEMPO_API_TOKEN environment variable")

        url = (os.getenv("TEMPO_URL") or DEFAULT_TEMPO_URL).strip().rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"TEMPO_URL must be an https URL, got: {url!r}")

        timeout = DEFAULT_TIMEOUT
        raw_timeout = os.getenv("TEMPO_TIMEOUT")
        if raw_timeout:
            try:
                timeout = int(raw_timeout)
            except ValueError:
                logger.warning(
                    f"Invalid TEMPO_TIMEOUT {raw_timeout!r}, using {DEFAULT_TIMEOUT}"
                )

        jira_url, jira_username, jira_api_token = _tempo_jira_from_env()

        return cls(
            api_token=api_token,
            url=url,
            ssl_verify=is_env_ssl_verify("TEMPO_SSL_VERIFY"),
            timeout=timeout,
            jira_url=jira_url,
            jira_username=jira_username,
            jira_api_token=jira_api_token,
        )


def _tempo_jira_from_env() -> tuple[str | None, str | None, str | None]:
    """Read the optional TEMPO_JIRA_URL / _USERNAME / _API_TOKEN triple.

    Returns:
        The site URL, user name and API token, or three Nones when none of
        the variables is set.

    Raises:
        ValueError: If only some are set, or the URL is not a Jira Cloud site.
    """
    values = {
        name: (os.getenv(name) or "").strip()
        for name in ("TEMPO_JIRA_URL", "TEMPO_JIRA_USERNAME", "TEMPO_JIRA_API_TOKEN")
    }
    if not any(values.values()):
        return None, None, None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(
            "TEMPO_JIRA_URL, TEMPO_JIRA_USERNAME and TEMPO_JIRA_API_TOKEN go "
            f"together; missing: {', '.join(missing)}"
        )
    jira_url = values["TEMPO_JIRA_URL"].rstrip("/")
    if urlparse(jira_url).scheme != "https" or not is_atlassian_cloud_url(jira_url):
        raise ValueError(
            f"TEMPO_JIRA_URL must be an https Jira Cloud site, got: {jira_url!r}"
        )
    return jira_url, values["TEMPO_JIRA_USERNAME"], values["TEMPO_JIRA_API_TOKEN"]
