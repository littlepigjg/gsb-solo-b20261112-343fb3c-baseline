import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from backend.storage import JsonStore
from backend.errors import StorageFailure
from backend.app import create_app


def test_atomic_concurrent_writes(tmp_path):
    store = JsonStore(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: store.insert("events", {"index": index}), range(40)))
    records = store.all("events")
    assert len(records) == 40
    assert {item["index"] for item in records} == set(range(40))
    assert not list(tmp_path.glob("*.tmp"))


def test_recovers_last_valid_backup(tmp_path):
    store = JsonStore(tmp_path)
    first = store.insert("events", {"message": "first"})
    store.insert("events", {"message": "second"})
    (tmp_path / "events.json").write_text("broken json", encoding="utf-8")
    recovered = JsonStore(tmp_path)
    assert recovered.get("events", first["id"])["message"] == "first"
    assert len(recovered.all("events")) == 1


def test_restart_marks_job_interrupted(tmp_path):
    root = tmp_path / "data"
    app = create_app(root, testing=True)
    store = app.extensions["studio"]["store"]
    job = store.insert("jobs", {"status": "running", "asset_ids": [], "results": []})
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(root, testing=True)
    try:
        record = second.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["status"] == "interrupted"
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_cancel_waiting_job(app, client, uploaded):
    processor = app.extensions["studio"]["processing"]
    processor.compute_lock.acquire()
    try:
        response = client.post("/api/jobs", json={"asset_ids": [uploaded["id"]], "steps": [{"operation": "color_invert", "params": {}}]})
        job_id = response.json["data"]["id"]
        cancelled = client.post(f"/api/jobs/{job_id}/cancel", json={})
        assert cancelled.status_code == 200
    finally:
        processor.compute_lock.release()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] == "cancelled":
            break
        time.sleep(0.02)
    assert job["status"] == "cancelled"
    assert job["results"] == []


def test_source_count_over_5000():
    from scripts.count_backend import count_file, ROOT
    records = [count_file(path) for path in (ROOT / "backend").rglob("*.py")]
    assert sum(item["code_lines_excluding_docstrings"] for item in records) > 5000


def _force_commit_failure(store, collection, monkeypatch):
    """Make the atomic replace fail for one collection."""
    original_replace = __import__("os").replace

    def failing_replace(source, target):
        if str(target).endswith(f"{collection}.json"):
            raise OSError("simulated atomic replace failure")
        return original_replace(source, target)

    monkeypatch.setattr("backend.storage.os.replace", failing_replace)


