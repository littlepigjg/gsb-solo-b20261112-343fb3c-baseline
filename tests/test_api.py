import io
import json
import os
import time
import zipfile
import pytest
from PIL import Image
from backend.app import create_app


def test_home_and_health(client):
    assert client.get("/").status_code == 200
    health = client.get("/api/health").json["data"]
    assert health["features"] == 10
    assert health["operations"] == 86
    assert client.get("/static/app.js").status_code == 200


def test_upload_and_metadata(client, uploaded):
    assert uploaded["name"] == "测试原图.png"
    assert uploaded["width"] == 384
    assert uploaded["has_alpha"] is True
    assert client.get(uploaded["url"]).mimetype == "image/png"
    assert client.get(uploaded["thumbnail_url"]).status_code == 200
    assert client.get("/api/assets").json["data"]["total"] == 1


def test_invalid_upload_rejected(client):
    response = client.post("/api/assets", data={"file": (io.BytesIO(b"not an image"), "fake.png")})
    assert response.status_code == 400


def test_disguised_extension_uses_decoded_content(client, sample_image):
    buffer = io.BytesIO()
    sample_image.save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, "../../safe.html")})
    assert response.status_code == 201
    asset = response.json["data"]
    assert asset["name"] == "safe.html"
    assert asset["format"] == "PNG"
    assert client.get(asset["url"]).mimetype == "image/png"


def test_pipeline_preserves_original(client, uploaded):
    before = client.get(uploaded["url"]).data
    response = client.post("/api/process", json={"asset_id": uploaded["id"], "steps": [
        {"operation": "crop_square", "params": {}},
        {"operation": "resize_exact", "params": {"width": 128, "height": 128}},
        {"operation": "filter_sepia", "params": {}},
    ]})
    assert response.status_code == 201, response.json
    result = response.json["data"]
    assert result["id"] != uploaded["id"]
    assert result["parent_id"] == uploaded["id"]
    assert result["root_id"] == uploaded["id"]
    assert result["width"] == result["height"] == 128
    assert client.get(uploaded["url"]).data == before
    assert len(client.get(f"/api/assets/{result['id']}/history").json["data"]) == 2
    assert client.delete(f"/api/assets/{uploaded['id']}").status_code == 409


@pytest.mark.parametrize("format_name", ["PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF"])
def test_real_format_export(client, uploaded, format_name):
    response = client.post("/api/exports", json={"asset_id": uploaded["id"], "options": {"format": format_name}})
    assert response.status_code == 201, response.json
    record = response.json["data"]
    download = client.get(record["url"])
    assert download.status_code == 200
    with Image.open(io.BytesIO(download.data)) as image:
        assert image.format == format_name
        assert image.size == (384, 320)
    assert "attachment" in download.headers["Content-Disposition"]


def test_archive(client, uploaded):
    response = client.post("/api/exports/archive", json={"asset_ids": [uploaded["id"]], "options": {"format": "JPEG"}})
    assert response.status_code == 201
    content = client.get(response.json["data"]["url"]).data
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert len(archive.namelist()) == 2
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["assets"] == [uploaded["id"]]
        assert archive.testzip() is None


def upload_color(client, color, name):
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def test_archive_dedupes_and_matches_manifest(client, uploaded):
    green = upload_color(client, (0, 255, 0), "green.png")
    blue = upload_color(client, (0, 0, 255), "blue.png")
    ids = [blue["id"], uploaded["id"], green["id"]]
    response = client.post("/api/exports/archive", json={
        "asset_ids": [ids[0], ids[1], ids[0], ids[2], ids[1]],
        "options": {"format": "PNG"},
    })
    assert response.status_code == 201, response.json
    record = response.json["data"]
    assert record["asset_ids"] == ids
    content = client.get(record["url"]).data
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        assert names == [f"01_{ids[0][:8]}.png", f"02_{ids[1][:8]}.png", f"03_{ids[2][:8]}.png", "manifest.json"]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["assets"] == ids
        assert manifest["count"] == 3
        with Image.open(io.BytesIO(archive.read(names[0]))) as image:
            assert image.convert("RGB").getpixel((24, 24)) == (0, 0, 255)
        with Image.open(io.BytesIO(archive.read(names[1]))) as image:
            assert image.size == (384, 320)
        with Image.open(io.BytesIO(archive.read(names[2]))) as image:
            assert image.convert("RGB").getpixel((24, 24)) == (0, 255, 0)
        assert archive.testzip() is None


