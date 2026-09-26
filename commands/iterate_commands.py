"""Commands that make the knowledge tree iterate.

Two operations, both asynchronous (an LLM pass is minutes, not milliseconds):

* ``synthesize_notes`` - material (notes and/or sources) becomes one new
  knowledge note, optionally growing out of a parent note. Provenance edges
  (``derived_from``) record what it was built from.
* ``refine_note`` - one note plus new evidence becomes a *proposed* revision.
  The proposal is deliberately not attached to any notebook, so it cannot leak
  into note lists or chat context before the user accepts it.

Neither command ever rewrites an existing note: applying a refinement is a
separate, explicit step (``Note.apply_refinement``).
"""

import re
import time
from typing import List, Optional, Tuple

from loguru import logger
from surreal_commands import CommandInput, CommandOutput, command

from open_notebook.domain.memory import MemoryItem
from open_notebook.domain.notebook import Note, Source
from open_notebook.exceptions import ConfigurationError, ContextLengthExceededError

try:
    from open_notebook.graphs.iterate import graph as iterate_graph
except ImportError as e:  # pragma: no cover - mirrors commands/source_commands.py
    logger.error(f"Failed to import iterate graph: {e}")
    raise ValueError("iterate graph not available")

ITERATE_RETRY_CONFIG = {
    "max_attempts": 5,
    "wait_strategy": "exponential_jitter",
    "wait_min": 1,
    "wait_max": 60,
    "stop_on": [ValueError, ConfigurationError, ContextLengthExceededError],
    "retry_log_level": "debug",
}

# Material is passed to the model in one human message. The cap keeps a runaway
# notebook from blowing past any provider's window; anything dropped is named in
# the prompt so the model (and the user, reading the note) knows it is missing.
MAX_MATERIAL_CHARS = 80_000
OMITTED_NOTICE = "\n\n[Omitted to fit the context budget: {ids}]"
# The pre-revision text is kept on the provenance edge as a safety net while
# notes have no version history of their own.
PREVIOUS_CONTENT_CAP = 20_000


class SynthesizeNotesInput(CommandInput):
    note_ids: List[str] = []
    source_ids: List[str] = []
    notebook_id: Optional[str] = None
    parent_note_id: Optional[str] = None
    instructions: Optional[str] = None
    model_id: Optional[str] = None


class SynthesizeNotesOutput(CommandOutput):
    success: bool
    note_id: Optional[str] = None
    title: Optional[str] = None
    processing_time: float
    error_message: Optional[str] = None


class RefineNoteInput(CommandInput):
    note_id: str
    source_ids: List[str] = []
    instructions: Optional[str] = None
    model_id: Optional[str] = None


class RefineNoteOutput(CommandOutput):
    success: bool
    target_note_id: str
    proposal_id: Optional[str] = None
    # False when the model judged the note already correct - a useful outcome,
    # and not something to turn into an empty proposal.
    changed: bool = False
    processing_time: float
    error_message: Optional[str] = None


