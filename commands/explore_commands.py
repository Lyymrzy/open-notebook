"""Commands that propose where a notebook's knowledge tree grows next.

`scan_frontier` looks at the frontier (the leaf notes), asks the model what is
worth exploring there, and stores the answers as exploration points anchored on
those leaves. It only *suggests* - nothing is generated or rewritten.

The frontier is read from `build_knowledge_graph`, the same projection the UI
draws, so a suggestion can never point at a node the user cannot see as a leaf.
"""

import re
import time
from typing import Any, List, Optional, Set, Tuple

from loguru import logger
from surreal_commands import CommandInput, CommandOutput, command

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.exploration import ExplorationPoint
from open_notebook.domain.knowledge_graph import build_knowledge_graph
from open_notebook.domain.notebook import Note, Notebook
from open_notebook.exceptions import ConfigurationError, ContextLengthExceededError

try:
    from open_notebook.graphs.explore import graph as explore_graph
except ImportError as e:  # pragma: no cover - mirrors commands/source_commands.py
    logger.error(f"Failed to import explore graph: {e}")
    raise ValueError("explore graph not available")

EXPLORE_RETRY_CONFIG = {
    "max_attempts": 5,
    "wait_strategy": "exponential_jitter",
    "wait_min": 1,
    "wait_max": 60,
    "stop_on": [ValueError, ConfigurationError, ContextLengthExceededError],
    "retry_log_level": "debug",
}

# Each leaf is represented by a bounded excerpt rather than its full body: at 10
# leaves the full bodies would blow any context window, and the frontier signal
# (what the note is about, what it leaves open) lives near the top.
LEAF_EXCERPT_CHARS = 2_000


class FrontierScanInput(CommandInput):
    notebook_id: str
    max_leaves: int = 10
    questions_per_leaf: int = 1
    model_id: Optional[str] = None


class FrontierScanOutput(CommandOutput):
    success: bool
    notebook_id: str
    leaves_considered: int = 0
    created: int = 0
    skipped: int = 0
    processing_time: float
    error_message: Optional[str] = None


@command("scan_frontier", app="open_notebook", retry=EXPLORE_RETRY_CONFIG)
async def scan_frontier_command(input_data: FrontierScanInput) -> FrontierScanOutput:
    """Propose next questions for the leaf notes of a notebook."""
    start_time = time.time()

    try:
        if not input_data.notebook_id:
            raise ValueError("A notebook ID is required")

        try:
            await Notebook.get(input_data.notebook_id)
        except Exception as e:
            raise ValueError(
                f"Notebook '{input_data.notebook_id}' not found: {e}"
            ) from e

        logger.info(f"Scanning the frontier of notebook {input_data.notebook_id}")

        projection = await build_knowledge_graph(
            notebook_id=input_data.notebook_id, include_explorations=False
        )
        leaf_ids = [
            str(node["id"])
            for node in projection["nodes"]
            if node.get("is_leaf")
        ][: max(1, input_data.max_leaves)]

        if not leaf_ids:
            # A fully grown tree is a legitimate state, not a failure.
            processing_time = time.time() - start_time
            logger.info(
                f"Notebook {input_data.notebook_id} has no leaf notes to explore"
            )
            return FrontierScanOutput(
                success=True,
                notebook_id=input_data.notebook_id,
                leaves_considered=0,
                created=0,
                skipped=0,
                processing_time=processing_time,
            )

        notes = await _load_leaf_notes(leaf_ids)
        if not notes:
            raise ValueError("None of the frontier notes could be loaded")

        context = _render_frontier(notes)

        # LangGraph accepts a partial state dict at runtime, but its typed
        # overloads require the full state type (langgraph typing limitation).
        result = await explore_graph.ainvoke(  # type: ignore[call-overload]
            {
                "context": context,
                "questions_per_leaf": input_data.questions_per_leaf,
            },
            config=dict(configurable={"model_id": input_data.model_id}),
        )
        plan = (result or {}).get("plan")
        suggestions = list(getattr(plan, "suggestions", None) or [])

        known = await _existing_questions(leaf_ids)
        created, skipped = await _store_suggestions(
            suggestions,
            leaf_ids=set(leaf_ids),
            known=known,
            notebook_id=input_data.notebook_id,
        )

        processing_time = time.time() - start_time
        logger.info(
            f"Frontier scan of {input_data.notebook_id}: {created} proposal(s) from "
            f"{len(notes)} leaf/leaves ({skipped} skipped) in {processing_time:.2f}s"
        )

        return FrontierScanOutput(
            success=True,
            notebook_id=input_data.notebook_id,
            leaves_considered=len(notes),
            created=created,
            skipped=skipped,
            processing_time=processing_time,
        )

    except ValueError as e:
        processing_time = time.time() - start_time
        logger.error(f"Failed to scan frontier: {e}")
        return FrontierScanOutput(
            success=False,
            notebook_id=input_data.notebook_id,
            processing_time=processing_time,
            error_message=str(e),
        )
    except Exception:
        # Transient failure - will be retried (surreal-commands logs final failure)
        raise


