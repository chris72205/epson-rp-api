import json
import queue
import time
from types import SimpleNamespace

import pika.exceptions
import pytest

from printapi.app import create_app
from printapi.config import Config
from printapi.jobs import DONE, FAILED, QUEUED, JobStore
from printapi.printer import PaperOut
from printapi.rabbitmq import (
    JOB_EXCHANGE,
    JOB_ROUTING_KEY,
    SOURCE_RE,
    STATUS_EXCHANGE,
    JobConsumer,
    PrinterStateMonitor,
    StatusPublisher,
)
from printapi.worker import PrintWorker

from .conftest import wait_for


class FakeChannel:
    def __init__(self, deliveries=None):
        self.published = []  # (exchange, routing_key, body, properties)
        self.acked = []
        self.nacked = []  # (delivery_tag, requeue)
        self.rejected = []  # (delivery_tag, requeue)
        self.exchanges = []  # (name, type, durable)
        self.queues = []  # (name, durable)
        self.bindings = []  # (queue, exchange, routing_key)
        self.prefetch = None
        self.cancelled = False
        self.publish_errors = 0  # first N publishes raise
        self._deliveries = list(deliveries or [])

    def exchange_declare(self, exchange, exchange_type=None, durable=False):
        self.exchanges.append((exchange, exchange_type, durable))

    def queue_declare(self, queue_name, durable=False):
        self.queues.append((queue_name, durable))

    def queue_bind(self, queue_name, exchange, routing_key=None):
        self.bindings.append((queue_name, exchange, routing_key))

    def basic_qos(self, prefetch_count=0):
        self.prefetch = prefetch_count

    def basic_publish(self, exchange, routing_key, body, properties=None):
        if self.publish_errors:
            self.publish_errors -= 1
            raise pika.exceptions.AMQPConnectionError("connection died mid-publish")
        self.published.append((exchange, routing_key, body, properties))

    def basic_ack(self, delivery_tag):
        self.acked.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue=False):
        self.nacked.append((delivery_tag, requeue))

    def basic_reject(self, delivery_tag, requeue=False):
        self.rejected.append((delivery_tag, requeue))

    def consume(self, queue_name, inactivity_timeout=None):
        while True:
            if self._deliveries:
                yield self._deliveries.pop(0)
            else:
                time.sleep(0.001)  # keep the fake's idle loop cool
                yield (None, None, None)  # inactivity tick

    def cancel(self):
        self.cancelled = True


class FakeConnection:
    def __init__(self, channel=None):
        self.chan = channel or FakeChannel()
        self.closed = False

    def channel(self):
        return self.chan

    def process_data_events(self, time_limit=0):
        pass

    def close(self):
        self.closed = True


class RecordingPublisher:
    """Stands in for StatusPublisher where only the emit surface matters."""

    def __init__(self):
        self.job_events = []  # (source, event, job_id, error)
        self.printer_events = []  # (printer, event, detail)

    def job_event(self, source, event, job_id, error=None):
        self.job_events.append((source, event, job_id, error))

    def printer_event(self, printer, event, detail=None):
        self.printer_events.append((printer, event, detail))


def job_message(**overrides):
    payload = {"version": 1, "source": "pos", "blocks": [{"type": "feed", "lines": 2}]}
    payload.update(overrides)
    return json.dumps(payload).encode()


def make_consumer(publisher=None, queue_max=50):
    store = JobStore()
    work_queue = queue.Queue(maxsize=queue_max)
    consumer = JobConsumer(
        None, store, work_queue, publisher, requeue_delay=0
    )
    return consumer, store, work_queue


# --- consumer message handling -------------------------------------------


def test_happy_path_acks_on_enqueue():
    consumer, store, work_queue = make_consumer()
    channel = FakeChannel()

    consumer._on_message(channel, 7, job_message())

    job_id = work_queue.get_nowait()
    # Acked while still queued: ack-on-enqueue, not ack-on-print.
    assert channel.acked == [7]
    job = store.get(job_id)
    assert job["status"] == QUEUED
    assert job["source"] == "pos"


