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

    worker_hooks = {}
    if config.rabbitmq_url:
        # Imported lazily so HTTP-only deployments never touch pika.
        from . import rabbitmq

        if not rabbitmq.SOURCE_RE.match(config.printer_name):
            raise SystemExit(
                f"PRINTER_NAME {config.printer_name!r} must match {rabbitmq.SOURCE_RE.pattern}"
            )
        factory = rabbitmq.connection_factory(config.rabbitmq_url)
        publisher = rabbitmq.StatusPublisher(factory)
        monitor = rabbitmq.PrinterStateMonitor(
            printer_manager,
            publisher,
            printer_name=config.printer_name,
            interval=config.rabbitmq_poll_interval,
        )
        consumer = rabbitmq.JobConsumer(
            factory, store, work_queue, publisher, queue_name=config.rabbitmq_job_queue
        )
        publisher.start()
        monitor.start()
        consumer.start()
        worker_hooks = {"on_job_event": publisher.job_event, "after_job": monitor.poke}
        log.info(
            "rabbitmq enabled (queue %s, printer name %s)",
            config.rabbitmq_job_queue,
            config.printer_name,
        )

    PrintWorker(
        work_queue,
        store,
        printer_manager,
        paper_width_dots=config.paper_width_dots,
        **worker_hooks,
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
