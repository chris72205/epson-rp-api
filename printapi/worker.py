import logging
import threading
import time

from escpos.exceptions import DeviceNotFoundError, USBNotFoundError

from .jobs import DONE, FAILED, PRINTING
from .render import render

try:
    from usb.core import USBError
except ImportError:  # pragma: no cover - pyusb is always installed in practice
    class USBError(Exception):
        pass

_STOP = object()
TRANSIENT_ERRORS = (USBError, USBNotFoundError, DeviceNotFoundError)

log = logging.getLogger(__name__)


class PrintWorker(threading.Thread):
    """Pulls job IDs off the queue and prints them strictly sequentially."""

    def __init__(self, work_queue, store, printer_manager, paper_width_dots=512, retry_delay=1.0):
        super().__init__(daemon=True, name="print-worker")
        self.q = work_queue
        self.store = store
        self.pm = printer_manager
        self.paper_width_dots = paper_width_dots
        self.retry_delay = retry_delay

    def stop(self):
        self.q.put(_STOP)

    def run(self):
        while True:
            job_id = self.q.get()
            if job_id is _STOP:
                return
            self.store.set_status(job_id, PRINTING)
            try:
                self._print_with_retry(job_id)
            except Exception as e:
                log.warning("job %s failed: %s", job_id, e)
                self.store.set_status(job_id, FAILED, error=self._describe(e))
            else:
                log.info("job %s done", job_id)
                self.store.set_status(job_id, DONE)
            finally:
                self.q.task_done()

    def _print_with_retry(self, job_id):
        try:
            self._attempt(job_id)
        except TRANSIENT_ERRORS:
            # One retry with a completely fresh USB session covers transient
            # enumeration hiccups after replugs or power cycles.
            log.info("job %s hit a USB error, retrying once", job_id)
            time.sleep(self.retry_delay)
            self._attempt(job_id)

    def _attempt(self, job_id):
        blocks = self.store.get_blocks(job_id)
        if blocks is None:
            raise RuntimeError("job vanished from store")
        with self.pm.session() as printer:
            self.pm.precheck(printer)
            render(printer, blocks, self.paper_width_dots)

    @staticmethod
    def _describe(e):
        if isinstance(e, (USBNotFoundError, DeviceNotFoundError)):
            return "printer not connected"
        return f"{type(e).__name__}: {e}"
