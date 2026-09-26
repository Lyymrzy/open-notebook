from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.command_service import CommandService
from api.models import (
    IterateJobResponse,
    NoteCreate,
    NoteProposalResponse,
    NoteResponse,
    NoteUpdate,
    RefineNoteRequest,
    SynthesizeNotesRequest,
)
from open_notebook.domain.notebook import Note
from open_notebook.exceptions import (
    InvalidInputError,
    NotFoundError,
    OpenNotebookError,
)

router = APIRouter()


async def _proposal_target(proposal: Note):
    """Find what a refinement proposal wants to replace.

    Returns (target_note_id, previous_content). Proposals carry this on their
    `derived_from` edge, recorded when the proposal was created.
    """
    for edge in await proposal.get_derived_from():
        if edge.get("kind") == "refinement_target":
            return str(edge.get("out")), edge.get("previous_content")
    return None, None


def _note_response(note: Note, command_id: Optional[str] = None) -> NoteResponse:
    """Single place that maps a Note onto the API shape."""
    return NoteResponse(
        id=note.id or "",
        title=note.title,
        content=note.content,
        note_type=note.note_type,
        created=str(note.created),
        updated=str(note.updated),
        command_id=str(command_id) if command_id else None,
        summary=note.summary,
        tags=note.tags,
        keywords=note.keywords,
        note_kind=note.note_kind,
        status=note.status,
        proposal_status=note.proposal_status,
        generated_by=note.generated_by,
    )


@router.get("/notes", response_model=List[NoteResponse])
async def get_notes(
    notebook_id: Optional[str] = Query(None, description="Filter by notebook ID"),
):
    """Get all notes with optional notebook filtering."""
    try:
        if notebook_id:
            # Get notes for a specific notebook
            from open_notebook.domain.notebook import Notebook

            notebook = await Notebook.get(notebook_id)
            notes = await notebook.get_notes()
        else:
            # Get all notes. Proposals are excluded: an unaccepted AI revision is
            # not knowledge yet and must not show up in note listings.
            notes = await Note.get_knowledge_notes(order_by="updated desc")

        return [_note_response(note) for note in notes]
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching notes: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching notes: {str(e)}")


@router.post("/notes", response_model=NoteResponse)
async def create_note(note_data: NoteCreate):
    """Create a new note."""
    try:
        # Auto-generate title if not provided and it's an AI note
        title = note_data.title
        if not title and note_data.note_type == "ai" and note_data.content:
            from open_notebook.graphs.prompt import graph as prompt_graph

            prompt = "Based on the Note below, please provide a Title for this content, with max 15 words"
            # LangGraph accepts a partial state dict at runtime, but its typed
            # overloads require the full state type (langgraph typing limitation).
            result = await prompt_graph.ainvoke(  # type: ignore[call-overload]
                {
                    "input_text": note_data.content,
                    "prompt": prompt,
                }
            )
            title = result.get("output", "Untitled Note")

        # Validate note_type
        note_type: Optional[Literal["human", "ai"]] = None
        if note_data.note_type in ("human", "ai"):
            note_type = note_data.note_type  # type: ignore[assignment]
        elif note_data.note_type is not None:
            raise HTTPException(
                status_code=400, detail="note_type must be 'human' or 'ai'"
            )

        new_note = Note(
            title=title,
            content=note_data.content,
            note_type=note_type,
        )
        command_id = await new_note.save()

        # Add to notebook if specified
        if note_data.notebook_id:
            from open_notebook.domain.notebook import Notebook

            # Verify the notebook exists (raises NotFoundError -> 404)
            await Notebook.get(note_data.notebook_id)
            await new_note.add_to_notebook(note_data.notebook_id)

        return _note_response(new_note, command_id=str(command_id) if command_id else None)
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error creating note: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error creating note: {str(e)}")


# NOTE: the /notes/proposals routes must stay above GET /notes/{note_id},
# otherwise "proposals" is swallowed by the path parameter (FastAPI matches in
# declaration order).
@router.get("/notes/proposals", response_model=List[NoteProposalResponse])
async def get_note_proposals():
    """List refinement proposals waiting for a decision."""
    try:
        proposals = await Note.get_pending_proposals()
        responses = []
        for proposal in proposals:
            # One extra query per proposal: the list is small (a human has to
            # review each one) and batching would need a group-by query for a
            # single scalar.
            target_note_id, previous_content = await _proposal_target(proposal)
            base = _note_response(proposal)
            responses.append(
                NoteProposalResponse(
                    **base.model_dump(),
                    target_note_id=target_note_id,
                    previous_content=previous_content,
                )
            )
        return responses
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching note proposals: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error fetching note proposals: {str(e)}"
        )


