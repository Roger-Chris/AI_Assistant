"""Self-check results for the app: GET /health/detail (the plain GET /health stays open)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import health
from ..auth import require_token

router = APIRouter(prefix="/health", tags=["health"], dependencies=[Depends(require_token)])


@router.get("/detail")
async def detail() -> dict:
    return await health.detail()
