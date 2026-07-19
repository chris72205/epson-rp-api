"""RabbitMQ integration: job intake and status events (docs/rabbitmq.md).

Jobs arrive on the durable direct exchange `print-jobs` and are acked as
soon as they are validated and enqueued locally -- the AMQP twin of the
HTTP 202. Status events go out on the durable topic exchange
`print-status` under two routing-key families: `job.<source>.<event>`
(success/failed) and `printer.<printer>.<event>` (paper/availability).

pika connections are NOT thread-safe: each thread below creates its own
BlockingConnection inside run() and is the only thread that ever touches
it. Cross-thread communication is queue.Queue/threading.Event only --
do not "fix" this by sharing a connection.
"""

import json
import logging
import queue
import re
import threading
import time
from datetime import datetime, timezone

import pika
import pika.exceptions

from .jobs import Job, new_job_id
from .validation import ValidationError, validate_document

# Exchange names and the job routing key are the published contract
# (docs/rabbitmq.md), so they are constants rather than config.
JOB_EXCHANGE = "print-jobs"  # direct
STATUS_EXCHANGE = "print-status"  # topic
JOB_ROUTING_KEY = "print"

# Both land inside routing keys, so '.', '*', '#' and whitespace are
# structurally unsafe. Sources are lowercase-only: AMQP keys are
# case-sensitive, and `POS` vs `pos` silently becoming different
# bindings is a footgun.
SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_STOP = object()

log = logging.getLogger(__name__)


def connection_factory(url):
    return lambda: pika.BlockingConnection(pika.URLParameters(url))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _BrokerThread(threading.Thread):
    """Owns one BlockingConnection; reconnects forever with backoff."""

    def __init__(self, conn_factory, name):
        super().__init__(daemon=True, name=name)
        self._connect = conn_factory
        self._stopping = threading.Event()
        self.backoff_start = 1.0
        self.backoff_max = 30.0

    def stop(self):
        self._stopping.set()

    def run(self):
        backoff = self.backoff_start
        while not self._stopping.is_set():
            connection = None
            connected_at = None
            try:
                connection = self._connect()
                channel = connection.channel()
                self._declare(channel)
                connected_at = time.monotonic()
                self._loop(connection, channel)
                return  # clean stop
            except pika.exceptions.AMQPChannelError as e:
                # Usually a declare mismatch with pre-existing topology
                # (e.g. a non-durable exchange of the same name). Keep
                # retrying so an operator can fix the broker live.
                log.error("%s: channel error: %s", self.name, e)
            except pika.exceptions.AMQPConnectionError as e:
                log.warning("%s: connection failed/lost: %s", self.name, e)
            except Exception:
                log.exception("%s: unexpected error", self.name)
            finally:
                # is_open guard: close() on a dead connection makes pika
                # log a spurious ERROR on every broker blip.
                if connection is not None and getattr(connection, "is_open", True):
                    try:
                        connection.close()
                    except Exception:
                        pass
            if connected_at is not None and time.monotonic() - connected_at > 30:
                backoff = self.backoff_start  # healthy session: reset
            self._stopping.wait(backoff)
            backoff = min(backoff * 2, self.backoff_max)

    def _declare(self, channel):
        raise NotImplementedError

    def _loop(self, connection, channel):
        raise NotImplementedError