@router.post("/notes/proposals/{proposal_id}/accept", response_model=NoteResponse)
async def accept_note_proposal(proposal_id: str):
    """Apply a refinement proposal to the note it revises.

    This is the only path that replaces an existing note's content, and it
    requires the user's explicit approval.
    """
    try:
        proposal = await Note.get(proposal_id)
        if proposal.proposal_status != "pending":
            raise HTTPException(
                status_code=400, detail="Only a pending proposal can be accepted"
            )

        target_note_id, _previous = await _proposal_target(proposal)
        if not target_note_id:
            raise HTTPException(
                status_code=409, detail="Proposal has no target note to apply to"
            )

        target = await Note.get(target_note_id)
        await target.apply_refinement(proposal)

        return _note_response(target)
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error accepting proposal {proposal_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error accepting proposal: {str(e)}"
        )


@router.post("/notes/proposals/{proposal_id}/reject", response_model=NoteResponse)
async def reject_note_proposal(proposal_id: str):
    """Discard a refinement proposal, leaving the target note untouched."""
    try:
        proposal = await Note.get(proposal_id)
        if proposal.proposal_status != "pending":
            raise HTTPException(
                status_code=400, detail="Only a pending proposal can be rejected"
            )

        await proposal.set_proposal_status("rejected", status="archived")
        return _note_response(proposal)
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error rejecting proposal {proposal_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error rejecting proposal: {str(e)}"
        )


@router.post(
    "/notes/iterate/synthesize",
    response_model=IterateJobResponse,
    status_code=202,
)
async def synthesize_notes(request: SynthesizeNotesRequest):
    """Submit a synthesis job: material becomes a new knowledge note.

    Runs on the surreal-commands worker (an LLM pass takes minutes), so the
    worker must be running or the job queues forever. Poll
    ``GET /api/commands/jobs/{job_id}`` for the outcome.
    """
    try:
        if not request.note_ids and not request.source_ids:
            raise HTTPException(
                status_code=400,
                detail="At least one note or source is required",
            )

        if request.notebook_id:
            from open_notebook.domain.notebook import Notebook

            await Notebook.get(request.notebook_id)
        if request.parent_note_id:
            await Note.get(request.parent_note_id)

        job_id = await CommandService.submit_command_job(
            "open_notebook", "synthesize_notes", request.model_dump()
        )

        return IterateJobResponse(
            job_id=job_id,
            status="queued",
            parent_note_id=request.parent_note_id,
        )
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook or note not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error submitting synthesis job: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error submitting synthesis job: {str(e)}"
        )


@router.post(
    "/notes/iterate/refine", response_model=IterateJobResponse, status_code=202
)
async def refine_note(request: RefineNoteRequest):
    """Submit a refinement job: propose a revised version of a note.

    The note itself is never modified by this; the result is a proposal the user
    accepts or rejects. Poll ``GET /api/commands/jobs/{job_id}`` for it.
    """
    try:
        await Note.get(request.note_id)  # 404 before queueing a doomed job

        job_id = await CommandService.submit_command_job(
            "open_notebook", "refine_note", request.model_dump()
        )

        return IterateJobResponse(job_id=job_id, status="queued")
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error submitting refinement job: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error submitting refinement job: {str(e)}"
        )


@router.get("/notes/{note_id}", response_model=NoteResponse)
async def get_note(note_id: str):
    """Get a specific note by ID."""
    try:
        note = await Note.get(note_id)

        return _note_response(note)
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching note {note_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching note: {str(e)}")


@router.put("/notes/{note_id}", response_model=NoteResponse)
async def update_note(note_id: str, note_update: NoteUpdate):
    """Update a note."""
    try:
        note = await Note.get(note_id)

        # Update only provided fields
        if note_update.title is not None:
            note.title = note_update.title
        if note_update.content is not None:
            note.content = note_update.content
        if note_update.note_type is not None:
            if note_update.note_type in ("human", "ai"):
                note.note_type = note_update.note_type  # type: ignore[assignment]
            else:
                raise HTTPException(
                    status_code=400, detail="note_type must be 'human' or 'ai'"
                )

        command_id = await note.save()

        return _note_response(
            note, command_id=str(command_id) if command_id else None
        )
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error updating note {note_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error updating note: {str(e)}")


@router.delete("/notes/{note_id}")
async def delete_note(note_id: str):
    """Delete a note."""
    try:
        note = await Note.get(note_id)

        await note.delete()

        return {"message": "Note deleted successfully"}
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error deleting note {note_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error deleting note: {str(e)}")
