"""Cancellation/terminal-state consistency for batch jobs.

These tests deliberately interleave cancellation requests with the worker's
final publication so that every race is deterministic rather than luck based.
"""
import random
import threading
import time

from backend.errors import StudioError
from backend.jobs import ACTIVE, TERMINAL


STEPS = [{"operation": "color_invert", "params": {}}]


def wait_for(client, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.01)
    raise AssertionError("job never reached a terminal state")


def submit(client, asset_ids):
    response = client.post("/api/jobs", json={"asset_ids": asset_ids, "steps": STEPS})
    assert response.status_code == 202, response.json
    return response.json["data"]["id"]


def second_asset(client):
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), "green").save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, "second.png")})
    assert response.status_code == 201
    return response.json["data"]


class PublishGate:
    """Pause the worker just before terminal publication, holding no locks."""

    def __init__(self, jobs):
        self.jobs = jobs
        self.original = jobs._publish_terminal
        self.worker_entered = threading.Event()
        self.release = threading.Event()
        self._waiting = False

    def install(self, monkeypatch):
        gate = self
        original = self.original

        def gated_publish(job_id, cancellation, changes):
            if not gate._waiting:
                gate._waiting = True
                gate.worker_entered.set()
                gate.release.wait(timeout=10)
            return original(job_id, cancellation, changes)

        monkeypatch.setattr(self.jobs, "_publish_terminal", gated_publish)

    def wait_until_results_persisted(self, client, job_id, count):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json["data"]
            if job["done"] == count:
                break
            time.sleep(0.01)
        assert self.worker_entered.wait(timeout=10)


def test_cancel_between_last_item_and_terminal_publish(app, client, uploaded, monkeypatch):
    jobs = app.extensions["studio"]["jobs"]
    gate = PublishGate(jobs)
    gate.install(monkeypatch)

    job_id = submit(client, [uploaded["id"]])
    gate.wait_until_results_persisted(client, job_id, 1)
    # The last image is already generated; the worker is paused before the
    # terminal write and holds no service lock, so the cancel can land now.
    response = client.post(f"/api/jobs/{job_id}/cancel", json={})
    assert response.status_code == 200
    assert response.json["data"]["status"] == "cancelling"
    gate.release.set()

    job = wait_for(client, job_id)
    assert job["status"] == "cancelled"
    assert job["finished_at"]
    assert job["current_asset_id"] is None
    assert len(job["results"]) == 1
    assert job["done"] == 1
    assert job["progress"] == 100.0
    assert job["errors"] == []
    assert job_id not in jobs.cancellations
    assert job_id not in jobs.futures
    # Repeated cancel on a terminal record is idempotent and cannot reactivate it.
    again = client.post(f"/api/jobs/{job_id}/cancel", json={}).json["data"]
    assert again["status"] == "cancelled"
    assert again["results"] == job["results"]


def test_completion_published_before_cancel_keeps_completed(app, client, uploaded, monkeypatch):
    jobs = app.extensions["studio"]["jobs"]
    gate = PublishGate(jobs)
    gate.install(monkeypatch)

    job_id = submit(client, [uploaded["id"]])
    gate.wait_until_results_persisted(client, job_id, 1)
    gate.release.set()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] == "completed":
            break
        time.sleep(0.01)
    assert job["status"] == "completed"
    assert job_id not in jobs.cancellations
    assert job_id not in jobs.futures

    # Cancellation lands strictly after the terminal publication.
    response = client.post(f"/api/jobs/{job_id}/cancel", json={})
    assert response.status_code == 200
    assert response.json["data"]["status"] == "completed"
    final = client.get(f"/api/jobs/{job_id}").json["data"]
    assert final["status"] == "completed"
    assert len(final["results"]) == 1
    assert final["done"] == 1
    assert final["progress"] == 100.0
    assert final["errors"] == []


