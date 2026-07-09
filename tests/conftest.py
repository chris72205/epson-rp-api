import queue
import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from printapi.app import create_app
from printapi.config import Config
from printapi.jobs import JobStore
from printapi.worker import PrintWorker


class FakePrinterManager:
    """Same two-method surface as PrinterManager, no USB."""

    def __init__(self):
        self.printer = MagicMock()
        self.session_error = None  # raised when opening a session
        self.precheck_error = None  # raised by precheck
        self._lock = threading.Lock()

    @contextmanager
    def session(self):
        with self._lock:
            if self.session_error is not None:
                raise self.session_error
            yield self.printer

    def precheck(self, printer):
        if self.precheck_error is not None:
            raise self.precheck_error

    def status(self):
        return {"connected": True, "busy": False, "online": True, "paper": "ok"}


@pytest.fixture
def fake_pm():
    return FakePrinterManager()


@pytest.fixture
def harness(fake_pm):
    """App + real store + real worker wired to the fake printer manager."""
    config = Config(fake=True, queue_max=50, history_max=100)
    store = JobStore(history_max=config.history_max)
    work_queue = queue.Queue(maxsize=config.queue_max)
    worker = PrintWorker(work_queue, store, fake_pm, retry_delay=0.01)
    worker.start()
    app = create_app(config, store, work_queue, fake_pm)
    yield app.test_client(), store, work_queue, fake_pm
    worker.stop()
    worker.join(timeout=2)


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
