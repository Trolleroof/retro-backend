"""Cloudflare Python Worker entrypoints for HTTP and Queue events."""

from http import HTTPMethod
from typing import Protocol, cast

import asgi
from workers import Blob, Request, Response, WorkerEntrypoint

from retro_backend.app import app
from retro_backend.chunk_gate import ChunkGate
from retro_backend.consumers.dispatch import dispatch_queue
from retro_backend.worker_http import fetch_with_body_limit

__all__ = ["ChunkGate", "Default"]


class _WorkerJsRequest(Protocol):
    url: str
    method: str
    headers: object
    body: object
    redirect: str


def _rebuild_worker_request(
    request: object,
    body: bytes,
    headers: list[tuple[str, str]],
) -> _WorkerJsRequest:
    raw_request = getattr(request, "js_object", request)
    incoming = cast(_WorkerJsRequest, raw_request)
    bounded_request = Request(
        incoming.url,
        method=cast(HTTPMethod, incoming.method),
        headers=headers,
        body=Blob(body).js_object,
        redirect=incoming.redirect,
    )
    return bounded_request.js_object


def _too_large_response() -> Response:
    return Response.from_json(
        {"detail": "request body exceeds maximum size"},
        status=413,
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request: object) -> object:
        return await fetch_with_body_limit(
            request,
            self.env,
            app=app,
            asgi_fetch=asgi.fetch,
            request_factory=_rebuild_worker_request,
            too_large_response=_too_large_response,
        )

    async def queue(self, batch: object) -> None:
        await dispatch_queue(batch, self.env)
