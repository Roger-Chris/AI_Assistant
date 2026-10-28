"""Alerts for the phone app (through the Orchestrator hub): what needs you, and your answer."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from .. import alerts
from ..auth import require_token
from ..db import Alert, get_session

router = APIRouter(prefix="/alerts", tags=["alerts"], dependencies=[Depends(require_token)])


@router.get("")
def list_open(session: Session = Depends(get_session)) -> list[dict]:
    return [alerts.as_dict(a) for a in alerts.open_alerts(session)]


class Ack(BaseModel):
    action: Literal["done", "snooze", "dismiss"]


@router.post("/{alert_id}/ack")
def ack(alert_id: int, body: Ack, session: Session = Depends(get_session)) -> dict:
    a = session.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "no such alert")
    return alerts.as_dict(alerts.ack(session, a, body.action))
