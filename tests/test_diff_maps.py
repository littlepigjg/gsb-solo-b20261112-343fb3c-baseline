"""POST /api/diff-maps: world-space placement, compositing and threshold rules."""
import io
import numpy as np
from PIL import Image


def upload(client, pixels, name="图层.png"):
    image = Image.fromarray(np.asarray(pixels, dtype=np.uint8), "RGBA")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def diff(client, first, second, **options):
    return client.post("/api/diff-maps", json={
        "first_id": first["id"],
        "second_id": second["id"],
        **options,
    })


def asset_pixels(client, asset):
    response = client.get(asset["url"])
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.data)) as image:
        assert image.format == "PNG"
        return np.asarray(image.convert("RGBA"))


def rgba(pixels):
    return np.asarray(pixels, dtype=np.uint8)


def test_diff_geometry_and_real_png(client):
    first_pixels = np.zeros((3, 4, 4), dtype=np.uint8)
    first_pixels[..., 3] = 255
    first_pixels[0, 0] = (255, 0, 0, 255)
    second_pixels = np.zeros((2, 3, 4), dtype=np.uint8)
    second_pixels[..., 3] = 255
    second_pixels[0, 1] = (255, 0, 0, 255)
    first = upload(client, first_pixels, "first.png")
    second = upload(client, second_pixels, "second.png")
    original = client.get(first["url"]).data

    response = diff(client, first, second, offset_x=2, offset_y=1, background="#000000")
    assert response.status_code == 201, response.json
    data = response.json["data"]
    assert data["origin"] == [0, 0]
    assert data["changed_pixels"] == 2
    assert data["changed_percent"] == 13.33
    assert data["bbox"] == [0, 0, 4, 2]
    assert data["max_difference"] == 255
    assert data["asset"]["width"] == 5 and data["asset"]["height"] == 3

    pixels = asset_pixels(client, data["asset"])
    assert tuple(pixels[0, 0]) == (255, 0, 0, 255)
    assert tuple(pixels[1, 3]) == (255, 0, 0, 255)
    assert pixels[..., 3].sum() == 2 * 255
    assert client.get(first["url"]).data == original


def test_transparent_hidden_rgb_is_ignored(client):
    first_pixels = rgba([[[255, 0, 0, 0], [0, 0, 0, 255]]])
    second_pixels = rgba([[[0, 255, 0, 0], [0, 0, 0, 255]]])
    first = upload(client, first_pixels)
    second = upload(client, second_pixels)
    response = diff(client, first, second)
    assert response.status_code == 201, response.json
    data = response.json["data"]
    assert data["changed_pixels"] == 0
    assert data["changed_percent"] == 0.0
    assert data["bbox"] is None
    assert data["max_difference"] == 0
    assert asset_pixels(client, data["asset"])[..., 3].sum() == 0


def test_tolerance_is_strictly_greater(client):
    first_pixels = rgba([[[0, 0, 0, 255], [0, 0, 0, 255]]])
    second_pixels = rgba([[[5, 0, 0, 255], [0, 0, 6, 255]]])
    first = upload(client, first_pixels)
    second = upload(client, second_pixels)
    response = diff(client, first, second, tolerance=5, background="#000000")
    data = response.json["data"]
    assert response.status_code == 201, response.json
    assert data["changed_pixels"] == 1
    assert data["bbox"] == [1, 0, 2, 1]
    assert data["max_difference"] == 6


def test_negative_offset_and_different_sizes(client):
    first_pixels = np.zeros((2, 3, 4), dtype=np.uint8)
    first_pixels[..., 3] = 255
    first_pixels[0, 1] = (255, 0, 0, 255)
    second_pixels = np.zeros((3, 2, 4), dtype=np.uint8)
    second_pixels[..., 3] = 255
    second_pixels[1, 0] = (255, 0, 0, 255)
    first = upload(client, first_pixels)
    second = upload(client, second_pixels)
    response = diff(client, first, second, offset_x=-2, offset_y=-1, background="#000000")
    assert response.status_code == 201, response.json
    data = response.json["data"]
    assert data["origin"] == [-2, -1]
    assert data["asset"]["width"] == 5 and data["asset"]["height"] == 3
    assert data["changed_pixels"] == 2
    assert data["bbox"] == [0, 1, 4, 2]
    pixels = asset_pixels(client, data["asset"])
    assert tuple(pixels[1, 3]) == (255, 0, 0, 255)
    assert tuple(pixels[1, 0]) == (255, 0, 0, 255)


def test_alpha_compositing_uses_background(client):
    first = upload(client, rgba([[[255, 0, 0, 128]]]))
    second = upload(client, rgba([[[0, 255, 0, 0]]]))
    response = diff(client, first, second, background="#0000ff")
    assert response.status_code == 201, response.json
    data = response.json["data"]
    # Red is half-blended over blue: (128, 0, 128); the fully transparent
    # second pixel is pure blue. Max channel distance is 128.
    assert data["max_difference"] == 128
    assert data["changed_pixels"] == 1
    assert data["bbox"] == [0, 0, 1, 1]


def test_invalid_requests_create_no_asset(client):
    first = upload(client, rgba([[[0, 0, 0, 255]]]))
    second = upload(client, rgba([[[0, 0, 0, 255]]]))
    before = client.get("/api/assets").json["data"]["total"]
    bad = [
        {"first_id": first["id"]},
        {"first_id": first["id"], "second_id": "not-an-id"},
        {"first_id": first["id"], "second_id": second["id"], "offset_x": 5000},
        {"first_id": first["id"], "second_id": second["id"], "offset_y": -4097},
        {"first_id": first["id"], "second_id": second["id"], "tolerance": 1.5},
        {"first_id": first["id"], "second_id": second["id"], "tolerance": 256},
        {"first_id": first["id"], "second_id": second["id"], "background": "red"},
        {"first_id": first["id"], "second_id": second["id"], "extra": True},
        {"first_id": "0" * 32, "second_id": second["id"]},
    ]
    for request in bad:
        response = client.post("/api/diff-maps", json=request)
        assert 400 <= response.status_code < 500, request
        assert response.json["ok"] is False
    assert client.get("/api/assets").json["data"]["total"] == before


def test_canvas_respects_dimension_limit(client):
    first_pixels = np.zeros((1, 6000, 4), dtype=np.uint8)
    first_pixels[..., 3] = 255
    second_pixels = np.zeros((1, 6000, 4), dtype=np.uint8)
    second_pixels[..., 3] = 255
    first = upload(client, first_pixels)
    second = upload(client, second_pixels)
    before = client.get("/api/assets").json["data"]["total"]
    response = diff(client, first, second, offset_x=4096)
    assert response.status_code == 400, response.json
    assert "单边" in response.json["error"]["message"]
    assert client.get("/api/assets").json["data"]["total"] == before
