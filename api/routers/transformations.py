import uuid
from datetime import datetime, timezone
from typing import Dict, List

from fastapi import APIRouter, BackgroundTasks, HTTPException
from loguru import logger

from api.models import (
    DefaultPromptResponse,
    DefaultPromptUpdate,
    TransformationCreate,
    TransformationExecuteRequest,
    TransformationExecuteResponse,
    TransformationJobStatusResponse,
    TransformationJobSubmitResponse,
    TransformationResponse,
    TransformationUpdate,
)
from open_notebook.ai.models import Model
from open_notebook.domain.transformation import DefaultPrompts, Transformation
from open_notebook.exceptions import InvalidInputError, OpenNotebookError
from open_notebook.graphs.transformation import graph as transformation_graph

router = APIRouter()

# In-memory store for asynchronous transformation jobs.
# Jobs only live in this process: a worker restart drops queued/running jobs
# (their status endpoint then returns 404). Open Notebook runs a single API
# worker, so this is safe; it is not shared across multiple workers.
_jobs: Dict[str, dict] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_transformation_job(job_id: str) -> None:
    """Execute a submitted transformation job and record its outcome."""
    job = _jobs.get(job_id)
    if job is None:
        return
    try:
        job["status"] = "running"
        job["started"] = _now_iso()
        # LangGraph accepts a partial state dict at runtime, but its typed
        # overloads require the full state type (langgraph typing limitation).
        result = await transformation_graph.ainvoke(  # type: ignore[call-overload]
            dict(
                input_text=job["input_text"],
                transformation=job["transformation"],
            ),
            config=dict(configurable={"model_id": job["model_id"]}),
        )
        job["status"] = "done"
        job["output"] = result["output"]
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        logger.error(f"Error executing transformation job {job_id}: {str(e)}")
    finally:
        job["finished"] = _now_iso()


def _transformation_response(transformation: Transformation) -> TransformationResponse:
    return TransformationResponse(
        id=transformation.id or "",
        name=transformation.name,
        title=transformation.title,
        description=transformation.description,
        prompt=transformation.prompt,
        apply_default=transformation.apply_default,
        model_id=transformation.model_id,
        created=str(transformation.created),
        updated=str(transformation.updated),
    )


@router.get("/transformations", response_model=List[TransformationResponse])
async def get_transformations():
    """Get all transformations."""
    try:
        transformations = await Transformation.get_all(order_by="name asc")

        return [
            _transformation_response(transformation)
            for transformation in transformations
        ]
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching transformations: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error fetching transformations: {str(e)}"
        )


@router.post("/transformations", response_model=TransformationResponse)
async def create_transformation(transformation_data: TransformationCreate):
    """Create a new transformation."""
    try:
        # Reject unknown model references up front (same check as execute);
        # otherwise an invalid model_id is stored and only fails at run time.
        if transformation_data.model_id:
            model = await Model.get(transformation_data.model_id)
            if not model:
                raise HTTPException(status_code=404, detail="Model not found")

        new_transformation = Transformation(
            name=transformation_data.name,
            title=transformation_data.title,
            description=transformation_data.description,
            prompt=transformation_data.prompt,
            apply_default=transformation_data.apply_default,
            model_id=transformation_data.model_id,
        )
        await new_transformation.save()

        return _transformation_response(new_transformation)
    except HTTPException:
        raise
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error creating transformation: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error creating transformation: {str(e)}"
        )


@router.post("/transformations/execute", response_model=TransformationExecuteResponse)
async def execute_transformation(execute_request: TransformationExecuteRequest):
    """Execute a transformation on input text."""
    try:
        # Validate transformation exists
        transformation = await Transformation.get(execute_request.transformation_id)
        if not transformation:
            raise HTTPException(status_code=404, detail="Transformation not found")

        model_id = execute_request.model_id or transformation.model_id

        # Validate explicit or transformation-specific model exists.
        # None is allowed so the graph can use the configured transformation default.
        if model_id:
            model = await Model.get(model_id)
            if not model:
                raise HTTPException(status_code=404, detail="Model not found")

        # Execute the transformation.
        # LangGraph accepts a partial state dict at runtime, but its typed
        # overloads require the full state type (langgraph typing limitation).
        result = await transformation_graph.ainvoke(  # type: ignore[call-overload]
            dict(
                input_text=execute_request.input_text,
                transformation=transformation,
            ),
            config=dict(configurable={"model_id": model_id}),
        )

        return TransformationExecuteResponse(
            output=result["output"],
            transformation_id=execute_request.transformation_id,
            model_id=model_id,
        )

    except HTTPException:
        raise
    except OpenNotebookError:
        raise  # Let global exception handlers return proper status codes
    except Exception as e:
        logger.error(f"Error executing transformation: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error executing transformation: {str(e)}"
        )


