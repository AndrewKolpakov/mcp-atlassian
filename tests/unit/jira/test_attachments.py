"""Tests for the Jira attachments module."""

from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from mcp_atlassian.jira import JiraFetcher
from mcp_atlassian.jira.attachments import AttachmentsMixin

# Test scenarios for AttachmentsMixin
#
# 1. Single Attachment Download (download_attachment method):
#    - Success case: Downloads attachment correctly with proper HTTP response
#    - Path handling: Converts relative path to absolute path
#    - Error cases:
#      - No URL provided
#      - HTTP error during download
#      - File write error
#      - File not created after write operation
#
# 2. Issue Attachments Download (download_issue_attachments method):
#    - Success case: Downloads all attachments for an issue
#    - Path handling: Converts relative target directory to absolute path
#    - Edge cases:
#      - Issue has no attachments
#      - Issue not found
#      - Issue has no fields
#      - Some attachments fail to download
#      - Attachment has missing URL
#
# 3. Single Attachment Upload (upload_attachment method), HTTP mocked at jira.post:
#    - Success case: POSTs multipart ``file`` with X-Atlassian-Token: no-check and
#      returns the created attachment as the simplified model plus its id
#    - Path handling: relative and absolute paths inside the workspace
#    - Refusals: no issue key, no file path, outside the workspace (incl. via
#      symlink), missing file, directory, over the size limit (limit read from
#      Jira's error, else from attachment/meta), 403, 404, other 4xx,
#      transport error, 2xx without metadata
#
# 4. Multiple Attachments Upload (upload_attachments method):
#    - Every path is reported exactly once, in ``uploaded`` or ``failed``
#    - One file's exception does not stop the batch
#    - Error cases:
#      - Empty list of file paths
#      - No issue key provided


