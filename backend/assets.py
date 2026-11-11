"""Durable image assets, thumbnails, provenance and asset-library queries."""
import hashlib
import io
import os
import threading
from pathlib import Path
from PIL import Image
from .errors import StudioError, Conflict
from .storage import new_id
from .imaging import decode_image
from .validation import identifier, text_value


class AssetService:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.lock = threading.RLock()

    def _file(self, asset_id, thumbnail=False):
        identifier(asset_id)
        suffix = ".thumb.png" if thumbnail else ".png"
        return self.settings.media / (asset_id + suffix)

    def public(self, record):
        result = dict(record)
        result["url"] = f"/api/assets/{record['id']}/file"
        result["thumbnail_url"] = f"/api/assets/{record['id']}/thumbnail"
        return result

    def get(self, asset_id):
        identifier(asset_id)
        return self.public(self.store.get("assets", asset_id))

    def list(self, query="", kind="all", favorite=False, limit=100, offset=0):
        records = self.store.all("assets")
        if query:
            query = query.casefold()
            records = [item for item in records if query in item["name"].casefold()]
        if kind != "all":
            records = [item for item in records if item["kind"] == kind]
        if favorite:
            records = [item for item in records if item.get("favorite")]
        records.sort(key=lambda item: item["created_at"], reverse=True)
        return {
            "items": [self.public(item) for item in records[offset:offset + limit]],
            "total": len(records),
            "limit": limit,
            "offset": offset,
        }

    def upload(self, payload, name):
        image, original_format, frames = decode_image(payload, self.settings)
        name = Path(str(name).replace("\\", "/")).name
        name = text_value(name or "未命名图片", "文件名", 180, 1)
        metadata = {
            "source_format": original_format,
            "source_bytes": len(payload),
            "source_frames": frames,
            "notice": "多帧图片仅导入第一帧" if frames > 1 else "",
        }
        record = self.save(image, name=name, kind="original", metadata=metadata)
        self.store.event("upload", f"导入 {name}", {"asset_id": record["id"]})
        return record

    def save(self, image, name, kind="result", parent=None, steps=None, metadata=None):
        from .validation import dimension_guard
        dimension_guard(*image.size, self.settings)
        asset_id = new_id()
        path = self._file(asset_id)
        thumbnail_path = self._file(asset_id, True)
        temporary = path.with_suffix(".tmp")
        image = image.convert("RGBA")
        image.info.clear()
        with self.lock:
            try:
                image.save(temporary, format="PNG", compress_level=4)
                os.replace(temporary, path)
                thumbnail = image.copy()
                thumbnail.thumbnail((320, 240), Image.Resampling.LANCZOS)
                thumbnail.save(thumbnail_path, format="PNG")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                record = {
                    "id": asset_id,
                    "name": name,
                    "kind": kind,
                    "width": image.width,
                    "height": image.height,
                    "mode": "RGBA",
                    "format": "PNG",
                    "bytes": path.stat().st_size,
                    "sha256": digest,
                    "has_alpha": image.getchannel("A").getextrema()[0] < 255,
                    "parent_id": parent["id"] if parent else None,
                    "root_id": (parent.get("root_id") or parent["id"]) if parent else asset_id,
                    "steps": steps or [],
                    "favorite": False,
                    "tags": [],
                    "metadata": metadata or {},
                }
                record = self.store.insert("assets", record)
                return self.public(record)
            except Exception:
                path.unlink(missing_ok=True)
                thumbnail_path.unlink(missing_ok=True)
                raise
            finally:
                temporary.unlink(missing_ok=True)

    def restore(self, new_id, entry, new_parent_id, new_root_id):
        """通过完整校验后写入迁移资产；PNG 字节原样落盘以保持摘要不变。"""
        identifier(new_id)
        path = self._file(new_id)
        thumbnail_path = self._file(new_id, True)
        temporary = path.with_suffix(".tmp")
        image = entry["image"].convert("RGBA")
        image.info.clear()
        try:
            temporary.write_bytes(entry["png"])
            os.replace(temporary, path)
            thumbnail = image.copy()
            thumbnail.thumbnail((320, 240), Image.Resampling.LANCZOS)
            thumbnail.save(thumbnail_path, format="PNG")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != entry["sha256"]:
                raise StudioError("导入图片哈希校验失败", code="hash_mismatch")
            record = {
                "id": new_id,
                "name": entry["name"],
                "kind": "original" if new_parent_id is None else "result",
                "width": entry["width"],
                "height": entry["height"],
                "mode": "RGBA",
                "format": "PNG",
                "bytes": path.stat().st_size,
                "sha256": digest,
                "has_alpha": image.getchannel("A").getextrema()[0] < 255,
                "parent_id": new_parent_id,
                "root_id": new_root_id,
                "steps": list(entry["steps"]),
                "favorite": entry["favorite"],
                "tags": list(entry["tags"]),
                "metadata": {"imported_from_bundle": True},
            }
            record = self.store.insert("assets", record)
            return self.public(record)
        except Exception:
            path.unlink(missing_ok=True)
            thumbnail_path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, asset_id):
        with self.lock:
            record = self.get(asset_id)
            path = self._file(asset_id)
            if not path.exists():
                raise StudioError("图片文件丢失，请重新导入", code="missing_file", status=410)
            with Image.open(path) as opened:
                opened.load()
                image = opened.convert("RGBA")
            return image, record

    def file(self, asset_id, thumbnail=False):
        record = self.get(asset_id)
        path = self._file(asset_id, thumbnail)
        if not path.exists():
            raise StudioError("图片文件丢失", status=410)
        return path, record

    def update(self, asset_id, changes):
        from .validation import reject_unknown, boolean_value, list_value
        self.get(asset_id)
        reject_unknown(changes, {"name", "favorite", "tags"})
        validated = {}
        if "name" in changes:
            validated["name"] = text_value(changes["name"], "名称", 180, 1)
        if "favorite" in changes:
            validated["favorite"] = boolean_value(changes["favorite"], "收藏状态")
        if "tags" in changes:
            tags = list_value(changes["tags"], "标签", minimum=0, maximum=12)
            validated["tags"] = list(dict.fromkeys(text_value(tag, "标签", 30, 1) for tag in tags))
        return self.public(self.store.update("assets", asset_id, validated))

    def delete(self, asset_id):
        with self.lock:
            record = self.get(asset_id)
            for job in self.store.all("jobs"):
                if job["status"] in {"queued", "running", "cancelling"} and asset_id in job["asset_ids"]:
                    raise Conflict("图片正在批量任务中使用，任务结束后才能删除")
            descendants = [item for item in self.store.all("assets") if item.get("parent_id") == asset_id]
            if descendants:
                raise Conflict("该图片有处理副本，请先删除其副本，以保留完整历史")
            self.store.delete("assets", asset_id)
            self._file(asset_id).unlink(missing_ok=True)
            self._file(asset_id, True).unlink(missing_ok=True)
            self.store.event("delete", f"删除 {record['name']}")

    def history(self, asset_id):
        record = self.get(asset_id)
        lineage = []
        seen = set()
        while record and record["id"] not in seen:
            lineage.append(record)
            seen.add(record["id"])
            parent_id = record.get("parent_id")
            record = self.get(parent_id) if parent_id else None
        lineage.reverse()
        return lineage

