"""Application factory, service wiring and HTTP error boundary."""
import atexit
import logging
import time
import uuid
from flask import Flask, jsonify, request, g, send_from_directory
from werkzeug.exceptions import HTTPException
from .settings import Settings, PROJECT_ROOT
from .storage import JsonStore
from .registry import Registry
from .assets import AssetService
from .processing import ProcessingService
from .jobs import JobService
from .exports import ExportService
from .atlases import AtlasService
from .analysis import AnalysisService
from .presets import PresetService
from .samples import SampleService
from .diagnostics import DiagnosticsService
from .errors import StudioError


def create_app(data_dir=None, testing=False):
    settings = Settings.from_environment(data_dir)
    settings.prepare()
    store = JsonStore(settings.database)
    registry = Registry()
    assets = AssetService(settings, store)
    processing = ProcessingService(settings, store, assets, registry)
    jobs = JobService(settings, store, assets, processing)
    exports = ExportService(settings, store, assets, processing)
    atlases = AtlasService(settings, store, assets)
    presets = PresetService(store, registry)
    services = {
        "settings": settings,
        "store": store,
        "registry": registry,
        "assets": assets,
        "processing": processing,
        "jobs": jobs,
        "exports": exports,
        "atlases": atlases,
        "presets": presets,
        "analysis": AnalysisService(assets),
        "samples": SampleService(assets, store),
        "diagnostics": DiagnosticsService(settings, store),
    }
    app = Flask(__name__, root_path=".", static_folder=str(PROJECT_ROOT / "frontend"), static_url_path="/static")
    app.config.update(
        MAX_CONTENT_LENGTH=settings.max_upload_bytes + 1024 * 1024,
        TESTING=testing,
        JSON_SORT_KEYS=False,
    )
    app.json.ensure_ascii = False
    app.extensions["studio"] = services
    if not testing:
        atexit.register(jobs.shutdown)

    @app.before_request
    def before_request():
        g.request_id = uuid.uuid4().hex[:12]
        g.started = time.perf_counter()
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                raise StudioError("不允许来自其他站点的写入请求", code="cross_origin", status=403)
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                raise StudioError("不允许跨站写入请求", code="cross_origin", status=403)

    @app.after_request
    def after_request(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        if request.path.startswith("/api/") and response.mimetype == "application/json":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(StudioError)
    def studio_error(error):
        return jsonify({"ok": False, "error": error.as_dict()}), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {413: "上传内容超过大小限制", 404: "接口或页面不存在", 405: "请求方法不受支持", 400: "请求格式不正确", 415: "需要 application/json 请求"}
        return jsonify({"ok": False, "error": {
            "code": "http_error",
            "message": messages.get(error.code, error.description),
            "details": {},
        }}), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        app.logger.exception("Request failed: %s", getattr(g, "request_id", ""))
        return jsonify({"ok": False, "error": {
            "code": "internal_error",
            "message": "处理发生异常，请查看服务日志。",
            "details": {"request_id": getattr(g, "request_id", "")},
        }}), 500

    @app.get("/")
    def index():
        return send_from_directory(PROJECT_ROOT / "frontend", "index.html")

    from .routes import api
    app.register_blueprint(api)
    return app

