"""Exploration points: the proposals that let the knowledge tree keep growing.

Listing and dismissing are plain CRUD; scanning the frontier is an LLM pass and
therefore a background job (see commands/explore_commands.py).
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.command_service import CommandService
from api.models import (
    ExplorationPointResponse,
    FrontierScanJobResponse,
    FrontierScanRequest,
)
from open_notebook.domain.exploration import ExplorationPoint
from open_notebook.domain.notebook import Notebook
from open_notebook.exceptions import (
    InvalidInputError,
    NotFoundError,
    OpenNotebookError,
)

router = APIRouter()

# Statuses a user can act on; consumed/dismissed points are history.
_OPEN_STATUSES = ["proposed", "accepted"]


async def _to_response(point: ExplorationPoint) -> ExplorationPointResponse:
    anchors = await point.get_anchors() if point.id else []
    return ExplorationPointResponse(
        id=point.id or "",
        question=point.question,
        rationale=point.rationale,
        kind=point.kind,
        status=point.status,
        score=point.score,
        notebook_id=str(point.notebook) if point.notebook else None,
        anchor_id=anchors[0] if anchors else None,
        created=str(point.created),
        updated=str(point.updated),
    )


@router.get("/explorations", response_model=List[ExplorationPointResponse])
async def list_explorations(
    notebook_id: Optional[str] = Query(
        None, description="Restrict to one notebook (default: everything)"
    ),
    status: Optional[str] = Query(
        None, description="Only return this status (proposed, accepted, ...)"
    ),
):
    """List exploration points, best first."""
    try:
        statuses = [status] if status else _OPEN_STATUSES
        if notebook_id:
            await Notebook.get(notebook_id)
            points = await ExplorationPoint.get_for_notebook(
                notebook_id, statuses=statuses
            )
        else:
            points = await ExplorationPoint.get_all(order_by="score desc")
            points = [point for point in points if point.status in statuses]
        return [await _to_response(point) for point in points]
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error listing exploration points: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error listing exploration points: {str(e)}"
        )


@router.post(
    "/explorations/{point_id}/dismiss", response_model=ExplorationPointResponse
)
async def dismiss_exploration(point_id: str):
    """Dismiss a proposal: the user is not interested in this direction."""
    return await _set_point_status(point_id, "dismissed")


@router.post(
    "/explorations/{point_id}/accept", response_model=ExplorationPointResponse
)
async def accept_exploration(point_id: str):
    """Accept a proposal: the user intends to explore it.

    Acceptance only marks intent - it does not generate anything. Producing the
    knowledge is a separate step (synthesize/refine, or a Q&A session that gets
    consolidated).
    """
    return await _set_point_status(point_id, "accepted")


@router.post(
    "/explorations/{point_id}/consume", response_model=ExplorationPointResponse
)
async def consume_exploration(point_id: str):
    """Retire a proposal that has produced knowledge.

    Distinct from dismissing: the direction *was* explored, so it should stop
    being offered without being recorded as a rejected idea.
    """
    return await _set_point_status(point_id, "consumed")


async def _set_point_status(point_id: str, status: str) -> ExplorationPointResponse:
    try:
        point = await ExplorationPoint.get(point_id)
        await point.set_status(status)  # type: ignore[arg-type]
        return await _to_response(point)
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Exploration point not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error updating exploration point {point_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error updating exploration point: {str(e)}"
        )


@router.post(
    "/explorations/scan", response_model=FrontierScanJobResponse, status_code=202
)
async def scan_frontier(request: FrontierScanRequest):
    """Submit a frontier scan: propose what to explore next.

    Runs on the surreal-commands worker (it is an LLM pass), so the worker must
    be running. Poll ``GET /api/commands/jobs/{job_id}`` for the outcome.
    """
    try:
        await Notebook.get(request.notebook_id)  # 404 before queueing a doomed job

        job_id = await CommandService.submit_command_job(
            "open_notebook", "scan_frontier", request.model_dump()
        )
        return FrontierScanJobResponse(
            job_id=job_id, status="queued", notebook_id=request.notebook_id
        )
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error submitting frontier scan: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error submitting frontier scan: {str(e)}"
        )
