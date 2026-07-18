"""Cloudflare Python Worker entrypoints for HTTP and Queue events."""

import asgi
from workers import WorkerEntrypoint

from retro_backend.app import app
from retro_backend.chunk_gate import ChunkGate
from retro_backend.consumers.dispatch import dispatch_queue

__all__ = ["ChunkGate", "Default"]


class Default(WorkerEntrypoint):
    async def fetch(self, request: object) -> object:
        return await asgi.fetch(app, request, self.env)

    async def queue(self, batch: object) -> None:
        await dispatch_queue(batch, self.env)
