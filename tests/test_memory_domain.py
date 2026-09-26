"""
Tests for the AI memory plane (P0 foundation, migration 25):
open_notebook/domain/memory.py and open_notebook/domain/exploration.py.

The memory plane is the AI's own recall layer - separate from the user's
knowledge tree - so the behaviors that matter here are:
  * a memory always carries its provenance (derived_from) or can be invalidated
    when its origin changes (stale / superseded), and
  * the only way a memory becomes knowledge is an explicit, user-triggered
    promotion (promoted_to), never an automatic write into the tree.

DB access is mocked at the domain module boundary, following the style of
tests/test_note_save_embed_resilience.py.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_notebook.domain.exploration import ExplorationPoint
from open_notebook.domain.memory import MemoryCore, MemoryItem, memory_search
from open_notebook.exceptions import DatabaseOperationError, InvalidInputError


def make_memory(**overrides):
    defaults = dict(
        id="memory_item:m1",
        content="Late blight spreads through airborne sporangia",
        kind="fact",
        status="active",
        origin="derived",
    )
    defaults.update(overrides)
    return MemoryItem(**defaults)


class TestMemoryItemValidation:
    def test_content_is_required_and_cannot_be_blank(self):
        with pytest.raises(InvalidInputError):
            MemoryItem(content="   ")

    def test_record_field_is_bound_as_record_id_on_save(self):
        memory = make_memory(notebook="notebook:nb1")
        prepared = memory._prepare_save_data()
        # A plain string would be rejected by the SCHEMAFULL record<notebook> field
        assert str(prepared["notebook"]) == "notebook:nb1"

    def test_absent_record_field_is_saved_as_none(self):
        # Listed in nullable_fields, so an unscoped memory is explicitly written
        # as NONE rather than left over from a previous save.
        assert make_memory()._prepare_save_data()["notebook"] is None


class TestMemoryItemSaveSubmitsEmbedding:
    @pytest.mark.asyncio
    async def test_save_submits_embed_memory(self):
        memory = make_memory()
        with (
            patch("open_notebook.domain.base.ObjectModel.save", new=AsyncMock()),
            patch(
                "open_notebook.domain.memory.submit_command",
                return_value="command:1",
            ) as mock_submit,
        ):
            command_id = await memory.save()

        assert command_id == "command:1"
        mock_submit.assert_called_once_with(
            "open_notebook", "embed_memory", {"memory_id": "memory_item:m1"}
        )

    @pytest.mark.asyncio
    async def test_save_survives_submission_failure(self):
        """The memory is already durable; a queue hiccup must not fail the save."""
        memory = make_memory()
        with (
            patch(
                "open_notebook.domain.base.ObjectModel.save", new=AsyncMock()
            ) as mock_super_save,
            patch(
                "open_notebook.domain.memory.submit_command",
                side_effect=RuntimeError("job queue is down"),
            ),
        ):
            command_id = await memory.save()

        assert command_id is None
        mock_super_save.assert_awaited_once()


class TestMemoryLifecycle:
    @pytest.mark.asyncio
    async def test_set_status_writes_and_updates_instance(self):
        memory = make_memory()
        moment = datetime(2026, 9, 26, 12, 0, 0)
        with patch(
            "open_notebook.domain.memory.repo_update", new=AsyncMock()
        ) as mock_update:
            await memory.set_status("stale", valid_until=moment)

        mock_update.assert_awaited_once_with(
            "memory_item",
            "memory_item:m1",
            {"status": "stale", "valid_until": moment},
        )
        assert memory.status == "stale"
        assert memory.valid_until == moment

    @pytest.mark.asyncio
    async def test_set_status_omits_valid_until_when_not_given(self):
        memory = make_memory()
        with patch(
            "open_notebook.domain.memory.repo_update", new=AsyncMock()
        ) as mock_update:
            await memory.set_status("pending")

        mock_update.assert_awaited_once_with(
            "memory_item", "memory_item:m1", {"status": "pending"}
        )

    @pytest.mark.asyncio
    async def test_set_status_wraps_database_failure(self):
        memory = make_memory()
        with patch(
            "open_notebook.domain.memory.repo_update",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(DatabaseOperationError):
                await memory.set_status("stale")

    @pytest.mark.asyncio
    async def test_supersede_links_new_to_old_and_closes_old(self):
        newer = make_memory(id="memory_item:new")
        older = make_memory(id="memory_item:old")
        with (
            patch.object(MemoryItem, "link", new=AsyncMock()) as mock_link,
            patch.object(MemoryItem, "set_status", new=AsyncMock()) as mock_status,
        ):
            await newer.supersede(older, reason="newer sample data")

        mock_link.assert_awaited_once_with(
            "supersedes", "memory_item:old", {"reason": "newer sample data"}
        )
        # The old memory is closed off, not deleted - it stays readable as history
        status_args = mock_status.await_args
        assert status_args.args[0] == "superseded"
        assert status_args.kwargs["valid_until"] is not None

    @pytest.mark.asyncio
    async def test_supersede_requires_a_saved_memory(self):
        with pytest.raises(InvalidInputError):
            await make_memory().supersede(MemoryItem(content="unsaved", id=None))

    @pytest.mark.asyncio
    async def test_approve_marks_confirmed_and_activates(self):
        memory = make_memory(status="pending", origin="inferred")
        with patch.object(MemoryItem, "set_status", new=AsyncMock()) as mock_status:
            await memory.approve()

        mock_status.assert_awaited_once_with("active")
        assert memory.origin == "confirmed"


class TestMemoryRelations:
    @pytest.mark.asyncio
    async def test_link_rejects_unknown_relationship(self):
        with pytest.raises(InvalidInputError):
            await make_memory().link("memory_item:other", "note:1")

    @pytest.mark.asyncio
    async def test_link_accepts_declared_relationship(self):
        memory = make_memory()
        with patch("open_notebook.domain.base.repo_relate", new=AsyncMock()) as mock_rel:
            await memory.link("memory_item:other", "supports", {"score": 0.9})

        mock_rel.assert_awaited_once_with(
            source="memory_item:m1",
            relationship="supports",
            target="memory_item:other",
            data={"score": 0.9},
        )

    @pytest.mark.asyncio
    async def test_relate_provenance_requires_a_target(self):
        with pytest.raises(InvalidInputError):
            await make_memory().relate_provenance("")

    @pytest.mark.asyncio
    async def test_get_provenance_reads_derived_from_edge(self):
        memory = make_memory()
        rows = [{"out": "source:s1", "kind": "source"}]
        with patch(
            "open_notebook.domain.memory.repo_query",
            new=AsyncMock(return_value=rows),
        ) as mock_query:
            result = await memory.get_provenance()

        assert result == rows
        assert "derived_from" in mock_query.await_args.args[0]

    @pytest.mark.asyncio
    async def test_get_related_ids_rejects_unknown_relationship(self):
        with pytest.raises(InvalidInputError):
            await make_memory().get_related_ids("' OR 1=1 --")

    @pytest.mark.asyncio
    async def test_get_related_ids_returns_both_directions(self):
        with patch(
            "open_notebook.domain.memory.repo_query",
            new=AsyncMock(return_value=["memory_item:x", None, "memory_item:y"]),
        ):
            result = await make_memory().get_related_ids("supports")

        assert result == ["memory_item:x", "memory_item:y"]


class TestMemoryPromotion:
    @pytest.mark.asyncio
    async def test_promote_creates_ai_note_and_links_back(self):
        memory = make_memory(
            content="# Late blight\nSporangia travel on the wind",
            tags=["phytopathology"],
        )
        fake_note = MagicMock()
        fake_note.id = "note:promoted"
        fake_note.save = AsyncMock()
        fake_note.add_to_notebook = AsyncMock()

        with (
            patch(
                "open_notebook.domain.notebook.Note", return_value=fake_note
            ) as mock_note_cls,
            patch.object(MemoryItem, "link", new=AsyncMock()) as mock_link,
        ):
            result = await memory.promote_to_note(notebook_id="notebook:nb1")

        assert result is fake_note
        kwargs = mock_note_cls.call_args.kwargs
        assert kwargs["note_type"] == "ai"
        assert kwargs["note_kind"] == "promoted_memory"
        assert kwargs["title"] == "Late blight"
        assert kwargs["tags"] == ["phytopathology"]
        fake_note.save.assert_awaited_once()
        fake_note.add_to_notebook.assert_awaited_once_with("notebook:nb1")
        mock_link.assert_awaited_once_with("promoted_to", "note:promoted")
        assert memory.origin == "confirmed"

    @pytest.mark.asyncio
    async def test_promote_without_notebook_skips_notebook_link(self):
        memory = make_memory()
        fake_note = MagicMock()
        fake_note.id = "note:promoted"
        fake_note.save = AsyncMock()
        fake_note.add_to_notebook = AsyncMock()

        with (
            patch("open_notebook.domain.notebook.Note", return_value=fake_note),
            patch.object(MemoryItem, "link", new=AsyncMock()),
        ):
            await memory.promote_to_note()

        fake_note.add_to_notebook.assert_not_called()


class TestMemoryQueries:
    @pytest.mark.asyncio
    async def test_get_for_notebook_requires_notebook_id(self):
        with pytest.raises(InvalidInputError):
            await MemoryItem.get_for_notebook("")

    @pytest.mark.asyncio
    async def test_get_for_notebook_excludes_superseded_by_default(self):
        row = dict(id="memory_item:m1", content="a fact", status="active")
        with patch(
            "open_notebook.domain.memory.repo_query",
            new=AsyncMock(return_value=[row]),
        ) as mock_query:
            result = await MemoryItem.get_for_notebook("notebook:nb1")

        assert len(result) == 1
        assert isinstance(result[0], MemoryItem)
        query, vars = mock_query.await_args.args
        assert "status IN $statuses" in query
        assert "superseded" not in vars["statuses"]

    @pytest.mark.asyncio
    async def test_find_neighbors_requires_an_embedding(self):
        with pytest.raises(InvalidInputError):
            await MemoryItem.find_neighbors([])

    @pytest.mark.asyncio
    async def test_find_neighbors_scopes_to_notebook(self):
        with patch(
            "open_notebook.domain.memory.repo_query",
            new=AsyncMock(return_value=[]),
        ) as mock_query:
            await MemoryItem.find_neighbors([0.1, 0.2], notebook_id="notebook:nb1")

        assert "fn::memory_search" in mock_query.await_args.args[0]
        assert str(mock_query.await_args.args[1]["notebook_ids"][0]) == "notebook:nb1"

    @pytest.mark.asyncio
    async def test_memory_search_orders_by_similarity_desc(self):
        rows = [
            {"id": "memory_item:a", "similarity": 0.4},
            {"id": "memory_item:b", "similarity": 0.9},
        ]
        with (
            patch(
                "open_notebook.utils.embedding.generate_embedding",
                new=AsyncMock(return_value=[0.1, 0.2]),
            ),
            patch(
                "open_notebook.domain.memory.repo_query",
                new=AsyncMock(return_value=rows),
            ),
        ):
            result = await memory_search("late blight")

        assert [row["id"] for row in result] == ["memory_item:b", "memory_item:a"]

    @pytest.mark.asyncio
    async def test_memory_search_rejects_empty_keyword(self):
        with pytest.raises(InvalidInputError):
            await memory_search("")

    @pytest.mark.asyncio
    async def test_memory_search_wraps_database_failure(self):
        with (
            patch(
                "open_notebook.utils.embedding.generate_embedding",
                new=AsyncMock(return_value=[0.1]),
            ),
            patch(
                "open_notebook.domain.memory.repo_query",
                new=AsyncMock(side_effect=RuntimeError("db down")),
            ),
        ):
            with pytest.raises(DatabaseOperationError):
                await memory_search("late blight")


class TestMemoryCore:
    def test_defaults_are_editable_and_empty(self):
        core = MemoryCore()
        assert core.record_id == "open_notebook:memory_core"
        assert core.profile == ""
        assert core.domains is None
        assert core.preferences is None


class TestExplorationPoint:
    def test_notebook_record_field_is_bound_on_save(self):
        point = ExplorationPoint(question="How does it overwinter?", notebook="notebook:nb1")
        assert str(point._prepare_save_data()["notebook"]) == "notebook:nb1"

    def test_default_status_is_proposed(self):
        assert ExplorationPoint(question="q").status == "proposed"

    @pytest.mark.asyncio
    async def test_anchor_to_creates_explores_edge(self):
        point = ExplorationPoint(id="exploration_point:e1", question="q")
        with patch("open_notebook.domain.base.repo_relate", new=AsyncMock()) as mock_rel:
            await point.anchor_to("note:leaf", reason="leaf of the tree")

        mock_rel.assert_awaited_once_with(
            source="exploration_point:e1",
            relationship="explores",
            target="note:leaf",
            data={"reason": "leaf of the tree"},
        )

    @pytest.mark.asyncio
    async def test_anchor_to_requires_a_note(self):
        with pytest.raises(InvalidInputError):
            await ExplorationPoint(id="exploration_point:e1", question="q").anchor_to("")

    @pytest.mark.asyncio
    async def test_set_status_updates_instance(self):
        point = ExplorationPoint(id="exploration_point:e1", question="q")
        with patch(
            "open_notebook.domain.exploration.repo_update", new=AsyncMock()
        ) as mock_update:
            await point.set_status("dismissed")

        mock_update.assert_awaited_once_with(
            "exploration_point", "exploration_point:e1", {"status": "dismissed"}
        )
        assert point.status == "dismissed"

    @pytest.mark.asyncio
    async def test_get_for_note_reads_through_explores_edge(self):
        with patch(
            "open_notebook.domain.exploration.repo_query",
            new=AsyncMock(return_value=[{"id": "exploration_point:e1", "question": "q"}]),
        ) as mock_query:
            result = await ExplorationPoint.get_for_note("note:leaf")

        assert len(result) == 1
        assert "explores" in mock_query.await_args.args[0]
        assert str(mock_query.await_args.args[1]["note_id"]) == "note:leaf"

    @pytest.mark.asyncio
    async def test_get_for_notebook_requires_notebook_id(self):
        with pytest.raises(InvalidInputError):
            await ExplorationPoint.get_for_notebook("")