async def _load_leaf_notes(leaf_ids: List[str]) -> List[Note]:
    """Load the frontier notes, skipping any that disappeared mid-scan."""
    notes: List[Note] = []
    for note_id in leaf_ids:
        try:
            notes.append(await Note.get(note_id))
        except Exception as e:
            logger.warning(f"Skipping frontier note {note_id}: {e}")
    return notes


def _render_frontier(notes: List[Note]) -> str:
    """Render the frontier as ID-labelled leaf blocks for the prompt."""
    blocks = []
    for note in notes:
        excerpt = (note.content or "").strip()[:LEAF_EXCERPT_CHARS]
        blocks.append(
            f"## LEAF [{note.id}]\n**Title:** {note.title or 'Untitled'}\n\n{excerpt}"
        )
    return "\n\n".join(blocks)


def _normalize(text: str) -> str:
    """Whitespace/case-insensitive key for duplicate detection."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


async def _existing_questions(note_ids: List[str]) -> Set[Tuple[str, str]]:
    """Questions already proposed for these notes, as (anchor, normalized) keys.

    Two queries rather than one nested SELECT: the anchor lives on the
    `explores` edge while the question lives on the point, and this keeps both
    sides explicit.
    """
    if not note_ids:
        return set()

    anchors = await repo_query(
        "SELECT in, out FROM explores WHERE out IN $ids",
        {"ids": [ensure_record_id(note_id) for note_id in note_ids]},
    )
    point_ids = {str(row.get("in")) for row in anchors if row.get("in")}
    if not point_ids:
        return set()

    points = await repo_query(
        "SELECT id, question FROM exploration_point WHERE id IN $ids "
        "AND status IN $statuses",
        {
            "ids": [ensure_record_id(point_id) for point_id in point_ids],
            "statuses": ["proposed", "accepted"],
        },
    )
    question_by_id = {
        str(row.get("id")): _normalize(str(row.get("question") or ""))
        for row in points
    }

    known: Set[Tuple[str, str]] = set()
    for row in anchors:
        anchor = str(row.get("out"))
        question = question_by_id.get(str(row.get("in")))
        if anchor and question:
            known.add((anchor, question))
    return known


async def _store_suggestions(
    suggestions: List[Any],
    *,
    leaf_ids: Set[str],
    known: Set[Tuple[str, str]],
    notebook_id: str,
) -> Tuple[int, int]:
    """Persist the usable suggestions, returning (created, skipped).

    Anything the model invented (an unknown note id) or repeated (a question
    already proposed for that leaf) is skipped rather than stored: a proposal
    list is only useful while every entry is a real, new option.
    """
    created = 0
    skipped = 0
    seen: Set[Tuple[str, str]] = set()

    for suggestion in suggestions:
        note_id = str(getattr(suggestion, "note_id", "") or "").strip()
        question = str(getattr(suggestion, "question", "") or "").strip()

        if note_id not in leaf_ids or not question:
            logger.debug(f"Dropping suggestion for unknown leaf {note_id!r}")
            skipped += 1
            continue

        key = (note_id, _normalize(question))
        if key in known or key in seen:
            skipped += 1
            continue
        seen.add(key)

        point = ExplorationPoint(
            question=question,
            rationale=getattr(suggestion, "rationale", None),
            kind=getattr(suggestion, "kind", None) or "gap",
            score=getattr(suggestion, "score", None),
            notebook=notebook_id,
            origin="frontier_scan",
        )
        await point.save()
        await point.anchor_to(note_id)
        created += 1

    return created, skipped
