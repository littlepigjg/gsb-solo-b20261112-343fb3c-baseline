"""Real format encoding and deterministic, path-safe ZIP delivery."""
import io
import os
import zipfile
from pathlib import Path
from PIL import Image
from .errors import StudioError
from .imaging import flatten
from .storage import new_id
from .validation import object_value, reject_unknown, choice_value, number_value, color_value, boolean_value, list_value


EXTENSIONS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "BMP": "bmp", "TIFF": "tiff", "GIF": "gif"}
MIMETYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "BMP": "image/bmp", "TIFF": "image/tiff", "GIF": "image/gif"}


class ExportService:
    def __init__(self, settings, store, assets, processing):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.processing = processing

    def validate(self, options):
        object_value(options, "导出参数")
        reject_unknown(options, {"format", "quality", "background", "lossless", "dpi"}, "导出参数")
        return {
            "format": choice_value(options.get("format", "PNG"), list(EXTENSIONS), "导出格式"),
            "quality": number_value(options.get("quality", 90), "质量", 1, 100, integer=True),
            "background": color_value(options.get("background", "#ffffff")),
            "lossless": boolean_value(options.get("lossless", False), "无损模式"),
            "dpi": number_value(options.get("dpi", 96), "DPI", 36, 1200, integer=True),
        }

    def encode(self, image, options):
        format_name = options["format"]
        buffer = io.BytesIO()
        kwargs = {}
        output = image
        if format_name in {"JPEG", "BMP", "GIF"}:
            output = flatten(image, options["background"])
        if format_name == "JPEG":
            kwargs.update(quality=options["quality"], optimize=True, progressive=True)
        elif format_name == "WEBP":
            kwargs.update(quality=options["quality"], lossless=options["lossless"], method=4)
        elif format_name == "PNG":
            kwargs.update(optimize=True, compress_level=6)
        elif format_name == "TIFF":
            kwargs.update(compression="tiff_lzw")
        elif format_name == "GIF":
            output = output.quantize(colors=256)
        if format_name in {"PNG", "JPEG", "TIFF", "BMP"}:
            kwargs["dpi"] = (options["dpi"], options["dpi"])
        output.info.clear()
        output.save(buffer, format=format_name, **kwargs)
        return buffer.getvalue()

    def _save_file(self, payload, extension, name, asset_ids, options, kind):
        export_id = new_id()
        filename = f"{export_id}.{extension}"
        path = self.settings.exports / filename
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
            record = self.store.insert("exports", {
                "id": export_id,
                "filename": filename,
                "download_name": name,
                "bytes": len(payload),
                "asset_ids": asset_ids,
                "options": options,
                "kind": kind,
            })
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        record["url"] = f"/api/exports/{export_id}/download"
        self.store.event("export", f"导出 {name}", {"export_id": export_id})
        return record

    def create(self, asset_id, options):
        options = self.validate(options)
        with self.processing.compute_lock:
            image, asset = self.assets.load(asset_id)
            payload = self.encode(image, options)
        extension = EXTENSIONS[options["format"]]
        name = asset["name"].rsplit(".", 1)[0][:100] + "." + extension
        return self._save_file(payload, extension, name, [asset_id], options, "image")

    def archive(self, asset_ids, options):
        from concurrent.futures import ThreadPoolExecutor
        import json
        asset_ids = list_value(asset_ids, "导出图片", maximum=self.settings.max_batch)
        asset_ids = list(dict.fromkeys(asset_ids))
        options = self.validate(options)
        buffer = io.BytesIO()
        extension = EXTENSIONS[options["format"]]
        def prepare(asset_id):
            image, asset = self.assets.load(asset_id)
            return self.encode(image, options)
        # Encoding runs concurrently, but results are gathered back in
        # selection order, so sequence numbers, filenames, payloads and the
        # manifest always describe the same asset regardless of which encode
        # finishes first. Any failure aborts the whole export before a
        # record or file is created.
        with self.processing.compute_lock:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(prepare, asset_id) for asset_id in asset_ids]
                payloads = []
                for asset_id, future in zip(asset_ids, futures):
                    try:
                        payloads.append(future.result())
                    except StudioError:
                        raise
                    except Exception as error:
                        raise StudioError(
                            "图片编码失败，已取消整个批量导出",
                            code="encode_failed",
                            status=422,
                            details={"asset_id": asset_id},
                        ) from error
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, (asset_id, payload) in enumerate(zip(asset_ids, payloads), start=1):
                    filename = f"{index:02d}_{asset_id[:8]}.{extension}"
                    archive.writestr(filename, payload)
                manifest = {"options": options, "assets": asset_ids, "count": len(asset_ids)}
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return self._save_file(buffer.getvalue(), "zip", "光屿_批量导出.zip", asset_ids, options, "archive")

    def file(self, export_id):
        from .validation import identifier
        identifier(export_id)
        record = self.store.get("exports", export_id)
        path = self.settings.exports / record["filename"]
        if not path.exists():
            raise StudioError("导出文件丢失，请重新导出", status=410)
        mime = "application/zip" if record["kind"] == "archive" else MIMETYPES[record["options"]["format"]]
        return path, record, mime

