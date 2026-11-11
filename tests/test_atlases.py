"""Tests for deterministic PNG sprite-atlas packing."""
import io

import numpy as np
import pytest
from PIL import Image


def upload(client, image, name):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def atlas_image(client, data):
    payload = client.get(data["asset"]["url"]).data
    return Image.open(io.BytesIO(payload)).convert("RGBA")


def source_image(client, sprite):
    payload = client.get(f"/api/assets/{sprite['asset_id']}/file").data
    return Image.open(io.BytesIO(payload)).convert("RGBA")


def visible_box(width, height, left, top, right, bottom, color=(220, 30, 40, 255)):
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = image.load()
    for y in range(top, bottom):
        for x in range(left, right):
            pixels[x, y] = color
    return image


def test_trim_shelf_layout_reconstructs_every_sprite(client):
    first = upload(client, visible_box(10, 6, 2, 1, 8, 5), "第一张.png")
    second = upload(client, visible_box(10, 3, 0, 0, 10, 3, (20, 180, 90, 255)), "第二张.png")
    transparent = upload(client, Image.new("RGBA", (5, 4), (7, 8, 9, 0)), "透明.png")

    response = client.post("/api/atlases", json={
        "items": [
            {"asset_id": first["id"], "name": "a"},
            {"asset_id": second["id"], "name": "b"},
            {"asset_id": transparent["id"], "name": "empty"},
        ],
        "max_width": 19,
        "max_height": 40,
        "padding": 1,
        "trim": True,
        "extrude": False,
    })
    assert response.status_code == 201, response.json
    data = response.json["data"]
    atlas = atlas_image(client, data)
    # First shelf is 8x6 with padding; the second 12x5 tile wraps to y=6.
    assert atlas.size == (19, 11)

    assert data["sprites"][0]["rect"] == [1, 1, 6, 4]
    assert data["sprites"][0]["source_rect"] == [2, 1, 6, 4]
    assert data["sprites"][0]["source_size"] == [10, 6]
    assert data["sprites"][1]["rect"] == [1, 7, 10, 3]
    assert data["sprites"][1]["source_rect"] == [0, 0, 10, 3]
    assert data["sprites"][2]["rect"] == [13, 7, 1, 1]
    assert data["sprites"][2]["source_rect"] == [0, 0, 0, 0]
    assert data["sprites"][2]["empty"] is True
    assert [sprite["name"] for sprite in data["sprites"]] == ["a", "b", "empty"]

    for sprite in data["sprites"]:
        if sprite["empty"]:
            continue
        rect = sprite["rect"]
        source_rect = sprite["source_rect"]
        packed = np.asarray(atlas.crop((rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])))
        source = np.asarray(source_image(client, sprite).crop((
            source_rect[0], source_rect[1],
            source_rect[0] + source_rect[2], source_rect[1] + source_rect[3],
        )))
        assert np.array_equal(packed, source)

    # padding is transparent when extrusion is off
    assert atlas.getpixel((0, 0))[3] == 0
    assert atlas.getpixel((8, 0))[3] == 0


def test_trim_false_keeps_full_canvas_and_duplicate_asset_names(client):
    image = visible_box(8, 5, 2, 1, 6, 4)
    asset = upload(client, image, "原图.png")
    response = client.post("/api/atlases", json={
        "items": [
            {"asset_id": asset["id"], "name": "same-one"},
            {"asset_id": asset["id"], "name": "same-two"},
        ],
        "max_width": 16,
        "max_height": 8,
        "padding": 0,
        "trim": False,
        "extrude": False,
    })
    assert response.status_code == 201, response.json
    data = response.json["data"]
    atlas = atlas_image(client, data)
    assert atlas.size == (16, 5)
    assert data["sprites"][0]["rect"] == [0, 0, 8, 5]
    assert data["sprites"][1]["rect"] == [8, 0, 8, 5]
    for sprite in data["sprites"]:
        assert sprite["source_rect"] == [0, 0, 8, 5]
        assert sprite["empty"] is False
        rect = sprite["rect"]
        packed = np.asarray(atlas.crop((rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])))
        assert np.array_equal(packed, np.asarray(image))


def test_fully_transparent_sprite_has_zero_source_rect_in_both_modes(client):
    asset = upload(client, Image.new("RGBA", (6, 5), (1, 2, 3, 0)), "全透明.png")
    for trim, expected_size in ((True, 1), (False, 6)):
        response = client.post("/api/atlases", json={
            "items": [{"asset_id": asset["id"], "name": "clear"}],
            "max_width": 8, "max_height": 8, "padding": 0, "trim": trim,
        })
        assert response.status_code == 201, response.json
        sprite = response.json["data"]["sprites"][0]
        assert sprite["empty"] is True
        assert sprite["source_rect"] == [0, 0, 0, 0]
        assert sprite["source_size"] == [6, 5]
        assert sprite["rect"] == [0, 0, expected_size, 1 if trim else 5]


