import queue

from escpos.exceptions import USBNotFoundError

from printapi.app import create_app
from printapi.config import Config
from printapi.jobs import JobStore
from printapi.printer import PaperOut

from .conftest import wait_for


def post_print(client, *blocks):
    return client.post("/print", json={"blocks": list(blocks)})


def test_print_roundtrip(harness):
    client, store, _, fake_pm = harness
    resp = post_print(client, {"type": "text", "content": "hello"}, {"type": "cut"})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "queued"
    assert body["status_url"] == f"/jobs/{body['id']}"

    assert wait_for(lambda: store.get(body["id"])["status"] == "done")
    fake_pm.printer.textln.assert_called_once_with("hello")
    fake_pm.printer.cut.assert_called_once()

    job = client.get(body["status_url"]).get_json()
    assert job["status"] == "done"
    assert job["started_at"] and job["finished_at"]


def test_print_text_convenience(harness):
    client, store, _, fake_pm = harness
    resp = client.post("/print/text", json={"text": "quick", "cut": True})
    assert resp.status_code == 202
    job_id = resp.get_json()["id"]
    assert wait_for(lambda: store.get(job_id)["status"] == "done")
    fake_pm.printer.textln.assert_called_once_with("quick")


def test_print_test_page(harness):
    client, store, _, fake_pm = harness
    resp = client.post("/print/test")
    assert resp.status_code == 202
    job_id = resp.get_json()["id"]
    assert wait_for(lambda: store.get(job_id)["status"] == "done")
    assert fake_pm.printer.qr.called
    assert fake_pm.printer.barcode.called
    assert fake_pm.printer.image.called


def test_validation_error_is_400(harness):
    client, _, _, _ = harness
    resp = post_print(client, {"type": "text"})
    assert resp.status_code == 400
    assert "content" in resp.get_json()["error"]


def test_malformed_json_is_400(harness):
    client, _, _, _ = harness
    resp = client.post("/print", data="not json", content_type="application/json")
    assert resp.status_code == 400


def test_unknown_job_is_404(harness):
    client, _, _, _ = harness
    assert client.get("/jobs/deadbeef1234").status_code == 404


def test_printer_not_connected_fails_job(harness):
    client, store, _, fake_pm = harness
    fake_pm.session_error = USBNotFoundError("no device")
    resp = post_print(client, {"type": "text", "content": "x"})
    job_id = resp.get_json()["id"]
    assert wait_for(lambda: store.get(job_id)["status"] == "failed")
    assert store.get(job_id)["error"] == "printer not connected"


def test_out_of_paper_fails_job(harness):
    client, store, _, fake_pm = harness
    fake_pm.precheck_error = PaperOut("out of paper")
    resp = post_print(client, {"type": "text", "content": "x"})
    job_id = resp.get_json()["id"]
    assert wait_for(lambda: store.get(job_id)["status"] == "failed")
    assert "out of paper" in store.get(job_id)["error"]


def test_transient_usb_error_retries_and_succeeds(harness):
    client, store, _, fake_pm = harness
    fake_pm.printer.textln.side_effect = [USBNotFoundError("hiccup"), None]
    resp = post_print(client, {"type": "text", "content": "x"})
    job_id = resp.get_json()["id"]
    assert wait_for(lambda: store.get(job_id)["status"] == "done")
    assert fake_pm.printer.textln.call_count == 2


def test_queue_full_returns_503(fake_pm):
    # No worker draining the queue, so it fills up.
    config = Config(fake=True)
    store = JobStore()
    work_queue = queue.Queue(maxsize=2)
    client = create_app(config, store, work_queue, fake_pm).test_client()

    assert post_print(client, {"type": "feed"}).status_code == 202
    assert post_print(client, {"type": "feed"}).status_code == 202
    resp = post_print(client, {"type": "feed"})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "queue full"
    assert len(store.recent(10)) == 2  # rejected job removed from store


def test_jobs_listing(harness):
    client, store, _, _ = harness
    ids = [post_print(client, {"type": "feed"}).get_json()["id"] for _ in range(3)]
    assert wait_for(lambda: all(store.get(i)["status"] == "done" for i in ids))
    listed = client.get("/jobs").get_json()["jobs"]
    assert [j["id"] for j in listed[:3]] == list(reversed(ids))


def test_status_endpoint(harness):
    client, _, _, _ = harness
    body = client.get("/status").get_json()
    assert body["printer"]["connected"] is True
    assert body["queue"]["depth"] == 0
    assert body["queue"]["current_job"] is None


def test_health(harness):
    client, _, _, _ = harness
    for path in ("/", "/health"):
        body = client.get(path).get_json()
        assert body["ok"] is True
        assert body["version"]
