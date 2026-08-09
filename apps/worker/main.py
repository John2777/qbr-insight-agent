from __future__ import annotations

import json
import logging
import os
import time

from packages.qbr_core import QBRService, Settings


def run() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), format="%(message)s")
    logger = logging.getLogger("qbr.worker")
    service = QBRService(settings)
    worker_id = f"worker-{os.getpid()}"
    while True:
        try:
            processed = service.process_next_run(worker_id)
            if not processed:
                processed = service.process_next_job(worker_id)
        except Exception as exc:
            logger.exception(
                json.dumps(
                    {"event": "worker_job_failed", "worker_id": worker_id, "error_type": type(exc).__name__},
                    separators=(",", ":"),
                )
            )
            processed = None
        if not processed:
            time.sleep(service.settings.worker_poll_seconds)


if __name__ == "__main__":
    run()
