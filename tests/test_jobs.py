import threading

from printapi.jobs import DONE, FAILED, PRINTING, QUEUED, Job, JobStore, new_job_id


def make_job(status=QUEUED):
    job = Job(id=new_job_id(), blocks=[{"type": "feed", "lines": 1}], source="print")
    job.status = status
    return job


def test_lifecycle_timestamps():
    store = JobStore()
    job = make_job()
    store.add(job)

    public = store.get(job.id)
    assert public["status"] == QUEUED
    assert public["created_at"] and not public["started_at"]

    store.set_status(job.id, PRINTING)
    assert store.get(job.id)["started_at"]

    store.set_status(job.id, DONE)
    public = store.get(job.id)
    assert public["status"] == DONE and public["finished_at"]
    assert public["error"] is None


def test_failed_keeps_error_message():
    store = JobStore()
    job = make_job()
    store.add(job)
    store.set_status(job.id, FAILED, error="out of paper")
    assert store.get(job.id)["error"] == "out of paper"


def test_public_dict_hides_blocks():
    store = JobStore()
    job = make_job()
    store.add(job)
    public = store.get(job.id)
    assert "blocks" not in public
    assert public["block_count"] == 1


def test_recent_newest_first():
    store = JobStore()
    jobs = [make_job() for _ in range(5)]
    for job in jobs:
        store.add(job)
    assert [j["id"] for j in store.recent(3)] == [jobs[4].id, jobs[3].id, jobs[2].id]


def test_prune_evicts_only_terminal_jobs():
    store = JobStore(history_max=10)
    terminal = [make_job(DONE) for _ in range(10)]
    for job in terminal:
        store.add(job)
    queued = [make_job() for _ in range(5)]
    for job in queued:
        store.add(job)

    # All queued jobs survive; the oldest terminal jobs were evicted.
    assert all(store.get(job.id) for job in queued)
    assert store.get(terminal[0].id) is None
    assert len(store.recent(100)) == 10


def test_prune_never_drops_below_nonterminal_count():
    store = JobStore(history_max=3)
    queued = [make_job() for _ in range(5)]
    for job in queued:
        store.add(job)
    assert all(store.get(job.id) for job in queued)


def test_current_job_id():
    store = JobStore()
    a, b = make_job(), make_job()
    store.add(a)
    store.add(b)
    assert store.current_job_id() is None
    store.set_status(a.id, PRINTING)
    assert store.current_job_id() == a.id


def test_concurrent_add_and_read():
    store = JobStore(history_max=50)
    errors = []

    def hammer():
        try:
            for _ in range(200):
                job = make_job(DONE)
                store.add(job)
                store.get(job.id)
                store.recent(10)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.recent(100)) == 50
