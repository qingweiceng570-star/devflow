import logging
import signal
import socket
import threading
from datetime import UTC, datetime
from uuid import uuid4

from app.core.config import get_settings
from app.persistence.database import create_database
from app.persistence.models import WorkerHeartbeat


def main():
    settings = get_settings()
    engine, factory = create_database(settings)
    worker_id = f"{socket.gethostname()}-{uuid4().hex[:8]}"
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.info("M0 worker started; business job processing begins in M1.")
    while not stopped.is_set():
        try:
            with factory() as db:
                heartbeat = db.get(WorkerHeartbeat, worker_id)
                if heartbeat is None:
                    heartbeat = WorkerHeartbeat(worker_id=worker_id, last_seen_at=datetime.now(UTC))
                    db.add(heartbeat)
                heartbeat.last_seen_at = datetime.now(UTC)
                db.commit()
        except Exception as error:
            logging.error("worker_heartbeat_failed type=%s", type(error).__name__)
        stopped.wait(settings.worker_heartbeat_seconds)
    engine.dispose()


if __name__ == "__main__":
    main()
