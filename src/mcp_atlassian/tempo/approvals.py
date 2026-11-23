"""Module for Tempo timesheet approval operations (read only)."""

from typing import Any
from urllib.parse import quote

from .client import TempoClient


class ApprovalsMixin(TempoClient):
    """Mixin for reading Tempo timesheet approvals.

    Submitting, approving, rejecting and reopening are deliberately not
    exposed: they change another person's timesheet state.
    """

    def get_timesheet_approval(
        self, account_id: str, date_from: str, date_to: str
    ) -> dict[str, Any]:
        """Get a user's timesheet approval for a period.

        Args:
            account_id: Atlassian account id of the timesheet's owner.
            date_from: First day of the period (YYYY-MM-DD).
            date_to: Last day of the period (YYYY-MM-DD).

        Returns:
            The raw approval (status, period, required/spent seconds, reviewer).
        """
        return self._request(
            "GET",
            f"/timesheet-approvals/user/{quote(account_id, safe='')}",
            params={"from": date_from, "to": date_to},
        )
