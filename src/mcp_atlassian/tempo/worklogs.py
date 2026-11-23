"""Module for Tempo worklog operations."""

import logging
from typing import Any

from .client import TempoClient

logger = logging.getLogger("mcp-atlassian.tempo.worklogs")


class WorklogsMixin(TempoClient):
    """Mixin for Tempo worklog operations.

    Tempo v4 identifies issues and projects by numeric id, never by key; the
    caller resolves keys through Jira.
    """

    def search_worklogs(
        self,
        *,
        author_account_ids: list[str] | None = None,
        issue_ids: list[int] | None = None,
        project_ids: list[int] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        updated_from: str | None = None,
        limit: int = 200,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Search worklogs (``POST /4/worklogs/search``).

        Args:
            author_account_ids: Atlassian account ids of the authors.
            issue_ids: Numeric Jira issue ids.
            project_ids: Numeric Jira project ids.
            date_from: First day, inclusive (YYYY-MM-DD).
            date_to: Last day, inclusive (YYYY-MM-DD).
            updated_from: Only worklogs updated since this date or datetime.
            limit: Maximum number of worklogs to return.

        Returns:
            The raw worklogs, and whether more matched beyond ``limit``.
        """
        body: dict[str, Any] = {}
        if author_account_ids:
            body["authorIds"] = author_account_ids
        if issue_ids:
            body["issueIds"] = issue_ids
        if project_ids:
            body["projectIds"] = project_ids
        if date_from:
            body["from"] = date_from
        if date_to:
            body["to"] = date_to
        if updated_from:
            body["updatedFrom"] = updated_from
        return self._paginate("POST", "/worklogs/search", limit=limit, json=body)

    def get_worklog(self, worklog_id: int) -> dict[str, Any]:
        """Get one worklog by its Tempo worklog id."""
        return self._request("GET", f"/worklogs/{int(worklog_id)}")

    def create_worklog(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a worklog (``POST /4/worklogs``).

        Args:
            payload: The Tempo worklog body (authorAccountId, issueId,
                startDate, timeSpentSeconds, and optional fields).

        Returns:
            The created worklog as Tempo stored it.
        """
        return self._request("POST", "/worklogs", json=payload)

    def update_worklog(
        self, worklog_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace a worklog (``PUT /4/worklogs/{id}``).

        Tempo's PUT replaces the whole worklog: a field left out of
        ``payload`` is cleared, not kept. Callers merge onto the current
        worklog first.

        Args:
            worklog_id: The Tempo worklog id.
            payload: The complete worklog body.

        Returns:
            The updated worklog as Tempo stored it.
        """
        return self._request("PUT", f"/worklogs/{int(worklog_id)}", json=payload)
