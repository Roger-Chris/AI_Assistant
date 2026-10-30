"""Brain entry point. CPU-only; runs on the HP for development, on the Mac mini for real."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import scheduler
from .config import settings
from .db import backfill_chats, init_db
from .routers import agent, alerts, control, ingest, learn, memory, tasks, workers
from .routers import health as health_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("brain")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines print every URL, keys included


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    backfill_chats()
    scheduler.start()
    log.info("brain up — classifier=%s planner=%s tz=%s", settings.classifier_model,
             settings.planner_model, settings.tz)
    try:
        yield
    finally:
        scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="AI_Assistant brain", version="0.1.0", lifespan=lifespan)
app.include_router(agent.router)
app.include_router(alerts.router)
app.include_router(control.router)
app.include_router(health_router.router)
app.include_router(ingest.router)
app.include_router(learn.router)
app.include_router(memory.router)
app.include_router(tasks.router)
app.include_router(workers.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
