"""FastAPI application composed for the Cloudflare ASGI adapter."""

from fastapi import FastAPI

from .api.chunks import router as chunks_router
from .api.health import router as health_router
from .api.results import router as results_router
from .api.rooms import router as rooms_router

app = FastAPI(title="Retro Backend", version="0.2.0")
app.include_router(health_router)
app.include_router(chunks_router)
app.include_router(results_router)
app.include_router(rooms_router)