def test_client_supplied_job_id_is_used():
    consumer, store, work_queue = make_consumer()
    consumer._on_message(FakeChannel(), 1, job_message(job_id="order-42_A"))
    assert work_queue.get_nowait() == "order-42_A"
    assert store.get("order-42_A") is not None


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b"\xff\xfe",
        json.dumps(["not", "a", "dict"]).encode(),
        json.dumps({"version": 1, "source": "pos", "blocks": [], "extra": 1}).encode(),
        job_message(version=2),
        json.dumps({"source": "pos", "blocks": [{"type": "cut"}]}).encode(),  # no version
        job_message(source="Not.Safe"),
        job_message(source="UPPER"),
        json.dumps({"version": 1, "blocks": [{"type": "cut"}]}).encode(),  # no source
        json.dumps({"version": 1, "source": "pos"}).encode(),  # no blocks
    ],
)
def test_bad_messages_rejected_without_requeue(body):
    consumer, store, work_queue = make_consumer()
    channel = FakeChannel()

    consumer._on_message(channel, 3, body)

    assert channel.rejected == [(3, False)]
    assert not channel.acked and not channel.nacked
    assert work_queue.empty()
    assert store.recent() == []


def test_bad_job_id_rejects_and_reports():
    publisher = RecordingPublisher()
    consumer, _, _ = make_consumer(publisher)
    channel = FakeChannel()

    consumer._on_message(channel, 4, job_message(job_id="has.dots"))

    assert channel.rejected == [(4, False)]
    (source, event, job_id, error) = publisher.job_events[0]
    assert (source, event, job_id) == ("pos", "failed", None)
    assert "job_id" in error


def test_invalid_blocks_reject_and_report():
    publisher = RecordingPublisher()
    consumer, store, work_queue = make_consumer(publisher)
    channel = FakeChannel()

    consumer._on_message(
        channel, 5, job_message(job_id="j1", blocks=[{"type": "feed", "lines": 999}])
    )

    assert channel.rejected == [(5, False)]
    assert work_queue.empty() and store.recent() == []
    (source, event, job_id, error) = publisher.job_events[0]
    assert (source, event, job_id) == ("pos", "failed", "j1")
    assert "blocks[0].lines" in error


def test_no_failed_event_without_trustworthy_source():
    publisher = RecordingPublisher()
    consumer, _, _ = make_consumer(publisher)
    consumer._on_message(FakeChannel(), 6, job_message(source="bad source!"))
    assert publisher.job_events == []


def test_duplicate_job_id_acked_and_dropped():
    consumer, store, work_queue = make_consumer()
    channel = FakeChannel()

    consumer._on_message(channel, 1, job_message(job_id="dup"))
    consumer._on_message(channel, 2, job_message(job_id="dup"))

    assert channel.acked == [1, 2]
    assert work_queue.qsize() == 1
    assert len(store.recent()) == 1


def test_local_queue_full_nacks_with_requeue():
    consumer, store, work_queue = make_consumer(queue_max=1)
    channel = FakeChannel()

    consumer._on_message(channel, 1, job_message())
    consumer._on_message(channel, 2, job_message())

    assert channel.acked == [1]
    assert channel.nacked == [(2, True)]
    # The rolled-back job must not linger in the store.
    assert len(store.recent()) == 1


def test_consumer_run_loop_declares_consumes_and_stops():
    delivery = (SimpleNamespace(delivery_tag=9), None, job_message())
    channel = FakeChannel(deliveries=[delivery])
    connection = FakeConnection(channel)
    store = JobStore()
    work_queue = queue.Queue(maxsize=10)
    consumer = JobConsumer(lambda: connection, store, work_queue, queue_name="q1")

    consumer.start()
    assert wait_for(lambda: channel.acked == [9])
    consumer.stop()
    consumer.join(timeout=2)

    assert not consumer.is_alive()
    assert (JOB_EXCHANGE, "direct", True) in channel.exchanges
    assert ("q1", True) in channel.queues
    assert ("q1", JOB_EXCHANGE, JOB_ROUTING_KEY) in channel.bindings
    assert channel.prefetch == 8
    assert channel.cancelled and connection.closed
    assert work_queue.qsize() == 1


