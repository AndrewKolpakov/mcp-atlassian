"""Module for Tempo work attribute operations."""

from typing import Any

from .client import TempoClient

# Work attribute catalogs are small; one page covers any real configuration.
_CATALOG_LIMIT = 1000


class WorkAttributesMixin(TempoClient):
    """Mixin for Tempo work attribute operations."""

    _work_attributes_cache: list[dict[str, Any]] | None = None

    def get_work_attributes(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """List the work attributes configured in Tempo.

        The catalog is cached for the lifetime of this client, since every
        worklog write validates against it.

        Args:
            refresh: Re-read the catalog instead of using the cached copy.

        Returns:
            The raw work attribute definitions (key, name, type, required,
            values/names for STATIC_LIST).
        """
        if self._work_attributes_cache is None or refresh:
            results, _ = self._paginate("GET", "/work-attributes", limit=_CATALOG_LIMIT)
            self._work_attributes_cache = results
        return self._work_attributes_cache