def test_extrude_copies_nearest_boundary_pixels_including_alpha(client):
    image = Image.new("RGBA", (4, 3), (0, 0, 0, 0))
    pixels = image.load()
    for y in range(3):
        for x in range(4):
            pixels[x, y] = (x * 20, y * 30, 50, 255 if (x + y) % 2 else 180)
    asset = upload(client, image, "边缘.png")
    response = client.post("/api/atlases", json={
        "items": [{"asset_id": asset["id"], "name": "edge"}],
        "max_width": 10,
        "max_height": 9,
        "padding": 3,
        "trim": False,
        "extrude": True,
    })
    assert response.status_code == 201, response.json
    data = response.json["data"]
    atlas = atlas_image(client, data)
    assert atlas.size == (10, 9)
    rect = data["sprites"][0]["rect"]
    assert rect == [3, 3, 4, 3]
    assert atlas.getpixel((0, 0)) == image.getpixel((0, 0))
    assert atlas.getpixel((atlas.width - 1, atlas.height - 1)) == image.getpixel((3, 2))
    # Every extruded pixel is one of the boundary source pixels, alpha included.
    for y in range(atlas.height):
        for x in range(atlas.width):
            source_x = min(max(x - rect[0], 0), rect[2] - 1)
            source_y = min(max(y - rect[1], 0), rect[3] - 1)
            assert atlas.getpixel((x, y)) == image.getpixel((source_x, source_y))


@pytest.mark.parametrize("payload", [
    {},
    {"items": []},
    {"items": [{"asset_id": "0" * 32, "name": ""}]},
    {"items": [{"asset_id": "0" * 32, "name": "x"}, {"asset_id": "0" * 32, "name": "x"}]},
    {"items": [{"asset_id": "0" * 32, "name": "x", "extra": 1}]},
    {"items": [{"asset_id": "bad-id", "name": "x"}]},
    {"items": [{"asset_id": "0" * 32, "name": "x"}], "max_width": 7},
    {"items": [{"asset_id": "0" * 32, "name": "x"}], "max_width": 4097},
    {"items": [{"asset_id": "0" * 32, "name": "x"}], "max_width": "8"},
    {"items": [{"asset_id": "0" * 32, "name": "x"}], "max_height": 4097},
    {"items": [{"asset_id": "0" * 32, "name": "x"}], "padding": 17},
    {"items": [{"asset_id": "0" * 32, "name": "x"}], "trim": "true"},
    {"items": [{"asset_id": "0" * 32, "name": "x"}], "extrude": 1},
])
def test_invalid_atlas_requests_are_rejected(client, payload):
    response = client.post("/api/atlases", json=payload)
    assert response.status_code == 400, response.json
    assert response.json["ok"] is False


def test_too_many_items_rejected(client):
    payload = {"items": [{"asset_id": "0" * 32, "name": f"n{index}"} for index in range(25)]}
    response = client.post("/api/atlases", json=payload)
    assert response.status_code == 400
    assert response.json["ok"] is False


def test_layout_failure_leaves_assets_and_sources_unchanged(client):
    image = visible_box(40, 30, 0, 0, 40, 30)
    asset = upload(client, image, "不可变.png")
    before_total = client.get("/api/assets").json["data"]["total"]
    before_file = client.get(asset["url"]).data

    for payload in (
        {"items": [{"asset_id": asset["id"], "name": "a"}], "max_width": 8, "max_height": 8},
        {"items": [{"asset_id": asset["id"], "name": "a"}], "max_width": 4096, "max_height": 8},
        {"items": [{"asset_id": asset["id"], "name": "a"}, {"asset_id": asset["id"], "name": "b"}],
         "max_width": 41, "max_height": 31, "padding": 0},
    ):
        response = client.post("/api/atlases", json=payload)
        assert response.status_code == 400, response.json
        assert client.get("/api/assets").json["data"]["total"] == before_total
        assert client.get(asset["url"]).data == before_file


def test_missing_asset_is_not_created(client, uploaded):
    before_total = client.get("/api/assets").json["data"]["total"]
    response = client.post("/api/atlases", json={
        "items": [
            {"asset_id": uploaded["id"], "name": "ok"},
            {"asset_id": "f" * 32, "name": "missing"},
        ],
        "max_width": 512, "max_height": 512,
    })
    assert response.status_code == 404
    assert client.get("/api/assets").json["data"]["total"] == before_total