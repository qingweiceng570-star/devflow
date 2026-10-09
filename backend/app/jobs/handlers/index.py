"""Index job handler (README 7.4): consume outbox rows, build and activate a
new generation. Idempotent: a retry rebuilds from committed DB state; outbox
rows created before the build started are marked processed only on success."""

import logging
from datetime import UTC, datetime

from sqlalchemy import update

from app.core.config import Settings
from app.jobs.queue import HandlerRetry
from app.persistence.models import IndexOutbox
from app.rag import index_manager

log = logging.getLogger("devflow.index")

MAX_BUILD_LOOPS = 3


def handle_index(
    db_factory,
    settings: Settings,
    job,
    worker_id: str,
    *,
    embedding_factory=None,
    vector_store_factory=None,
) -> dict:
    repo_id = job.entity_id
    last_generation = None
    for attempt in range(MAX_BUILD_LOOPS):
        with db_factory() as db:
            pending = index_manager.pending_outbox(db, repo_id)
            if not pending:
                if last_generation is None:
                    return {"status": "noop", "reason": "no_pending_changes"}
                return {
                    "status": "activated",
                    "generation_id": str(last_generation.id),
                    "chunks": last_generation.chunk_count,
                    "vector": bool((last_generation.embedding_config or {}).get("enabled")),
                    "loops": attempt,
                }
            build_start = datetime.now(UTC)
            last_generation = index_manager.build_and_activate(
                db,
                settings,
                repo_id,
                embedding_factory=embedding_factory,
                vector_store_factory=vector_store_factory,
            )
            # Rows that arrived after the build started stay pending and will
            # be picked up by the next loop (or by a later retry).
            db.execute(
                update(IndexOutbox)
                .where(
                    IndexOutbox.repo_id == repo_id,
                    IndexOutbox.status == "pending",
                    IndexOutbox.created_at <= build_start,
                )
                .values(status="processed", processed_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            db.commit()
    # Uploads kept arriving while we built; come back shortly.
    raise HandlerRetry(15)
