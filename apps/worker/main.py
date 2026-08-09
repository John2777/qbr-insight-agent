from __future__ import annotations

import os
import time

from packages.qbr_core import QBRService, Settings


def run() -> None:
    service = QBRService(Settings.from_env())
    worker_id = f"worker-{os.getpid()}"
    while True:
        try:
            processed = service.process_next_run(worker_id)
            if not processed:
                processed = service.process_next_job(worker_id)
        except Exception as exc:
            print(f"worker job failed: {type(exc).__name__}", flush=True)
            processed = None
        if not processed:
            time.sleep(service.settings.worker_poll_seconds)


if __name__ == "__main__":
    run()