class TestAttachmentsMixin:
    """Tests for the AttachmentsMixin class."""

    @pytest.fixture
    def attachments_mixin(self, jira_fetcher: JiraFetcher) -> AttachmentsMixin:
        """Set up test fixtures before each test method."""
        # Create a mock Jira client
        attachments_mixin = jira_fetcher
        attachments_mixin.jira = MagicMock()
        attachments_mixin.jira._session = MagicMock()
        return attachments_mixin

    def test_download_attachment_success(self, attachments_mixin: AttachmentsMixin):
        """Test successful attachment download."""
        import os

        # Mock the response
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [b"test content"]
        mock_response.raise_for_status = MagicMock()
        attachments_mixin.jira._session.get.return_value = mock_response

        # A subdirectory of CWD (downloads may not land in the CWD root itself).
        download_path = "/tmp/downloads/test_file.txt"
        expected_path = os.path.abspath(download_path)

        # Mock file operations
        with (
            patch("builtins.open", mock_open()) as mock_file,
            patch("os.path.exists") as mock_exists,
            patch("os.path.getsize") as mock_getsize,
            patch("os.makedirs") as mock_makedirs,
            patch("os.getcwd", return_value="/tmp"),
        ):
            mock_exists.return_value = True
            mock_getsize.return_value = 12  # Length of "test content"

            # Call the method
            result = attachments_mixin.download_attachment(
                "https://test.url/attachment", download_path
            )

            # Assertions
            assert result is True
            attachments_mixin.jira._session.get.assert_called_once_with(
                "https://test.url/attachment", stream=True
            )
            mock_file.assert_called_once_with(expected_path, "wb")
            mock_file().write.assert_called_once_with(b"test content")
            mock_makedirs.assert_called_once()

    def test_download_attachment_relative_path(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test attachment download with a relative path."""
        # Mock the response
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [b"test content"]
        mock_response.raise_for_status = MagicMock()
        attachments_mixin.jira._session.get.return_value = mock_response

        # Mock file operations and os.path.abspath
        with (
            patch("builtins.open", mock_open()) as mock_file,
            patch("os.path.exists") as mock_exists,
            patch("os.path.getsize") as mock_getsize,
            patch("os.makedirs") as mock_makedirs,
            patch("os.path.abspath") as mock_abspath,
            patch("os.path.isabs") as mock_isabs,
            patch("os.getcwd", return_value="/absolute/path"),
            patch("mcp_atlassian.jira.attachments.validate_safe_path"),
        ):
            mock_exists.return_value = True
            mock_getsize.return_value = 12
            mock_isabs.return_value = False
            # Target a subdirectory (downloads may not land in the CWD root itself).
            mock_abspath.side_effect = lambda p: (
                "/absolute/path/downloads/test_file.txt"
                if p == "downloads/test_file.txt"
                else p
            )

            # Call the method with a relative path
            result = attachments_mixin.download_attachment(
                "https://test.url/attachment", "downloads/test_file.txt"
            )

            # Assertions
            assert result is True
            mock_isabs.assert_any_call("downloads/test_file.txt")
            mock_abspath.assert_any_call("downloads/test_file.txt")
            mock_file.assert_called_once_with(
                "/absolute/path/downloads/test_file.txt", "wb"
            )

    def test_download_attachment_no_url(self, attachments_mixin: AttachmentsMixin):
        """Test attachment download with no URL."""
        result = attachments_mixin.download_attachment("", "/tmp/test_file.txt")
        assert result is False

    def test_download_attachment_http_error(self, attachments_mixin: AttachmentsMixin):
        """Test attachment download with an HTTP error."""
        # Mock the response to raise an HTTP error
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("HTTP Error")
        attachments_mixin.jira._session.get.return_value = mock_response

        result = attachments_mixin.download_attachment(
            "https://test.url/attachment", "/tmp/test_file.txt"
        )
        assert result is False

    def test_download_attachment_file_write_error(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test attachment download with a file write error."""
        # Mock the response
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [b"test content"]
        mock_response.raise_for_status = MagicMock()
        attachments_mixin.jira._session.get.return_value = mock_response

        # Mock file operations to raise an exception during write
        with (
            patch("builtins.open", mock_open()) as mock_file,
            patch("os.makedirs") as mock_makedirs,
        ):
            mock_file().write.side_effect = OSError("Write error")

            result = attachments_mixin.download_attachment(
                "https://test.url/attachment", "/tmp/test_file.txt"
            )
            assert result is False

    def test_download_attachment_file_not_created(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test attachment download when file is not created."""
        # Mock the response
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [b"test content"]
        mock_response.raise_for_status = MagicMock()
        attachments_mixin.jira._session.get.return_value = mock_response

        # Mock file operations
        with (
            patch("builtins.open", mock_open()) as mock_file,
            patch("os.path.exists") as mock_exists,
            patch("os.makedirs") as mock_makedirs,
        ):
            mock_exists.return_value = False  # File doesn't exist after write

            result = attachments_mixin.download_attachment(
                "https://test.url/attachment", "/tmp/test_file.txt"
            )
            assert result is False

    def test_download_issue_attachments_success(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test successful download of all issue attachments."""
        # Mock the issue data
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "test1.txt",
                        "content": "https://test.url/attachment1",
                        "size": 100,
                    },
                    {
                        "filename": "test2.txt",
                        "content": "https://test.url/attachment2",
                        "size": 200,
                    },
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        # Mock JiraAttachment.from_api_response
        mock_attachment1 = MagicMock()
        mock_attachment1.filename = "test1.txt"
        mock_attachment1.url = "https://test.url/attachment1"
        mock_attachment1.size = 100

        mock_attachment2 = MagicMock()
        mock_attachment2.filename = "test2.txt"
        mock_attachment2.url = "https://test.url/attachment2"
        mock_attachment2.size = 200

        # Mock the download_attachment method
        with (
            patch.object(
                attachments_mixin, "download_attachment", return_value=True
            ) as mock_download,
            patch("pathlib.Path.mkdir") as mock_mkdir,
            patch(
                "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
                side_effect=[mock_attachment1, mock_attachment2],
            ),
            patch("os.getcwd", return_value="/tmp"),
        ):
            result = attachments_mixin.download_issue_attachments(
                "TEST-123", "/tmp/attachments"
            )

            # Assertions
            assert result["success"] is True
            assert len(result["downloaded"]) == 2
            assert len(result["failed"]) == 0
            assert result["total"] == 2
            assert result["issue_key"] == "TEST-123"
            assert mock_download.call_count == 2
            mock_mkdir.assert_called_once()

    def test_download_issue_attachments_relative_path(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test download issue attachments with a relative path."""
        # Mock the issue data
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "test1.txt",
                        "content": "https://test.url/attachment1",
                        "size": 100,
                    }
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        # Mock attachment
        mock_attachment = MagicMock()
        mock_attachment.filename = "test1.txt"
        mock_attachment.url = "https://test.url/attachment1"
        mock_attachment.size = 100

        # Mock path operations
        with (
            patch.object(
                attachments_mixin, "download_attachment", return_value=True
            ) as mock_download,
            patch("pathlib.Path.mkdir") as mock_mkdir,
            patch(
                "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
                return_value=mock_attachment,
            ),
            patch("os.path.isabs") as mock_isabs,
            patch("os.path.abspath") as mock_abspath,
            patch("os.getcwd", return_value="/absolute/path"),
            patch("mcp_atlassian.jira.attachments.validate_safe_path"),
        ):
            mock_isabs.return_value = False
            mock_abspath.return_value = "/absolute/path/attachments"

            result = attachments_mixin.download_issue_attachments(
                "TEST-123", "attachments"
            )

            # Assertions
            assert result["success"] is True
            mock_isabs.assert_called_once_with("attachments")
            mock_abspath.assert_called_once_with("attachments")

    def test_download_issue_attachments_no_attachments(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test download when issue has no attachments."""
        # Mock the issue data with no attachments
        mock_issue = {"fields": {"attachment": []}}
        attachments_mixin.jira.issue.return_value = mock_issue

        with (
            patch("pathlib.Path.mkdir") as mock_mkdir,
            patch("os.getcwd", return_value="/tmp"),
        ):
            result = attachments_mixin.download_issue_attachments(
                "TEST-123", "/tmp/attachments"
            )

            # Assertions
            assert result["success"] is True
            assert "No attachments found" in result["message"]
            assert len(result["downloaded"]) == 0
            assert len(result["failed"]) == 0
            mock_mkdir.assert_called_once()

    def test_download_issue_attachments_issue_not_found(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test download when issue cannot be retrieved."""
        attachments_mixin.jira.issue.return_value = None

        with (
            patch("os.getcwd", return_value="/tmp"),
            pytest.raises(
                TypeError,
                match="Unexpected return value type from `jira.issue`: <class 'NoneType'>",
            ),
        ):
            attachments_mixin.download_issue_attachments("TEST-123", "/tmp/attachments")

    def test_download_issue_attachments_no_fields(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test download when issue has no fields."""
        # Mock the issue data with no fields
        mock_issue = {}  # Missing 'fields' key
        attachments_mixin.jira.issue.return_value = mock_issue

        with patch("os.getcwd", return_value="/tmp"):
            result = attachments_mixin.download_issue_attachments(
                "TEST-123", "/tmp/attachments"
            )

        # Assertions
        assert result["success"] is False
        assert "Could not retrieve issue" in result["error"]

    def test_download_issue_attachments_some_failures(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test download when some attachments fail to download."""
        # Mock the issue data
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "test1.txt",
                        "content": "https://test.url/attachment1",
                        "size": 100,
                    },
                    {
                        "filename": "test2.txt",
                        "content": "https://test.url/attachment2",
                        "size": 200,
                    },
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        # Mock attachments
        mock_attachment1 = MagicMock()
        mock_attachment1.filename = "test1.txt"
        mock_attachment1.url = "https://test.url/attachment1"
        mock_attachment1.size = 100

        mock_attachment2 = MagicMock()
        mock_attachment2.filename = "test2.txt"
        mock_attachment2.url = "https://test.url/attachment2"
        mock_attachment2.size = 200

        # Mock the download_attachment method to succeed for first attachment and fail for second
        with (
            patch.object(
                attachments_mixin, "download_attachment", side_effect=[True, False]
            ) as mock_download,
            patch("pathlib.Path.mkdir") as mock_mkdir,
            patch(
                "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
                side_effect=[mock_attachment1, mock_attachment2],
            ),
            patch("os.getcwd", return_value="/tmp"),
        ):
            result = attachments_mixin.download_issue_attachments(
                "TEST-123", "/tmp/attachments"
            )

            # Assertions
            assert result["success"] is True
            assert len(result["downloaded"]) == 1
            assert len(result["failed"]) == 1
            assert result["downloaded"][0]["filename"] == "test1.txt"
            assert result["failed"][0]["filename"] == "test2.txt"
            assert mock_download.call_count == 2

    def test_download_issue_attachments_missing_url(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test download when an attachment has no URL."""
        # Mock the issue data
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "test1.txt",
                        "content": "https://test.url/attachment1",
                        "size": 100,
                    }
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        # Mock attachment with no URL
        mock_attachment = MagicMock()
        mock_attachment.filename = "test1.txt"
        mock_attachment.url = None  # No URL
        mock_attachment.size = 100

        # Mock path operations
        with (
            patch("pathlib.Path.mkdir") as mock_mkdir,
            patch(
                "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
                return_value=mock_attachment,
            ),
            patch("os.getcwd", return_value="/tmp"),
        ):
            result = attachments_mixin.download_issue_attachments(
                "TEST-123", "/tmp/attachments"
            )

            # Assertions
            assert result["success"] is True
            assert len(result["downloaded"]) == 0
            assert len(result["failed"]) == 1
            assert result["failed"][0]["filename"] == "test1.txt"
            assert "No URL available" in result["failed"][0]["error"]

    # Tests for upload_attachment method
    #
    # The HTTP layer is mocked at ``self.jira.post`` (the authenticated
    # atlassian-python-api client); files are real files in a temporary
    # workspace that is pinned as the working directory.

    @staticmethod
    def _response(
        status: int, body: object = None, text: str | None = None
    ) -> MagicMock:
        """Build a mock ``requests.Response``."""
        response = MagicMock()
        response.status_code = status
        response.reason = "Reason"
        if body is None:
            response.json.side_effect = ValueError("no json")
        else:
            response.json.return_value = body
        response.text = text if text is not None else ""
        return response

    @staticmethod
    def _created(attachment_id: str, filename: str, size: int) -> list[dict]:
        """The JSON array Jira returns from POST .../attachments."""
        return [
            {
                "self": f"https://test.atlassian.net/rest/api/2/attachment/{attachment_id}",
                "id": attachment_id,
                "filename": filename,
                "size": size,
                "mimeType": "text/plain",
                "created": "2026-11-04T10:00:00.000+0000",
                "content": (
                    "https://test.atlassian.net/secure/attachment/"
                    f"{attachment_id}/{filename}"
                ),
                "author": {"displayName": "Test User", "accountId": "abc"},
            }
        ]

    @pytest.fixture
    def workspace(self, tmp_path: Path):
        """A temporary directory pinned as the server's working directory."""
        with patch("os.getcwd", return_value=str(tmp_path)):
            yield tmp_path

    def test_upload_attachment_success(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """A file inside the workspace is posted and its metadata returned."""
        (workspace / "test_file.txt").write_bytes(b"test content")
        attachments_mixin.jira.resource_url.return_value = (
            "rest/api/2/issue/TEST-123/attachments"
        )
        attachments_mixin.jira.post.return_value = self._response(
            200, self._created("12345", "test_file.txt", 12)
        )

        result = attachments_mixin.upload_attachment("TEST-123", "test_file.txt")

        assert result["success"] is True
        assert result["issue_key"] == "TEST-123"
        assert result["filename"] == "test_file.txt"
        assert result["size"] == 12
        assert result["id"] == "12345"
        assert result["file_path"] == str((workspace / "test_file.txt").resolve())
        attachment = dict(result["attachment"])
        assert attachment.pop("author")["display_name"] == "Test User"
        assert attachment == {
            "id": "12345",
            "filename": "test_file.txt",
            "size": 12,
            "url": "https://test.atlassian.net/secure/attachment/12345/test_file.txt",
            "content_type": "text/plain",
            "created": "2026-11-04T10:00:00.000+0000",
        }

        attachments_mixin.jira.resource_url.assert_called_once_with(
            "issue/TEST-123/attachments", api_version="3"
        )
        attachments_mixin.jira.post.assert_called_once()
        args, kwargs = attachments_mixin.jira.post.call_args
        assert args == ("rest/api/2/issue/TEST-123/attachments",)
        assert kwargs["headers"] == {"X-Atlassian-Token": "no-check"}
        assert kwargs["advanced_mode"] is True
        name, handle, mime = kwargs["files"]["file"]
        assert (name, mime) == ("test_file.txt", "text/plain")
        assert handle.name == str((workspace / "test_file.txt").resolve())

    @pytest.mark.parametrize(("is_cloud", "version"), [(True, "3"), (False, None)])
    def test_upload_attachment_api_version_follows_deployment(
        self,
        attachments_mixin: AttachmentsMixin,
        workspace: Path,
        is_cloud: bool,
        version: str | None,
    ):
        """Cloud posts to v3 and Server/DC to the client default (v2)."""
        (workspace / "a.txt").write_bytes(b"x")
        attachments_mixin.config.url = (
            "https://test.atlassian.net" if is_cloud else "https://jira.example.com"
        )
        assert attachments_mixin.config.is_cloud is is_cloud
        attachments_mixin.jira.post.return_value = self._response(
            200, self._created("7", "a.txt", 1)
        )

        attachments_mixin.upload_attachment("TEST-1", "a.txt")

        attachments_mixin.jira.resource_url.assert_called_once_with(
            "issue/TEST-1/attachments", api_version=version
        )

    def test_upload_attachment_matches_get_issue_shape(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """The returned attachment is the simplified model jira_get_issue uses."""
        from mcp_atlassian.models.jira import JiraAttachment

        (workspace / "a.txt").write_bytes(b"x")
        created = self._created("7", "a.txt", 1)
        attachments_mixin.jira.post.return_value = self._response(200, created)

        result = attachments_mixin.upload_attachment("TEST-1", "a.txt")

        expected = JiraAttachment.from_api_response(created[0]).to_simplified_dict()
        assert result["attachment"] == {"id": "7", **expected}

    def test_upload_attachment_absolute_path_inside_workspace(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """An absolute path that stays inside the workspace is accepted."""
        target = workspace / "sub" / "report.pdf"
        target.parent.mkdir()
        target.write_bytes(b"%PDF")
        attachments_mixin.jira.post.return_value = self._response(
            200, self._created("9", "report.pdf", 4)
        )

        result = attachments_mixin.upload_attachment("TEST-1", str(target))

        assert result["success"] is True
        name, _, mime = attachments_mixin.jira.post.call_args.kwargs["files"]["file"]
        assert (name, mime) == ("report.pdf", "application/pdf")

    def test_upload_attachment_unknown_type_is_octet_stream(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """A file with no guessable type is sent as application/octet-stream."""
        (workspace / "blob.zzunknown").write_bytes(b"\x00")
        attachments_mixin.jira.post.return_value = self._response(
            200, self._created("9", "blob.zzunknown", 1)
        )

        attachments_mixin.upload_attachment("TEST-1", "blob.zzunknown")

        _, _, mime = attachments_mixin.jira.post.call_args.kwargs["files"]["file"]
        assert mime == "application/octet-stream"

    def test_upload_attachment_no_issue_key(self, attachments_mixin: AttachmentsMixin):
        """Test attachment upload with no issue key."""
        result = attachments_mixin.upload_attachment("", "/path/to/file.txt")

        assert result["success"] is False
        assert "No issue key provided" in result["error"]
        attachments_mixin.jira.post.assert_not_called()

    def test_upload_attachment_no_file_path(self, attachments_mixin: AttachmentsMixin):
        """Test attachment upload with no file path."""
        result = attachments_mixin.upload_attachment("TEST-123", "")

        assert result["success"] is False
        assert "No file path provided" in result["error"]
        attachments_mixin.jira.post.assert_not_called()

    def test_upload_attachment_file_not_found(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """A missing file inside the workspace is refused before any request."""
        result = attachments_mixin.upload_attachment("TEST-123", "missing.txt")

        assert result["success"] is False
        assert "File not found" in result["error"]
        assert result["filename"] == "missing.txt"
        attachments_mixin.jira.post.assert_not_called()

    def test_upload_attachment_directory_is_refused(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """A directory is not uploadable."""
        (workspace / "adir").mkdir()

        result = attachments_mixin.upload_attachment("TEST-123", "adir")

        assert result["success"] is False
        assert "Not a regular file" in result["error"]
        attachments_mixin.jira.post.assert_not_called()

    def test_upload_attachment_outside_workspace_is_refused_clearly(
        self, attachments_mixin: AttachmentsMixin, tmp_path: Path
    ):
        """The refusal names the allowed root, and does not reveal existence."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"secret")

        with patch("os.getcwd", return_value=str(workspace)):
            existing = attachments_mixin.upload_attachment("TEST-1", str(outside))
            missing = attachments_mixin.upload_attachment(
                "TEST-1", str(tmp_path / "nope.txt")
            )

        for result in (existing, missing):
            assert result["success"] is False
            assert "outside the allowed root" in result["error"]
            assert str(workspace.resolve()) in result["error"]
            assert "not found" not in result["error"].lower()
        attachments_mixin.jira.post.assert_not_called()

    def test_upload_attachment_symlink_escaping_workspace_is_refused(
        self, attachments_mixin: AttachmentsMixin, tmp_path: Path
    ):
        """A symlink inside the workspace pointing outside it is refused."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"secret")
        (workspace / "link.txt").symlink_to(outside)

        with patch("os.getcwd", return_value=str(workspace)):
            result = attachments_mixin.upload_attachment("TEST-1", "link.txt")

        assert result["success"] is False
        assert "outside the allowed root" in result["error"]
        attachments_mixin.jira.post.assert_not_called()

    @pytest.mark.parametrize(
        ("message", "limit"),
        [
            (
                "The field file exceeds its maximum permitted size of 10485760 bytes.",
                10485760,
            ),
            (
                "The field file exceeds its maximum permitted size of 1,048,576 bytes.",
                1048576,
            ),
        ],
    )
    def test_upload_attachment_size_limit_read_from_error(
        self,
        attachments_mixin: AttachmentsMixin,
        workspace: Path,
        message: str,
        limit: int,
    ):
        """A 413 reports the limit Jira stated, not a hardcoded one."""
        (workspace / "big.bin").write_bytes(b"0" * 16)
        attachments_mixin.jira.post.return_value = self._response(
            413, {"errorMessages": [message], "errors": {}}
        )

        result = attachments_mixin.upload_attachment("TEST-1", "big.bin")

        assert result["success"] is False
        assert result["status_code"] == 413
        assert f"limit of {limit} bytes" in result["error"]
        assert message in result["error"]
        attachments_mixin.jira.get.assert_not_called()

    def test_upload_attachment_size_limit_from_meta_when_error_is_silent(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """A bare 413 (e.g. from a proxy) falls back to GET attachment/meta."""
        (workspace / "big.bin").write_bytes(b"0" * 16)
        attachments_mixin.jira.post.return_value = self._response(
            413, None, text="<html><body>Request Entity Too Large</body></html>"
        )
        attachments_mixin.jira.resource_url.side_effect = (
            lambda r, api_version=None: f"rest/api/{api_version or 2}/{r}"
        )
        attachments_mixin.jira.get.return_value = {
            "enabled": True,
            "uploadLimit": 2097152,
        }

        result = attachments_mixin.upload_attachment("TEST-1", "big.bin")

        assert result["success"] is False
        assert "limit of 2097152 bytes" in result["error"]
        assert "<html>" not in result["error"]
        attachments_mixin.jira.get.assert_called_once_with("rest/api/2/attachment/meta")

    def test_upload_attachment_size_limit_unknown(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """When no source states the limit, the message still says it is size."""
        import requests

        (workspace / "big.bin").write_bytes(b"0" * 16)
        attachments_mixin.jira.post.return_value = self._response(413, None)
        attachments_mixin.jira.get.side_effect = requests.HTTPError("403")

        result = attachments_mixin.upload_attachment("TEST-1", "big.bin")

        assert result["success"] is False
        assert "exceeds the instance's attachment size limit" in result["error"]

    def test_upload_attachment_forbidden(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """A 403 says the token may not add attachments to the issue."""
        (workspace / "a.txt").write_bytes(b"x")
        jira_msg = "You do not have permission to create attachments for this issue."
        attachments_mixin.jira.post.return_value = self._response(
            403, {"errorMessages": [jira_msg]}
        )

        result = attachments_mixin.upload_attachment("TEST-1", "a.txt")

        assert result["success"] is False
        assert result["status_code"] == 403
        assert "may not add attachments to TEST-1" in result["error"]
        assert jira_msg in result["error"]

    def test_upload_attachment_issue_not_found(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """A 404 says the issue is missing or invisible to the token."""
        (workspace / "a.txt").write_bytes(b"x")
        attachments_mixin.jira.post.return_value = self._response(
            404, {"errorMessages": ["Issue does not exist"]}
        )

        result = attachments_mixin.upload_attachment("TEST-404", "a.txt")

        assert result["success"] is False
        assert result["status_code"] == 404
        assert "TEST-404 does not exist or is not visible" in result["error"]

    def test_upload_attachment_field_errors_reported(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """Jira's per-field ``errors`` are carried into the message."""
        (workspace / "a.txt").write_bytes(b"x")
        attachments_mixin.jira.post.return_value = self._response(
            400, {"errorMessages": [], "errors": {"file": "Bad file"}}
        )

        result = attachments_mixin.upload_attachment("TEST-1", "a.txt")

        assert result["success"] is False
        assert "HTTP 400" in result["error"]
        assert "file: Bad file" in result["error"]

    def test_upload_attachment_request_exception(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """A transport failure is returned as a failure, not raised."""
        import requests

        (workspace / "a.txt").write_bytes(b"x")
        attachments_mixin.jira.post.side_effect = requests.ConnectionError("boom")

        result = attachments_mixin.upload_attachment("TEST-1", "a.txt")

        assert result["success"] is False
        assert "boom" in result["error"]

    def test_upload_attachment_no_metadata_in_response(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """A 2xx without attachment metadata is not reported as success."""
        (workspace / "a.txt").write_bytes(b"x")
        attachments_mixin.jira.post.return_value = self._response(200, [])

        result = attachments_mixin.upload_attachment("TEST-1", "a.txt")

        assert result["success"] is False
        assert "returned no attachment metadata" in result["error"]

    # Tests for upload_attachments method

    def test_upload_attachments_success(self, attachments_mixin: AttachmentsMixin):
        """Test successful upload of multiple attachments."""
        file_paths = [
            "/path/to/file1.txt",
            "/path/to/file2.pdf",
            "/path/to/file3.jpg",
        ]
        mock_results = [
            {
                "success": True,
                "issue_key": "TEST-123",
                "filename": f"file{i + 1}.{ext}",
                "size": 100 * (i + 1),
                "id": f"id{i + 1}",
                "attachment": {"id": f"id{i + 1}"},
            }
            for i, ext in enumerate(["txt", "pdf", "jpg"])
        ]

        with patch.object(
            attachments_mixin, "upload_attachment", side_effect=mock_results
        ) as mock_upload:
            result = attachments_mixin.upload_attachments("TEST-123", file_paths)

        assert result["success"] is True
        assert result["issue_key"] == "TEST-123"
        assert result["total"] == 3
        assert len(result["uploaded"]) == 3
        assert len(result["failed"]) == 0
        assert mock_upload.call_count == 3
        for i, path in enumerate(file_paths):
            mock_upload.assert_any_call("TEST-123", path)
            assert result["uploaded"][i]["file_path"] == path
            assert result["uploaded"][i]["size"] == 100 * (i + 1)
            assert result["uploaded"][i]["id"] == f"id{i + 1}"
            assert result["uploaded"][i]["attachment"] == {"id": f"id{i + 1}"}
        assert [u["filename"] for u in result["uploaded"]] == [
            "file1.txt",
            "file2.pdf",
            "file3.jpg",
        ]

    def test_upload_attachments_mixed_results(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Every file is reported once, in uploaded or failed."""
        file_paths = [
            "/path/to/file1.txt",  # Will succeed
            "/path/to/file2.pdf",  # Will fail
            "/path/to/file3.jpg",  # Will succeed
        ]
        mock_results = [
            {"success": True, "filename": "file1.txt", "size": 100, "id": "id1"},
            {
                "success": False,
                "error": "File not found: /path/to/file2.pdf",
                "status_code": 404,
            },
            {"success": True, "filename": "file3.jpg", "size": 300, "id": "id3"},
        ]

        with patch.object(
            attachments_mixin, "upload_attachment", side_effect=mock_results
        ) as mock_upload:
            result = attachments_mixin.upload_attachments("TEST-123", file_paths)

        # Overall success is True even with partial failures
        assert result["success"] is True
        assert result["total"] == 3
        assert mock_upload.call_count == 3
        assert [u["filename"] for u in result["uploaded"]] == [
            "file1.txt",
            "file3.jpg",
        ]
        assert [u["id"] for u in result["uploaded"]] == ["id1", "id3"]
        assert result["failed"] == [
            {
                "file_path": "/path/to/file2.pdf",
                "filename": "file2.pdf",
                "error": "File not found: /path/to/file2.pdf",
                "status_code": 404,
            }
        ]

    def test_upload_attachments_unexpected_exception_does_not_stop_batch(
        self, attachments_mixin: AttachmentsMixin
    ):
        """An exception for one file is reported and the rest still run."""
        mock_results = [
            RuntimeError("kaboom"),
            {"success": True, "filename": "b.txt", "size": 1, "id": "2"},
        ]

        with patch.object(
            attachments_mixin, "upload_attachment", side_effect=mock_results
        ):
            result = attachments_mixin.upload_attachments(
                "TEST-123", ["a.txt", "b.txt"]
            )

        assert result["total"] == 2
        assert len(result["uploaded"]) == 1
        assert result["failed"][0]["file_path"] == "a.txt"
        assert "RuntimeError: kaboom" in result["failed"][0]["error"]

    def test_upload_attachments_real_files_mixed(
        self, attachments_mixin: AttachmentsMixin, workspace: Path
    ):
        """End-to-end through upload_attachment: success, missing, too large."""
        (workspace / "ok.txt").write_bytes(b"ok")
        (workspace / "big.txt").write_bytes(b"big")
        attachments_mixin.jira.post.side_effect = [
            self._response(200, self._created("1", "ok.txt", 2)),
            self._response(
                413,
                {
                    "errorMessages": [
                        "The field file exceeds its maximum permitted size of 2 bytes."
                    ]
                },
            ),
        ]

        result = attachments_mixin.upload_attachments(
            "TEST-1", ["ok.txt", "missing.txt", "big.txt"]
        )

        assert result["total"] == 3
        assert [u["file_path"] for u in result["uploaded"]] == ["ok.txt"]
        assert result["uploaded"][0]["attachment"]["id"] == "1"
        assert [f["file_path"] for f in result["failed"]] == [
            "missing.txt",
            "big.txt",
        ]
        assert "File not found" in result["failed"][0]["error"]
        assert "limit of 2 bytes" in result["failed"][1]["error"]
        assert result["failed"][1]["status_code"] == 413
        assert attachments_mixin.jira.post.call_count == 2

    def test_upload_attachments_all_fail(self, attachments_mixin: AttachmentsMixin):
        """Report failure when no attachment is uploaded."""
        with patch.object(
            attachments_mixin,
            "upload_attachment",
            return_value={"success": False, "error": "File not found"},
        ):
            result = attachments_mixin.upload_attachments(
                "TEST-123", ["/path/to/file1.txt", "/path/to/file2.txt"]
            )

        assert result["success"] is False
        assert result["uploaded"] == []
        assert len(result["failed"]) == 2

    def test_upload_attachments_empty_list(self, attachments_mixin: AttachmentsMixin):
        """Test upload with an empty list of file paths."""
        result = attachments_mixin.upload_attachments("TEST-123", [])

        assert result["success"] is False
        assert "No file paths provided" in result["error"]

    def test_upload_attachments_no_issue_key(self, attachments_mixin: AttachmentsMixin):
        """Test upload with no issue key provided."""
        result = attachments_mixin.upload_attachments("", ["/path/to/file.txt"])

        assert result["success"] is False
        assert "No issue key provided" in result["error"]

    # Tests for fetch_attachment_content method

    def test_fetch_attachment_content_success(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test successful in-memory attachment fetch."""
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [b"chunk1", b"chunk2"]
        mock_response.raise_for_status = MagicMock()
        attachments_mixin.jira._session.get.return_value = mock_response

        result = attachments_mixin.fetch_attachment_content(
            "https://test.url/attachment"
        )

        assert result == b"chunk1chunk2"
        attachments_mixin.jira._session.get.assert_called_once_with(
            "https://test.url/attachment", stream=True
        )

    def test_fetch_attachment_content_no_url(self, attachments_mixin: AttachmentsMixin):
        """Test fetch with no URL returns None."""
        result = attachments_mixin.fetch_attachment_content("")
        assert result is None

    def test_fetch_attachment_content_http_error(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test fetch with an HTTP error returns None."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("HTTP Error")
        attachments_mixin.jira._session.get.return_value = mock_response

        result = attachments_mixin.fetch_attachment_content(
            "https://test.url/attachment"
        )
        assert result is None

    # Tests for get_issue_attachment_contents method

    def test_get_issue_attachment_contents_success(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test successful in-memory fetch of all issue attachments."""
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "test1.txt",
                        "content": "https://test.url/attachment1",
                        "size": 100,
                        "mimeType": "text/plain",
                    },
                    {
                        "filename": "test2.png",
                        "content": "https://test.url/attachment2",
                        "size": 200,
                        "mimeType": "image/png",
                    },
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        mock_attachment1 = MagicMock()
        mock_attachment1.filename = "test1.txt"
        mock_attachment1.url = "https://test.url/attachment1"
        mock_attachment1.content_type = "text/plain"
        mock_attachment1.size = 100

        mock_attachment2 = MagicMock()
        mock_attachment2.filename = "test2.png"
        mock_attachment2.url = "https://test.url/attachment2"
        mock_attachment2.content_type = "image/png"
        mock_attachment2.size = 200

        with (
            patch.object(
                attachments_mixin,
                "fetch_attachment_content",
                side_effect=[b"content1", b"image_data"],
            ) as mock_fetch,
            patch(
                "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
                side_effect=[mock_attachment1, mock_attachment2],
            ),
        ):
            result = attachments_mixin.get_issue_attachment_contents("TEST-123")

            assert result["success"] is True
            assert result["issue_key"] == "TEST-123"
            assert result["total"] == 2
            assert len(result["attachments"]) == 2
            assert len(result["failed"]) == 0
            assert result["attachments"][0]["filename"] == "test1.txt"
            assert result["attachments"][0]["data"] == b"content1"
            assert result["attachments"][0]["content_type"] == "text/plain"
            assert result["attachments"][1]["filename"] == "test2.png"
            assert result["attachments"][1]["data"] == b"image_data"
            assert mock_fetch.call_count == 2

    def test_get_issue_attachment_contents_no_attachments(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test fetch when issue has no attachments."""
        mock_issue = {"fields": {"attachment": []}}
        attachments_mixin.jira.issue.return_value = mock_issue

        result = attachments_mixin.get_issue_attachment_contents("TEST-123")

        assert result["success"] is True
        assert "No attachments found" in result["message"]
        assert len(result["attachments"]) == 0
        assert len(result["failed"]) == 0

    def test_get_issue_attachment_contents_issue_not_found(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test fetch when issue returns unexpected type."""
        attachments_mixin.jira.issue.return_value = None

        with pytest.raises(
            TypeError,
            match="Unexpected return value type from `jira.issue`: <class 'NoneType'>",
        ):
            attachments_mixin.get_issue_attachment_contents("TEST-123")

    def test_get_issue_attachment_contents_no_fields(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test fetch when issue has no fields."""
        mock_issue = {}
        attachments_mixin.jira.issue.return_value = mock_issue

        result = attachments_mixin.get_issue_attachment_contents("TEST-123")

        assert result["success"] is False
        assert "Could not retrieve issue" in result["error"]

    def test_get_issue_attachment_contents_some_failures(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test fetch when some attachments fail."""
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "good.txt",
                        "content": "https://test.url/good",
                        "size": 100,
                    },
                    {
                        "filename": "bad.txt",
                        "content": "https://test.url/bad",
                        "size": 200,
                    },
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        mock_attachment1 = MagicMock()
        mock_attachment1.filename = "good.txt"
        mock_attachment1.url = "https://test.url/good"
        mock_attachment1.content_type = "text/plain"
        mock_attachment1.size = 100

        mock_attachment2 = MagicMock()
        mock_attachment2.filename = "bad.txt"
        mock_attachment2.url = "https://test.url/bad"
        mock_attachment2.content_type = "text/plain"
        mock_attachment2.size = 200

        with (
            patch.object(
                attachments_mixin,
                "fetch_attachment_content",
                side_effect=[b"good_data", None],
            ),
            patch(
                "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
                side_effect=[mock_attachment1, mock_attachment2],
            ),
        ):
            result = attachments_mixin.get_issue_attachment_contents("TEST-123")

            assert result["success"] is True
            assert len(result["attachments"]) == 1
            assert len(result["failed"]) == 1
            assert result["attachments"][0]["filename"] == "good.txt"
            assert result["failed"][0]["filename"] == "bad.txt"

    def test_get_issue_attachment_contents_missing_url(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Test fetch when an attachment has no URL."""
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "no_url.txt",
                        "size": 100,
                    }
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        mock_attachment = MagicMock()
        mock_attachment.filename = "no_url.txt"
        mock_attachment.url = None
        mock_attachment.content_type = None

        with patch(
            "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
            return_value=mock_attachment,
        ):
            result = attachments_mixin.get_issue_attachment_contents("TEST-123")

            assert result["success"] is True
            assert len(result["attachments"]) == 0
            assert len(result["failed"]) == 1
            assert "No URL available" in result["failed"][0]["error"]

    # Tests for 50MB attachment size limit

    def test_get_issue_attachment_contents_skips_oversized(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Attachment with size > 50MB in metadata is skipped before download."""
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "huge.bin",
                        "content": "https://test.url/huge",
                        "size": 60 * 1024 * 1024,  # 60 MB
                    }
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        mock_attachment = MagicMock()
        mock_attachment.filename = "huge.bin"
        mock_attachment.url = "https://test.url/huge"
        mock_attachment.size = 60 * 1024 * 1024
        mock_attachment.content_type = "application/octet-stream"

        with (
            patch.object(
                attachments_mixin,
                "fetch_attachment_content",
            ) as mock_fetch,
            patch(
                "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
                return_value=mock_attachment,
            ),
        ):
            result = attachments_mixin.get_issue_attachment_contents("TEST-123")

            # fetch_attachment_content should NOT be called for oversized file
            mock_fetch.assert_not_called()
            assert len(result["attachments"]) == 0
            assert len(result["failed"]) == 1
            assert "50 MB" in result["failed"][0]["error"]

    def test_get_issue_attachment_contents_allows_zero_size(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Attachment with size=0 (metadata missing) still gets downloaded."""
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "unknown.bin",
                        "content": "https://test.url/unknown",
                    }
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        mock_attachment = MagicMock()
        mock_attachment.filename = "unknown.bin"
        mock_attachment.url = "https://test.url/unknown"
        mock_attachment.size = 0
        mock_attachment.content_type = "application/octet-stream"

        with (
            patch.object(
                attachments_mixin,
                "fetch_attachment_content",
                return_value=b"data",
            ) as mock_fetch,
            patch(
                "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
                return_value=mock_attachment,
            ),
        ):
            result = attachments_mixin.get_issue_attachment_contents("TEST-123")

            mock_fetch.assert_called_once()
            assert len(result["attachments"]) == 1
            assert len(result["failed"]) == 0

    def test_get_issue_attachment_contents_allows_small(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Attachment with size < 50MB gets downloaded normally."""
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "small.txt",
                        "content": "https://test.url/small",
                        "size": 1024,
                    }
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        mock_attachment = MagicMock()
        mock_attachment.filename = "small.txt"
        mock_attachment.url = "https://test.url/small"
        mock_attachment.size = 1024
        mock_attachment.content_type = "text/plain"

        with (
            patch.object(
                attachments_mixin,
                "fetch_attachment_content",
                return_value=b"small content",
            ) as mock_fetch,
            patch(
                "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
                return_value=mock_attachment,
            ),
        ):
            result = attachments_mixin.get_issue_attachment_contents("TEST-123")

            mock_fetch.assert_called_once()
            assert len(result["attachments"]) == 1
            assert len(result["failed"]) == 0

    def test_get_issue_attachment_contents_mixed_sizes(
        self, attachments_mixin: AttachmentsMixin
    ):
        """Mix of large and small attachments — only large ones skipped."""
        mock_issue = {
            "fields": {
                "attachment": [
                    {
                        "filename": "small.txt",
                        "content": "https://test.url/small",
                        "size": 1024,
                    },
                    {
                        "filename": "huge.bin",
                        "content": "https://test.url/huge",
                        "size": 60 * 1024 * 1024,
                    },
                    {
                        "filename": "medium.txt",
                        "content": "https://test.url/medium",
                        "size": 10 * 1024 * 1024,
                    },
                ]
            }
        }
        attachments_mixin.jira.issue.return_value = mock_issue

        mock_small = MagicMock()
        mock_small.filename = "small.txt"
        mock_small.url = "https://test.url/small"
        mock_small.size = 1024
        mock_small.content_type = "text/plain"

        mock_huge = MagicMock()
        mock_huge.filename = "huge.bin"
        mock_huge.url = "https://test.url/huge"
        mock_huge.size = 60 * 1024 * 1024
        mock_huge.content_type = "application/octet-stream"

        mock_medium = MagicMock()
        mock_medium.filename = "medium.txt"
        mock_medium.url = "https://test.url/medium"
        mock_medium.size = 10 * 1024 * 1024
        mock_medium.content_type = "text/plain"

        with (
            patch.object(
                attachments_mixin,
                "fetch_attachment_content",
                side_effect=[b"small data", b"medium data"],
            ) as mock_fetch,
            patch(
                "mcp_atlassian.models.jira.JiraAttachment.from_api_response",
                side_effect=[mock_small, mock_huge, mock_medium],
            ),
        ):
            result = attachments_mixin.get_issue_attachment_contents("TEST-123")

            # Only small and medium should be fetched
            assert mock_fetch.call_count == 2
            assert len(result["attachments"]) == 2
            assert len(result["failed"]) == 1
            assert result["failed"][0]["filename"] == "huge.bin"
            assert "50 MB" in result["failed"][0]["error"]
            filenames = [a["filename"] for a in result["attachments"]]
            assert "small.txt" in filenames
            assert "medium.txt" in filenames

    # Tests for path traversal rejection (PR #949 security hardening)

    @pytest.mark.parametrize(
        "malicious_path",
        [
            # Absolute paths that resolve outside cwd
            "/etc/passwd",
            "/tmp/evil/file.txt",
            "/var/log/secrets.txt",
        ],
        ids=[
            "absolute-etc-passwd",
            "absolute-tmp-evil",
            "absolute-var-log",
        ],
    )
    def test_download_attachment_rejects_path_traversal(
        self,
        attachments_mixin: AttachmentsMixin,
        malicious_path: str,
    ):
        """Regression: path traversal in attachment filenames must be rejected.

        The guard in download_attachment checks that the resolved absolute path
        is within cwd via ``Path(target_path).is_relative_to(cwd)``.  Paths
        outside cwd trigger a ValueError which is caught internally, causing
        the method to return False.
        """
        with patch("os.getcwd", return_value="/safe/working/dir"):
            result = attachments_mixin.download_attachment(
                "https://test.url/attachment",
                malicious_path,
            )
            assert result is False

    @pytest.mark.parametrize(
        "relative_path",
        [
            "../../../etc/passwd",
            "normal/../../../etc/shadow",
        ],
        ids=[
            "relative-traversal-etc-passwd",
            "relative-traversal-nested",
        ],
    )
    def test_download_attachment_rejects_relative_path_traversal(
        self,
        attachments_mixin: AttachmentsMixin,
        relative_path: str,
    ):
        """Regression: relative paths that resolve outside cwd are rejected.

        When a relative path is given, os.path.abspath resolves it.  If the
        resolved path escapes cwd, the guard rejects it and returns False.
        """
        import os

        cwd = os.getcwd()
        with patch("os.getcwd", return_value=cwd):
            result = attachments_mixin.download_attachment(
                "https://test.url/attachment",
                relative_path,
            )
            # The relative path resolves outside cwd (it traverses up)
            resolved = os.path.abspath(relative_path)
            if not Path(resolved).is_relative_to(cwd):
                assert result is False

    @pytest.mark.parametrize(
        "malicious_dir",
        [
            "/etc",
            "/tmp/evil",
            "/var/log",
        ],
        ids=[
            "absolute-etc",
            "absolute-tmp-evil",
            "absolute-var-log",
        ],
    )
    def test_download_issue_attachments_rejects_path_traversal(
        self,
        attachments_mixin: AttachmentsMixin,
        malicious_dir: str,
    ):
        """Regression: path traversal in target_dir must raise ValueError.

        Unlike download_attachment (which catches the error internally),
        download_issue_attachments lets the ValueError propagate.
        """
        with (
            patch("os.getcwd", return_value="/safe/working/dir"),
            pytest.raises(ValueError, match="Path traversal detected"),
        ):
            attachments_mixin.download_issue_attachments(
                "TEST-123",
                malicious_dir,
            )

    def test_get_issue_attachments_metadata(self, attachments_mixin: AttachmentsMixin):
        """get_issue_attachments returns JiraAttachment list without downloading."""
        attachments_mixin.jira.issue.return_value = {
            "fields": {
                "attachment": [
                    {
                        "id": "101",
                        "filename": "photo.png",
                        "size": 1024,
                        "mimeType": "image/png",
                        "content": "https://jira.example.com/att/101",
                    },
                    {
                        "id": "102",
                        "filename": "report.pdf",
                        "size": 2048,
                        "mimeType": "application/pdf",
                        "content": "https://jira.example.com/att/102",
                    },
                ]
            }
        }

        from mcp_atlassian.models.jira import JiraAttachment

        result = attachments_mixin.get_issue_attachments("TEST-123")
        assert len(result) == 2
        assert all(isinstance(a, JiraAttachment) for a in result)
        assert result[0].filename == "photo.png"
        assert result[0].content_type == "image/png"
        assert result[1].filename == "report.pdf"
        # No download calls should have been made
        attachments_mixin.jira._session.get.assert_not_called()


class TestUploadPathTraversalRegression:
    """Regression — upload-side attachment path traversal / arbitrary file read.

    Covers GHSA-wm45, vc25, 93xw, 6cr4, f4p7, f6pj, 2xj6, mrq8, wv8v, p6hp, h7wj,
    mfv2, f26r, 9547, cc5h (read half), and the 6vmq download-overwrite variant.

    The download side gained ``validate_safe_path`` (CVE-2026-27825), but the
    upload path used to feed any caller-supplied ``file_path`` to the API after
    only an ``os.path.exists`` check, so a path outside the workspace was read and
    exfiltrated. These tests assert the secure outcome: a traversal/absolute path
    never reaches the upload sink.

    Assertions target the *sink* (``jira.post`` — the upload request — and the
    legacy ``add_attachment`` both not called), not an exception
    type, because ``upload_attachment`` wraps its body in ``except Exception`` and
    returns an error dict — so the fix may reject by raising or by returning a dict,
    and either way the secret must never reach the API.
    """

    @pytest.fixture
    def attachments_mixin(self, jira_fetcher: JiraFetcher) -> AttachmentsMixin:
        """AttachmentsMixin with a mocked Jira client (Cloud by default)."""
        mixin = jira_fetcher
        mixin.jira = MagicMock()
        mixin.jira._session = MagicMock()
        return mixin

    @pytest.mark.security_regression
    @pytest.mark.parametrize("attack", ["absolute_outside_cwd", "relative_traversal"])
    def test_upload_attachment_does_not_read_outside_workspace(
        self,
        attachments_mixin: AttachmentsMixin,
        tmp_path: Path,
        attack: str,
    ) -> None:
        """A file_path resolving outside the workspace must not reach the API."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        secret = tmp_path / "secret.txt"  # sibling of workspace -> outside it
        secret.write_bytes(b"SECRET-EXFIL")
        malicious = str(secret) if attack == "absolute_outside_cwd" else "../secret.txt"

        with patch("os.getcwd", return_value=str(workspace)):
            attachments_mixin.upload_attachment("PROJ-1", malicious)

        attachments_mixin.jira.post.assert_not_called()
        attachments_mixin.jira._session.post.assert_not_called()
        attachments_mixin.jira.add_attachment.assert_not_called()

    @pytest.mark.security_regression
    def test_upload_attachments_list_does_not_read_outside_workspace(
        self,
        attachments_mixin: AttachmentsMixin,
        tmp_path: Path,
    ) -> None:
        """The list/confused-deputy path (update_issue -> upload_attachments)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"SECRET-EXFIL")

        with patch("os.getcwd", return_value=str(workspace)):
            attachments_mixin.upload_attachments("PROJ-1", [str(secret)])

        attachments_mixin.jira.post.assert_not_called()
        attachments_mixin.jira._session.post.assert_not_called()
        attachments_mixin.jira.add_attachment.assert_not_called()

    @pytest.mark.security_regression
    def test_download_into_cwd_module_is_rejected(
        self,
        attachments_mixin: AttachmentsMixin,
        tmp_path: Path,
    ) -> None:
        """Downloading onto a source file inside CWD must be confined/rejected."""
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [b"import os; os.system('id')\n"]
        mock_response.raise_for_status = MagicMock()
        attachments_mixin.jira._session.get.return_value = mock_response

        with (
            patch("builtins.open", mock_open()),
            patch("os.path.exists", return_value=True),
            patch("os.path.getsize", return_value=27),
            patch("os.makedirs"),
            patch("os.getcwd", return_value=str(tmp_path)),
        ):
            result = attachments_mixin.download_attachment(
                "https://test.url/evil",
                "important_module.py",  # relative -> resolves inside CWD
            )

        assert result is False, (
            "download targeting the CWD (where Python imports modules) must be "
            "confined to a dedicated directory, not allowed to overwrite source files"
        )
