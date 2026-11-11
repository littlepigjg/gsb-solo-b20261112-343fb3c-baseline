"""Documented REST surface. All business data is exchanged as JSON."""
import io
import json
from flask import Blueprint, current_app, jsonify, request, send_file
from .errors import StudioError
from .validation import object_value, reject_unknown, number_value, choice_value, identifier, color_value


api = Blueprint("api", __name__, url_prefix="/api")


def service(name):
    return current_app.extensions["studio"][name]


def payload(allowed=None):
    data = object_value(request.get_json())
    if allowed is not None:
        reject_unknown(data, allowed)
    return data


def ok(data=None, status=200):
    return jsonify({"ok": True, "data": data}), status


@api.get("/health")
def health():
    return ok({"status": "ready", "version": "1.0.0", "storage": "JSON", "features": 10, "operations": len(service("registry").specs)})


@api.get("/config")
def config():
    return ok(service("settings").public())


@api.get("/diagnostics")
def diagnostics():
    return ok(service("diagnostics").inspect())


@api.get("/catalog")
def catalog():
    return ok(service("registry").catalog())


@api.get("/stats")
def stats():
    assets = service("store").all("assets")
    jobs = service("store").all("jobs")
    exports = service("store").all("exports")
    return ok({
        "assets": len(assets),
        "originals": sum(item["kind"] == "original" for item in assets),
        "results": sum(item["kind"] == "result" for item in assets),
        "samples": sum(item["kind"] == "sample" for item in assets),
        "bytes": sum(item["bytes"] for item in assets),
        "exports": len(exports),
        "active_jobs": sum(item["status"] in {"queued", "running", "cancelling"} for item in jobs),
        "completed_jobs": sum(item["status"] == "completed" for item in jobs),
    })


@api.get("/assets")
def asset_list():
    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
    except ValueError as error:
        raise StudioError("分页参数必须是整数") from error
    number_value(limit, "每页数量", 1, 200, integer=True)
    number_value(offset, "起始位置", 0, 1000000, integer=True)
    kind = choice_value(request.args.get("kind", "all"), ["all", "original", "result", "sample"], "图片类型")
    return ok(service("assets").list(
        query=request.args.get("q", "")[:180],
        kind=kind,
        favorite=request.args.get("favorite") == "true",
        limit=limit,
        offset=offset,
    ))


@api.post("/assets")
def asset_upload():
    file = request.files.get("file")
    if file is None:
        raise StudioError("请选择图片文件")
    limit = service("settings").max_upload_bytes
    image = service("assets").upload(file.stream.read(limit + 1), file.filename)
    return ok(image, 201)


@api.get("/assets/<asset_id>")
def asset_get(asset_id):
    return ok(service("assets").get(asset_id))


@api.patch("/assets/<asset_id>")
def asset_update(asset_id):
    return ok(service("assets").update(asset_id, payload()))


@api.delete("/assets/<asset_id>")
def asset_delete(asset_id):
    service("assets").delete(asset_id)
    return ok({"deleted": asset_id})


@api.get("/assets/<asset_id>/file")
def asset_file(asset_id):
    path, record = service("assets").file(asset_id)
    return send_file(path, mimetype="image/png", conditional=True, etag=record["sha256"])


@api.get("/assets/<asset_id>/thumbnail")
def asset_thumbnail(asset_id):
    path, record = service("assets").file(asset_id, thumbnail=True)
    return send_file(path, mimetype="image/png", conditional=True)


@api.get("/assets/<asset_id>/history")
def asset_history(asset_id):
    return ok(service("assets").history(asset_id))


@api.get("/assets/<asset_id>/analysis")
def asset_analysis(asset_id):
    return ok(service("analysis").inspect(asset_id))


@api.post("/compare")
def compare():
    data = payload({"first_id", "second_id"})
    return ok(service("analysis").compare(data.get("first_id"), data.get("second_id")))


@api.post("/diff-maps")
def diff_maps():
    data = payload({"first_id", "second_id", "offset_x", "offset_y", "tolerance", "background"})
    first_id = identifier(data.get("first_id"), "第一张图片ID")
    second_id = identifier(data.get("second_id"), "第二张图片ID")
    offset_x = number_value(data.get("offset_x", 0), "水平偏移", -4096, 4096, integer=True)
    offset_y = number_value(data.get("offset_y", 0), "垂直偏移", -4096, 4096, integer=True)
    tolerance = number_value(data.get("tolerance", 0), "容差", 0, 255, integer=True)
    background = color_value(data.get("background", "#ffffff"), "背景颜色")
    return ok(service("diff_maps").build(
        first_id, second_id, offset_x, offset_y, tolerance, background), 201)


@api.post("/process")
def process():
    data = payload({"asset_id", "steps", "name"})
    return ok(service("processing").process(data.get("asset_id"), data.get("steps"), data.get("name")), 201)


@api.post("/preview")
def preview():
    data = payload({"asset_id", "steps"})
    buffer = service("processing").preview(data.get("asset_id"), data.get("steps"))
    return send_file(buffer, mimetype="image/png", max_age=0)


@api.post("/samples")
def samples():
    payload(set())
    return ok(service("samples").ensure())


@api.get("/presets")
def preset_list():
    return ok(service("presets").list())


@api.post("/presets")
def preset_create():
    return ok(service("presets").create(payload()), 201)


@api.patch("/presets/<preset_id>")
def preset_update(preset_id):
    return ok(service("presets").update(preset_id, payload()))


@api.delete("/presets/<preset_id>")
def preset_delete(preset_id):
    service("presets").delete(preset_id)
    return ok({"deleted": preset_id})


@api.get("/jobs")
def job_list():
    return ok(service("jobs").list())


@api.post("/jobs")
def job_create():
    data = payload({"asset_ids", "steps", "title"})
    return ok(service("jobs").submit(data.get("asset_ids"), data.get("steps"), data.get("title", "批量图像处理")), 202)


@api.get("/jobs/<job_id>")
def job_get(job_id):
    return ok(service("jobs").get(job_id))


@api.post("/jobs/<job_id>/cancel")
def job_cancel(job_id):
    payload(set())
    return ok(service("jobs").cancel(job_id))


@api.post("/exports")
def export_create():
    data = payload({"asset_id", "options"})
    return ok(service("exports").create(data.get("asset_id"), data.get("options", {})), 201)


@api.post("/exports/archive")
def export_archive():
    data = payload({"asset_ids", "options"})
    return ok(service("exports").archive(data.get("asset_ids"), data.get("options", {})), 201)


@api.get("/exports/<export_id>/download")
def export_download(export_id):
    path, record, mime = service("exports").file(export_id)
    return send_file(path, mimetype=mime, as_attachment=True, download_name=record["download_name"])


@api.get("/events")
def event_list():
    events = service("store").all("events")
    events.sort(key=lambda item: item["created_at"], reverse=True)
    return ok(events[:100])


@api.get("/backup")
def backup():
    snapshot = service("store").snapshot()
    payload_bytes = json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    return send_file(io.BytesIO(payload_bytes), mimetype="application/json", as_attachment=True, download_name="image-studio-metadata.json")

