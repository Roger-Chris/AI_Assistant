"""Brain entry point. CPU-only; runs on the HP for development, on the Mac mini for real."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import scheduler, telegram
from .config import settings
from .db import backfill_chats, init_db
from .routers import ingest, tasks, workers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("brain")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the bot token


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    backfill_chats()
    scheduler.start()
    poller = asyncio.create_task(telegram.poll_forever())
    log.info("brain up — classifier=%s planner=%s tz=%s", settings.classifier_model,
             settings.planner_model, settings.tz)
    try:
        yield
    finally:
        poller.cancel()
        scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="AI_Assistant brain", version="0.1.0", lifespan=lifespan)
app.include_router(ingest.router)
app.include_router(tasks.router)
app.include_router(workers.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/debug/brief")
async def debug_brief() -> dict:
    """Trigger the morning brief now (handy while testing)."""
    await scheduler.daily_brief()
    return {"ok": True}
