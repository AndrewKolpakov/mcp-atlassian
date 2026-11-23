"""Module for Tempo team operations."""

from typing import Any

from .client import TempoClient


class TeamsMixin(TempoClient):
    """Mixin for Tempo team operations."""

    def get_teams(
        self, *, name: str | None = None, limit: int = 200
    ) -> tuple[list[dict[str, Any]], bool]:
        """List Tempo teams (``GET /4/teams``).

        Args:
            name: Optional team name filter.
            limit: Maximum number of teams to return.

        Returns:
            The raw teams, and whether more matched beyond ``limit``.
        """
        params = {"name": name} if name else None
        return self._paginate("GET", "/teams", limit=limit, params=params)

    def get_team_members(
        self, team_id: int, *, limit: int = 500
    ) -> tuple[list[dict[str, Any]], bool]:
        """List the members of a Tempo team (``GET /4/teams/{id}/members``).

        Args:
            team_id: The Tempo team id.
            limit: Maximum number of members to return.

        Returns:
            The raw memberships, and whether more existed beyond ``limit``.
        """
        return self._paginate("GET", f"/teams/{int(team_id)}/members", limit=limit)
