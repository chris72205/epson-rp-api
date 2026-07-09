"""Single owner of the USB printer device.

Every USB access goes through PrinterManager: the worker holds the lock for
a whole job via session(); GET /status grabs it with a short timeout so
status queries never wedge behind a long print job. The connection is opened
fresh per use — enumeration is fast and a fresh handle self-heals after
cable pulls or printer power cycles.
"""

import threading
from contextlib import contextmanager

from escpos.exceptions import Error as EscposError


class PrinterError(Exception):
    pass


class PrinterOffline(PrinterError):
    pass


class PaperOut(PrinterError):
    pass


PAPER_STATES = {0: "out", 1: "near_end", 2: "ok"}


class PrinterManager:
    def __init__(self, config):
        self._cfg = config
        self._lock = threading.Lock()

    def _connect(self):
        if self._cfg.fake:
            from escpos.printer import Dummy

            return Dummy(profile=self._cfg.profile)
        from escpos.printer import Usb

        return Usb(
            self._cfg.usb_vendor,
            self._cfg.usb_product,
            timeout=self._cfg.usb_timeout_ms,
            profile=self._cfg.profile,
        )

    @contextmanager
    def session(self):
        with self._lock:
            printer = self._connect()
            try:
                yield printer
            finally:
                try:
                    printer.close()
                except Exception:
                    pass

    def precheck(self, printer):
        """Raise if the printer can't take a job right now. Runs inside a session."""
        if self._cfg.fake:
            return
        if not printer.is_online():
            raise PrinterOffline("printer offline (cover open or error state)")
        if printer.paper_status() == 0:
            raise PaperOut("out of paper")

    def status(self):
        if self._cfg.fake:
            return {"connected": True, "busy": False, "online": True, "paper": "ok", "fake": True}
        if not self._lock.acquire(timeout=2.0):
            return {"connected": True, "busy": True}
        try:
            printer = self._connect()
            try:
                online = printer.is_online()
                paper = printer.paper_status()
            finally:
                try:
                    printer.close()
                except Exception:
                    pass
            return {
                "connected": True,
                "busy": False,
                "online": online,
                "paper": PAPER_STATES.get(paper, "unknown"),
            }
        except EscposError as e:
            return {"connected": False, "error": str(e)}
        except Exception as e:
            return {"connected": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            self._lock.release()
