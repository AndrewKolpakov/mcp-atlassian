"""Tempo Cloud API module for mcp_atlassian.

Tempo Cloud (``api.tempo.io``) is a separate service from Jira with its own
bearer token. Issues and projects are referenced by numeric Jira id there, so
the server layer pairs this client with a Jira fetcher to translate keys.
"""

from .accounts import AccountsMixin
from .approvals import ApprovalsMixin
from .client import TempoApiError, TempoClient
from .config import TempoConfig
from .teams import TeamsMixin
from .work_attributes import WorkAttributesMixin
from .worklogs import WorklogsMixin


class TempoFetcher(
    WorklogsMixin,
    WorkAttributesMixin,
    AccountsMixin,
    TeamsMixin,
    ApprovalsMixin,
):
    """The main Tempo client class providing access to all Tempo operations."""

    pass


__all__ = ["TempoApiError", "TempoClient", "TempoConfig", "TempoFetcher"]
