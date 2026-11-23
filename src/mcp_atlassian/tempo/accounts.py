"""Module for Tempo account operations."""

from typing import Any

from .client import TempoClient


class AccountsMixin(TempoClient):
    """Mixin for Tempo account operations."""

    def search_accounts(
        self,
        *,
        keys: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Search Tempo accounts (``POST /4/accounts/search``).

        Args:
            keys: Account keys to match.
            statuses: Account statuses (OPEN, CLOSED, ARCHIVED).
            limit: Maximum number of accounts to return.

        Returns:
            The raw accounts, and whether more matched beyond ``limit``.
        """
        body: dict[str, Any] = {}
        if keys:
            body["keys"] = keys
        if statuses:
            body["statuses"] = statuses
        return self._paginate("POST", "/accounts/search", limit=limit, json=body)
