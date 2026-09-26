from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client after environment variables have been cleared by conftest."""
    from api.main import app

    return TestClient(app)


def _note_mock(**overrides):
    """A Note stand-in exposing every field the API response reads.

    The knowledge-tree metadata (migration 25) is None for a plain note; a
    MagicMock would leak MagicMock objects into NoteResponse and fail
    validation, which is what these tests exist to catch.
    """
    mock_note = AsyncMock()
    mock_note.id = "note:abc123"
    mock_note.title = "Test Note"
    mock_note.content = "Some content"
    mock_note.note_type = "human"
    mock_note.created = "2026-01-01T00:00:00Z"
    mock_note.updated = "2026-01-01T00:00:00Z"
    mock_note.summary = None
    mock_note.tags = None
    mock_note.keywords = None
    mock_note.note_kind = None
    mock_note.status = None
    mock_note.proposal_status = None
    mock_note.generated_by = None
    for key, value in overrides.items():
        setattr(mock_note, key, value)
    return mock_note


class TestNoteCreation:
    """Test suite for Note API endpoints."""

    @patch("api.routers.notes.Note")
    def test_create_note_returns_command_id(self, mock_note_cls, client):
        """Test that creating a note returns the embed command_id."""
        mock_note = _note_mock()
        mock_note.save.return_value = "command:embed123"
        mock_note.add_to_notebook = AsyncMock()
        mock_note_cls.return_value = mock_note

        response = client.post(
            "/api/notes",
            json={"content": "Some content", "note_type": "human"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["command_id"] == "command:embed123"
        assert data["id"] == "note:abc123"

    @patch("api.routers.notes.Note")
    def test_create_note_command_id_none_when_no_content_embedding(
        self, mock_note_cls, client
    ):
        """Test that command_id is None when save returns None (no embedding)."""
        mock_note = _note_mock(id="note:abc456", title="Empty Note")
        mock_note.save.return_value = None
        mock_note.add_to_notebook = AsyncMock()
        mock_note_cls.return_value = mock_note

        response = client.post(
            "/api/notes",
            json={"content": "Some content", "note_type": "human"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["command_id"] is None


class TestNoteUpdate:
    """Test suite for Note update endpoint."""

    @patch("api.routers.notes.Note")
    def test_update_note_returns_command_id(self, mock_note_cls, client):
        """Test that updating a note returns the embed command_id."""
        mock_note = _note_mock(content="Original content")
        mock_note.save.return_value = "command:embed789"
        mock_note_cls.get = AsyncMock(return_value=mock_note)

        response = client.put(
            "/api/notes/note:abc123",
            json={"content": "Updated content"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["command_id"] == "command:embed789"

    @patch("api.routers.notes.Note")
    def test_update_note_command_id_none_when_no_embedding(
        self, mock_note_cls, client
    ):
        """Test that command_id is None on update when no embedding is triggered."""
        mock_note = _note_mock()
        mock_note.save.return_value = None
        mock_note_cls.get = AsyncMock(return_value=mock_note)

        response = client.put(
            "/api/notes/note:abc123",
            json={"title": "Updated Title"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["command_id"] is None