def test_cancel_while_queued_ends_cancelled_and_is_idempotent(client, uploaded):
    job_id = submit(client, [uploaded["id"]])
    first = client.post(f"/api/jobs/{job_id}/cancel", json={}).json["data"]
    assert first["status"] in {"cancelling", "cancelled"}
    second = client.post(f"/api/jobs/{job_id}/cancel", json={}).json["data"]
    assert second["status"] in {"cancelling", "cancelled"}

    job = wait_for(client, job_id)
    assert job["status"] == "cancelled"
    assert job["results"] == []
    assert job["done"] == 0
    assert job["finished_at"]
    terminal = client.post(f"/api/jobs/{job_id}/cancel", json={}).json["data"]
    assert terminal["status"] == "cancelled"


def test_cancel_mid_item_keeps_completed_results(app, client, uploaded):
    processing = app.extensions["studio"]["processing"]
    other = second_asset(client)
    original_process = processing.process
    release = threading.Event()
    cancelled_seen = threading.Event()
    calls = {"count": 0}

    def fake_process(asset_id, steps, progress=None, cancelled=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return original_process(asset_id, steps, progress=progress, cancelled=cancelled)
        while not release.is_set():
            if cancelled and cancelled():
                cancelled_seen.set()
                release.set()
            time.sleep(0.005)
        raise StudioError("任务已取消", code="cancelled", status=409)

    processing.process = fake_process
    try:
        job_id = submit(client, [uploaded["id"], other["id"]])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json["data"]
            if job["done"] == 1:
                break
            time.sleep(0.01)
        assert job["done"] == 1
        response = client.post(f"/api/jobs/{job_id}/cancel", json={})
        assert response.status_code == 200
        assert cancelled_seen.wait(timeout=10)
        job = wait_for(client, job_id)
    finally:
        processing.process = original_process
        release.set()

    assert job["status"] == "cancelled"
    assert len(job["results"]) == 1
    assert job["done"] == 1
    assert job["errors"] == []
    assert job["progress"] == 50.0
    assert job["finished_at"]


def test_repeated_cancels_under_race_never_reactivate(app, client, uploaded, monkeypatch):
    store = app.extensions["studio"]["store"]
    original_update = store.update

    def jittered_update(collection, record_id, changes):
        time.sleep(random.uniform(0, 0.004))
        return original_update(collection, record_id, changes)

    monkeypatch.setattr(store, "update", jittered_update)

    job_ids = [submit(client, [uploaded["id"]]) for _ in range(4)]
    threads = []
    for job_id in job_ids:
        time.sleep(random.uniform(0, 0.02))

        def hammer(target=job_id):
            for _ in range(5):
                client.post(f"/api/jobs/{target}/cancel", json={})
                time.sleep(random.uniform(0, 0.01))

        thread = threading.Thread(target=hammer)
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    for job_id in job_ids:
        job = wait_for(client, job_id)
        assert job["status"] in TERMINAL
        assert job["status"] not in ACTIVE
        assert job["finished_at"]
        # done/results/errors always describe what actually finished.
        assert job["done"] == len(job["results"]) + len(job["errors"])
        if job["status"] == "completed":
            assert len(job["results"]) == job["total"]
            assert job["errors"] == []
        else:
            assert job["status"] == "cancelled"
        # One more cancel must not move it out of the terminal state.
        again = client.post(f"/api/jobs/{job_id}/cancel", json={}).json["data"]
        assert again["status"] == job["status"]


def test_active_record_without_worker_flag_ends_safely(app, client, uploaded):
    jobs = app.extensions["studio"]["jobs"]
    orphan = app.extensions["studio"]["store"].insert("jobs", {
        "title": "孤儿任务",
        "asset_ids": [uploaded["id"]],
        "steps": STEPS,
        "status": "running",
        "total": 1,
        "done": 0,
        "progress": 0,
        "results": [],
        "errors": [],
        "current_asset_id": uploaded["id"],
        "started_at": None,
        "finished_at": None,
        "message": "正在处理",
    })
    assert orphan["id"] not in jobs.cancellations
    response = client.post(f"/api/jobs/{orphan['id']}/cancel", json={})
    assert response.status_code == 200
    assert response.json["data"]["status"] == "interrupted"
    assert response.json["data"]["finished_at"]
    again = client.post(f"/api/jobs/{orphan['id']}/cancel", json={}).json["data"]
    assert again["status"] == "interrupted"