def _render_material(
    notes: Optional[List[Note]] = None,
    sources: Optional[List[Source]] = None,
    memories: Optional[List[MemoryItem]] = None,
    budget_chars: int = MAX_MATERIAL_CHARS,
) -> str:
    """Render loaded records into the ID-labelled material block for the prompt.

    Ordering is notes, then sources, then memories: the user's own writing is
    what the note should be organised around, and the AI's memories are the
    least authoritative input, so they are the first thing to fall off the
    budget.
    """
    blocks: List[Tuple[str, str]] = []

    for note in notes or []:
        blocks.append(
            (
                str(note.id),
                f"## NOTE [{note.id}]\n**Title:** {note.title or 'Untitled'}\n\n"
                f"{note.content or ''}",
            )
        )
    for source in sources or []:
        blocks.append(
            (
                str(source.id),
                f"## SOURCE [{source.id}]\n**Title:** {source.title or 'Untitled'}\n\n"
                f"{source.full_text or ''}",
            )
        )
    for memory in memories or []:
        detail = ", ".join(
            part
            for part in (
                memory.kind,
                f"confidence={memory.confidence}"
                if memory.confidence is not None
                else None,
                memory.status,
            )
            if part
        )
        blocks.append(
            (
                str(memory.id),
                f"## AI MEMORY [{memory.id}]{f' ({detail})' if detail else ''}\n"
                f"{memory.content}",
            )
        )

    kept: List[str] = []
    omitted: List[str] = []
    used = 0
    for record_id, rendered in blocks:
        remaining = budget_chars - used
        if remaining <= 0:
            omitted.append(record_id)
            continue
        if len(rendered) > remaining:
            rendered = (
                rendered[:remaining]
                + f"\n\n[Truncated: {record_id} exceeds the context budget.]"
            )
        kept.append(rendered)
        used += len(rendered)

    material = "\n\n".join(kept)
    if omitted:
        material += OMITTED_NOTICE.format(ids=", ".join(omitted))
    return material


async def _load_notes(note_ids: List[str]) -> List[Note]:
    """Load notes, failing permanently (ValueError) on the first unknown id."""
    notes: List[Note] = []
    for note_id in note_ids:
        try:
            notes.append(await Note.get(note_id))
        except Exception as e:
            raise ValueError(f"Note '{note_id}' not found: {e}") from e
    return notes


async def _load_sources(source_ids: List[str]) -> List[Source]:
    """Load sources, failing permanently (ValueError) on the first unknown id."""
    sources: List[Source] = []
    for source_id in source_ids:
        try:
            sources.append(await Source.get(source_id))
        except Exception as e:
            raise ValueError(f"Source '{source_id}' not found: {e}") from e
    return sources


def _normalize(text: Optional[str]) -> str:
    """Whitespace-insensitive comparison for "did the model change anything?"."""
    return re.sub(r"\s+", " ", text or "").strip()


@command("synthesize_notes", app="open_notebook", retry=ITERATE_RETRY_CONFIG)
async def synthesize_notes_command(
    input_data: SynthesizeNotesInput,
) -> SynthesizeNotesOutput:
    """Consolidate notes and/or sources into one new knowledge note."""
    start_time = time.time()

    try:
        if not input_data.note_ids and not input_data.source_ids:
            raise ValueError("At least one note or source is required")

        logger.info(
            f"Synthesizing note from {len(input_data.note_ids)} note(s) and "
            f"{len(input_data.source_ids)} source(s)"
        )

        notes = await _load_notes(input_data.note_ids)
        sources = await _load_sources(input_data.source_ids)

        # Resolve the parent before calling the model: a typo'd id must not cost
        # a (potentially expensive) LLM pass before failing.
        parent: Optional[Note] = None
        if input_data.parent_note_id:
            try:
                parent = await Note.get(input_data.parent_note_id)
            except Exception as e:
                raise ValueError(
                    f"Parent note '{input_data.parent_note_id}' not found: {e}"
                ) from e

        material = _render_material(notes=notes, sources=sources)

        # LangGraph accepts a partial state dict at runtime, but its typed
        # overloads require the full state type (langgraph typing limitation).
        result = await iterate_graph.ainvoke(  # type: ignore[call-overload]
            {
                "mode": "synthesize",
                "context": material,
                "instructions": input_data.instructions or "",
            },
            config=dict(configurable={"model_id": input_data.model_id}),
        )
        content = (result or {}).get("output") or ""
        if not content.strip():
            raise ValueError("The model returned an empty note")

        title = (result or {}).get("title") or _fallback_title(notes, sources)

        note = Note(
            title=title,
            content=content,
            note_type="ai",
            note_kind="synthesis",
            status="active",
            generated_by=input_data.model_id,
        )
        await note.save()

        if input_data.notebook_id:
            await note.add_to_notebook(input_data.notebook_id)

        if parent is not None:
            await parent.add_child(str(note.id))

        for source_note in notes:
            await note.relate("derived_from", str(source_note.id), {"kind": "note"})
        for source in sources:
            await note.relate("derived_from", str(source.id), {"kind": "source"})

        processing_time = time.time() - start_time
        logger.info(f"Created synthesized note {note.id} in {processing_time:.2f}s")

        return SynthesizeNotesOutput(
            success=True,
            note_id=str(note.id) if note.id else None,
            title=note.title,
            processing_time=processing_time,
        )

    except ValueError as e:
        processing_time = time.time() - start_time
        logger.error(f"Failed to synthesize note: {e}")
        return SynthesizeNotesOutput(
            success=False,
            processing_time=processing_time,
            error_message=str(e),
        )
    except Exception:
        # Transient failure - will be retried (surreal-commands logs final failure)
        raise


