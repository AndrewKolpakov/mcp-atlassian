"""Tempo worklog and work attribute models."""

from typing import Any

from ...tempo.utils import format_duration
from ..base import ApiModel


class TempoWorklog(ApiModel):
    """A Tempo worklog, with the Jira issue key resolved where known."""

    id: int
    issue_id: int | None = None
    issue_key: str | None = None
    issue_summary: str | None = None
    author_account_id: str | None = None
    start_date: str | None = None
    start_time: str | None = None
    time_spent_seconds: int = 0
    billable_seconds: int | None = None
    description: str | None = None
    attributes: dict[str, str | None] = {}
    created: str | None = None
    updated: str | None = None

    @classmethod
    def from_api_response(cls, data: dict[str, Any], **kwargs: Any) -> "TempoWorklog":
        """Build a worklog from a Tempo v4 worklog object.

        Args:
            data: The Tempo worklog.
            **kwargs: ``issues`` maps a numeric issue id to
                ``{"key": ..., "summary": ...}``.

        Returns:
            The worklog model.
        """
        issues: dict[int, dict[str, str]] = kwargs.get("issues") or {}
        issue_id = (data.get("issue") or {}).get("id")
        issue = issues.get(issue_id, {}) if issue_id is not None else {}
        values = (data.get("attributes") or {}).get("values") or []
        return cls(
            id=data["tempoWorklogId"],
            issue_id=issue_id,
            issue_key=issue.get("key"),
            issue_summary=issue.get("summary"),
            author_account_id=(data.get("author") or {}).get("accountId"),
            start_date=data.get("startDate"),
            start_time=data.get("startTime"),
            time_spent_seconds=data.get("timeSpentSeconds") or 0,
            billable_seconds=data.get("billableSeconds"),
            description=data.get("description") or None,
            attributes={v["key"]: v.get("value") for v in values if "key" in v},
            created=data.get("createdAt"),
            updated=data.get("updatedAt"),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to the dictionary the tools return."""
        result: dict[str, Any] = {
            "id": self.id,
            "issue": {
                "id": self.issue_id,
                "key": self.issue_key,
                "summary": self.issue_summary,
            },
            "author_account_id": self.author_account_id,
            "start_date": self.start_date,
            "start_time": self.start_time,
            "time_spent": format_duration(self.time_spent_seconds),
            "time_spent_seconds": self.time_spent_seconds,
            "billable_seconds": self.billable_seconds,
            "description": self.description,
            "attributes": self.attributes,
            "created": self.created,
            "updated": self.updated,
        }
        result["issue"] = {k: v for k, v in result["issue"].items() if v is not None}
        return {k: v for k, v in result.items() if v not in (None, {}, "")}


def simplify_work_attribute(data: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Tempo work attribute definition to what a caller needs.

    For STATIC_LIST attributes the stored ``value`` is an immutable id and
    ``names`` maps it to the label shown in the UI, so the options are
    returned as value/name pairs.
    """
    result: dict[str, Any] = {
        "key": data.get("key"),
        "name": data.get("name"),
        "type": data.get("type"),
        "required": bool(data.get("required")),
    }
    values = data.get("values") or []
    if values:
        names = data.get("names") or {}
        result["options"] = [{"value": v, "name": names.get(v, v)} for v in values]
    return result