def test_failed_insert_keeps_last_committed_state(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    first = store.insert("presets", {"name": "first", "steps": []})
    _force_commit_failure(store, "presets", monkeypatch)
    with pytest.raises(StorageFailure):
        store.insert("presets", {"name": "rejected", "steps": []})
    monkeypatch.undo()
    names = [item["name"] for item in store.all("presets")]
    assert names == ["first"]
    assert store.get("presets", first["id"])["name"] == "first"
    assert not list(tmp_path.glob("*.tmp"))
    # A later successful write must not carry the rejected record.
    store.insert("presets", {"name": "second", "steps": []})
    assert sorted(item["name"] for item in store.all("presets")) == ["first", "second"]


def test_failed_update_is_fully_rolled_back(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    record = store.insert("assets", {
        "name": "原图",
        "metadata": {"nested": {"tags": ["a"], "score": 1}, "ok": True},
        "tags": ["x"],
    })
    _force_commit_failure(store, "assets", monkeypatch)
    with pytest.raises(StorageFailure):
        store.update("assets", record["id"], {
            "name": "失败的修改",
            "metadata": {"nested": {"tags": ["a", "b", "c"], "score": 99}},
            "tags": ["x", "y"],
        })
    monkeypatch.undo()
    current = store.get("assets", record["id"])
    assert current["name"] == "原图"
    assert current["metadata"] == {"nested": {"tags": ["a"], "score": 1}, "ok": True}
    assert current["tags"] == ["x"]
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_delete_does_not_remove_record(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    record = store.insert("jobs", {"status": "queued", "results": []})
    _force_commit_failure(store, "jobs", monkeypatch)
    with pytest.raises(StorageFailure):
        store.delete("jobs", record["id"])
    monkeypatch.undo()
    assert store.get("jobs", record["id"])["status"] == "queued"
    assert not list(tmp_path.glob("*.tmp"))


def test_restart_matches_committed_state_after_failures(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    keep = store.insert("assets", {
        "name": "保留",
        "metadata": {"profile": {"gamma": 2.2, "labels": ["raw"]}},
    })
    failed_id_seed = {"name": "失败插入"}
    _force_commit_failure(store, "assets", monkeypatch)
    with pytest.raises(StorageFailure):
        store.insert("assets", failed_id_seed)
    with pytest.raises(StorageFailure):
        store.update("assets", keep["id"], {"name": "不应持久化"})
    with pytest.raises(StorageFailure):
        store.delete("assets", keep["id"])
    monkeypatch.undo()
    store.insert("assets", {"name": "后续成功"})

    restarted = JsonStore(tmp_path)
    records = restarted.all("assets")
    assert sorted(item["name"] for item in records) == ["保留", "后续成功"]
    surviving = restarted.get("assets", keep["id"])
    assert surviving["name"] == "保留"
    assert surviving["metadata"] == {"profile": {"gamma": 2.2, "labels": ["raw"]}}
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.recovery"))


def test_failed_write_keeps_backup_at_last_commit(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    first = store.insert("events", {"message": "v1"})
    second = store.insert("events", {"message": "v2"})
    _force_commit_failure(store, "events", monkeypatch)
    with pytest.raises(StorageFailure):
        store.insert("events", {"message": "v3-never-committed"})
    monkeypatch.undo()
    # Both main file and backup stay at the last committed state (v1 + v2).
    backup = json.loads((tmp_path / "events.json.bak").read_text(encoding="utf-8"))
    assert sorted(item["message"] for item in backup["records"].values()) == ["v1", "v2"]
    main = json.loads((tmp_path / "events.json").read_text(encoding="utf-8"))
    assert sorted(item["message"] for item in main["records"].values()) == ["v1", "v2"]
    assert not list(tmp_path.glob("*.tmp"))
    # The backup recovers the full committed state if the main file breaks.
    (tmp_path / "events.json").write_text("{broken", encoding="utf-8")
    restarted = JsonStore(tmp_path)
    recovered_ids = {item["id"] for item in restarted.all("events")}
    assert recovered_ids == {first["id"], second["id"]}


def test_corrupt_main_file_recovers_backup_on_next_read(tmp_path):
    store = JsonStore(tmp_path)
    first = store.insert("events", {"message": "committed-one"})
    store.insert("events", {"message": "committed-two"})
    (tmp_path / "events.json").write_text("{broken", encoding="utf-8")
    restarted = JsonStore(tmp_path)
    messages = sorted(item["message"] for item in restarted.all("events"))
    assert messages == ["committed-one"]
    assert restarted.get("events", first["id"])["message"] == "committed-one"
    # The recovered main file is now valid JSON.
    json.loads((tmp_path / "events.json").read_text(encoding="utf-8"))
    assert not list(tmp_path.glob("*.recovery"))


def test_corrupt_file_without_backup_raises_instead_of_empty_data(tmp_path):
    JsonStore(tmp_path)
    (tmp_path / "presets.json").write_text("not json at all", encoding="utf-8")
    (tmp_path / "presets.json.bak").unlink(missing_ok=True)
    with pytest.raises(RuntimeError):
        JsonStore(tmp_path)


def test_api_surfaces_storage_failure_as_500_and_keeps_committed_state(app, client, uploaded, monkeypatch):
    _force_commit_failure(app.extensions["studio"]["store"], "assets", monkeypatch)
    response = client.patch(f"/api/assets/{uploaded['id']}", json={"name": "不会保存"})
    assert response.status_code == 500
    body = response.json
    assert body["ok"] is False
    assert body["error"]["code"] == "internal_error"
    monkeypatch.undo()
    fresh = client.get(f"/api/assets/{uploaded['id']}").json["data"]
    assert fresh["name"] == "测试原图.png"
    # Nested metadata from the original upload is untouched.
    assert fresh["metadata"]["source_format"] == "PNG"
    assert not list(app.extensions["studio"]["settings"].database.glob("*.tmp"))


def test_event_recording_still_works_after_storage_failure(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    _force_commit_failure(store, "presets", monkeypatch)
    with pytest.raises(StorageFailure):
        store.insert("presets", {"name": "blocked"})
    monkeypatch.undo()
    store.event("upload", "事件照常写入", {"asset_id": "abc"})
    events = store.all("events")
    assert len(events) == 1
    assert events[0]["details"] == {"asset_id": "abc"}
    assert store.all("presets") == []


def test_image_business_survives_storage_failure_and_restart(app, client, uploaded, monkeypatch):
    store = app.extensions["studio"]["store"]
    # A failing metadata edit must not disturb the stored asset or its files.
    _force_commit_failure(store, "assets", monkeypatch)
    failed = client.patch(f"/api/assets/{uploaded['id']}", json={"name": "失败名称", "tags": ["坏"]})
    assert failed.status_code == 500
    monkeypatch.undo()

    # The normal upload -> process -> download chain still works end to end.
    processed = client.post("/api/process", json={
        "asset_id": uploaded["id"],
        "steps": [{"operation": "resize_exact", "params": {"width": 96, "height": 80}}],
    })
    assert processed.status_code == 201, processed.json
    result_id = processed.json["data"]["id"]
    assert client.get(f"/api/assets/{result_id}/file").status_code == 200
    assert client.get(f"/api/assets/{result_id}/thumbnail").status_code == 200
    export = client.post("/api/exports", json={"asset_id": result_id, "options": {"format": "PNG"}})
    assert export.status_code == 201
    assert client.get(export.json["data"]["url"]).status_code == 200

    # A later metadata edit succeeds and carries none of the failed changes.
    ok_edit = client.patch(f"/api/assets/{uploaded['id']}", json={"favorite": True, "tags": ["好"]})
    assert ok_edit.status_code == 200
    assert ok_edit.json["data"]["name"] == "测试原图.png"
    assert ok_edit.json["data"]["tags"] == ["好"]
    assert ok_edit.json["data"]["favorite"] is True

    root = app.extensions["studio"]["settings"].root
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(root, testing=True)
    try:
        second_client = second.test_client()
        restarted_asset = second_client.get(f"/api/assets/{uploaded['id']}").json["data"]
        assert restarted_asset["name"] == "测试原图.png"
        assert restarted_asset["tags"] == ["好"]
        assert restarted_asset["favorite"] is True
        # Nested metadata (upload provenance and processing timings) survives.
        assert restarted_asset["metadata"]["source_format"] == "PNG"
        processed_again = second_client.get(f"/api/assets/{result_id}").json["data"]
        assert "timings" in processed_again["metadata"]
        assert processed_again["parent_id"] == uploaded["id"]
        assert second_client.get(f"/api/assets/{result_id}/file").status_code == 200
        assert second_client.get("/api/events").status_code == 200
    finally:
        second.extensions["studio"]["jobs"].shutdown()

    database = app.extensions["studio"]["settings"].database
    assert not list(database.glob("*.tmp"))
    assert not list(database.glob("*.recovery"))