@router.post(
    "/transformations/execute-async",
    response_model=TransformationJobSubmitResponse,
    status_code=202,
)
async def execute_transformation_async(
    execute_request: TransformationExecuteRequest,
    background_tasks: BackgroundTasks,
):
    """Submit a transformation for asynchronous execution and return a job id.

    The endpoint responds immediately (202) with a job id; the caller polls
    ``GET /transformations/jobs/{job_id}`` for the outcome. This avoids tying up
    a single synchronous HTTP request for the whole (potentially minutes-long)
    LLM generation. The synchronous ``POST /transformations/execute`` is kept
    for backward compatibility.
    """
    try:
        # Validate transformation exists
        transformation = await Transformation.get(execute_request.transformation_id)
        if not transformation:
            raise HTTPException(status_code=404, detail="Transformation not found")

        model_id = execute_request.model_id or transformation.model_id

        # Validate explicit or transformation-specific model exists.
        # None is allowed so the graph can use the configured transformation default.
        if model_id:
            model = await Model.get(model_id)
            if not model:
                raise HTTPException(status_code=404, detail="Model not found")

        job_id = f"job_{uuid.uuid4().hex}"
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "transformation_id": execute_request.transformation_id,
            "model_id": model_id,
            "input_text": execute_request.input_text,
            "transformation": transformation,
            "created": _now_iso(),
            "started": None,
            "finished": None,
            "output": None,
            "error": None,
        }
        background_tasks.add_task(_run_transformation_job, job_id)

        return TransformationJobSubmitResponse(
            job_id=job_id,
            transformation_id=execute_request.transformation_id,
            status="queued",
            model_id=model_id,
        )
    except HTTPException:
        raise
    except OpenNotebookError:
        raise  # Let global exception handlers return proper status codes
    except Exception as e:
        logger.error(f"Error submitting transformation: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error submitting transformation: {str(e)}"
        )


@router.get(
    "/transformations/jobs/{job_id}", response_model=TransformationJobStatusResponse
)
async def get_transformation_job_status(job_id: str):
    """Get the status and, once finished, the result of an async job."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return TransformationJobStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        output=job.get("output"),
        error=job.get("error"),
        transformation_id=job.get("transformation_id"),
        model_id=job.get("model_id"),
        created=job.get("created"),
        started=job.get("started"),
        finished=job.get("finished"),
    )


@router.get("/transformations/default-prompt", response_model=DefaultPromptResponse)
async def get_default_prompt():
    """Get the default transformation prompt."""
    try:
        default_prompts: DefaultPrompts = await DefaultPrompts.get_instance()  # type: ignore[assignment]

        return DefaultPromptResponse(
            transformation_instructions=default_prompts.transformation_instructions
            or ""
        )
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching default prompt: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error fetching default prompt: {str(e)}"
        )


@router.put("/transformations/default-prompt", response_model=DefaultPromptResponse)
async def update_default_prompt(prompt_update: DefaultPromptUpdate):
    """Update the default transformation prompt."""
    try:
        default_prompts: DefaultPrompts = await DefaultPrompts.get_instance()  # type: ignore[assignment]

        default_prompts.transformation_instructions = (
            prompt_update.transformation_instructions
        )
        await default_prompts.update()

        return DefaultPromptResponse(
            transformation_instructions=default_prompts.transformation_instructions
        )
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error updating default prompt: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error updating default prompt: {str(e)}"
        )


@router.get(
    "/transformations/{transformation_id}", response_model=TransformationResponse
)
async def get_transformation(transformation_id: str):
    """Get a specific transformation by ID."""
    try:
        transformation = await Transformation.get(transformation_id)
        if not transformation:
            raise HTTPException(status_code=404, detail="Transformation not found")

        return _transformation_response(transformation)
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching transformation {transformation_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error fetching transformation: {str(e)}"
        )


@router.put(
    "/transformations/{transformation_id}", response_model=TransformationResponse
)
async def update_transformation(
    transformation_id: str, transformation_update: TransformationUpdate
):
    """Update a transformation."""
    try:
        transformation = await Transformation.get(transformation_id)
        if not transformation:
            raise HTTPException(status_code=404, detail="Transformation not found")

        # Update only provided fields
        if transformation_update.name is not None:
            transformation.name = transformation_update.name
        if transformation_update.title is not None:
            transformation.title = transformation_update.title
        if transformation_update.description is not None:
            transformation.description = transformation_update.description
        if transformation_update.prompt is not None:
            transformation.prompt = transformation_update.prompt
        if transformation_update.apply_default is not None:
            transformation.apply_default = transformation_update.apply_default
        if "model_id" in transformation_update.model_fields_set:
            # Validate a newly supplied model reference (allow clearing to None).
            if transformation_update.model_id:
                model = await Model.get(transformation_update.model_id)
                if not model:
                    raise HTTPException(status_code=404, detail="Model not found")
            transformation.model_id = transformation_update.model_id

        await transformation.save()

        return _transformation_response(transformation)
    except HTTPException:
        raise
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error updating transformation {transformation_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error updating transformation: {str(e)}"
        )


@router.delete("/transformations/{transformation_id}")
async def delete_transformation(transformation_id: str):
    """Delete a transformation."""
    try:
        transformation = await Transformation.get(transformation_id)
        if not transformation:
            raise HTTPException(status_code=404, detail="Transformation not found")

        await transformation.delete()

        return {"message": "Transformation deleted successfully"}
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error deleting transformation {transformation_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error deleting transformation: {str(e)}"
        )