@command("refine_note", app="open_notebook", retry=ITERATE_RETRY_CONFIG)
async def refine_note_command(input_data: RefineNoteInput) -> RefineNoteOutput:
    """Propose the next revision of a note. Never touches the note itself."""
    start_time = time.time()

    try:
        if not input_data.note_id:
            raise ValueError("A note ID is required")

        logger.info(f"Proposing a revision of note {input_data.note_id}")

        note = await _load_notes([input_data.note_id])
        target = note[0]
        sources = await _load_sources(input_data.source_ids)
        # What the AI already remembers about this note is legitimate evidence
        # for a revision, and it is the reason a refinement can improve a note
        # even when no new source was added.
        memories = await target.get_memories(statuses=["active", "pending"])
        material = _render_material(notes=[target], sources=sources, memories=memories)

        result = await iterate_graph.ainvoke(  # type: ignore[call-overload]
            {
                "mode": "refine",
                "context": material,
                "instructions": input_data.instructions or "",
            },
            config=dict(configurable={"model_id": input_data.model_id}),
        )
        content = (result or {}).get("output") or ""
        if not content.strip():
            raise ValueError("The model returned an empty note")

        if _normalize(content) == _normalize(target.content):
            processing_time = time.time() - start_time
            logger.info(
                f"Refinement of {target.id} produced no change in "
                f"{processing_time:.2f}s"
            )
            return RefineNoteOutput(
                success=True,
                target_note_id=str(target.id),
                proposal_id=None,
                changed=False,
                processing_time=processing_time,
            )

        title = (result or {}).get("title") or target.title

        # Not attached to a notebook on purpose: an unaccepted proposal must not
        # appear in note lists or leak into chat context.
        proposal = Note(
            title=title,
            content=content,
            note_type="ai",
            note_kind="refinement",
            proposal_status="pending",
            status="active",
            generated_by=input_data.model_id,
        )
        await proposal.save()
        await proposal.relate(
            "derived_from",
            str(target.id),
            {
                "kind": "refinement_target",
                "previous_content": (target.content or "")[:PREVIOUS_CONTENT_CAP],
            },
        )

        processing_time = time.time() - start_time
        logger.info(f"Created refinement proposal {proposal.id} in {processing_time:.2f}s")

        return RefineNoteOutput(
            success=True,
            target_note_id=str(target.id),
            proposal_id=str(proposal.id) if proposal.id else None,
            changed=True,
            processing_time=processing_time,
        )

    except ValueError as e:
        processing_time = time.time() - start_time
        logger.error(f"Failed to refine note {input_data.note_id}: {e}")
        return RefineNoteOutput(
            success=False,
            target_note_id=input_data.note_id,
            processing_time=processing_time,
            error_message=str(e),
        )
    except Exception:
        # Transient failure - will be retried (surreal-commands logs final failure)
        raise


def _fallback_title(notes: List[Note], sources: List[Source]) -> str:
    """Title used when the model skipped the leading H1 line."""
    for note in notes:
        if note.title:
            return str(note.title)
    for source in sources:
        if source.title:
            return str(source.title)
    return "Untitled note"
