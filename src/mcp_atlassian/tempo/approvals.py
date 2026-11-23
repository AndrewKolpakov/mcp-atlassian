"""Module for Tempo timesheet approval operations (read only)."""

from typing import Any
from urllib.parse import quote

from .client import TempoClient


class ApprovalsMixin(TempoClient):
    """Mixin for reading Tempo timesheet approvals.

    Submitting, approving, rejecting and reopening are deliberately not
    exposed: they change another person's timesheet state.
    """

    def get_period(self, day: str) -> dict[str, str]:
        """Get the approval period that contains a day (``GET /4/periods``).

        Tempo answers an approval request only for one of its own periods
        (a calendar month, a week, ... as the site configures them), so the
        boundaries are looked up rather than guessed.

        Args:
            day: Any day of the period (YYYY-MM-DD).

        Returns:
            The period as ``{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}``.

        Raises:
            ValueError: If Tempo has no period containing the day.
        """
        body = self._request("GET", "/periods", params={"from": day, "to": day}) or {}
        for period in body.get("periods") or []:
            if period.get("from", "") <= day <= period.get("to", ""):
                return {"from": period["from"], "to": period["to"]}
        msg = f"Tempo has no timesheet period containing {day}."
        raise ValueError(msg)

    def get_timesheet_approval(
        self, account_id: str, date_from: str, date_to: str
    ) -> dict[str, Any]:
        """Get a user's timesheet approval for a period.

        Args:
            account_id: Atlassian account id of the timesheet's owner.
            date_from: First day of the period (YYYY-MM-DD); must be a
                Tempo period boundary, see ``get_period``.
            date_to: Last day of the period (YYYY-MM-DD).

        Returns:
            The raw approval (status, period, required/spent seconds, reviewer).
        """
        return self._request(
            "GET",
            f"/timesheet-approvals/user/{quote(account_id, safe='')}",
            params={"from": date_from, "to": date_to},
        )
