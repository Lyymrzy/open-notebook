"""
Tests for knowledge iteration (P1): the iterate graph helpers, the
synthesize/refine commands and the proposal endpoints.

The behaviors that matter here are the safety properties, not the LLM output:
  * refining never writes to the note it revises - it only proposes;
  * a proposal is not attached to a notebook, so it cannot leak into note
    listings or chat context before it is accepted;
  * material is id-labelled and budgeted, on the record, so a note built from
    it stays traceable.

DB access and the graph are mocked at the module boundary, following the style
of tests/test_note_save_embed_resilience.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from commands.iterate_commands import (
    MAX_MATERIAL_CHARS,
    RefineNoteInput,
    SynthesizeNotesInput,
    _render_material,
    refine_note_command,
    synthesize_notes_command,
)
from open_notebook.domain.notebook import Note, Source
from open_notebook.graphs.iterate import split_titled_markdown


def make_note(**overrides):
    defaults = dict(id="note:1", title="Original", content="original body")
    defaults.update(overrides)
    return Note(**defaults)


def make_source(**overrides):
    defaults = dict(id="source:1", title="Paper", full_text="full text")
    defaults.update(overrides)
    return Source(**defaults)


class TestSplitTitledMarkdown:
    def test_splits_title_from_body(self):
        title, body = split_titled_markdown("# Blight spread\n\nSporangia travel.")
        assert title == "Blight spread"
        assert body == "Sporangia travel."

    def test_ignores_leading_blank_lines(self):
        title, body = split_titled_markdown("\n\n# Real title\nbody")
        assert title == "Real title"
        assert body == "body"

    def test_returns_none_title_when_missing(self):
        title, body = split_titled_markdown("Just a body without a title.")
        assert title is None
        assert body == "Just a body without a title."

    def test_h2_is_not_treated_as_the_title(self):
        title, body = split_titled_markdown("## Section\nbody")
        assert title is None
        assert body == "## Section\nbody"

    def test_empty_input(self):
        title, body = split_titled_markdown("")
        assert title is None
        assert body == ""

    def test_title_only_note(self):
        title, body = split_titled_markdown("# Only a title")
        assert title == "Only a title"
        assert body == ""

    def test_title_is_truncated(self):
        title, _ = split_titled_markdown("# " + "x" * 500)
        assert title is not None
        assert len(title) <= 200


class TestRenderMaterial:
    def test_labels_every_item_with_its_id(self):
        material = _render_material(
            notes=[make_note()], sources=[make_source()], memories=[]
        )
        assert "[note:1]" in material
        assert "[source:1]" in material
        assert "original body" in material
        assert "full text" in material

    def test_memories_come_last_and_are_labelled(self):
        memory = MagicMock()
        memory.id = "memory_item:m1"
        memory.content = "a remembered fact"
        memory.kind = "fact"
        memory.confidence = 0.8
        memory.status = "active"

        material = _render_material(
            notes=[make_note()], sources=[], memories=[memory]
        )

        assert material.index("[note:1]") < material.index("[memory_item:m1]")
        assert "AI MEMORY" in material
        assert "a remembered fact" in material

    def test_oversized_item_is_truncated_in_the_prompt(self):
        huge = make_source(full_text="x" * (MAX_MATERIAL_CHARS + 1000))
        material = _render_material(notes=[], sources=[huge], memories=[])
        assert "[Truncated: source:1" in material
        assert len(material) < MAX_MATERIAL_CHARS + 1000

    def test_items_beyond_the_budget_are_named_as_omitted(self):
        # Notes are rendered first, so a note that fills the budget leaves a
        # following note with nothing.
        huge = make_note(id="note:big", content="x" * MAX_MATERIAL_CHARS)
        later = make_note(id="note:later")
        material = _render_material(notes=[huge, later], sources=[], memories=[])
        assert "Omitted to fit the context budget" in material
        assert "note:later" in material


def _patched_note_class(existing=None, created=None):
    """A MagicMock Note class with an async `get` and record constructors."""
    note_cls = MagicMock()
    note_cls.get = AsyncMock(return_value=existing or make_note())
    instance = created or MagicMock()
    instance.id = "note:new"
    instance.title = "New note"
    instance.save = AsyncMock(return_value="command:1")
    instance.add_to_notebook = AsyncMock()
    instance.relate = AsyncMock()
    note_cls.return_value = instance
    return note_cls, instance


class TestSynthesizeNotesCommand:
    @pytest.mark.asyncio
    async def test_requires_material(self):
        result = await synthesize_notes_command(SynthesizeNotesInput())

        assert result.success is False
        assert "At least one note or source" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_unknown_note_id_fails_permanently(self):
        with patch("commands.iterate_commands.Note") as note_cls:
            note_cls.get = AsyncMock(side_effect=RuntimeError("not found"))
            result = await synthesize_notes_command(
                SynthesizeNotesInput(note_ids=["note:missing"])
            )

        assert result.success is False
        assert "note:missing" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_creates_an_ai_note_with_provenance(self):
        note_cls, created = _patched_note_class()
        with (
            patch("commands.iterate_commands.Note", note_cls),
            patch(
                "commands.iterate_commands.iterate_graph",
                MagicMock(
                    ainvoke=AsyncMock(
                        return_value={"output": "synthesized body", "title": "Synth"}
                    )
                ),
            ),
        ):
            result = await synthesize_notes_command(
                SynthesizeNotesInput(note_ids=["note:1"], notebook_id="notebook:nb1")
            )

        assert result.success is True
        assert result.note_id == "note:new"

        kwargs = note_cls.call_args.kwargs
        assert kwargs["title"] == "Synth"
        assert kwargs["note_type"] == "ai"
        assert kwargs["note_kind"] == "synthesis"
        assert kwargs["content"] == "synthesized body"
        created.add_to_notebook.assert_awaited_once_with("notebook:nb1")
        created.relate.assert_awaited_once_with(
            "derived_from", "note:1", {"kind": "note"}
        )

    @pytest.mark.asyncio
    async def test_grows_out_of_a_parent_note(self):
        parent = MagicMock()
        parent.add_child = AsyncMock()
        note_cls, _created = _patched_note_class()

        async def get_by_id(note_id):
            if note_id == "note:parent":
                return parent
            return make_note()

        note_cls.get = AsyncMock(side_effect=get_by_id)

        with (
            patch("commands.iterate_commands.Note", note_cls),
            patch(
                "commands.iterate_commands.iterate_graph",
                MagicMock(
                    ainvoke=AsyncMock(
                        return_value={"output": "body", "title": "T"}
                    )
                ),
            ),
        ):
            result = await synthesize_notes_command(
                SynthesizeNotesInput(note_ids=["note:1"], parent_note_id="note:parent")
            )

        assert result.success is True
        parent.add_child.assert_awaited_once_with("note:new")

    @pytest.mark.asyncio
    async def test_missing_parent_fails_permanently(self):
        note_cls, _created = _patched_note_class()

        async def get_by_id(note_id):
            if note_id == "note:parent":
                raise RuntimeError("not found")
            return make_note()

        note_cls.get = AsyncMock(side_effect=get_by_id)
        with patch("commands.iterate_commands.Note", note_cls):
            result = await synthesize_notes_command(
                SynthesizeNotesInput(note_ids=["note:1"], parent_note_id="note:parent")
            )

        assert result.success is False
        assert "Parent note" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_empty_model_output_fails(self):
        note_cls, _created = _patched_note_class()
        with (
            patch("commands.iterate_commands.Note", note_cls),
            patch(
                "commands.iterate_commands.iterate_graph",
                MagicMock(ainvoke=AsyncMock(return_value={"output": "  "})),
            ),
        ):
            result = await synthesize_notes_command(
                SynthesizeNotesInput(note_ids=["note:1"])
            )

        assert result.success is False
        assert "empty note" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_transient_graph_failure_is_reraised_for_retry(self):
        note_cls, _created = _patched_note_class()
        with (
            patch("commands.iterate_commands.Note", note_cls),
            patch(
                "commands.iterate_commands.iterate_graph",
                MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("provider down"))),
            ),
        ):
            with pytest.raises(RuntimeError):
                await synthesize_notes_command(
                    SynthesizeNotesInput(note_ids=["note:1"])
                )


class TestRefineNoteCommand:
    def _target(self, content="original body"):
        target = MagicMock()
        target.id = "note:t"
        target.title = "Original"
        target.content = content
        target.get_memories = AsyncMock(return_value=[])
        target.save = AsyncMock()
        return target

    @pytest.mark.asyncio
    async def test_unchanged_result_creates_no_proposal_and_writes_nothing(self):
        target = self._target()
        note_cls, _created = _patched_note_class(existing=target)
        with (
            patch("commands.iterate_commands.Note", note_cls),
            patch(
                "commands.iterate_commands.iterate_graph",
                MagicMock(
                    ainvoke=AsyncMock(
                        return_value={
                            "output": "original body",
                            "title": "Original",
                        }
                    )
                ),
            ),
        ):
            result = await refine_note_command(RefineNoteInput(note_id="note:t"))

        assert result.success is True
        assert result.changed is False
        assert result.proposal_id is None
        target.save.assert_not_awaited()
        note_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_result_creates_a_pending_detached_proposal(self):
        target = self._target()
        note_cls, created = _patched_note_class(existing=target)
        with (
            patch("commands.iterate_commands.Note", note_cls),
            patch(
                "commands.iterate_commands.iterate_graph",
                MagicMock(
                    ainvoke=AsyncMock(
                        return_value={
                            "output": "improved body",
                            "title": "Improved",
                        }
                    )
                ),
            ),
        ):
            result = await refine_note_command(RefineNoteInput(note_id="note:t"))

        assert result.success is True
        assert result.changed is True
        assert result.proposal_id == "note:new"
        assert result.target_note_id == "note:t"

        kwargs = note_cls.call_args.kwargs
        assert kwargs["note_kind"] == "refinement"
        assert kwargs["proposal_status"] == "pending"
        # The proposal is deliberately not attached to a notebook.
        created.add_to_notebook.assert_not_called()

        created.relate.assert_awaited_once_with(
            "derived_from",
            "note:t",
            {"kind": "refinement_target", "previous_content": "original body"},
        )

    @pytest.mark.asyncio
    async def test_target_note_is_never_written(self):
        target = self._target()
        note_cls, _created = _patched_note_class(existing=target)
        with (
            patch("commands.iterate_commands.Note", note_cls),
            patch(
                "commands.iterate_commands.iterate_graph",
                MagicMock(
                    ainvoke=AsyncMock(
                        return_value={"output": "new", "title": "New"}
                    )
                ),
            ),
        ):
            await refine_note_command(RefineNoteInput(note_id="note:t"))

        assert target.content == "original body"
        target.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_note_fails_permanently(self):
        with patch("commands.iterate_commands.Note") as note_cls:
            note_cls.get = AsyncMock(side_effect=RuntimeError("not found"))
            result = await refine_note_command(RefineNoteInput(note_id="note:gone"))

        assert result.success is False
        assert "note:gone" in (result.error_message or "")


class TestNoteProposalDomain:
    @pytest.mark.asyncio
    async def test_knowledge_notes_excludes_proposals(self):
        with patch(
            "open_notebook.domain.notebook.repo_query", new=AsyncMock(return_value=[])
        ) as mock_query:
            await Note.get_knowledge_notes()

        query = mock_query.await_args.args[0]
        assert "proposal_status = NONE" in query
        assert "ORDER BY updated desc" in query

    @pytest.mark.asyncio
    async def test_apply_refinement_replaces_content_and_archives_proposal(self):
        target = make_note()
        proposal = make_note(
            id="note:proposal", title="Improved", content="improved body"
        )

        with (
            patch.object(Note, "save", new=AsyncMock()) as mock_save,
            patch.object(
                Note, "set_proposal_status", new=AsyncMock()
            ) as mock_status,
        ):
            await target.apply_refinement(proposal)

        assert target.title == "Improved"
        assert target.content == "improved body"
        mock_save.assert_awaited_once()
        mock_status.assert_awaited_once_with("accepted", status="archived")

    @pytest.mark.asyncio
    async def test_apply_refinement_rejects_empty_proposal(self):
        target = make_note()
        empty = MagicMock()
        empty.content = "   "
        with pytest.raises(Exception):
            await target.apply_refinement(empty)


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


class TestProposalEndpoints:
    def _proposal(self, **overrides):
        proposal = MagicMock()
        proposal.id = "note:proposal"
        proposal.title = "Improved"
        proposal.content = "improved body"
        proposal.note_type = "ai"
        proposal.note_kind = "refinement"
        proposal.proposal_status = overrides.pop("proposal_status", "pending")
        proposal.status = "active"
        proposal.summary = None
        proposal.tags = None
        proposal.keywords = None
        proposal.generated_by = None
        proposal.created = "2026-09-26 10:00:00"
        proposal.updated = "2026-09-26 10:00:00"
        proposal.get_derived_from = AsyncMock(
            return_value=[
                {
                    "out": "note:t",
                    "kind": "refinement_target",
                    "previous_content": "original body",
                }
            ]
        )
        proposal.set_proposal_status = AsyncMock()
        for key, value in overrides.items():
            setattr(proposal, key, value)
        return proposal

    def test_listing_proposals_is_not_shadowed_by_the_note_id_route(self, client):
        proposal = self._proposal()
        with patch(
            "api.routers.notes.Note.get_pending_proposals",
            new=AsyncMock(return_value=[proposal]),
        ):
            response = client.get("/api/notes/proposals")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["id"] == "note:proposal"
        assert body[0]["target_note_id"] == "note:t"
        assert body[0]["previous_content"] == "original body"

    def test_accepting_a_non_pending_proposal_is_rejected(self, client):
        proposal = self._proposal(proposal_status="accepted")
        with patch(
            "api.routers.notes.Note.get",
            new=AsyncMock(return_value=proposal),
        ):
            response = client.post("/api/notes/proposals/note:proposal/accept")

        assert response.status_code == 400

    def test_accepting_applies_the_proposal(self, client):
        proposal = self._proposal()
        target = MagicMock()
        target.apply_refinement = AsyncMock()
        target.id = "note:t"
        target.title = "Improved"
        target.content = "improved body"
        target.note_type = "human"
        target.created = "2026-09-26 10:00:00"
        target.updated = "2026-09-26 10:05:00"
        target.summary = None
        target.tags = None
        target.keywords = None
        target.note_kind = None
        target.status = "active"
        target.proposal_status = None
        target.generated_by = None

        with patch(
            "api.routers.notes.Note.get",
            new=AsyncMock(side_effect=[proposal, target]),
        ):
            response = client.post("/api/notes/proposals/note:proposal/accept")

        assert response.status_code == 200
        assert response.json()["content"] == "improved body"
        target.apply_refinement.assert_awaited_once_with(proposal)

    def test_rejecting_a_proposal_leaves_the_target_alone(self, client):
        proposal = self._proposal()
        with patch(
            "api.routers.notes.Note.get", new=AsyncMock(return_value=proposal)
        ):
            response = client.post("/api/notes/proposals/note:proposal/reject")

        assert response.status_code == 200
        proposal.set_proposal_status.assert_awaited_once_with(
            "rejected", status="archived"
        )
