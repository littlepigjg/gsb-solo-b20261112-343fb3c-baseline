"""可迁移素材包的导出、导入与安全校验测试。"""
import hashlib
import io
import json
import zipfile
import pytest
from PIL import Image
from backend.app import create_app


def export_zip(client, asset_ids, expected=201):
    response = client.post("/api/bundles/export", json={"asset_ids": asset_ids})
    assert response.status_code == expected, response.json
    if expected != 201:
        return response
    record = response.json["data"]
    assert record["kind"] == "bundle"
    download = client.get(record["url"])
    assert download.status_code == 200
    assert download.mimetype == "application/zip"
    assert "attachment" in download.headers["Content-Disposition"]
    return download.data


def read_bundle(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert "manifest.json" in names
        manifest = json.loads(archive.read("manifest.json"))
        files = {member: archive.read(member) for member in names}
    return manifest, files, names


def process_child(client, parent_id, name=None):
    payload = {"asset_id": parent_id,
               "steps": [{"operation": "color_invert", "params": {}}]}
    if name:
        payload["name"] = name
    response = client.post("/api/process", json=payload)
    assert response.status_code == 201, response.json
    return response.json["data"]


def test_export_bundle_shape_and_manifest(client, uploaded):
    blob = export_zip(client, [uploaded["id"]])
    manifest, files, names = read_bundle(blob)
    assert set(names) == {"manifest.json", f"images/{uploaded['id']}.png"}
    assert manifest["version"] == 1
    assert manifest["selected"] == [uploaded["id"]]
    entry = manifest["assets"][0]
    assert set(entry) == {"id", "name", "parent_id", "root_id", "width",
                          "height", "sha256", "file", "steps", "favorite",
                          "tags"}
    assert entry["id"] == uploaded["id"]
    assert entry["name"] == "测试原图.png"
    assert entry["parent_id"] is None
    assert entry["root_id"] == uploaded["id"]
    assert entry["file"] == f"images/{uploaded['id']}.png"
    png = files[f"images/{uploaded['id']}.png"]
    assert hashlib.sha256(png).hexdigest() == entry["sha256"]
    assert png == client.get(uploaded["url"]).data
    with Image.open(io.BytesIO(png)) as image:
        assert image.format == "PNG"
        assert image.size == (entry["width"], entry["height"])


def test_export_auto_includes_ancestors_parent_first(client, uploaded):
    child = process_child(client, uploaded["id"], "副本 壹")
    grandchild = process_child(client, child["id"], "副本 贰")
    blob = export_zip(client, [grandchild["id"]])
    manifest, _, names = read_bundle(blob)
    ids = [item["id"] for item in manifest["assets"]]
    assert ids == [uploaded["id"], child["id"], grandchild["id"]]
    assert manifest["selected"] == [grandchild["id"]]
    assert set(names) == {"manifest.json"} | {f"images/{id_}.png" for id_ in ids}


def test_export_deduplicates_shared_ancestors_and_preserves_order(client, uploaded):
    child_a = process_child(client, uploaded["id"])
    child_b = process_child(client, uploaded["id"])
    blob = export_zip(client, [child_b["id"], child_a["id"], uploaded["id"]])
    manifest, _, _ = read_bundle(blob)
    ids = [item["id"] for item in manifest["assets"]]
    assert len(ids) == 3
    assert set(ids) == {uploaded["id"], child_a["id"], child_b["id"]}
    assert ids[0] == uploaded["id"]
    assert manifest["selected"] == [child_b["id"], child_a["id"], uploaded["id"]]


def test_export_rejects_unknown_and_empty_selection(client, uploaded):
    response = client.post("/api/bundles/export", json={"asset_ids": ["0" * 32]})
    assert response.status_code == 404
    assert response.json["ok"] is False
    response = client.post("/api/bundles/export", json={"asset_ids": []})
    assert response.status_code == 400


def test_export_rejects_closure_over_24(client, uploaded):
    current = uploaded
    for _ in range(24):
        current = process_child(client, current["id"])
    # 链上已有 25 个资产（含原图），闭包超限。
    response = client.post("/api/bundles/export", json={"asset_ids": [current["id"]]})
    assert response.status_code == 400
    assert response.json["error"]["code"] == "bundle_too_large"


def test_export_accepts_exactly_24_assets(client, uploaded):
    current = uploaded
    for _ in range(23):
        current = process_child(client, current["id"])
    blob = export_zip(client, [current["id"]])
    manifest, _, _ = read_bundle(blob)
    assert len(manifest["assets"]) == 24
    # 24 项的合法包可以重新导入。
    response = import_blob(client, blob)
    assert response.status_code == 201
    assert len(response.json["data"]["mapping"]) == 24


def test_round_trip_restores_relations_names_steps_flags_tags(client, uploaded):
    patched = client.patch(f"/api/assets/{uploaded['id']}", json={
        "name": "中文原图 ✓", "favorite": True, "tags": ["作品集", "风景"]})
    assert patched.status_code == 200
    child = process_child(client, uploaded["id"], "中文副本 ✓")
    client.patch(f"/api/assets/{child['id']}", json={
        "favorite": True, "tags": ["夜景"]})
    blob = export_zip(client, [child["id"]])

    before = client.get("/api/assets").json["data"]["total"]
    response = client.post("/api/bundles/import",
                           data={"file": (io.BytesIO(blob), "bundle.zip")},
                           content_type="multipart/form-data")
    assert response.status_code == 201, response.json
    result = response.json["data"]
    assert set(result) == {"mapping", "selected"}
    mapping = result["mapping"]
    assert set(mapping) == {uploaded["id"], child["id"]}
    assert len(set(mapping.values())) == 2
    assert result["selected"] == [mapping[child["id"]]]

    # 原资产保持不变。
    assert client.get("/api/assets").json["data"]["total"] == before + 2
    original = client.get(f"/api/assets/{uploaded['id']}").json["data"]
    assert original["name"] == "中文原图 ✓"

    new_root = client.get(f"/api/assets/{mapping[uploaded['id']]}").json["data"]
    new_child = client.get(f"/api/assets/{mapping[child['id']]}").json["data"]
    assert new_root["name"] == "中文原图 ✓"
    assert new_root["favorite"] is True
    assert new_root["tags"] == ["作品集", "风景"]
    assert new_root["parent_id"] is None
    assert new_root["root_id"] == new_root["id"]
    assert new_child["name"] == "中文副本 ✓"
    assert new_child["parent_id"] == new_root["id"]
    assert new_child["root_id"] == new_root["id"]
    assert new_child["steps"] == child["steps"]
    assert new_child["favorite"] is True
    assert new_child["tags"] == ["夜景"]

    # 哈希与像素字节完全一致，历史链完整。
    old_bytes = client.get(child["url"]).data
    new_bytes = client.get(new_child["url"]).data
    assert hashlib.sha256(new_bytes).hexdigest() == new_child["sha256"]
    assert new_bytes == old_bytes
    history = client.get(f"/api/assets/{mapping[child['id']]}/history").json["data"]
    assert [item["id"] for item in history] == [new_root["id"], new_child["id"]]


def test_import_second_copy_never_overwrites(client, uploaded):
    blob = export_zip(client, [uploaded["id"]])
    first = client.post("/api/bundles/import",
                        data={"file": (io.BytesIO(blob), "a.zip")},
                        content_type="multipart/form-data")
    assert first.status_code == 201
    second = client.post("/api/bundles/import",
                         data={"file": (io.BytesIO(blob), "b.zip")},
                         content_type="multipart/form-data")
    assert second.status_code == 201
    assert first.json["data"]["mapping"] != second.json["data"]["mapping"]
    total = client.get("/api/assets").json["data"]["total"]
    assert total == 3
    # 原始资产仍可读取且未被覆盖。
    assert client.get(f"/api/assets/{uploaded['id']}").status_code == 200


def test_import_requires_file_field(client):
    response = client.post("/api/bundles/import", data={},
                           content_type="multipart/form-data")
    assert response.status_code == 400


def test_import_rejects_non_zip(client):
    response = client.post("/api/bundles/import",
                           data={"file": (io.BytesIO(b"not a zip"), "x.zip")},
                           content_type="multipart/form-data")
    assert response.status_code == 400


def build_bundle(manifest, images, extras=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name, payload in images.items():
            archive.writestr(name, payload)
        for name, payload in (extras or {}).items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def png_bytes(size=(8, 8), color=(10, 20, 30, 255)):
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def valid_manifest(asset_id, payload, **overrides):
    entry = {
        "id": asset_id,
        "name": "素材",
        "parent_id": None,
        "root_id": asset_id,
        "width": 8,
        "height": 8,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "file": f"images/{asset_id}.png",
        "steps": [],
        "favorite": False,
        "tags": [],
    }
    entry.update(overrides)
    return {"version": 1, "selected": [asset_id], "assets": [entry]}


def import_blob(client, blob):
    return client.post("/api/bundles/import",
                       data={"file": (io.BytesIO(blob), "bundle.zip")},
                       content_type="multipart/form-data")


def test_import_accepts_valid_minimal_bundle(client):
    asset_id = "a" * 32
    payload = png_bytes()
    blob = build_bundle(valid_manifest(asset_id, payload),
                        {f"images/{asset_id}.png": payload})
    response = import_blob(client, blob)
    assert response.status_code == 201, response.json
    assert response.json["data"]["mapping"][asset_id]


@pytest.mark.parametrize("bad_name", [
    "../escape.png",
    "images/../escape.png",
    "/absolute.png",
    "images/sub/deep.png",
    "images/nothex.png",
    "readme.txt",
    "images/extra.png",
])
def test_import_rejects_illegal_member_names(client, bad_name):
    asset_id = "a" * 32
    payload = png_bytes()
    manifest = valid_manifest(asset_id, payload)
    blob = build_bundle(manifest, {f"images/{asset_id}.png": payload},
                        extras={bad_name: b"x"})
    response = import_blob(client, blob)
    assert response.status_code == 400
    assert client.get("/api/assets").json["data"]["total"] == 0


def test_import_rejects_backslash_and_absolute_member(client):
    asset_id = "a" * 32
    payload = png_bytes()
    manifest = valid_manifest(asset_id, payload)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("images\\bad.png", payload)
    response = import_blob(client, buffer.getvalue())
    assert response.status_code == 400


def test_import_rejects_duplicate_members(client):
    asset_id = "a" * 32
    payload = png_bytes()
    manifest = valid_manifest(asset_id, payload)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(f"images/{asset_id}.png", payload)
        archive.writestr(f"images/{asset_id}.png", payload)
    response = import_blob(client, buffer.getvalue())
    assert response.status_code == 400


def test_import_rejects_symlink_member(client):
    asset_id = "a" * 32
    payload = png_bytes()
    manifest = valid_manifest(asset_id, payload)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(f"images/{asset_id}.png", payload)
        info = zipfile.ZipInfo("images/link.png")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, "target")
    response = import_blob(client, buffer.getvalue())
    assert response.status_code == 400


def test_import_rejects_encrypted_flag_member(client):
    import struct
    asset_id = "a" * 32
    payload = png_bytes()
    manifest = valid_manifest(asset_id, payload)
    blob = bytearray(build_bundle(manifest, {f"images/{asset_id}.png": payload}))
    target = f"images/{asset_id}.png"
    eocd = bytes(blob).rfind(b"PK\x05\x06")
    central = struct.unpack_from("<I", blob, eocd + 16)[0]
    cursor = central
    while bytes(blob[cursor:cursor + 4]) == b"PK\x01\x02":
        name_length = struct.unpack_from("<H", blob, cursor + 28)[0]
        extra_length = struct.unpack_from("<H", blob, cursor + 30)[0]
        comment_length = struct.unpack_from("<H", blob, cursor + 32)[0]
        member = bytes(blob[cursor + 46:cursor + 46 + name_length]).decode()
        if member == target:
            flags = struct.unpack_from("<H", blob, cursor + 8)[0]
            struct.pack_into("<H", blob, cursor + 8, flags | 0x1)
            break
        cursor += 46 + name_length + extra_length + comment_length
    response = import_blob(client, bytes(blob))
    assert response.status_code == 400
    assert client.get("/api/assets").json["data"]["total"] == 0


def test_import_rejects_directory_member(client):
    asset_id = "a" * 32
    payload = png_bytes()
    manifest = valid_manifest(asset_id, payload)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(f"images/{asset_id}.png", payload)
        archive.writestr(zipfile.ZipInfo("images/folder/"), b"")
    response = import_blob(client, buffer.getvalue())
    assert response.status_code == 400


def test_import_rejects_wrong_version(client):
    asset_id = "a" * 32
    payload = png_bytes()
    manifest = valid_manifest(asset_id, payload)
    manifest["version"] = 2
    blob = build_bundle(manifest, {f"images/{asset_id}.png": payload})
    response = import_blob(client, blob)
    assert response.status_code == 400


def test_import_rejects_bad_hash(client):
    asset_id = "a" * 32
    payload = png_bytes()
    manifest = valid_manifest(asset_id, payload)
    manifest["assets"][0]["sha256"] = "f" * 64
    blob = build_bundle(manifest, {f"images/{asset_id}.png": payload})
    response = import_blob(client, blob)
    assert response.status_code == 400


def test_import_rejects_dimension_mismatch(client):
    asset_id = "a" * 32
    payload = png_bytes(size=(8, 8))
    manifest = valid_manifest(asset_id, payload, width=16, height=16)
    blob = build_bundle(manifest, {f"images/{asset_id}.png": payload})
    response = import_blob(client, blob)
    assert response.status_code == 400


def test_import_rejects_non_png_content(client):
    asset_id = "a" * 32
    payload = b"GIF89a" + b"\x00" * 16
    manifest = valid_manifest(asset_id, payload)
    manifest["assets"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
    blob = build_bundle(manifest, {f"images/{asset_id}.png": payload})
    response = import_blob(client, blob)
    assert response.status_code == 400


def test_import_rejects_duplicate_ids(client):
    asset_id = "a" * 32
    payload = png_bytes()
    entry = valid_manifest(asset_id, payload)["assets"][0]
    manifest = {"version": 1, "selected": [asset_id],
                "assets": [entry, dict(entry)]}
    blob = build_bundle(manifest, {f"images/{asset_id}.png": payload})
    response = import_blob(client, blob)
    assert response.status_code == 400


def test_import_rejects_missing_parent(client):
    parent_id = "b" * 32
    child_id = "c" * 32
    parent_png = png_bytes(color=(1, 2, 3, 255))
    child_png = png_bytes(color=(4, 5, 6, 255))
    child_entry = {
        "id": child_id, "name": "子", "parent_id": parent_id,
        "root_id": parent_id, "width": 8, "height": 8,
        "sha256": hashlib.sha256(child_png).hexdigest(),
        "file": f"images/{child_id}.png", "steps": [],
        "favorite": False, "tags": [],
    }
    manifest = {"version": 1, "selected": [child_id], "assets": [child_entry]}
    blob = build_bundle(manifest, {f"images/{child_id}.png": child_png})
    response = import_blob(client, blob)
    assert response.status_code == 400


def test_import_rejects_cycle(client):
    first_id = "1" * 32
    second_id = "2" * 32
    first_png = png_bytes(color=(1, 1, 1, 255))
    second_png = png_bytes(color=(2, 2, 2, 255))
    first_entry = {
        "id": first_id, "name": "甲", "parent_id": second_id,
        "root_id": first_id, "width": 8, "height": 8,
        "sha256": hashlib.sha256(first_png).hexdigest(),
        "file": f"images/{first_id}.png", "steps": [],
        "favorite": False, "tags": [],
    }
    second_entry = {
        "id": second_id, "name": "乙", "parent_id": first_id,
        "root_id": first_id, "width": 8, "height": 8,
        "sha256": hashlib.sha256(second_png).hexdigest(),
        "file": f"images/{second_id}.png", "steps": [],
        "favorite": False, "tags": [],
    }
    manifest = {"version": 1, "selected": [first_id],
                "assets": [first_entry, second_entry]}
    blob = build_bundle(manifest, {
        f"images/{first_id}.png": first_png,
        f"images/{second_id}.png": second_png,
    })
    response = import_blob(client, blob)
    assert response.status_code == 400


def test_import_rejects_wrong_root_relation(client):
    root_id = "d" * 32
    child_id = "e" * 32
    other_id = "f" * 32
    root_png = png_bytes(color=(9, 9, 9, 255))
    child_png = png_bytes(color=(8, 8, 8, 255))
    other_png = png_bytes(color=(7, 7, 7, 255))

    def entry(asset_id, name, parent, root, payload):
        return {
            "id": asset_id, "name": name, "parent_id": parent,
            "root_id": root, "width": 8, "height": 8,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "file": f"images/{asset_id}.png", "steps": [],
            "favorite": False, "tags": [],
        }

    manifest = {
        "version": 1, "selected": [child_id],
        "assets": [
            entry(root_id, "根", None, root_id, root_png),
            entry(child_id, "子", root_id, other_id, child_png),
            entry(other_id, "另", None, other_id, other_png),
        ],
    }
    blob = build_bundle(manifest, {
        f"images/{root_id}.png": root_png,
        f"images/{child_id}.png": child_png,
        f"images/{other_id}.png": other_png,
    })
    response = import_blob(client, blob)
    assert response.status_code == 400


def test_bad_bundle_creates_no_records_or_files(app, client):
    asset_id = "a" * 32
    payload = png_bytes()
    manifest = valid_manifest(asset_id, payload)
    manifest["assets"][0]["sha256"] = "0" * 64
    blob = build_bundle(manifest, {f"images/{asset_id}.png": payload})
    response = import_blob(client, blob)
    assert response.status_code == 400
    settings = app.extensions["studio"]["settings"]
    assert client.get("/api/assets").json["data"]["total"] == 0
    assert list(settings.media.glob("*.png")) == []
    diagnostics = client.get("/api/diagnostics").json["data"]
    assert diagnostics["orphan_media_count"] == 0


def test_import_rejects_expanded_over_64mib(app, client):
    asset_id = "a" * 32
    big_payload = b"\x00" * (64 * 1024 * 1024 + 1)
    manifest = valid_manifest(asset_id, big_payload)
    manifest["assets"][0]["sha256"] = hashlib.sha256(big_payload).hexdigest()
    blob = build_bundle(manifest, {f"images/{asset_id}.png": big_payload})
    # Flask 层 MAX_CONTENT_LENGTH 为 25 MiB，超限同样拒绝且不落库。
    response = import_blob(client, blob)
    assert response.status_code in (400, 413)
    assert client.get("/api/assets").json["data"]["total"] == 0


def test_import_rejects_zip_bomb_via_declared_size(app, client):
    import struct
    asset_id = "a" * 32
    payload = png_bytes()
    manifest = valid_manifest(asset_id, payload)
    blob = bytearray(build_bundle(manifest, {f"images/{asset_id}.png": payload}))
    # 仅篡改中央目录中图片条目的未压缩尺寸字段（infolist 读取该字段）。
    bogus = 65 * 1024 * 1024
    target = f"images/{asset_id}.png"
    eocd = bytes(blob).rfind(b"PK\x05\x06")
    central_dir_offset = struct.unpack_from("<I", blob, eocd + 16)[0]
    cursor = central_dir_offset
    while bytes(blob[cursor:cursor + 4]) == b"PK\x01\x02":
        name_length = struct.unpack_from("<H", blob, cursor + 28)[0]
        extra_length = struct.unpack_from("<H", blob, cursor + 30)[0]
        comment_length = struct.unpack_from("<H", blob, cursor + 32)[0]
        member_name = bytes(blob[cursor + 46:cursor + 46 + name_length]).decode()
        if member_name == target:
            struct.pack_into("<I", blob, cursor + 24, bogus)
            break
        cursor += 46 + name_length + extra_length + comment_length
    response = import_blob(client, bytes(blob))
    assert response.status_code == 400
    assert response.json["error"]["code"] == "bundle_too_large"


def test_round_trip_survives_restart(app, client, uploaded):
    child = process_child(client, uploaded["id"])
    blob = export_zip(client, [child["id"]])
    response = import_blob(client, blob)
    assert response.status_code == 201
    mapping = response.json["data"]["mapping"]
    root = app.extensions["studio"]["settings"].root
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(root, testing=True)
    try:
        data = second.test_client().get(
            f"/api/assets/{mapping[child['id']]}").json["data"]
        assert data["parent_id"] == mapping[uploaded["id"]]
        assert data["root_id"] == mapping[uploaded["id"]]
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_bundle_export_then_import_keeps_shared_ancestor(client, uploaded):
    child_a = process_child(client, uploaded["id"])
    child_b = process_child(client, uploaded["id"])
    blob = export_zip(client, [child_a["id"], child_b["id"]])
    response = import_blob(client, blob)
    assert response.status_code == 201
    mapping = response.json["data"]["mapping"]
    new_a = client.get(f"/api/assets/{mapping[child_a['id']]}").json["data"]
    new_b = client.get(f"/api/assets/{mapping[child_b['id']]}").json["data"]
    assert new_a["root_id"] == new_b["root_id"] == mapping[uploaded["id"]]
    assert new_a["parent_id"] == new_b["parent_id"] == mapping[uploaded["id"]]
    assert response.json["data"]["selected"] == [
        mapping[child_a["id"]], mapping[child_b["id"]]]


def test_export_rejects_unknown_fields(client, uploaded):
    response = client.post("/api/bundles/export",
                           json={"asset_ids": [uploaded["id"]], "extra": 1})
    assert response.status_code == 400
