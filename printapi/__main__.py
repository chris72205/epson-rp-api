import logging
import queue

import waitress

from .app import create_app
from .config import Config
from .jobs import JobStore
from .printer import PrinterManager
from .worker import PrintWorker


def main():
    config = Config.from_env()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("printapi")

    store = JobStore(history_max=config.history_max)
    work_queue = queue.Queue(maxsize=config.queue_max)
    printer_manager = PrinterManager(config)
    PrintWorker(
        work_queue, store, printer_manager, paper_width_dots=config.paper_width_dots
    ).start()

    app = create_app(config, store, work_queue, printer_manager)
    log.info(
        "serving on %s:%s (printer %04x:%04x%s)",
        config.host,
        config.port,
        config.usb_vendor,
        config.usb_product,
        ", FAKE mode" if config.fake else "",
    )
    waitress.serve(app, host=config.host, port=config.port, threads=4)


if __name__ == "__main__":
    main()
