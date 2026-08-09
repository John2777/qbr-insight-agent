from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .db import Database


@dataclass(frozen=True, slots=True)
class LeasePolicy:
    """Timing policy shared by persistent background-work queues."""

    lease_seconds: int
    heartbeat_seconds: int

    def __post_init__(self) -> None:
        if self.lease_seconds < 2:
            raise ValueError("lease_seconds must be at least 2")
        if not 0 < self.heartbeat_seconds < self.lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than lease_seconds")


class LeaseCoordinator:
    """Refresh database leases without coupling workers to a specific queue.

    The queue owner is still responsible for atomically claiming work. This
    component owns the cross-cutting heartbeat lifecycle used by ingestion and
    answer workers, so both queues follow exactly the same failure semantics.
    """

    _SUPPORTED_TABLES = frozenset({"ingestion_jobs", "runs"})

    def __init__(self, db: Database, policy: LeasePolicy) -> None:
        self.db = db
        self.policy = policy

    def expiry(self) -> str:
        return (
            datetime.now(UTC) + timedelta(seconds=self.policy.lease_seconds)
        ).isoformat().replace("+00:00", "Z")

    @contextmanager
    def heartbeat(self, table: str, item_id: str, owner: str) -> Iterator[None]:
        if table not in self._SUPPORTED_TABLES:
            raise ValueError(f"Unsupported lease table: {table}")
        stopped = threading.Event()

        def refresh() -> None:
            while not stopped.wait(self.policy.heartbeat_seconds):
                try:
                    with self.db.transaction(immediate=True) as conn:
                        conn.execute(
                            f"UPDATE {table} SET lease_expires_at=? "
                            "WHERE id=? AND lease_owner=? AND status='running'",
                            (self.expiry(), item_id, owner),
                        )
                except sqlite3.Error:
                    # A transient SQLite lock must not kill the worker that owns
                    # the lease. The next heartbeat or the lease timeout recovers.
                    continue

        thread = threading.Thread(target=refresh, name=f"lease-{item_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)
