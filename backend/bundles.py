"""可迁移素材包：祖先闭包导出与全量校验后原子导入。

包格式（ZIP）仅包含：

- ``manifest.json``：``{"version": 1, "selected": [原ID], "assets": [...]}``
- ``images/<原ID>.png``：素材库中的规范化 PNG 字节，清单中的 sha256
  为这些包内字节的摘要。

导入在写入任何资产之前完成整个包的校验；任何一项不合法都不会产生
部分记录或孤立图片。
"""
import hashlib
import io
import json
import os
import threading
import zipfile
from PIL import Image, UnidentifiedImageError
from .errors import StudioError, MissingRecord
from .storage import new_id
from .validation import (
    identifier,
    list_value,
    text_value,
    boolean_value,
    dimension_guard,
)


BUNDLE_VERSION = 1
MANIFEST_NAME = "manifest.json"
IMAGES_PREFIX = "images/"
IMAGE_EXTENSION = ".png"
MANIFEST_FIELDS = {"id", "name", "parent_id", "root_id", "width", "height",
                   "sha256", "file", "steps", "favorite", "tags"}
MAX_STEPS = 12
MAX_TAGS = 12
READ_CHUNK = 1024 * 1024


class BundleService:
    def __init__(self, settings, store, assets, exports):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.exports = exports
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ 导出

    def export_bundle(self, asset_ids):
        asset_ids = list_value(asset_ids, "素材", minimum=1,
                               maximum=self.settings.max_batch)
        asset_ids = list(dict.fromkeys(asset_ids))
        records = {record["id"]: record for record in self.store.all("assets")}
        closure = self._closure(asset_ids, records)
        if len(closure) > self.settings.max_bundle_assets:
            raise StudioError(
                f"素材包最多包含 {self.settings.max_bundle_assets} 项，"
                f"补齐祖先后共有 {len(closure)} 项",
                code="bundle_too_large",
            )
        ordered = self._parent_first(closure, records)
        manifest_assets = []
        with self.lock:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for asset_id in ordered:
                    path = self.assets._file(asset_id)
                    if not path.exists():
                        raise StudioError("图片文件丢失，请重新导入",
                                          code="missing_file", status=410)
                    payload = path.read_bytes()
                    digest = hashlib.sha256(payload).hexdigest()
                    record = records[asset_id]
                    manifest_assets.append({
                        "id": record["id"],
                        "name": record["name"],
                        "parent_id": record.get("parent_id"),
                        "root_id": record["root_id"],
                        "width": record["width"],
                        "height": record["height"],
                        "sha256": digest,
                        "file": f"{IMAGES_PREFIX}{record['id']}{IMAGE_EXTENSION}",
                        "steps": list(record.get("steps") or []),
                        "favorite": bool(record.get("favorite")),
                        "tags": list(record.get("tags") or []),
                    })
                    archive.writestr(
                        f"{IMAGES_PREFIX}{asset_id}{IMAGE_EXTENSION}", payload)
                manifest = {
                    "version": BUNDLE_VERSION,
                    "selected": list(asset_ids),
                    "assets": manifest_assets,
                }
                archive.writestr(
                    MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
        export = self.exports.save_bundle(buffer.getvalue(), asset_ids)
        self.store.event("bundle_export", "导出素材迁移包",
                         {"asset_ids": asset_ids, "count": len(closure),
                          "export_id": export["id"]})
        return export

    def _closure(self, asset_ids, records):
        closure = []
        queued = set()

        def add_with_ancestors(asset_id):
            identifier(asset_id)
            chain = []
            current = asset_id
            seen = set()
            while current is not None:
                if current not in records:
                    raise MissingRecord("assets", current)
                if current in seen:
                    raise StudioError("素材血缘存在环，无法导出",
                                      code="lineage_cycle")
                seen.add(current)
                if current in queued:
                    break
                chain.append(current)
                current = records[current].get("parent_id")
            for ancestor in reversed(chain):
                queued.add(ancestor)
                closure.append(ancestor)

        for asset_id in asset_ids:
            add_with_ancestors(asset_id)
        return closure

    @staticmethod
    def _parent_first(closure, records):
        def depth(asset_id):
            level = 0
            current = asset_id
            seen = set()
            while current is not None and current not in seen:
                seen.add(current)
                parent = records[current].get("parent_id")
                if parent is None or parent not in records:
                    break
                current = parent
                level += 1
            return level

        return sorted(closure, key=depth)

    # ------------------------------------------------------------------ 导入

    def import_bundle(self, payload):
        if not payload:
            raise StudioError("上传文件为空")
        if len(payload) > self.settings.max_upload_bytes:
            raise StudioError("素材包超过 24 MiB 上传限制", status=413)
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except (zipfile.BadZipFile, OSError) as error:
            raise StudioError("文件不是合法的 ZIP 素材包") from error
        with archive:
            self._check_members(archive)
            manifest_bytes = self._read_member(archive, MANIFEST_NAME)
            manifest = self._parse_manifest(manifest_bytes)
            members = self._read_images(archive, manifest)
            entries = self._validate_manifest(manifest, members)
            self._validate_graph(entries)
            return self._restore(entries, manifest["selected"])

    def _check_members(self, archive):
        seen = set()
        total = 0
        for member_info in archive.infolist():
            member = member_info.filename
            if member.endswith("/"):
                raise StudioError("素材包不允许目录成员",
                                  details={"member": member})
            if "\\" in member:
                raise StudioError("素材包成员名不允许反斜杠",
                                  details={"member": member})
            if member.startswith("/") or os.path.isabs(member):
                raise StudioError("素材包成员不允许绝对路径",
                                  details={"member": member})
            if any(part in {"", ".", ".."} for part in member.split("/")):
                raise StudioError("素材包不允许路径穿越或不规范路径",
                                  details={"member": member})
            if member in seen:
                raise StudioError("素材包包含重复成员",
                                  details={"member": member})
            seen.add(member)
            if member_info.flag_bits & 0x1:
                raise StudioError("素材包不允许加密成员",
                                  details={"member": member})
            unix_mode = (member_info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise StudioError("素材包不允许符号链接",
                                  details={"member": member})
            if unix_mode not in (0, 0o100000):
                raise StudioError("素材包只允许普通文件",
                                  details={"member": member})
            if member != MANIFEST_NAME and not (
                    member.startswith(IMAGES_PREFIX)
                    and member.endswith(IMAGE_EXTENSION)
                    and len(member) == len(IMAGES_PREFIX) + 32 + len(IMAGE_EXTENSION)
                    and all(char in "0123456789abcdef"
                            for char in member[len(IMAGES_PREFIX):-len(IMAGE_EXTENSION)])):
                raise StudioError("素材包包含未知文件",
                                  details={"member": member})
            total += self._member_size(member_info)
            if total > self.settings.max_bundle_expanded_bytes:
                raise StudioError("素材包解压后超过 64 MiB 限制",
                                  code="bundle_too_large")
        if MANIFEST_NAME not in seen:
            raise StudioError("素材包缺少 manifest.json")

    @staticmethod
    def _member_size(member_info):
        # 不信任压缩头之外的声明，但仍以其作为展开预算的第一道门槛。
        return max(member_info.file_size, 0)

    def _read_member(self, archive, member_name):
        try:
            with archive.open(member_name) as stream:
                buffer = io.BytesIO()
                total = 0
                while True:
                    chunk = stream.read(READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.settings.max_bundle_expanded_bytes:
                        raise StudioError("素材包解压后超过 64 MiB 限制",
                                          code="bundle_too_large")
                    buffer.write(chunk)
                return buffer.getvalue()
        except (zipfile.BadZipFile, OSError, RuntimeError) as error:
            if isinstance(error, StudioError):
                raise
            raise StudioError("素材包无法读取，可能已损坏或被加密") from error

    def _read_images(self, archive, manifest):
        members = {}
        expected = {
            f"{IMAGES_PREFIX}{asset['id']}{IMAGE_EXTENSION}"
            for asset in manifest["assets"]
        }
        actual = {member_info.filename for member_info in archive.infolist()
                  if member_info.filename != MANIFEST_NAME}
        missing = expected - actual
        if missing:
            raise StudioError("素材包缺少图片文件", details={"files": sorted(missing)})
        if actual - expected:
            raise StudioError("素材包包含未知文件",
                              details={"files": sorted(actual - expected)})
        for name in sorted(expected):
            members[name] = self._read_member(archive, name)
        return members

    def _parse_manifest(self, payload):
        try:
            manifest = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StudioError("manifest.json 不是合法 UTF-8 JSON") from error
        if not isinstance(manifest, dict):
            raise StudioError("manifest.json 必须是 JSON 对象")
        unknown = sorted(set(manifest) - {"version", "selected", "assets"})
        if unknown:
            raise StudioError("manifest.json 包含未知字段",
                              details={"fields": unknown})
        if manifest.get("version") != BUNDLE_VERSION:
            raise StudioError("素材包版本不受支持",
                              details={"version": manifest.get("version")})
        selected = manifest.get("selected")
        assets = manifest.get("assets")
        list_value(selected, "selected", minimum=1,
                   maximum=self.settings.max_bundle_assets)
        list_value(assets, "assets", minimum=1,
                   maximum=self.settings.max_bundle_assets)
        if len(set(selected)) != len(selected):
            raise StudioError("selected 包含重复 ID")
        for asset_id in selected:
            identifier(asset_id, "selected ID")
        return manifest

    def _validate_manifest(self, manifest, members):
        entries = {}
        for item in manifest["assets"]:
            if not isinstance(item, dict):
                raise StudioError("assets 中的每项必须是 JSON 对象")
            unknown = sorted(set(item) - MANIFEST_FIELDS)
            if unknown:
                raise StudioError("素材清单包含未知字段",
                                  details={"fields": unknown})
            missing = sorted(MANIFEST_FIELDS - set(item))
            if missing:
                raise StudioError("素材清单缺少必填字段",
                                  details={"fields": missing})
            asset_id = identifier(item["id"], "素材 ID")
            if asset_id in entries:
                raise StudioError("素材包包含重复 ID",
                                  details={"id": asset_id})
            asset_name = text_value(item["name"], "素材名称", 180, 1)
            parent_id = item["parent_id"]
            if parent_id is not None:
                identifier(parent_id, "parent_id")
            root_id = identifier(item["root_id"], "root_id")
            width = item["width"]
            height = item["height"]
            if isinstance(width, bool) or isinstance(height, bool) or \
                    not isinstance(width, int) or not isinstance(height, int):
                raise StudioError("width 和 height 必须是整数")
            dimension_guard(width, height, self.settings)
            digest = item["sha256"]
            if not isinstance(digest, str) or len(digest) != 64 or \
                    any(char not in "0123456789abcdef" for char in digest):
                raise StudioError("sha256 必须是 64 位小写十六进制摘要")
            expected_file = f"{IMAGES_PREFIX}{asset_id}{IMAGE_EXTENSION}"
            if item["file"] != expected_file:
                raise StudioError("file 字段与素材 ID 不匹配",
                                  details={"id": asset_id})
            self._validate_steps(item["steps"])
            favorite = boolean_value(item["favorite"], "favorite")
            tags = item["tags"]
            if not isinstance(tags, list) or len(tags) > MAX_TAGS:
                raise StudioError(f"标签必须是不超过 {MAX_TAGS} 项的数组")
            clean_tags = []
            for tag in tags:
                clean = text_value(tag, "标签", 30, 1)
                if clean not in clean_tags:
                    clean_tags.append(clean)
            png_bytes = members[expected_file]
            actual_digest = hashlib.sha256(png_bytes).hexdigest()
            if actual_digest != digest:
                raise StudioError("图片哈希与清单不一致",
                                  details={"id": asset_id})
            decoded = self._validate_png(png_bytes, asset_id, width, height)
            entries[asset_id] = {
                "id": asset_id,
                "name": asset_name,
                "parent_id": parent_id,
                "root_id": root_id,
                "width": width,
                "height": height,
                "sha256": digest,
                "steps": item["steps"],
                "favorite": favorite,
                "tags": clean_tags,
                "png": png_bytes,
                "image": decoded,
            }
        for selected_id in manifest["selected"]:
            if selected_id not in entries:
                raise StudioError("selected 引用了包内不存在的素材",
                                  details={"id": selected_id})
        return entries

    def _validate_steps(self, steps):
        if not isinstance(steps, list) or len(steps) > MAX_STEPS:
            raise StudioError(f"steps 必须是不超过 {MAX_STEPS} 项的数组")
        for step in steps:
            if not isinstance(step, dict):
                raise StudioError("每个处理步骤必须是 JSON 对象")
            operation = step.get("operation")
            if not isinstance(operation, str) or not operation.strip():
                raise StudioError("处理步骤缺少 operation")
            params = step.get("params")
            if not isinstance(params, dict):
                raise StudioError("处理步骤的 params 必须是 JSON 对象")
            unknown = sorted(set(step) - {"operation", "params"})
            if unknown:
                raise StudioError("处理步骤包含未知字段",
                                  details={"fields": unknown})

    def _validate_png(self, payload, asset_id, width, height):
        try:
            with Image.open(io.BytesIO(payload)) as image:
                if image.format != "PNG":
                    raise StudioError("素材包中的图片必须是 PNG",
                                      details={"id": asset_id})
                image.load()
                if image.size != (width, height):
                    raise StudioError(
                        "图片尺寸与清单不一致",
                        details={"id": asset_id,
                                 "actual": list(image.size),
                                 "manifest": [width, height]})
                return image.convert("RGBA")
        except StudioError:
            raise
        except (UnidentifiedImageError, OSError, ValueError,
                Image.DecompressionBombError) as error:
            raise StudioError("素材包包含无法解码的 PNG",
                              details={"id": asset_id}) from error

    def _validate_graph(self, entries):
        for asset_id, entry in entries.items():
            parent_id = entry["parent_id"]
            if parent_id is not None and parent_id not in entries:
                raise StudioError("素材包缺失父图",
                                  details={"id": asset_id,
                           "parent_id": parent_id})
            if entry["root_id"] not in entries:
                raise StudioError("素材包缺失根图",
                                  details={"id": asset_id,
                           "root_id": entry["root_id"]})
        # 环检测：所有 parent 都在包内，沿父链必须在有限步内抵达根。
        for asset_id, entry in entries.items():
            seen = set()
            current = asset_id
            while current is not None:
                if current in seen:
                    raise StudioError("素材包血缘存在环",
                                      details={"id": asset_id})
                seen.add(current)
                current = entries[current]["parent_id"]
        for asset_id, entry in entries.items():
            root_id = entry["root_id"]
            if entries[root_id]["parent_id"] is not None:
                raise StudioError("根素材自身不能有父图",
                                  details={"id": root_id})
            current = asset_id
            while entries[current]["parent_id"] is not None:
                current = entries[current]["parent_id"]
            if current != root_id:
                raise StudioError("root_id 与父链根节点不一致",
                                  details={"id": asset_id})

    def _restore(self, entries, selected):
        existing = {record["id"] for record in self.store.all("assets")}
        mapping = {}
        for old_id in entries:
            created_id = new_id()
            if created_id in existing or created_id in mapping.values():
                raise StudioError("生成的资产 ID 冲突，请重试",
                                  code="conflict", status=409)
            mapping[old_id] = created_id
        ordered = self._parent_first(list(entries), entries)
        created = []
        try:
            with self.assets.lock:
                for old_id in ordered:
                    entry = entries[old_id]
                    record = self.assets.restore(
                        new_id=mapping[old_id],
                        entry=entry,
                        new_parent_id=(mapping[entry["parent_id"]]
                                       if entry["parent_id"] else None),
                        new_root_id=mapping[entry["root_id"]],
                    )
                    created.append(record["id"])
        except Exception:
            for created_id in reversed(created):
                try:
                    self.store.delete("assets", created_id)
                except Exception:
                    pass
                self.assets._file(created_id).unlink(missing_ok=True)
                self.assets._file(created_id, True).unlink(missing_ok=True)
            raise
        mapped_selected = [mapping[old_id] for old_id in selected]
        self.store.event("bundle_import", "导入素材迁移包",
                         {"count": len(entries), "mapping": mapping,
                          "selected": mapped_selected})
        return {"mapping": mapping, "selected": mapped_selected}
