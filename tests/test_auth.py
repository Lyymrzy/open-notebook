"""Tests for API password authentication middleware."""

import pytest
from fastapi import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from api.auth import PasswordAuthMiddleware


async def _unused_app(_: Scope, __: Receive, ___: Send) -> None:
    raise AssertionError("dispatch should use the supplied call_next")


async def _allow_request(_: Request) -> Response:
    return Response(status_code=200)


async def _request(
    monkeypatch: pytest.MonkeyPatch,
    *,
    password: str | None,
    credentials: bytes | None = None,
    path: str = "/api/protected",
) -> int:
    if password is None:
        monkeypatch.delenv("OPEN_NOTEBOOK_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("OPEN_NOTEBOOK_PASSWORD", password)

    headers = []
    if credentials is not None:
        headers.append((b"authorization", b"Bearer " + credentials))

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
    }
    middleware = PasswordAuthMiddleware(_unused_app)
    response = await middleware.dispatch(Request(scope), _allow_request)
    return response.status_code


class TestPasswordAuthMiddleware:
    @pytest.mark.asyncio
    async def test_accepts_ascii_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status = await _request(monkeypatch, password="secret", credentials=b"secret")

        assert status == 200

    @pytest.mark.asyncio
    async def test_rejects_wrong_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status = await _request(monkeypatch, password="secret", credentials=b"wrong")

        assert status == 401


class TestNoPasswordConfigured:
    @pytest.mark.asyncio
    async def test_allows_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        status = await _request(monkeypatch, password=None)

        assert status == 200


class TestNonAsciiPassword:
    @pytest.mark.asyncio
    async def test_accepts_utf8_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        password = "pässwörd-中文"

        status = await _request(
            monkeypatch, password=password, credentials=password.encode("utf-8")
        )

        assert status == 200

    @pytest.mark.asyncio
    async def test_invalid_utf8_returns_unauthorized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status = await _request(monkeypatch, password="secret", credentials=b"\xff")

        assert status == 401


class TestForkAuthScope:
    """This fork enforces auth on /api* only - a deliberate divergence.

    The API process also serves the statically exported frontend, and browser
    document/asset requests cannot carry the Bearer token the SPA keeps in
    storage. Gating those paths would make the UI unreachable whenever a
    password is set, so `api/auth.py` lets non-/api paths through. Upstream's
    tests above use `/api` paths so they exercise the middleware for real
    instead of passing because auth was skipped.
    """

    @pytest.mark.asyncio
    async def test_non_api_paths_skip_authentication(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status = await _request(
            monkeypatch,
            password="secret",
            credentials=b"wrong",
            path="/notebooks/",
        )

        assert status == 200

    @pytest.mark.asyncio
    async def test_api_paths_still_enforce_authentication(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status = await _request(
            monkeypatch,
            password="secret",
            credentials=b"wrong",
            path="/api/notes",
        )

        assert status == 401
