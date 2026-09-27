"""
Tests for the knowledge graph read model (P2), the frontier scan command and
the graph/exploration endpoints.

The properties worth pinning here:
  * the graph is a projection - leaf detection must agree with the tree edges,
    or the UI and the scan would disagree about where the frontier is;
  * links reaching outside the requested scope are counted, not silently
    dropped, so an apparently cut-off tree can be explained;
  * a scan never stores a suggestion pointing at a note that is not a leaf, nor
    a question that is already on the list.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from commands.explore_commands import (
    LEAF_EXCERPT_CHARS,
    FrontierScanInput,
    _existing_questions,
    _render_frontier,
    scan_frontier_command,
)
from open_notebook.domain.knowledge_graph import build_knowledge_graph
from open_notebook.domain.notebook import Note
from open_notebook.exceptions import NotFoundError
from open_notebook.graphs.explore import FrontierPlan, Suggestion


def note_row(note_id, title="Note", **overrides):
    row = {
        "id": note_id,
        "title": title,
        "content": "body",
        "note_type": "human",
        "created": "2026-09-26 10:00:00",
        "updated": "2026-09-26 10:00:00",
    }
    row.update(overrides)
    return row


def _patch_queries(notes, includes=None, relates_to=None, points=None, explores=None):
    return patch(
        "open_notebook.domain.knowledge_graph.repo_query",
        new=AsyncMock(
            side_effect=[
                notes,
                includes or [],
                relates_to or [],
                points or [],
                explores or [],
            ]
        ),
    )


class TestBuildKnowledgeGraph:
    @pytest.mark.asyncio
    async def test_leaves_are_notes_nothing_grows_out_of(self):
        notes = [note_row("note:parent"), note_row("note:child")]
        includes = [{"in": "note:parent", "out": "note:child"}]

        with _patch_queries(notes, includes=includes):
            graph = await build_knowledge_graph(include_explorations=False)

        by_id = {node["id"]: node for node in graph["nodes"]}
        assert by_id["note:parent"]["is_leaf"] is False
        assert by_id["note:parent"]["child_count"] == 1
        assert by_id["note:child"]["is_leaf"] is True
        assert graph["counts"]["notes"] == 2

    @pytest.mark.asyncio
    async def test_tree_and_web_edges_are_typed(self):
        notes = [note_row("note:a"), note_row("note:b")]
        includes = [{"in": "note:a", "out": "note:b"}]
        relates_to = [{"in": "note:b", "out": "note:a", "reason": "same topic"}]

        with _patch_queries(notes, includes=includes, relates_to=relates_to):
            graph = await build_knowledge_graph(include_explorations=False)

        kinds = {edge["kind"] for edge in graph["edges"]}
        assert kinds == {"includes", "relates_to"}
        web = next(e for e in graph["edges"] if e["kind"] == "relates_to")
        assert web["label"] == "same topic"

    @pytest.mark.asyncio
    async def test_edges_outside_the_scope_are_counted_not_returned(self):
        notes = [note_row("note:a")]
        includes = [
            {"in": "note:a", "out": "note:elsewhere"},
            {"in": "note:elsewhere", "out": "note:a"},
        ]

        with _patch_queries(notes, includes=includes):
            graph = await build_knowledge_graph(include_explorations=False)

        assert graph["edges"] == []
        assert graph["external_link_count"] == 2

    @pytest.mark.asyncio
    async def test_exploration_points_become_nodes_anchored_on_their_leaf(self):
        notes = [note_row("note:leaf")]
        points = [
            {
                "id": "exploration_point:e1",
                "question": "How does it overwinter?",
                "kind": "gap",
                "status": "proposed",
                "score": 0.8,
                "created": "2026-09-26 10:00:00",
                "updated": "2026-09-26 10:00:00",
            }
        ]
        explores = [{"in": "exploration_point:e1", "out": "note:leaf"}]

        with _patch_queries(notes, points=points, explores=explores):
            graph = await build_knowledge_graph()

        point_nodes = [n for n in graph["nodes"] if n["kind"] == "exploration"]
        assert len(point_nodes) == 1
        assert point_nodes[0]["anchor_id"] == "note:leaf"
        assert graph["counts"]["explorations"] == 1
        assert any(edge["kind"] == "explores" for edge in graph["edges"])

    @pytest.mark.asyncio
    async def test_exploration_anchored_outside_the_scope_is_omitted(self):
        notes = [note_row("note:a")]
        points = [
            {
                "id": "exploration_point:e1",
                "question": "q",
                "status": "proposed",
            }
        ]
        explores = [{"in": "exploration_point:e1", "out": "note:elsewhere"}]

        with _patch_queries(notes, points=points, explores=explores):
            graph = await build_knowledge_graph()

        assert not any(edge["kind"] == "explores" for edge in graph["edges"])

    @pytest.mark.asyncio
    async def test_truncation_is_reported(self):
        notes = [note_row(f"note:{i}") for i in range(3)]

        with _patch_queries(notes):
            graph = await build_knowledge_graph(include_explorations=False, max_nodes=3)

        assert graph["truncated"] is True

    @pytest.mark.asyncio
    async def test_explorations_can_be_left_out(self):
        notes = [note_row("note:a")]
        with patch(
            "open_notebook.domain.knowledge_graph.repo_query",
            new=AsyncMock(return_value=notes),
        ) as mock_query:
            graph = await build_knowledge_graph(include_explorations=False)

        assert graph["counts"]["explorations"] == 0
        # notes + includes + relates_to only
        assert mock_query.await_count == 3


class TestGraphEndpoint:
    @pytest.fixture
    def client(self):
        from api.main import app

        return TestClient(app)

    def test_unknown_notebook_is_a_404_not_an_empty_graph(self, client):
        # ObjectModel.get raises NotFoundError for a missing record; a typo'd id
        # must not render as a legitimately empty graph.
        with patch(
            "api.routers.graph.Notebook.get",
            new=AsyncMock(side_effect=NotFoundError("not found")),
        ):
            response = client.get("/api/graph?notebook_id=notebook:missing")

        assert response.status_code == 404

    def test_returns_nodes_and_edges(self, client):
        payload = {
            "nodes": [
                {
                    "id": "note:a",
                    "kind": "note",
                    "label": "A",
                    "is_leaf": True,
                    "child_count": 0,
                    "tags": [],
                }
            ],
            "edges": [
                {
                    "id": "includes:note:a->note:b",
                    "source": "note:a",
                    "target": "note:b",
                    "kind": "includes",
                }
            ],
            "max_nodes": 500,
            "truncated": False,
            "external_link_count": 1,
            "counts": {"notes": 1},
        }
        with patch(
            "api.routers.graph.build_knowledge_graph",
            new=AsyncMock(return_value=payload),
        ):
            response = client.get("/api/graph")

        assert response.status_code == 200
        body = response.json()
        assert body["nodes"][0]["id"] == "note:a"
        assert body["edges"][0]["kind"] == "includes"
        assert body["external_link_count"] == 1


class TestRenderFrontier:
    def test_labels_each_leaf_with_its_id(self):
        rendered = _render_frontier([Note(id="note:1", title="T", content="body")])
        assert "## LEAF [note:1]" in rendered
        assert "body" in rendered

    def test_excerpt_is_capped(self):
        note = Note(id="note:1", title="T", content="x" * (LEAF_EXCERPT_CHARS + 500))
        rendered = _render_frontier([note])
        assert len(rendered) < LEAF_EXCERPT_CHARS + 200


class TestExistingQuestions:
    @pytest.mark.asyncio
    async def test_builds_anchor_question_keys(self):
        anchors = [{"in": "exploration_point:e1", "out": "note:leaf"}]
        points = [{"id": "exploration_point:e1", "question": "How  does it   survive?"}]

        with patch(
            "commands.explore_commands.repo_query",
            new=AsyncMock(side_effect=[anchors, points]),
        ):
            known = await _existing_questions(["note:leaf"])

        assert ("note:leaf", "how does it survive?") in known

    @pytest.mark.asyncio
    async def test_returns_empty_without_notes(self):
        assert await _existing_questions([]) == set()


def _patched_point_class():
    point_cls = MagicMock()
    instance = MagicMock()
    instance.id = "exploration_point:new"
    instance.save = AsyncMock()
    instance.anchor_to = AsyncMock()
    point_cls.return_value = instance
    return point_cls, instance


def _projection(leaf_ids):
    return {
        "nodes": [
            {
                "id": note_id,
                "kind": "note",
                "label": note_id,
                "is_leaf": True,
                "child_count": 0,
            }
            for note_id in leaf_ids
        ],
        "edges": [],
        "counts": {"notes": len(leaf_ids), "explorations": 0, "edges": 0},
    }


class TestScanFrontierCommand:
    @pytest.mark.asyncio
    async def test_unknown_notebook_fails_permanently(self):
        with patch(
            "commands.explore_commands.Notebook.get",
            new=AsyncMock(side_effect=Exception("nope")),
        ):
            result = await scan_frontier_command(
                FrontierScanInput(notebook_id="notebook:missing")
            )

        assert result.success is False
        assert "notebook:missing" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_no_leaves_is_a_success_not_an_error(self):
        with (
            patch("commands.explore_commands.Notebook.get", new=AsyncMock()),
            patch(
                "commands.explore_commands.build_knowledge_graph",
                new=AsyncMock(return_value={"nodes": [], "edges": []}),
            ),
        ):
            result = await scan_frontier_command(
                FrontierScanInput(notebook_id="notebook:nb1")
            )

        assert result.success is True
        assert result.created == 0
        assert result.leaves_considered == 0

    @pytest.mark.asyncio
    async def test_stores_suggestions_anchored_on_the_leaf(self):
        point_cls, point = _patched_point_class()
        leaf = Note(id="note:leaf", title="T", content="body")
        plan = FrontierPlan(
            suggestions=[
                Suggestion(
                    note_id="note:leaf",
                    question="How does it overwinter?",
                    rationale="Never stated",
                    kind="gap",
                    score=0.7,
                )
            ]
        )

        with (
            patch("commands.explore_commands.Notebook.get", new=AsyncMock()),
            patch(
                "commands.explore_commands.build_knowledge_graph",
                new=AsyncMock(return_value=_projection(["note:leaf"])),
            ),
            patch(
                "commands.explore_commands.Note.get", new=AsyncMock(return_value=leaf)
            ),
            patch(
                "commands.explore_commands.explore_graph",
                MagicMock(ainvoke=AsyncMock(return_value={"plan": plan})),
            ),
            patch(
                "commands.explore_commands._existing_questions",
                new=AsyncMock(return_value=set()),
            ),
            patch("commands.explore_commands.ExplorationPoint", point_cls),
        ):
            result = await scan_frontier_command(
                FrontierScanInput(notebook_id="notebook:nb1")
            )

        assert result.success is True
        assert result.created == 1
        kwargs = point_cls.call_args.kwargs
        assert kwargs["origin"] == "frontier_scan"
        assert kwargs["notebook"] == "notebook:nb1"
        assert kwargs["kind"] == "gap"
        point.save.assert_awaited_once()
        point.anchor_to.assert_awaited_once_with("note:leaf")

    @pytest.mark.asyncio
    async def test_invented_note_ids_are_skipped(self):
        point_cls, _point = _patched_point_class()
        leaf = Note(id="note:leaf", title="T", content="body")
        plan = FrontierPlan(
            suggestions=[
                Suggestion(note_id="note:does-not-exist", question="Made up?"),
                Suggestion(note_id="note:leaf", question="A real one?"),
            ]
        )

        with (
            patch("commands.explore_commands.Notebook.get", new=AsyncMock()),
            patch(
                "commands.explore_commands.build_knowledge_graph",
                new=AsyncMock(return_value=_projection(["note:leaf"])),
            ),
            patch(
                "commands.explore_commands.Note.get", new=AsyncMock(return_value=leaf)
            ),
            patch(
                "commands.explore_commands.explore_graph",
                MagicMock(ainvoke=AsyncMock(return_value={"plan": plan})),
            ),
            patch(
                "commands.explore_commands._existing_questions",
                new=AsyncMock(return_value=set()),
            ),
            patch("commands.explore_commands.ExplorationPoint", point_cls),
        ):
            result = await scan_frontier_command(
                FrontierScanInput(notebook_id="notebook:nb1")
            )

        assert result.created == 1
        assert result.skipped == 1

    @pytest.mark.asyncio
    async def test_duplicate_questions_are_skipped(self):
        point_cls, _point = _patched_point_class()
        leaf = Note(id="note:leaf", title="T", content="body")
        plan = FrontierPlan(
            suggestions=[
                Suggestion(note_id="note:leaf", question="How does it overwinter?")
            ]
        )

        with (
            patch("commands.explore_commands.Notebook.get", new=AsyncMock()),
            patch(
                "commands.explore_commands.build_knowledge_graph",
                new=AsyncMock(return_value=_projection(["note:leaf"])),
            ),
            patch(
                "commands.explore_commands.Note.get", new=AsyncMock(return_value=leaf)
            ),
            patch(
                "commands.explore_commands.explore_graph",
                MagicMock(ainvoke=AsyncMock(return_value={"plan": plan})),
            ),
            patch(
                "commands.explore_commands._existing_questions",
                new=AsyncMock(
                    return_value={("note:leaf", "how does it overwinter?")}
                ),
            ),
            patch("commands.explore_commands.ExplorationPoint", point_cls),
        ):
            result = await scan_frontier_command(
                FrontierScanInput(notebook_id="notebook:nb1")
            )

        assert result.created == 0
        assert result.skipped == 1


class TestExplorationEndpoints:
    @pytest.fixture
    def client(self):
        from api.main import app

        return TestClient(app)

    def _point(self, **overrides):
        point = MagicMock()
        point.id = "exploration_point:e1"
        point.question = "How does it overwinter?"
        point.rationale = "Not stated"
        point.kind = "gap"
        point.status = overrides.pop("status", "proposed")
        point.score = 0.7
        point.notebook = "notebook:nb1"
        point.created = "2026-09-26 10:00:00"
        point.updated = "2026-09-26 10:00:00"
        point.get_anchors = AsyncMock(return_value=["note:leaf"])
        point.set_status = AsyncMock()
        for key, value in overrides.items():
            setattr(point, key, value)
        return point

    def test_listing_returns_the_anchor(self, client):
        point = self._point()
        with patch(
            "api.routers.explorations.ExplorationPoint.get_all",
            new=AsyncMock(return_value=[point]),
        ):
            response = client.get("/api/explorations")

        assert response.status_code == 200
        body = response.json()
        assert body[0]["id"] == "exploration_point:e1"
        assert body[0]["anchor_id"] == "note:leaf"

    def test_dismissing_a_point(self, client):
        point = self._point()
        with patch(
            "api.routers.explorations.ExplorationPoint.get",
            new=AsyncMock(return_value=point),
        ):
            response = client.post("/api/explorations/exploration_point:e1/dismiss")

        assert response.status_code == 200
        point.set_status.assert_awaited_once_with("dismissed")

    def test_consuming_a_point_is_not_a_dismissal(self, client):
        # A direction that produced knowledge was explored, not rejected.
        point = self._point()
        with patch(
            "api.routers.explorations.ExplorationPoint.get",
            new=AsyncMock(return_value=point),
        ):
            response = client.post("/api/explorations/exploration_point:e1/consume")

        assert response.status_code == 200
        point.set_status.assert_awaited_once_with("consumed")

    def test_scan_submits_a_job(self, client):
        with (
            patch("api.routers.explorations.Notebook.get", new=AsyncMock()),
            patch(
                "api.routers.explorations.CommandService.submit_command_job",
                new=AsyncMock(return_value="command:1"),
            ) as mock_submit,
        ):
            response = client.post(
                "/api/explorations/scan", json={"notebook_id": "notebook:nb1"}
            )

        assert response.status_code == 202
        assert response.json()["job_id"] == "command:1"
        assert mock_submit.await_args.args[1] == "scan_frontier"

    def test_scan_404s_on_unknown_notebook(self, client):
        with patch(
            "api.routers.explorations.Notebook.get",
            new=AsyncMock(side_effect=NotFoundError("not found")),
        ):
            response = client.post(
                "/api/explorations/scan", json={"notebook_id": "notebook:missing"}
            )

        assert response.status_code == 404
