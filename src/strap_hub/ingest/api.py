"""Authenticated FastAPI endpoints for digest v1."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import closing
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from strap_hub.ingest.models import Digest
from strap_hub.ingest.writes import (
    log_digest,
    record_push_seq,
    revoke_workout,
    utc_now,
    write_day,
    write_night,
    write_transaction,
    write_workout,
)
from strap_hub.store import connect

MAX_BODY_BYTES = 2_000_000


def authorize(request: Request) -> None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not compare_digest(token.encode(), request.app.state.hub_token):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})


class DigestRequest(Request):
    async def stream(self) -> AsyncIterator[bytes]:
        size = 0
        async for chunk in super().stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise HTTPException(413, "Digest exceeds 2 MB")
            yield chunk

    async def json(self) -> Any:
        try:
            return json.loads((await self.body()).decode("utf-8"))
        except (ValueError, RecursionError):
            raise HTTPException(422, "Invalid JSON") from None


class DigestRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handle = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            authorize(request)
            content_length = request.headers.get("content-length", "").lstrip("0")
            if content_length.isascii() and content_length.isdecimal():
                if len(content_length) > len(str(MAX_BODY_BYTES)) or int(content_length) > MAX_BODY_BYTES:
                    raise HTTPException(413, "Digest exceeds 2 MB")
            bounded = DigestRequest(request.scope, request.receive)
            await bounded.body()
            content_type = request.headers.get("content-type", "").split(";", 1)[0]
            if content_type.strip().lower() != "application/json":
                raise HTTPException(422, "Expected application/json")
            return await handle(bounded)

        return guarded


def create_app(*, token: str, db_path: str | Path) -> FastAPI:
    if not token or any(char.isspace() for char in token):
        raise ValueError("HUB_TOKEN must be a nonempty bearer token")

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)
    app.state.hub_token = token.encode()

    @app.exception_handler(RequestValidationError)
    async def invalid_digest(_request: Request, _error: RequestValidationError) -> JSONResponse:
        # Error inputs and dictionary keys may contain sensitive data.
        return JSONResponse(status_code=422, content={"detail": "Invalid digest schema"})

    @app.get("/v1/health")
    def health(request: Request) -> dict[str, bool | str | None]:
        authorize(request)
        with closing(connect(db_path)) as connection:
            row = connection.execute(
                "SELECT received_at FROM digest_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {"ok": True, "last_digest_at": row["received_at"] if row else None}

    router = APIRouter(route_class=DigestRoute)

    @router.post("/v1/digest")
    def digest(payload: Digest) -> dict[str, str | dict[str, int]]:
        accepted = {
            "days": len(payload.days),
            "nights": len(payload.nights),
            "workouts": len(payload.workouts),
        }
        version = payload.source.algo_version
        freshness = payload.freshness.last_band_record_at
        result = {"accepted": accepted, "freshness": freshness}
        canonical_payload = payload.model_dump(mode="json", by_alias=True)
        if not payload.revoked_workout_ids:
            # Keep matching pre-v2 receipts for digests without revocations.
            del canonical_payload["revoked_workout_ids"]
        if payload.seq is None:
            del canonical_payload["seq"]
        canonical = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
        receipt_key = f"push_digest:{sha256(canonical.encode()).hexdigest()}"

        with write_transaction(db_path) as connection:
            # Reserve the receipt in the same transaction as the rows and audit log.
            receipt = connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, '1')", (receipt_key,)
            )
            if receipt.rowcount == 0:
                return result

            received_at = utc_now()
            for day in payload.days:
                write_day(connection, day, version, received_at, source="push")
            for night in payload.nights:
                write_night(connection, night, version, source="push")
            for workout in payload.workouts:
                write_workout(connection, workout, version, received_at, source="push", seq=payload.seq)
            for workout_id in payload.revoked_workout_ids:
                revoke_workout(connection, workout_id, received_at, source="push", seq=payload.seq)
            record_push_seq(connection, payload.seq)
            log_digest(
                connection,
                received_at=received_at,
                sent_at=payload.sent_at,
                app_version=payload.source.app_version,
                algo_version=version,
                last_band_record_at=freshness,
                source="push",
                **accepted,
            )

        return result

    app.include_router(router)
    return app
