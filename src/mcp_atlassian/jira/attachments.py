"""Attachment operations for Jira API."""

import logging
import mimetypes
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from ..models.jira import JiraAttachment
from ..utils.io import validate_safe_path
from ..utils.media import ATTACHMENT_MAX_BYTES
from .client import JiraClient
from .protocols import AttachmentsOperationsProto

# Configure logging
logger = logging.getLogger("mcp-jira")


class AttachmentsMixin(JiraClient, AttachmentsOperationsProto):
    """Mixin for Jira attachment operations."""

    def download_attachment(self, url: str, target_path: str) -> bool:
        """
        Download a Jira attachment to the specified path.

        Args:
            url: The URL of the attachment to download
            target_path: The path where the attachment should be saved

        Returns:
            True if successful, False otherwise
        """
        if not url:
            logger.error("No URL provided for attachment download")
            return False

        try:
            # Convert to absolute path if relative
            if not os.path.isabs(target_path):
                target_path = os.path.abspath(target_path)

            # Guard against path traversal (resolves symlinks)
            validate_safe_path(target_path)

            # Do not write into the working-directory root itself: that is where
            # Python resolves imports first, so a download landing there could
            # overwrite an importable module and gain code execution (GHSA-6vmq).
            # Attachments must be saved into a subdirectory.
            resolved = Path(target_path).resolve()
            if resolved.parent == Path(os.getcwd()).resolve():
                logger.error(
                    f"Refusing to download into the working-directory root: {target_path}"
                )
                return False

            logger.info(f"Downloading attachment from {url} to {target_path}")

            # Create the directory if it doesn't exist
            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            # Use the Jira session to download the file
            response = self.jira._session.get(url, stream=True)
            response.raise_for_status()

            # Write the file to disk
            with open(target_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Verify the file was created
            if os.path.exists(target_path):
                file_size = os.path.getsize(target_path)
                logger.info(
                    f"Successfully downloaded attachment to {target_path} (size: {file_size} bytes)"
                )
                return True
            else:
                logger.error(f"File was not created at {target_path}")
                return False

        except Exception as e:
            logger.error(f"Error downloading attachment: {str(e)}")
            return False

    def fetch_attachment_content(self, url: str) -> bytes | None:
        """
        Fetch attachment content into memory.

        Args:
            url: The URL of the attachment to download

        Returns:
            The raw bytes of the attachment, or None on failure
        """
        if not url:
            logger.error("No URL provided for attachment fetch")
            return None

        try:
            logger.info(f"Fetching attachment from {url}")
            response = self.jira._session.get(url, stream=True)
            response.raise_for_status()

            chunks: list[bytes] = []
            for chunk in response.iter_content(chunk_size=8192):
                chunks.append(chunk)

            data = b"".join(chunks)
            logger.info(
                f"Successfully fetched attachment from {url} (size: {len(data)} bytes)"
            )
            return data

        except Exception as e:
            logger.error(f"Error fetching attachment: {str(e)}")
            return None

    def get_issue_attachments(self, issue_key: str) -> list[JiraAttachment]:
        """Return attachment metadata for a Jira issue without downloading.

        Args:
            issue_key: The Jira issue key (e.g., 'PROJ-123').

        Returns:
            A list of JiraAttachment instances.
        """
        logger.info(f"Fetching attachment metadata for {issue_key}")
        issue_data = self.jira.issue(issue_key, fields="attachment")

        if not isinstance(issue_data, dict):
            msg = f"Unexpected return value type from `jira.issue`: {type(issue_data)}"
            logger.error(msg)
            raise TypeError(msg)

        if "fields" not in issue_data:
            logger.error(f"Could not retrieve issue {issue_key}")
            return []

        attachment_data = issue_data.get("fields", {}).get("attachment", [])
        return [
            JiraAttachment.from_api_response(item)
            for item in attachment_data
            if isinstance(item, dict)
        ]

    def get_issue_attachment_contents(self, issue_key: str) -> dict[str, Any]:
        """
        Fetch all attachment contents for a Jira issue into memory.

        Unlike download_issue_attachments, this method does not write to
        the filesystem.  Each attachment is returned as raw bytes so the
        caller (e.g. the MCP server layer) can serialise them however it
        needs (base64 embedded resources, etc.).

        Args:
            issue_key: The Jira issue key (e.g., 'PROJ-123')

        Returns:
            A dictionary with:
                success (bool)
                issue_key (str)
                total (int)
                attachments (list[dict]): each dict has 'filename',
                    'content_type', 'size', and 'data' (bytes)
                failed (list[dict]): each dict has 'filename' and 'error'
        """
        logger.info(f"Fetching attachment contents for {issue_key}")

        issue_data = self.jira.issue(issue_key, fields="attachment")

        if not isinstance(issue_data, dict):
            msg = f"Unexpected return value type from `jira.issue`: {type(issue_data)}"
            logger.error(msg)
            raise TypeError(msg)

        if "fields" not in issue_data:
            logger.error(f"Could not retrieve issue {issue_key}")
            return {
                "success": False,
                "error": f"Could not retrieve issue {issue_key}",
            }

        attachment_data = issue_data.get("fields", {}).get("attachment", [])

        if not attachment_data:
            return {
                "success": True,
                "message": f"No attachments found for issue {issue_key}",
                "attachments": [],
                "failed": [],
            }

        attachments: list[JiraAttachment] = []
        for item in attachment_data:
            if isinstance(item, dict):
                attachments.append(JiraAttachment.from_api_response(item))

        if not attachments:
            return {
                "success": True,
                "message": f"No attachments found for issue {issue_key}",
                "attachments": [],
                "failed": [],
            }

        fetched: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for attachment in attachments:
            if not attachment.url:
                logger.warning(f"No URL for attachment {attachment.filename}")
                failed.append(
                    {"filename": attachment.filename, "error": "No URL available"}
                )
                continue

            if attachment.size > ATTACHMENT_MAX_BYTES:
                logger.warning(
                    f"Skipping attachment {attachment.filename}: "
                    f"{attachment.size} bytes exceeds 50 MB limit"
                )
                failed.append(
                    {
                        "filename": attachment.filename,
                        "error": (
                            f"Attachment '{attachment.filename}' is "
                            f"{attachment.size} bytes which exceeds "
                            "the 50 MB inline limit. Retrieve it "
                            "directly from Jira."
                        ),
                    }
                )
                continue

            data = self.fetch_attachment_content(attachment.url)
            if data is not None:
                content_type = (
                    attachment.content_type
                    or mimetypes.guess_type(attachment.filename)[0]
                    or "application/octet-stream"
                )
                fetched.append(
                    {
                        "filename": attachment.filename,
                        "content_type": content_type,
                        "size": len(data),
                        "data": data,
                    }
                )
            else:
                failed.append(
                    {"filename": attachment.filename, "error": "Fetch failed"}
                )

        return {
            "success": True,
            "issue_key": issue_key,
            "total": len(attachments),
            "attachments": fetched,
            "failed": failed,
        }

    def download_issue_attachments(
        self, issue_key: str, target_dir: str
    ) -> dict[str, Any]:
        """
        Download all attachments for a Jira issue.

        Args:
            issue_key: The Jira issue key (e.g., 'PROJ-123')
            target_dir: The directory where attachments should be saved

        Returns:
            A dictionary with download results
        """
        # Convert to absolute path if relative
        if not os.path.isabs(target_dir):
            target_dir = os.path.abspath(target_dir)

        # Guard against path traversal (resolves symlinks)
        validate_safe_path(target_dir)

        logger.info(
            f"Downloading attachments for {issue_key} to directory: {target_dir}"
        )

        # Create the target directory if it doesn't exist
        target_path = Path(target_dir)
        target_path.mkdir(parents=True, exist_ok=True)

        # Get the issue with attachments
        logger.info(f"Fetching issue {issue_key} with attachments")
        issue_data = self.jira.issue(issue_key, fields="attachment")

        if not isinstance(issue_data, dict):
            msg = f"Unexpected return value type from `jira.issue`: {type(issue_data)}"
            logger.error(msg)
            raise TypeError(msg)

        if "fields" not in issue_data:
            logger.error(f"Could not retrieve issue {issue_key}")
            return {"success": False, "error": f"Could not retrieve issue {issue_key}"}

        # Process attachments
        attachments = []
        results = []

        # Extract attachments from the API response
        attachment_data = issue_data.get("fields", {}).get("attachment", [])

        if not attachment_data:
            return {
                "success": True,
                "message": f"No attachments found for issue {issue_key}",
                "downloaded": [],
                "failed": [],
            }

        # Create JiraAttachment objects for each attachment
        for attachment in attachment_data:
            if isinstance(attachment, dict):
                attachments.append(JiraAttachment.from_api_response(attachment))

        # Download each attachment
        downloaded = []
        failed = []

        for attachment in attachments:
            if not attachment.url:
                logger.warning(f"No URL for attachment {attachment.filename}")
                failed.append(
                    {"filename": attachment.filename, "error": "No URL available"}
                )
                continue

            # Create a safe filename
            safe_filename = Path(attachment.filename).name
            file_path = target_path / safe_filename

            # Download the attachment
            success = self.download_attachment(attachment.url, str(file_path))

            if success:
                downloaded.append(
                    {
                        "filename": attachment.filename,
                        "path": str(file_path),
                        "size": attachment.size,
                    }
                )
            else:
                failed.append(
                    {"filename": attachment.filename, "error": "Download failed"}
                )

        return {
            "success": True,
            "issue_key": issue_key,
            "total": len(attachments),
            "downloaded": downloaded,
            "failed": failed,
        }

    def upload_attachment(self, issue_key: str, file_path: str) -> dict[str, Any]:
        """Upload a single file as an attachment to a Jira issue.

        Sends ``POST /rest/api/{2|3}/issue/{key}/attachments`` (multipart field
        ``file``, header ``X-Atlassian-Token: no-check``) through the already
        authenticated client, so Cloud and Server/DC share the code path; v3 is
        used on Cloud and v2 on Server/DC, matching how issues are read.

        The file must resolve, symlinks included, inside the server's working
        directory: that is the only filesystem root an upload may read from.
        Every refusal is returned as ``success: False`` with an ``error`` that
        says why, rather than raised.

        Args:
            issue_key: The Jira issue key (e.g., 'PROJ-123').
            file_path: Path of the file to upload, absolute or relative to the
                server's working directory.

        Returns:
            On success: ``success``, ``issue_key``, ``file_path``, ``filename``,
            ``size``, ``id`` and ``attachment`` (the created attachment as a
            simplified ``JiraAttachment`` plus its ``id``). On failure:
            ``success: False``, ``file_path``, ``filename``, ``error`` and, when
            Jira answered, ``status_code``.
        """
        if not issue_key:
            logger.error("No issue key provided for attachment upload")
            return {"success": False, "error": "No issue key provided"}

        if not file_path:
            logger.error("No file path provided for attachment upload")
            return {"success": False, "error": "No file path provided"}

        filename = Path(file_path).name
        failure: dict[str, Any] = {
            "success": False,
            "file_path": file_path,
            "filename": filename,
        }

        # Confine the upload source to the workspace before anything about the
        # file is looked at: a path that escapes CWD (traversal, absolute path,
        # symlink) must be refused without revealing whether it exists.
        try:
            resolved = validate_safe_path(Path(file_path).expanduser())
        except ValueError:
            root = Path(os.getcwd()).resolve()
            logger.error(f"Refusing to upload from outside {root}: {file_path}")
            failure["error"] = (
                f"Refusing to upload '{file_path}': it resolves outside the "
                f"allowed root {root} (the server's working directory)."
            )
            return failure

        if not resolved.exists():
            logger.error(f"File not found: {resolved}")
            failure["error"] = f"File not found: {resolved}"
            return failure

        if not resolved.is_file():
            logger.error(f"Not a regular file: {resolved}")
            failure["error"] = f"Not a regular file: {resolved}"
            return failure

        filename = resolved.name
        failure["filename"] = filename
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        # Same API version the issue is read with (v3 on Cloud, v2 on
        # Server/DC), so the returned content URL matches jira_get_issue's.
        url = self.jira.resource_url(
            f"issue/{urllib.parse.quote(issue_key, safe='')}/attachments",
            api_version="3" if self.config.is_cloud else None,
        )

        logger.info(f"Uploading attachment {resolved} to issue {issue_key}")
        try:
            with resolved.open("rb") as file_handle:
                response = self.jira.post(
                    url,
                    headers={"X-Atlassian-Token": "no-check"},
                    files={"file": (filename, file_handle, mime_type)},
                    advanced_mode=True,
                )
        except OSError as e:
            logger.error(f"Could not read {resolved}: {e}")
            failure["error"] = f"Could not read {resolved}: {e}"
            return failure
        except requests.RequestException as e:
            logger.error(f"Error uploading attachment to {issue_key}: {e}")
            failure["error"] = f"Upload request failed: {e}"
            return failure

        status = response.status_code
        if status >= 400:
            failure["status_code"] = status
            failure["error"] = self._describe_upload_error(
                response, issue_key, filename
            )
            logger.error(f"Upload of {filename} to {issue_key} failed: {failure}")
            return failure

        try:
            body = response.json()
        except ValueError:
            body = None
        # Jira answers with a JSON array holding one entry per uploaded file.
        item = body[0] if isinstance(body, list) and body else body
        if not isinstance(item, dict) or not item.get("id"):
            failure["status_code"] = status
            failure["error"] = (
                f"Jira accepted the upload of '{filename}' to {issue_key} "
                f"(HTTP {status}) but returned no attachment metadata."
            )
            logger.error(failure["error"])
            return failure

        attachment = JiraAttachment.from_api_response(item)
        logger.info(
            f"Uploaded attachment {attachment.filename} (id {attachment.id}) "
            f"to {issue_key} (size: {attachment.size} bytes)"
        )
        return {
            "success": True,
            "issue_key": issue_key,
            "file_path": str(resolved),
            "filename": attachment.filename or filename,
            "size": attachment.size,
            "id": attachment.id,
            "attachment": {"id": attachment.id, **attachment.to_simplified_dict()},
        }

    def _describe_upload_error(
        self, response: requests.Response, issue_key: str, filename: str
    ) -> str:
        """Turn a failed attachment-upload response into an actionable message.

        Args:
            response: The HTTP response Jira returned (status >= 400).
            issue_key: The issue the upload targeted.
            filename: The name of the file that was sent.

        Returns:
            A message naming the cause and carrying Jira's own wording.
        """
        status = response.status_code
        detail = _jira_error_detail(response)
        suffix = f": {detail}" if detail else "."

        if status == 413 or _SIZE_ERROR_PATTERN.search(detail):
            limit = _parse_size_limit(detail)
            if limit is None:
                limit = self._get_attachment_upload_limit()
            limit_text = (
                f"the instance's limit of {limit} bytes"
                if limit is not None
                else "the instance's attachment size limit"
            )
            return (
                f"'{filename}' exceeds {limit_text}; Jira rejected it "
                f"(HTTP {status}){suffix}"
            )
        if status == 401:
            return f"Jira rejected the credentials (HTTP 401){suffix}"
        if status == 403:
            return (
                f"This token may not add attachments to {issue_key}: it lacks "
                "the Create Attachments permission on the issue, or "
                f"attachments are disabled (HTTP 403){suffix}"
            )
        if status == 404:
            return (
                f"Issue {issue_key} does not exist or is not visible to this "
                f"token (HTTP 404){suffix}"
            )
        return f"Jira rejected the upload of '{filename}' (HTTP {status}){suffix}"

    def _get_attachment_upload_limit(self) -> int | None:
        """Ask Jira for its attachment size limit (``GET attachment/meta``).

        Used only when a size rejection does not state the limit itself.

        Returns:
            The limit in bytes, or None when it cannot be determined.
        """
        try:
            meta = self.jira.get(self.jira.resource_url("attachment/meta"))
        except (requests.RequestException, ValueError) as e:
            logger.debug(f"Could not read the attachment upload limit: {e}")
            return None
        if isinstance(meta, dict):
            limit = meta.get("uploadLimit")
            if isinstance(limit, int) and not isinstance(limit, bool):
                return limit
        return None

    def upload_attachments(
        self, issue_key: str, file_paths: list[str]
    ) -> dict[str, Any]:
        """Upload several files to a Jira issue, reporting every file's outcome.

        Each file is uploaded independently; one failure does not stop the
        rest. Every input path appears exactly once, in ``uploaded`` or in
        ``failed``.

        Args:
            issue_key: The Jira issue key (e.g., 'PROJ-123').
            file_paths: Paths of the files to upload.

        Returns:
            ``success`` (True when at least one file was uploaded),
            ``issue_key``, ``total``, ``uploaded`` (``file_path``,
            ``filename``, ``size``, ``id``, ``attachment``) and ``failed``
            (``file_path``, ``filename``, ``error``, optional ``status_code``).
        """
        if not issue_key:
            logger.error("No issue key provided for attachment upload")
            return {"success": False, "error": "No issue key provided"}

        if not file_paths:
            logger.error("No file paths provided for attachment upload")
            return {"success": False, "error": "No file paths provided"}

        logger.info(f"Uploading {len(file_paths)} attachments to issue {issue_key}")

        uploaded: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for file_path in file_paths:
            try:
                result = self.upload_attachment(issue_key, file_path)
            except Exception as e:  # noqa: BLE001 - report every file's outcome
                logger.error(f"Unexpected error uploading {file_path}: {e}")
                result = {"success": False, "error": f"{type(e).__name__}: {e}"}

            if result.get("success"):
                uploaded.append(
                    {
                        "file_path": file_path,
                        "filename": result.get("filename"),
                        "size": result.get("size"),
                        "id": result.get("id"),
                        "attachment": result.get("attachment"),
                    }
                )
            else:
                entry: dict[str, Any] = {
                    "file_path": file_path,
                    "filename": os.path.basename(file_path),
                    "error": result.get("error") or "Upload failed",
                }
                if "status_code" in result:
                    entry["status_code"] = result["status_code"]
                failed.append(entry)

        return {
            "success": bool(uploaded),
            "issue_key": issue_key,
            "total": len(file_paths),
            "uploaded": uploaded,
            "failed": failed,
        }


_SIZE_ERROR_PATTERN = re.compile(
    r"maximum permitted size|exceeds|too large", re.IGNORECASE
)
_SIZE_LIMIT_PATTERN = re.compile(r"(\d[\d,]*)\s*bytes", re.IGNORECASE)


def _jira_error_detail(response: requests.Response) -> str:
    """Extract Jira's error wording from a failed response.

    Jira reports ``{"errorMessages": [...], "errors": {...}}``; a proxy in
    front of it may answer with plain text or HTML instead.

    Args:
        response: The failed HTTP response.

    Returns:
        The joined error messages, or a trimmed body, or an empty string.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        parts: list[str] = [str(m) for m in body.get("errorMessages") or [] if m]
        errors = body.get("errors")
        if isinstance(errors, dict):
            parts.extend(f"{k}: {v}" for k, v in errors.items() if v)
        message = body.get("message")
        if not parts and message:
            parts.append(str(message))
        if parts:
            return "; ".join(parts)
    text = (getattr(response, "text", "") or "").strip()
    if text.startswith("<"):
        return response.reason or ""
    return text[:500]


def _parse_size_limit(detail: str) -> int | None:
    """Read the byte limit out of a Jira size-rejection message.

    Args:
        detail: Jira's error text, e.g. "... maximum permitted size of
            10485760 bytes."

    Returns:
        The limit in bytes, or None when the text does not state one.
    """
    match = _SIZE_LIMIT_PATTERN.search(detail)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))