# --- status publisher ------------------------------------------------------


def drain_publisher(publisher, channel=None):
    """Run the publish loop until the stop sentinel is consumed."""
    channel = channel or FakeChannel()
    publisher.stop()
    publisher._loop(FakeConnection(channel), channel)
    return channel


def test_job_event_formatting():
    publisher = StatusPublisher(None)
    publisher.job_event("pos", "failed", "j1", error="out of paper")
    channel = drain_publisher(publisher)

    exchange, routing_key, body, props = channel.published[0]
    assert exchange == STATUS_EXCHANGE
    assert routing_key == "job.pos.failed"
    payload = json.loads(body)
    assert payload["version"] == 1
    assert payload["event"] == "failed"
    assert payload["job_id"] == "j1"
    assert payload["source"] == "pos"
    assert payload["error"] == "out of paper"
    assert payload["ts"]
    assert props.delivery_mode == 2
    assert props.content_type == "application/json"


def test_printer_event_formatting():
    publisher = StatusPublisher(None)
    publisher.printer_event("receipt", "out_of_paper")
    channel = drain_publisher(publisher)

    _, routing_key, body, props = channel.published[0]
    assert routing_key == "printer.receipt.out_of_paper"
    payload = json.loads(body)
    assert payload["event"] == "out_of_paper"
    assert payload["printer"] == "receipt"
    assert "detail" not in payload
    assert props.delivery_mode == 1


def test_pending_overflow_drops_instead_of_blocking():
    publisher = StatusPublisher(None, pending_max=1)
    publisher.job_event("pos", "success", "j1")
    publisher.job_event("pos", "success", "j2")  # dropped, no exception

    channel = drain_publisher(publisher)
    assert len(channel.published) == 1


def test_publisher_reconnects_and_delivers():
    channels = []
    attempts = {"n": 0}

    def factory():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise pika.exceptions.AMQPConnectionError("refused")
        connection = FakeConnection()
        channels.append(connection.chan)
        return connection

    publisher = StatusPublisher(factory)
    publisher.backoff_start = 0.01
    publisher.start()
    publisher.job_event("pos", "success", "j1")

    assert wait_for(lambda: channels and channels[-1].published)
    assert (STATUS_EXCHANGE, "topic", True) in channels[-1].exchanges
    publisher.stop()
    publisher.join(timeout=2)
    assert not publisher.is_alive()


def test_publisher_republishes_event_lost_mid_flight():
    first = FakeChannel()
    first.publish_errors = 1
    second = FakeChannel()
    connections = [FakeConnection(first), FakeConnection(second)]

    publisher = StatusPublisher(lambda: connections.pop(0))
    publisher.backoff_start = 0.01
    publisher.start()
    publisher.job_event("pos", "success", "j1")

    assert wait_for(lambda: second.published)
    assert first.published == []
    assert json.loads(second.published[0][2])["job_id"] == "j1"
    publisher.stop()
    publisher.join(timeout=2)


# --- printer state monitor ---------------------------------------------------


OK = {"connected": True, "busy": False, "online": True, "paper": "ok"}


def make_monitor(statuses):
    pm = SimpleNamespace(status=lambda: statuses.pop(0))
    publisher = RecordingPublisher()
    monitor = PrinterStateMonitor(pm, publisher, printer_name="receipt")
    return monitor, publisher


def events(publisher):
    return [event for _, event, _ in publisher.printer_events]


def test_first_sample_publishes_baseline_then_only_edges():
    monitor, publisher = make_monitor([OK, OK, dict(OK, paper="out")])
    monitor._check()
    assert events(publisher) == ["online", "paper_ok"]
    monitor._check()  # identical: no new events
    assert events(publisher) == ["online", "paper_ok"]
    monitor._check()
    assert events(publisher) == ["online", "paper_ok", "out_of_paper"]


