"""SSE framing, cursor validation and heartbeats (README 19)."""

import asyncio
import time
from uuid import UUID

from fastapi import Request
from sqlalchemy import select

from app.core.errors import DomainError
from app.persistence.models import Run, RunEvent
from app.schemas.responses import event_view

HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 1.0
TERMINAL_TYPES = {"run.completed", "run.failed", "run.cancelled", "run.timed_out"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}


def resolve_after(request: Request, query_after: int | None) -> int:
    """Last-Event-ID header wins; both are strict non-negative integers."""
    header = request.headers.get("last-event-id")
    raw = header if header is not None else query_after
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise DomainError("INVALID_REQUEST", "事件游标不合法", 400) from None
    if value < 0:
        raise DomainError("INVALID_REQUEST", "事件游标不合法", 400)
    return value


def format_event(view: dict) -> str:
    import json

    return (
        f"id: {view['seq']}\n"
        f"event: {view['type']}\n"
        f"data: {json.dumps(view, ensure_ascii=False)}\n\n"
    )


async def stream(db_factory, run_id: UUID, after_seq: int):
    """Replay events after `after_seq`, then follow until a terminal event."""
    last = after_seq
    last_beat = time.monotonic()
    while True:
        with db_factory() as db:
            rows = db.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > last)
                .order_by(RunEvent.seq)
                .limit(500)
            ).all()
            events = [event_view(row) for row in rows]
            status = None
            if not events:
                status = db.scalar(select(Run.status).where(Run.id == run_id))
        for view in events:
            last = view["seq"]
            yield format_event(view)
            if view["type"] in TERMINAL_TYPES:
                return
        if not events and status in TERMINAL_STATUSES:
            return
        if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            last_beat = time.monotonic()
        await asyncio.sleep(POLL_SECONDS)
