"""
Tests for the knowledge-tree half of migration 25:
open_notebook/domain/notebook.py (Note tree traversal + proposals) and the
migration registration itself.

The tree is carried by the `includes` edge rather than a parent field, so these
tests pin the edge direction conventions (in = parent, out = child in
SurrealDB's RELATE) because getting them backwards would silently invert every
tree traversal.
"""

from unittest.mock import AsyncMock, patch

import pytest

from open_notebook.domain.memory import MemoryItem
from open_notebook.domain.notebook import Note
from open_notebook.exceptions import DatabaseOperationError, InvalidInputError


def make_note(**overrides):
    defaults = dict(id="note:parent", title="Parent", content="parent body")
    defaults.update(overrides)
    return Note(**defaults)


class TestNoteMetadata:
    def test_new_metadata_fields_default_to_none(self):
        note = Note(title="t", content="c")
        assert note.tags is None
        assert note.keywords is None
        assert note.note_kind is None
        assert note.status is None
        assert note.proposal_status is None
        assert note.summary is None

    def test_proposal_status_is_constrained(self):
        with pytest.raises(Exception):
            Note(title="t", content="c", proposal_status="maybe")


class TestNoteTreeEdges:
    @pytest.mark.asyncio
    async def test_add_child_creates_includes_edge(self):
        parent = make_note()
        with (
            patch("open_notebook.domain.notebook.Note.get", new=AsyncMock()),
            patch("open_notebook.domain.base.repo_relate", new=AsyncMock()) as mock_rel,
        ):
            await parent.add_child("note:child")

        mock_rel.assert_awaited_once_with(
            source="note:parent",
            relationship="includes",
            target="note:child",
            data={},
        )

    @pytest.mark.asyncio
    async def test_add_child_rejects_self_reference(self):
        with pytest.raises(InvalidInputError):
            await make_note().add_child("note:parent")

    @pytest.mark.asyncio
    async def test_add_child_requires_an_id(self):
        with pytest.raises(InvalidInputError):
            await make_note().add_child("")

    @pytest.mark.asyncio
    async def test_remove_child_deletes_the_edge(self):
        with patch(
            "open_notebook.domain.notebook.repo_query", new=AsyncMock()
        ) as mock_query:
            await make_note().remove_child("note:child")

        query, vars = mock_query.await_args.args
        assert "DELETE includes" in query
        assert str(vars["parent"]) == "note:parent"
        assert str(vars["child"]) == "note:child"

    @pytest.mark.asyncio
    async def test_get_children_reads_out_of_the_edge(self):
        note = make_note()
        child = {"id": "note:child", "title": "Child", "content": "child body"}
        with patch(
            "open_notebook.domain.notebook.repo_query",
            new=AsyncMock(side_effect=[["note:child"], [child]]),
        ) as mock_query:
            children = await note.get_children()

        assert [c.id for c in children] == ["note:child"]
        assert "SELECT VALUE out FROM includes WHERE in = $id" in (
            mock_query.await_args_list[0].args[0]
        )

    @pytest.mark.asyncio
    async def test_get_parents_reads_into_the_edge(self):
        note = make_note(id="note:child")
        with patch(
            "open_notebook.domain.notebook.repo_query",
            new=AsyncMock(side_effect=[["note:parent"], [{"id": "note:parent"}]]),
        ) as mock_query:
            parents = await note.get_parents()

        assert [p.id for p in parents] == ["note:parent"]
        assert "SELECT VALUE in FROM includes WHERE out = $id" in (
            mock_query.await_args_list[0].args[0]
        )

    @pytest.mark.asyncio
    async def test_get_children_short_circuits_when_no_ids(self):
        with patch(
            "open_notebook.domain.notebook.repo_query",
            new=AsyncMock(return_value=[]),
        ) as mock_query:
            children = await make_note().get_children()

        assert children == []
        mock_query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_related_unions_both_directions(self):
        with patch(
            "open_notebook.domain.notebook.repo_query",
            new=AsyncMock(side_effect=[["note:a"], [{"id": "note:a"}]]),
        ) as mock_query:
            await make_note().get_related()

        query = mock_query.await_args_list[0].args[0]
        assert "relates_to WHERE in = $id" in query
        assert "relates_to WHERE out = $id" in query

    @pytest.mark.asyncio
    async def test_traversal_requires_a_saved_note(self):
        with pytest.raises(InvalidInputError):
            await Note(title="t", content="c").get_children()

    @pytest.mark.asyncio
    async def test_traversal_wraps_database_failure(self):
        with patch(
            "open_notebook.domain.notebook.repo_query",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            with pytest.raises(DatabaseOperationError):
                await make_note().get_children()


class TestNoteLeafDetection:
    @pytest.mark.asyncio
    async def test_is_leaf_when_no_children(self):
        with patch(
            "open_notebook.domain.notebook.repo_query",
            new=AsyncMock(return_value=[]),
        ):
            assert await make_note().is_leaf() is True

    @pytest.mark.asyncio
    async def test_is_not_leaf_when_children_exist(self):
        with patch(
            "open_notebook.domain.notebook.repo_query",
            new=AsyncMock(return_value=["note:child"]),
        ):
            assert await make_note().is_leaf() is False

    @pytest.mark.asyncio
    async def test_is_leaf_requires_a_saved_note(self):
        with pytest.raises(InvalidInputError):
            await Note(title="t", content="c").is_leaf()


class TestNoteRelatedAndProposals:
    @pytest.mark.asyncio
    async def test_link_related_creates_relates_to_edge(self):
        with patch("open_notebook.domain.base.repo_relate", new=AsyncMock()) as mock_rel:
            await make_note().link_related("note:other", reason="same topic")

        mock_rel.assert_awaited_once_with(
            source="note:parent",
            relationship="relates_to",
            target="note:other",
            data={"reason": "same topic"},
        )

    @pytest.mark.asyncio
    async def test_set_proposal_status_writes_and_updates_instance(self):
        note = Note(id="note:proposal", title="t", content="c")
        with patch(
            "open_notebook.domain.notebook.repo_update", new=AsyncMock()
        ) as mock_update:
            await note.set_proposal_status("accepted", status="active")

        mock_update.assert_awaited_once_with(
            "note",
            "note:proposal",
            {"proposal_status": "accepted", "status": "active"},
        )
        assert note.proposal_status == "accepted"
        assert note.status == "active"

    @pytest.mark.asyncio
    async def test_set_proposal_status_requires_a_saved_note(self):
        with pytest.raises(InvalidInputError):
            await Note(title="t", content="c").set_proposal_status("accepted")


class TestNoteMemories:
    @pytest.mark.asyncio
    async def test_get_memories_returns_memory_items(self):
        row = dict(id="memory_item:m1", content="a fact", status="active")
        with patch(
            "open_notebook.domain.notebook.repo_query",
            new=AsyncMock(return_value=[row]),
        ) as mock_query:
            memories = await make_note().get_memories()

        assert len(memories) == 1
        assert isinstance(memories[0], MemoryItem)
        query, vars = mock_query.await_args.args
        assert "derived_from" in query
        assert "superseded" not in vars["statuses"]

    @pytest.mark.asyncio
    async def test_get_memories_requires_a_saved_note(self):
        with pytest.raises(InvalidInputError):
            await Note(title="t", content="c").get_memories()


class TestMigration25Registration:
    """Migrations are hard-coded in AsyncMigrationManager, not discovered."""

    def test_migration_is_registered_in_both_directions(self):
        from open_notebook.database.async_migrate import AsyncMigrationManager

        manager = AsyncMigrationManager()
        assert len(manager.up_migrations) >= 25
        assert len(manager.up_migrations) == len(manager.down_migrations)

    def test_up_migration_defines_the_memory_plane_and_edges(self):
        from open_notebook.database.async_migrate import AsyncMigrationManager

        sql = AsyncMigrationManager().up_migrations[24].sql
        assert "memory_item" in sql
        assert "exploration_point" in sql
        assert "memory_core" in sql
        assert "TYPE RELATION IN note OUT note" in sql
        assert "IN note, memory_item OUT source, note, memory_item" in sql
        assert "fn::memory_search" in sql

    def test_down_migration_drops_what_up_created(self):
        from open_notebook.database.async_migrate import AsyncMigrationManager

        sql = AsyncMigrationManager().down_migrations[24].sql
        assert "REMOVE TABLE IF EXISTS memory_item" in sql
        assert "REMOVE TABLE IF EXISTS exploration_point" in sql
        assert "REMOVE FUNCTION IF EXISTS fn::memory_search" in sql
        assert "REMOVE FIELD IF EXISTS tags ON TABLE note" in sql
