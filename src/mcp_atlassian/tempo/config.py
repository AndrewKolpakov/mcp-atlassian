"""Configuration module for the Tempo Cloud REST API."""

import logging
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from ..utils.env import is_env_ssl_verify

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
    """

    api_token: str
    url: str = DEFAULT_TEMPO_URL
    ssl_verify: bool = True
    timeout: int = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> "TempoConfig":
        """Create the configuration from environment variables.

        Returns:
            TempoConfig with values from the environment.

        Raises:
            ValueError: If TEMPO_API_TOKEN is missing or TEMPO_URL is not an
                https URL.
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

        return cls(
            api_token=api_token,
            url=url,
            ssl_verify=is_env_ssl_verify("TEMPO_SSL_VERIFY"),
            timeout=timeout,
        )
