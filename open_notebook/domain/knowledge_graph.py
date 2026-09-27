"""Read model for the knowledge graph: nodes and edges, ready to be drawn.

Deliberately a read-only assembly layer with no new storage: the graph *is* the
`includes` (tree) and `relates_to` (web) edges plus the exploration points
anchored on notes (migration 25). Keeping it a projection means the graph can
never drift from the notes it claims to represent.

Scope is one notebook by default. Links that reach outside the scope are not
returned as edges (the other end is a node the caller did not ask for) but they
are counted, so a client can say "N links continue elsewhere" instead of
silently showing a tree that looks cut off.
"""

from collections import Counter
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.exceptions import DatabaseOperationError, InvalidInputError

# Above this the layout gets unreadable and the browser pays for it; the caller
# is told the graph was truncated rather than being handed a fraction silently.
DEFAULT_MAX_NODES = 500


async def build_knowledge_graph(
    notebook_id: Optional[str] = None,
    include_explorations: bool = True,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> Dict[str, Any]:
    """Assemble the knowledge graph for a notebook (or the whole knowledge base).

    Returns a dict with ``nodes``, ``edges`` and counters. Node ids are the
    record ids, so a client can navigate straight back to the note.
    """
    if max_nodes <= 0:
        raise InvalidInputError("max_nodes must be positive")

    notebook_scope = ensure_record_id(notebook_id) if notebook_id else None

    try:
        notes = await _load_notes(notebook_scope, max_nodes)
    except InvalidInputError:
        raise
    except Exception as e:
        logger.error(f"Error loading notes for the knowledge graph: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError("Failed to load the knowledge graph")

    note_ids: Set[str] = {str(note.id) for note in notes if note.id}

    try:
        tree_edges, web_edges, external_links = await _load_note_edges(note_ids)
    except Exception as e:
        logger.error(f"Error loading graph edges: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError("Failed to load the knowledge graph edges")

    edges: List[Dict[str, Any]] = []
    for row in tree_edges:
        edges.append(
            _edge("includes", str(row.get("in")), str(row.get("out")), row)
        )
    for row in web_edges:
        edges.append(
            _edge("relates_to", str(row.get("in")), str(row.get("out")), row)
        )

    # A note with no outgoing `includes` edge is the frontier: nothing grows out
    # of it yet, which is exactly where exploration points get proposed.
    child_counts = Counter(str(row.get("in")) for row in tree_edges)

    nodes: List[Dict[str, Any]] = []
    for note in notes:
        node_id = str(note.id)
        children = child_counts.get(node_id, 0)
        nodes.append(
            {
                "id": node_id,
                "kind": "note",
                "label": note.title or "Untitled",
                "note_type": note.note_type,
                "note_kind": note.note_kind,
                "status": note.status,
                "tags": note.tags or [],
                "is_leaf": children == 0,
                "child_count": children,
                "created": str(note.created),
                "updated": str(note.updated),
            }
        )

    exploration_count = 0
    if include_explorations:
        points, anchor_edges = await _load_exploration_points(notebook_scope, note_ids)
        exploration_count = len(points)
        anchors = {str(row.get("in")): str(row.get("out")) for row in anchor_edges}
        for point in points:
            node_id = str(point.id)
            nodes.append(
                {
                    "id": node_id,
                    "kind": "exploration",
                    "label": point.question,
                    "exploration_kind": point.kind,
                    "status": point.status,
                    "score": point.score,
                    "rationale": point.rationale,
                    "anchor_id": anchors.get(node_id),
                    "is_leaf": True,
                    "child_count": 0,
                    "created": str(point.created),
                    "updated": str(point.updated),
                }
            )
            anchor_id = anchors.get(node_id)
            if anchor_id and anchor_id in note_ids:
                edges.append(
                    {
                        "id": f"explores:{node_id}->{anchor_id}",
                        "source": node_id,
                        "target": anchor_id,
                        "kind": "explores",
                        "label": None,
                    }
                )

    return {
        "nodes": nodes,
        "edges": edges,
        "notebook_id": notebook_id,
        "max_nodes": max_nodes,
        "truncated": len(notes) >= max_nodes,
        "external_link_count": external_links,
        "counts": {
            "notes": len(notes),
            "explorations": exploration_count,
            "edges": len(edges),
        },
    }


def _edge(kind: str, source: str, target: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an edge row into the shape a graph renderer expects."""
    return {
        "id": f"{kind}:{source}->{target}",
        "source": source,
        "target": target,
        "kind": kind,
        "label": row.get("reason") or None,
    }


async def _load_notes(notebook_scope: Any, max_nodes: int) -> List[Any]:
    """Load the knowledge notes in scope, newest first.

    Ordered by `created asc` so the node order (and therefore the layout) is
    stable across requests instead of shuffling on every refresh.
    """
    from open_notebook.domain.notebook import Note

    if notebook_scope is not None:
        rows = await repo_query(
            """
            SELECT * FROM note
            WHERE proposal_status = NONE
              AND id IN (SELECT VALUE in FROM artifact WHERE out = $notebook_id)
            ORDER BY created ASC
            LIMIT $limit
            """,
            {"notebook_id": notebook_scope, "limit": max_nodes},
        )
    else:
        rows = await repo_query(
            "SELECT * FROM note WHERE proposal_status = NONE "
            "ORDER BY created ASC LIMIT $limit",
            {"limit": max_nodes},
        )
    return [Note(**row) for row in rows]


async def _load_note_edges(
    note_ids: Set[str],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Load `includes` / `relates_to` edges touching the scoped notes.

    Returns (tree edges, web edges, count of links whose other end is outside
    the scope). Edges reaching outside are counted, not returned: a renderer
    would have nowhere to attach them.
    """
    if not note_ids:
        return [], [], 0

    record_ids = [ensure_record_id(note_id) for note_id in note_ids]

    tree_rows = await repo_query(
        "SELECT * FROM includes WHERE in IN $ids OR out IN $ids",
        {"ids": record_ids},
    )
    web_rows = await repo_query(
        "SELECT * FROM relates_to WHERE in IN $ids OR out IN $ids",
        {"ids": record_ids},
    )

    def split(rows: List[Dict[str, Any]]):
        internal, external = [], 0
        for row in rows:
            source, target = str(row.get("in")), str(row.get("out"))
            if source in note_ids and target in note_ids:
                internal.append(row)
            else:
                external += 1
        return internal, external

    tree_edges, tree_external = split(tree_rows)
    web_edges, web_external = split(web_rows)
    return tree_edges, web_edges, tree_external + web_external


async def _load_exploration_points(
    notebook_scope: Any, note_ids: Set[str]
) -> tuple[List[Any], List[Dict[str, Any]]]:
    """Load open exploration points and their anchors."""
    from open_notebook.domain.exploration import ExplorationPoint

    if notebook_scope is not None:
        rows = await repo_query(
            "SELECT * FROM exploration_point WHERE notebook = $notebook_id "
            "AND status IN $statuses ORDER BY score DESC, created ASC",
            {
                "notebook_id": notebook_scope,
                "statuses": ["proposed", "accepted"],
            },
        )
    else:
        rows = await repo_query(
            "SELECT * FROM exploration_point WHERE status IN $statuses "
            "ORDER BY score DESC, created ASC",
            {"statuses": ["proposed", "accepted"]},
        )

    points = [ExplorationPoint(**row) for row in rows]
    point_ids = [str(point.id) for point in points if point.id]
    if not point_ids:
        return points, []

    anchors = await repo_query(
        "SELECT in, out FROM explores WHERE in IN $ids",
        {"ids": [ensure_record_id(point_id) for point_id in point_ids]},
    )
    # Anchors outside the scope would strand the exploration node in the layout.
    anchors = [row for row in anchors if str(row.get("out")) in note_ids]
    return points, anchors