def test_archive_order_not_affected_by_encode_speed(client, app, monkeypatch):
    red = upload_color(client, (255, 0, 0), "red.png")
    green = upload_color(client, (0, 255, 0), "green.png")
    blue = upload_color(client, (0, 0, 255), "blue.png")
    ids = [red["id"], green["id"], blue["id"]]
    assets = app.extensions["studio"]["assets"]
    original_load = assets.load

    def slowed_load(asset_id):
        if asset_id == ids[0]:
            time.sleep(0.5)  # first selection finishes encoding last
        return original_load(asset_id)

    monkeypatch.setattr(assets, "load", slowed_load)
    response = client.post("/api/exports/archive", json={"asset_ids": ids, "options": {"format": "PNG"}})
    assert response.status_code == 201, response.json
    record = response.json["data"]
    content = client.get(record["url"]).data
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        assert names[:3] == [f"01_{ids[0][:8]}.png", f"02_{ids[1][:8]}.png", f"03_{ids[2][:8]}.png"]
        with Image.open(io.BytesIO(archive.read(names[0]))) as image:
            assert image.convert("RGB").getpixel((10, 10)) == (255, 0, 0)
        with Image.open(io.BytesIO(archive.read(names[2]))) as image:
            assert image.convert("RGB").getpixel((10, 10)) == (0, 0, 255)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["assets"] == ids
        assert manifest["count"] == 3


def assert_no_export_created(client, app, before):
    assert client.get("/api/stats").json["data"]["exports"] == before
    exports_dir = app.extensions["studio"]["settings"].exports
    assert os.listdir(exports_dir) == []


def test_archive_with_missing_member_aborts(client, uploaded, app):
    before = client.get("/api/stats").json["data"]["exports"]
    response = client.post("/api/exports/archive", json={
        "asset_ids": [uploaded["id"], "0" * 32],
        "options": {"format": "PNG"},
    })
    assert response.status_code == 404
    assert response.json["ok"] is False
    assert_no_export_created(client, app, before)


def test_archive_with_missing_media_aborts(client, uploaded, app):
    settings = app.extensions["studio"]["settings"]
    (settings.media / f"{uploaded['id']}.png").unlink()
    before = client.get("/api/stats").json["data"]["exports"]
    response = client.post("/api/exports/archive", json={
        "asset_ids": [uploaded["id"]],
        "options": {"format": "PNG"},
    })
    assert response.status_code == 410
    assert response.json["ok"] is False
    assert_no_export_created(client, app, before)


def test_archive_encode_failure_aborts(client, uploaded, app, monkeypatch):
    exports_service = app.extensions["studio"]["exports"]

    def broken_encode(image, options):
        raise RuntimeError("simulated encoder crash")

    monkeypatch.setattr(exports_service, "encode", broken_encode)
    before = client.get("/api/stats").json["data"]["exports"]
    response = client.post("/api/exports/archive", json={
        "asset_ids": [uploaded["id"]],
        "options": {"format": "PNG"},
    })
    assert response.status_code == 422
    assert response.json["ok"] is False
    assert response.json["error"]["code"] == "encode_failed"
    assert_no_export_created(client, app, before)


def test_preview_is_ephemeral(client, uploaded):
    response = client.post("/api/preview", json={"asset_id": uploaded["id"], "steps": [{"operation": "color_invert", "params": {}}]})
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert client.get("/api/assets").json["data"]["total"] == 1


def test_metadata_update_and_delete(client, uploaded):
    response = client.patch(f"/api/assets/{uploaded['id']}", json={"name": "新名称", "favorite": True, "tags": ["作品", "作品"]})
    assert response.json["data"]["tags"] == ["作品"]
    assert client.get("/api/assets?favorite=true").json["data"]["total"] == 1
    assert client.delete(f"/api/assets/{uploaded['id']}").status_code == 200
    assert client.get(uploaded["url"]).status_code == 404