def test_busy_sample_is_skipped_and_preserves_state():
    monitor, publisher = make_monitor([OK, {"connected": True, "busy": True}, OK])
    monitor._check()
    monitor._check()  # busy: no events, no state change
    monitor._check()  # unchanged state: still no events
    assert events(publisher) == ["online", "paper_ok"]


def test_disconnect_and_reconnect_cycle():
    monitor, publisher = make_monitor(
        [OK, {"connected": False, "error": "USBNotFoundError"}, OK]
    )
    monitor._check()
    monitor._check()
    assert publisher.printer_events[-1] == ("receipt", "offline", "USBNotFoundError")
    monitor._check()
    # Back online; paper unchanged across the outage, so no paper event.
    assert events(publisher) == ["online", "paper_ok", "offline", "online"]


def test_paper_low_and_recovery():
    monitor, publisher = make_monitor(
        [OK, dict(OK, paper="near_end"), dict(OK, paper="out"), OK]
    )
    for _ in range(4):
        monitor._check()
    assert events(publisher) == ["online", "paper_ok", "paper_low", "out_of_paper", "paper_ok"]


def test_offline_via_cover_open():
    monitor, publisher = make_monitor([OK, dict(OK, online=False)])
    monitor._check()
    monitor._check()
    assert events(publisher)[-1] == "offline"


# --- worker event emission ---------------------------------------------------


@pytest.fixture
def event_harness(fake_pm):
    recorder = RecordingPublisher()
    pokes = []
    config = Config(fake=True, queue_max=50, history_max=100)
    store = JobStore(history_max=config.history_max)
    work_queue = queue.Queue(maxsize=config.queue_max)
    worker = PrintWorker(
        work_queue,
        store,
        fake_pm,
        retry_delay=0.01,
        on_job_event=recorder.job_event,
        after_job=lambda: pokes.append(1),
    )
    worker.start()
    app = create_app(config, store, work_queue, fake_pm)
    yield app.test_client(), store, recorder, pokes, fake_pm
    worker.stop()
    worker.join(timeout=2)


def test_worker_emits_success_event(event_harness):
    client, store, recorder, pokes, _ = event_harness
    job_id = client.post("/print/text", json={"text": "hi"}).get_json()["id"]
    assert wait_for(lambda: store.get(job_id)["status"] == DONE)
    assert wait_for(lambda: recorder.job_events == [("text", "success", job_id, None)])
    assert wait_for(lambda: len(pokes) == 1)


def test_worker_emits_failed_event(event_harness):
    client, store, recorder, pokes, fake_pm = event_harness
    fake_pm.precheck_error = PaperOut("out of paper")
    job_id = client.post("/print/text", json={"text": "hi"}).get_json()["id"]
    assert wait_for(lambda: store.get(job_id)["status"] == FAILED)
    assert wait_for(
        lambda: recorder.job_events == [("text", "failed", job_id, "PaperOut: out of paper")]
    )
    assert wait_for(lambda: len(pokes) == 1)


def test_raising_hooks_do_not_break_printing(fake_pm):
    def explode(*args, **kwargs):
        raise RuntimeError("hook bug")

    store = JobStore()
    work_queue = queue.Queue(maxsize=10)
    worker = PrintWorker(
        work_queue, store, fake_pm, retry_delay=0.01, on_job_event=explode, after_job=explode
    )
    worker.start()
    try:
        from printapi.jobs import Job, new_job_id

        job = Job(id=new_job_id(), blocks=[{"type": "feed", "lines": 1}], source="print")
        store.add(job)
        work_queue.put(job.id)
        assert wait_for(lambda: store.get(job.id)["status"] == DONE)
    finally:
        worker.stop()
        worker.join(timeout=2)


# --- routing key safety ------------------------------------------------------


@pytest.mark.parametrize("source", ["pos", "kiosk-1", "back_office", "a", "0" * 32])
def test_source_re_accepts(source):
    assert SOURCE_RE.match(source)


@pytest.mark.parametrize(
    "source", ["", "a.b", "job.*", "#", "POS", "-leading", "a" * 33, "café", "a b"]
)
def test_source_re_rejects(source):
    assert not SOURCE_RE.match(source)
