from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from open_notebook.domain.transformation import Transformation


def _client() -> TestClient:
    from api.main import app

    return TestClient(app)


def _transformation(model_id: str | None = None) -> Transformation:
    return Transformation(
        id="transformation:123",
        name="summary",
        title="Summary",
        description="Summarize the source",
        prompt="Summarize this",
        apply_default=False,
        model_id=model_id,
        created=datetime(2026, 1, 1, 12, 0, 0),
        updated=datetime(2026, 1, 1, 12, 0, 0),
    )


def _submit(
    client: TestClient,
    transformation: Transformation,
    *,
    payload_extra: dict | None = None,
    ainvoke_return=None,
    ainvoke_side_effect=None,
    model_get_return=object(),
):
    """POST execute-async with the standard transformation mocks applied."""
    payload = {"transformation_id": transformation.id, "input_text": "Input text"}
    if payload_extra:
        payload.update(payload_extra)

    with (
        patch(
            "api.routers.transformations.Transformation.get",
            new_callable=AsyncMock,
            return_value=transformation,
        ),
        patch(
            "api.routers.transformations.Model.get",
            new_callable=AsyncMock,
            return_value=model_get_return,
        ) as mock_model_get,
        patch(
            "api.routers.transformations.transformation_graph.ainvoke",
            new_callable=AsyncMock,
            return_value=ainvoke_return,
            side_effect=ainvoke_side_effect,
        ) as mock_ainvoke,
    ):
        response = client.post("/api/transformations/execute-async", json=payload)

    return response, mock_model_get, mock_ainvoke


def _get_job(client: TestClient, job_id: str) -> dict:
    response = client.get(f"/api/transformations/jobs/{job_id}")
    assert response.status_code == 200
    return response.json()


def test_execute_async_submits_job_and_reports_done():
    client = _client()
    transformation = _transformation()
    response, mock_model_get, mock_ainvoke = _submit(
        client, transformation, ainvoke_return={"output": "Async output"}
    )

    # Submit returns 202 with a job id immediately.
    assert response.status_code == 202
    body = response.json()
    assert body["job_id"].startswith("job_")
    assert body["status"] == "queued"
    assert body["transformation_id"] == transformation.id
    assert body["model_id"] is None

    # FastAPI TestClient runs background tasks to completion before returning,
    # so the job is already done when we poll.
    job = _get_job(client, body["job_id"])
    assert job["status"] == "done"
    assert job["output"] == "Async output"
    assert job["error"] is None
    assert job["finished"] is not None

    mock_model_get.assert_not_awaited()
    assert (
        mock_ainvoke.call_args.kwargs["config"]["configurable"]["model_id"] is None
    )


def test_execute_async_uses_request_model_id_and_validates_it():
    client = _client()
    transformation = _transformation(model_id="model:stored")
    response, mock_model_get, mock_ainvoke = _submit(
        client,
        transformation,
        payload_extra={"model_id": "model:override"},
        ainvoke_return={"output": "Override output"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["model_id"] == "model:override"

    job = _get_job(client, body["job_id"])
    assert job["status"] == "done"
    assert job["output"] == "Override output"

    mock_model_get.assert_awaited_once_with("model:override")
    assert (
        mock_ainvoke.call_args.kwargs["config"]["configurable"]["model_id"]
        == "model:override"
    )


def test_execute_async_reports_error_state():
    client = _client()
    transformation = _transformation()
    response, _, _ = _submit(
        client,
        transformation,
        ainvoke_side_effect=Exception("boom"),
    )

    assert response.status_code == 202
    job = _get_job(client, response.json()["job_id"])
    assert job["status"] == "error"
    assert job["output"] is None
    assert "boom" in (job["error"] or "")


def test_execute_async_unknown_transformation_returns_404():
    client = _client()

    with patch(
        "api.routers.transformations.Transformation.get",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = client.post(
            "/api/transformations/execute-async",
            json={"transformation_id": "transformation:missing", "input_text": "x"},
        )

    assert response.status_code == 404


def test_execute_async_unknown_model_returns_404():
    client = _client()
    transformation = _transformation()

    response, _, _ = _submit(
        client,
        transformation,
        payload_extra={"model_id": "model:missing"},
        model_get_return=None,
    )

    assert response.status_code == 404


def test_job_status_unknown_job_returns_404():
    client = _client()
    response = client.get("/api/transformations/jobs/job_does_not_exist")
    assert response.status_code == 404