def test_analysis_and_compare(client, uploaded):
    analysis = client.get(f"/api/assets/{uploaded['id']}/analysis").json["data"]
    assert len(analysis["histograms"]["red"]) == 256
    assert analysis["transparent_percent"] > 0
    compare = client.post("/api/compare", json={"first_id": uploaded["id"], "second_id": uploaded["id"]}).json["data"]
    assert compare["mean_squared_error"] == 0
    assert compare["psnr_db"] is None


def test_preset_round_trip(client):
    payload = {"name": "测试流程", "description": "示例", "steps": [{"operation": "color_invert", "params": {}}]}
    response = client.post("/api/presets", json=payload)
    assert response.status_code == 201
    record = response.json["data"]
    assert client.patch(f"/api/presets/{record['id']}", json={"name": "修改后的流程"}).status_code == 200
    assert client.delete(f"/api/presets/{record['id']}").status_code == 200
    builtin = client.get("/api/presets").json["data"][0]
    assert client.delete(f"/api/presets/{builtin['id']}").status_code == 400


def test_batch_job_completed(client, uploaded):
    response = client.post("/api/jobs", json={"asset_ids": [uploaded["id"]], "steps": [{"operation": "color_grayscale", "params": {}}]})
    assert response.status_code == 202
    job_id = response.json["data"]["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert job["status"] == "completed"
    assert job["done"] == 1
    assert job["progress"] == 100
    assert len(job["results"]) == 1


def test_batch_item_failure_is_reported(client, uploaded):
    response = client.post("/api/jobs", json={"asset_ids": [uploaded["id"]], "steps": [{"operation": "crop_rect", "params": {"width": 8000}}]})
    job_id = response.json["data"]["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert job["status"] == "failed"
    assert len(job["errors"]) == 1


def test_multi_image_batch_reports_partial_success(client, uploaded):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(buffer, "PNG")
    buffer.seek(0)
    small = client.post("/api/assets", data={"file": (buffer, "small.png")}).json["data"]
    response = client.post("/api/jobs", json={
        "asset_ids": [uploaded["id"], small["id"]],
        "steps": [{"operation": "crop_rect", "params": {"width": 128, "height": 128}}],
    })
    assert response.status_code == 202
    job_id = response.json["data"]["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert job["status"] == "partial"
    assert job["done"] == job["total"] == 2
    assert len(job["results"]) == len(job["errors"]) == 1
    assert job["results"][0]["source_id"] == uploaded["id"]
    assert job["errors"][0]["asset_id"] == small["id"]


def test_png_export_retains_alpha(client, uploaded):
    result = client.post("/api/exports", json={"asset_id": uploaded["id"], "options": {"format": "PNG"}}).json["data"]
    with Image.open(io.BytesIO(client.get(result["url"]).data)) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((0, 15))[3] == 120


def test_json_persists_across_restart(app, uploaded):
    root = app.extensions["studio"]["settings"].root
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(root, testing=True)
    try:
        assert second.test_client().get(f"/api/assets/{uploaded['id']}").json["data"]["name"] == "测试原图.png"
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_cross_origin_write_rejected(client, uploaded):
    response = client.patch(f"/api/assets/{uploaded['id']}", json={"name": "bad"}, headers={"Origin": "https://unrelated.example"})
    assert response.status_code == 403


@pytest.mark.parametrize("request_payload", [None, [], {"asset_id": "../../file", "steps": []}, {"asset_id": "0" * 32, "steps": [{"operation": "does_not_exist"}]}])
def test_invalid_api_requests(client, request_payload):
    response = client.post("/api/process", json=request_payload)
    assert 400 <= response.status_code < 500
    assert response.json["ok"] is False


def test_backup_and_diagnostics(client, uploaded):
    backup = json.loads(client.get("/api/backup").data)
    assert uploaded["id"] in backup["collections"]["assets"]["records"]
    diagnostics = client.get("/api/diagnostics").json["data"]
    assert diagnostics["healthy"] is True
    assert diagnostics["media_file_count"] == 2


def test_sample_creation_idempotent(client):
    first = client.post("/api/samples", json={}).json["data"]
    second = client.post("/api/samples", json={}).json["data"]
    assert [item["id"] for item in first] == [item["id"] for item in second]
    assert len(first) == 3
