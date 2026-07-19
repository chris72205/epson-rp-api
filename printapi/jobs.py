import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

QUEUED = "queued"
PRINTING = "printing"
DONE = "done"
FAILED = "failed"

TERMINAL_STATUSES = (DONE, FAILED)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_job_id():
    return uuid.uuid4().hex[:12]


@dataclass
class Job:
    id: str
    blocks: list
    source: str  # HTTP: "print" | "text" | "test"; RabbitMQ: client-supplied
    status: str = QUEUED
    error: str = None
    created_at: str = field(default_factory=_now)
    started_at: str = None
    finished_at: str = None

    def to_public(self):
        return {
            "id": self.id,
            "source": self.source,
            "status": self.status,
            "error": self.error,
            "block_count": len(self.blocks),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobStore:
    """Thread-safe, bounded job registry.

    History pruning only ever evicts terminal (done/failed) jobs, so a
    queued job can never disappear before the worker reaches it.
    """

    def __init__(self, history_max=100):
        self._history_max = history_max
        self._jobs = OrderedDict()
        self._lock = threading.Lock()

    def add(self, job):
        with self._lock:
            self._jobs[job.id] = job
            self._prune()

    def remove(self, job_id):
        with self._lock:
            self._jobs.pop(job_id, None)

    def set_status(self, job_id, status, error=None):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status
            job.error = error
            if status == PRINTING:
                job.started_at = _now()
            elif status in TERMINAL_STATUSES:
                job.finished_at = _now()
            self._prune()

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_public() if job else None

    def get_blocks(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return job.blocks if job else None

    def recent(self, n=100):
        with self._lock:
            jobs = list(self._jobs.values())[-n:]
            return [job.to_public() for job in reversed(jobs)]

    def current_job_id(self):
        with self._lock:
            for job in reversed(self._jobs.values()):
                if job.status == PRINTING:
                    return job.id
            return None

    def _prune(self):
        # Caller holds the lock.
        while len(self._jobs) > self._history_max:
            oldest_terminal = next(
                (jid for jid, job in self._jobs.items() if job.status in TERMINAL_STATUSES),
                None,
            )
            if oldest_terminal is None:
                return
            del self._jobs[oldest_terminal]