class StatusPublisher(_BrokerThread):
    """Publishes status events; the emit methods are safe from any thread.

    job_event()/printer_event() only enqueue onto a bounded local queue --
    the print path must never block on (or crash from) the broker. Events
    are dropped with a warning when the queue is full. The in-flight
    hold-back re-publishes an event whose basic_publish died mid-flight,
    so delivery is at-least-once: consumers can rarely see a duplicate.
    """

    def __init__(self, conn_factory, pending_max=1000):
        super().__init__(conn_factory, name="rmq-publisher")
        self._pending = queue.Queue(maxsize=pending_max)
        self._inflight = None

    def job_event(self, source, event, job_id, error=None):
        body = {"version": 1, "event": event, "job_id": job_id, "source": source, "ts": _now()}
        if error is not None:
            body["error"] = error
        # Job events are correlation-critical: persistent so they survive
        # a broker restart in durable consumer queues.
        self._enqueue(f"job.{source}.{event}", body, persistent=True)

    def printer_event(self, printer, event, detail=None):
        body = {"version": 1, "event": event, "printer": printer, "ts": _now()}
        if detail is not None:
            body["detail"] = detail
        # Superseded by the next poll, so transient is enough.
        self._enqueue(f"printer.{printer}.{event}", body, persistent=False)

    def stop(self):
        self._stopping.set()
        try:
            self._pending.put_nowait(_STOP)
        except queue.Full:
            pass

    def _enqueue(self, routing_key, body, persistent):
        try:
            self._pending.put_nowait((routing_key, body, persistent))
        except queue.Full:
            log.warning("status event queue full, dropping %s", routing_key)

    def _declare(self, channel):
        channel.exchange_declare(STATUS_EXCHANGE, exchange_type="topic", durable=True)

    def _loop(self, connection, channel):
        while True:
            item = self._inflight
            if item is None:
                try:
                    item = self._pending.get(timeout=1.0)
                except queue.Empty:
                    # Idle tick: keep heartbeats serviced.
                    connection.process_data_events(time_limit=0)
                    if self._stopping.is_set():
                        return
                    continue
            if item is _STOP:
                return
            self._inflight = item
            routing_key, body, persistent = item
            channel.basic_publish(
                STATUS_EXCHANGE,
                routing_key,
                json.dumps(body),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2 if persistent else 1,
                ),
            )
            self._inflight = None


class _Reject(Exception):
    """Message cannot be processed; carries what's known for a failed event."""

    def __init__(self, message, source=None, job_id=None):
        super().__init__(message)
        self.source = source
        self.job_id = job_id


class JobConsumer(_BrokerThread):
    """Consumes job messages into the JobStore + work queue.

    Acks on local enqueue (the HTTP 202 equivalent): an acked-but-unprinted
    job is lost if the service restarts, matching the documented in-memory
    queue contract. Malformed messages are rejected without requeue.
    """

    def __init__(
        self,
        conn_factory,
        store,
        work_queue,
        publisher=None,
        queue_name="print-jobs",
        prefetch=8,
        requeue_delay=1.0,
    ):
        super().__init__(conn_factory, name="rmq-consumer")
        self.store = store
        self.work_queue = work_queue
        self.publisher = publisher
        self.queue_name = queue_name
        self.prefetch = prefetch
        self.requeue_delay = requeue_delay

    def _declare(self, channel):
        channel.exchange_declare(JOB_EXCHANGE, exchange_type="direct", durable=True)
        channel.queue_declare(self.queue_name, durable=True)
        channel.queue_bind(self.queue_name, JOB_EXCHANGE, routing_key=JOB_ROUTING_KEY)
        channel.basic_qos(prefetch_count=self.prefetch)

    def _loop(self, connection, channel):
        # Generator form rather than start_consuming() so stop() takes
        # effect within a second; pika services heartbeats during the
        # inactivity timeout, so a quiet queue never times out.
        for method, _properties, body in channel.consume(
            self.queue_name, inactivity_timeout=1.0
        ):
            if self._stopping.is_set():
                break
            if method is None:  # inactivity tick
                continue
            self._on_message(channel, method.delivery_tag, body)
        channel.cancel()

    def _on_message(self, channel, delivery_tag, body):
        try:
            job = self._parse(body)
        except _Reject as e:
            log.warning("rejecting job message: %s", e)
            if e.source is not None and self.publisher is not None:
                self.publisher.job_event(e.source, "failed", e.job_id, error=str(e))
            channel.basic_reject(delivery_tag, requeue=False)
            return
        if self.store.get(job.id) is not None:
            # Duplicate delivery (publisher retry, or redelivery after a
            # lost ack). The original job emits its own terminal event;
            # a second one under the same job_id would corrupt correlation.
            log.info("duplicate job_id %s, dropping", job.id)
            channel.basic_ack(delivery_tag)
            return
        self.store.add(job)
        try:
            self.work_queue.put_nowait(job.id)
        except queue.Full:
            self.store.remove(job.id)
            # Let RabbitMQ hold the backlog; the wait paces the
            # redeliver-and-retry loop under prefetch.
            self._stopping.wait(self.requeue_delay)
            channel.basic_nack(delivery_tag, requeue=True)
            return
        channel.basic_ack(delivery_tag)
        log.info("job %s accepted from rabbitmq (source %s)", job.id, job.source)

    @staticmethod
    def _parse(body):
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            raise _Reject("body is not valid JSON") from None
        if not isinstance(payload, dict):
            raise _Reject("body must be a JSON object")
        unknown = set(payload) - {"version", "source", "job_id", "blocks"}
        if unknown:
            raise _Reject(f"unknown field(s): {', '.join(sorted(unknown))}")
        if payload.get("version") != 1:
            raise _Reject("version is required and must be 1")
        source = payload.get("source")
        if not isinstance(source, str) or not SOURCE_RE.match(source):
            raise _Reject(f"source must match {SOURCE_RE.pattern}")
        job_id = payload.get("job_id")
        if job_id is None:
            job_id = new_job_id()
        elif not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            raise _Reject(f"job_id must match {JOB_ID_RE.pattern}", source=source)
        if "blocks" not in payload:
            raise _Reject("missing required field 'blocks'", source=source, job_id=job_id)
        try:
            # Envelope fields would trip validate_document's unknown-field
            # check, so pass blocks alone.
            blocks = validate_document({"blocks": payload["blocks"]})
        except ValidationError as e:
            raise _Reject(f"invalid blocks: {e}", source=source, job_id=job_id) from None
        return Job(id=job_id, blocks=blocks, source=source)


class PrinterStateMonitor(threading.Thread):
    """Publishes edge-triggered printer state events.

    All sampling, previous-state tracking, and publication happen on this
    one thread, so the worker's post-job poke() and the periodic poll
    cannot race -- no lock needed. A busy or errored status sample is
    skipped without updating state: unknown is not a transition.
    """

    _PAPER_EVENTS = {"ok": "paper_ok", "near_end": "paper_low", "out": "out_of_paper"}

    def __init__(self, printer_manager, publisher, printer_name="receipt", interval=30.0):
        super().__init__(daemon=True, name="printer-monitor")
        self.pm = printer_manager
        self.publisher = publisher
        self.printer_name = printer_name
        self.interval = interval
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._prev = None  # (availability event, paper event)

    def poke(self):
        """Request an immediate check; called by the worker after each job."""
        self._wake.set()

    def stop(self):
        self._stopping.set()
        self._wake.set()

    def run(self):
        while not self._stopping.is_set():
            try:
                self._check()
            except Exception:
                log.exception("printer state check failed")
            self._wake.wait(timeout=self.interval)
            self._wake.clear()

    def _check(self):
        status = self.pm.status()
        if status.get("busy"):
            return  # a job is printing; state query was skipped
        if not status.get("connected"):
            availability, paper = "offline", None
        elif "online" not in status or "paper" not in status:
            return
        else:
            availability = "online" if status["online"] else "offline"
            paper = self._PAPER_EVENTS.get(status["paper"])  # None for "unknown"
        prev_availability, prev_paper = self._prev or (None, None)
        # The first successful sample publishes the current state once as
        # a startup baseline; after that, only transitions.
        if availability != prev_availability:
            self.publisher.printer_event(
                self.printer_name, availability, detail=status.get("error")
            )
        if paper is not None and paper != prev_paper:
            self.publisher.printer_event(self.printer_name, paper)
        # Keep the last known paper state while it is unreadable
        # (disconnected), so reconnecting to unchanged paper is not an edge.
        self._prev = (availability, paper if paper is not None else prev_paper)
