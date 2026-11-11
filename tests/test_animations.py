import io
import numpy as np
import pytest
from PIL import Image

from backend.app import create_app


def upload(client, image, name="frame.png"):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def solid_rgba(size, rgb, alpha=255):
    image = Image.new("RGBA", size, rgb + (alpha,))
    return image


def gif_bytes(client, payload):
    response = client.post("/api/animations/gif", json=payload)
    assert response.status_code == 201, response.json
    body = response.json["data"]
    record = body["export"]
    download = client.get(record["url"])
    assert download.status_code == 200
    assert download.mimetype == "image/gif"
    assert "attachment" in download.headers["Content-Disposition"]
    return body, io.BytesIO(download.data)


def open_gif(buffer):
    image = Image.open(buffer)
    assert image.format == "GIF"
    return image


def base_payload(first_id, second_id=None, **overrides):
    second_id = second_id or first_id
    payload = {
        "frames": [
            {"asset_id": first_id, "duration_ms": 100},
            {"asset_id": second_id, "duration_ms": 250},
        ],
        "width": 64,
        "height": 48,
        "background": "#000000",
        "loop": 0,
    }
    payload.update(overrides)
    return payload


def test_gif_happy_path_canvas_loop_timing_and_response(client, uploaded):
    other = upload(client, solid_rgba((16, 16), (0, 128, 255)), name="other.png")
    body, buffer = gif_bytes(client, base_payload(uploaded["id"], other["id"]))
    assert body["frame_count"] == 2
    assert body["total_duration_ms"] == 350
    export = body["export"]
    assert export["kind"] == "animation"
    assert export["options"]["format"] == "GIF"
    assert export["bytes"] > 0
    assert export["download_name"].endswith(".gif")
    assert export["asset_ids"] == [uploaded["id"], other["id"]]
    image = open_gif(buffer)
    assert image.size == (64, 48)
    assert image.n_frames == 2
    assert image.info["loop"] == 0
    image.seek(0)
    assert image.info["duration"] == 100
    image.seek(1)
    assert image.info["duration"] == 250


def test_gif_loop_count_round_trips(client, uploaded):
    body, buffer = gif_bytes(client, base_payload(uploaded["id"], loop=7))
    assert open_gif(buffer).info["loop"] == 7
    body, buffer = gif_bytes(client, base_payload(uploaded["id"], loop=65535))
    assert open_gif(buffer).info["loop"] == 65535


def test_gif_centered_lanczos_contain_with_upscale(client):
    small = upload(client, solid_rgba((10, 20), (255, 0, 0)))
    body, buffer = gif_bytes(client, base_payload(small["id"], width=100, height=100,
                                                  background="#ffffff"))
    image = open_gif(buffer)
    image.seek(0)
    pixels = np.asarray(image.convert("RGB"))
    # contain scale = min(100/10, 100/20) = 5 -> 50x100, centered at x offset 25
    assert pixels[50, 24].tolist() == [255, 255, 255]
    assert pixels[50, 25].tolist() == [255, 0, 0]
    assert pixels[50, 74].tolist() == [255, 0, 0]
    assert pixels[50, 75].tolist() == [255, 255, 255]
    assert pixels[0, 50].tolist() == [255, 0, 0]
    assert pixels[99, 50].tolist() == [255, 0, 0]


def test_gif_alpha_composited_on_background(client):
    half = solid_rgba((40, 40), (255, 0, 0), alpha=128)
    asset = upload(client, half)
    body, buffer = gif_bytes(client, base_payload(asset["id"], width=40, height=40,
                                                  background="#00ff00"))
    image = open_gif(buffer)
    image.seek(0)
    pixels = np.asarray(image.convert("RGB"))
    # straight-alpha over green: 128/255*255 = 128 red, 127 green, 0 blue
    assert pixels[20, 20].tolist() == [128, 127, 0]


def test_gif_exact_colors_when_under_256_and_shared_palette(client):
    # same-size solids: contain needs no resampling, so visible colors must survive exactly
    red = upload(client, solid_rgba((32, 32), (200, 100, 50)))
    blue = upload(client, solid_rgba((32, 32), (30, 60, 90)))
    payload = {
        "frames": [
            {"asset_id": red["id"], "duration_ms": 100},
            {"asset_id": blue["id"], "duration_ms": 200},
            {"asset_id": red["id"], "duration_ms": 300},
        ],
        "width": 32,
        "height": 32,
        "background": "#102030",
        "loop": 0,
    }
    body, buffer = gif_bytes(client, payload)
    image = open_gif(buffer)
    assert image.n_frames == 3  # non-consecutive repeat is retained
    seen = set()
    for index in range(3):
        image.seek(index)
        assert image.info["duration"] == [100, 200, 300][index]
        seen.update({tuple(int(channel) for channel in pixel)
                     for pixel in np.asarray(image.convert("RGB")).reshape(-1, 3)})
    assert seen == {(200, 100, 50), (30, 60, 90)}
    # exactly one global color table, no per-frame local tables
    raw = buffer.getvalue()
    packed = raw[10]
    assert packed & 0x80
    offset = 13 + (2 ** ((packed & 0x07) + 1)) * 3
    local_tables = descriptors = 0
    while offset < len(raw):
        marker = raw[offset]
        if marker == 0x3B:
            break
        if marker == 0x21:
            offset += 2
            while raw[offset] != 0:
                offset += raw[offset] + 1
            offset += 1
            continue
        if marker == 0x2C:
            frame_packed = raw[offset + 9]
            descriptors += 1
            if frame_packed & 0x80:
                local_tables += 1
            offset += 10
            if frame_packed & 0x80:
                offset += (2 ** ((frame_packed & 0x07) + 1)) * 3
            offset += 1
            while raw[offset] != 0:
                offset += raw[offset] + 1
            offset += 1
            continue
        offset += 1
    assert descriptors == 3
    assert local_tables == 0


def test_gif_visible_background_color_exact(client):
    # 4x8 inside 16x16: contain scale 2 -> 8x16, leaving background columns visible
    asset = upload(client, solid_rgba((4, 8), (7, 7, 7)))
    payload = base_payload(asset["id"], width=16, height=16, background="#808080")
    body, buffer = gif_bytes(client, payload)
    image = open_gif(buffer)
    image.seek(0)
    seen = {tuple(int(c) for c in pixel)
            for pixel in np.asarray(image.convert("RGB")).reshape(-1, 3)}
    assert seen == {(7, 7, 7), (0x80, 0x80, 0x80)}


def test_gif_complex_input_uses_one_palette_without_dithering(client):
    rng = np.random.default_rng(4)
    images = [
        Image.fromarray(rng.integers(0, 256, (24, 24, 4), dtype=np.uint8))
        for _ in range(3)
    ]
    for image in images:
        image.putalpha(255)
    ids = [upload(client, image, name=f"complex-{i}.png")["id"] for i, image in enumerate(images)]
    payload = {
        "frames": [{"asset_id": asset_id, "duration_ms": 100} for asset_id in ids],
        "width": 32,
        "height": 32,
        "background": "#ffffff",
        "loop": 1,
    }
    body, buffer = gif_bytes(client, payload)
    image = open_gif(buffer)
    assert image.size == (32, 32)
    raw = buffer.getvalue()
    packed = raw[10]
    offset = 13 + (2 ** ((packed & 0x07) + 1)) * 3
    local_tables = descriptors = 0
    while offset < len(raw):
        marker = raw[offset]
        if marker == 0x3B:
            break
        if marker == 0x21:
            offset += 2
            while raw[offset] != 0:
                offset += raw[offset] + 1
            offset += 1
            continue
        if marker == 0x2C:
            frame_packed = raw[offset + 9]
            descriptors += 1
            if frame_packed & 0x80:
                local_tables += 1
            offset += 10
            if frame_packed & 0x80:
                offset += (2 ** ((frame_packed & 0x07) + 1)) * 3
            offset += 1
            while raw[offset] != 0:
                offset += raw[offset] + 1
            offset += 1
            continue
        offset += 1
    assert descriptors == 3
    assert local_tables == 0
    # the single global table never exceeds 256 entries
    assert 2 ** ((packed & 0x07) + 1) <= 256


def test_gif_consecutive_identical_frames_merge_but_keep_timing(client, uploaded):
    other = upload(client, solid_rgba((16, 16), (0, 128, 255)), name="other.png")
    payload = base_payload(uploaded["id"], other["id"])
    payload["frames"] = [
        {"asset_id": uploaded["id"], "duration_ms": 100},
        {"asset_id": uploaded["id"], "duration_ms": 100},
        {"asset_id": other["id"], "duration_ms": 250},
    ]
    body, buffer = gif_bytes(client, payload)
    assert body["frame_count"] == 3
    assert body["total_duration_ms"] == 450
    image = open_gif(buffer)
    durations = []
    for index in range(image.n_frames):
        image.seek(index)
        durations.append(image.info["duration"])
    assert sum(durations) == 450
    # the encoder may collapse the two identical consecutive frames into one 200 ms frame
    if image.n_frames < 3:
        assert durations == [200, 250]
    else:
        assert durations == [100, 100, 250]


def test_gif_repeated_asset_allowed_and_24_frame_bounds(client, uploaded):
    payload = base_payload(uploaded["id"])
    payload["frames"] = [
        {"asset_id": uploaded["id"], "duration_ms": 10} for _ in range(24)
    ]
    body, buffer = gif_bytes(client, payload)
    assert body["frame_count"] == 24
    assert body["total_duration_ms"] == 240
    assert open_gif(buffer).n_frames >= 1


def test_gif_no_new_asset_and_original_unchanged(client, uploaded):
    before = client.get(uploaded["url"]).data
    total_before = client.get("/api/assets").json["data"]["total"]
    gif_bytes(client, base_payload(uploaded["id"]))
    assert client.get("/api/assets").json["data"]["total"] == total_before
    assert client.get(uploaded["url"]).data == before
    exports = client.get("/api/stats").json["data"]["exports"]
    assert exports == 1


def test_gif_missing_asset_fails_before_export_record(client, uploaded):
    payload = base_payload(uploaded["id"])
    payload["frames"][1]["asset_id"] = "f" * 32
    exports_before = client.get("/api/stats").json["data"]["exports"]
    response = client.post("/api/animations/gif", json=payload)
    assert response.status_code == 404
    assert response.json["ok"] is False
    assert client.get("/api/stats").json["data"]["exports"] == exports_before


@pytest.mark.parametrize("mutation", [
    lambda p: p.pop("frames"),
    lambda p: p.pop("width"),
    lambda p: p.pop("height"),
    lambda p: p.pop("background"),
    lambda p: p.pop("loop"),
    lambda p: p.update(frames=[{"asset_id": "x", "duration_ms": 100}]),
    lambda p: p.update(frames=[{"asset_id": "x", "duration_ms": 100}] * 25),
    lambda p: p.update(width=0),
    lambda p: p.update(height=1025),
    lambda p: p.update(background="black"),
    lambda p: p.update(background="#fff"),
    lambda p: p.update(loop=-1),
    lambda p: p.update(loop=65536),
])
def test_gif_validation_errors(client, uploaded, mutation):
    payload = base_payload(uploaded["id"])
    mutation(payload)
    if isinstance(payload.get("frames"), list):
        for frame in payload["frames"]:
            if frame.get("asset_id") == "x":
                frame["asset_id"] = uploaded["id"]
    exports_before = client.get("/api/stats").json["data"]["exports"]
    response = client.post("/api/animations/gif", json=payload)
    assert 400 <= response.status_code < 500
    assert response.json["ok"] is False
    assert client.get("/api/stats").json["data"]["exports"] == exports_before


@pytest.mark.parametrize("duration", [9, 10001, 15, 100.5])
def test_gif_duration_rules(client, uploaded, duration):
    payload = base_payload(uploaded["id"])
    payload["frames"][1]["duration_ms"] = duration
    response = client.post("/api/animations/gif", json=payload)
    assert response.status_code == 400
    assert response.json["ok"] is False


@pytest.mark.parametrize("field, value", [
    ("duration_ms", True),
    ("duration_ms", "100"),
    ("duration_ms", None),
    ("asset_id", 123),
    ("asset_id", "not-a-hex-id"),
    ("asset_id", None),
])
def test_gif_frame_field_types_rejected(client, uploaded, field, value):
    payload = base_payload(uploaded["id"])
    payload["frames"][0][field] = value
    response = client.post("/api/animations/gif", json=payload)
    assert response.status_code == 400
    assert response.json["ok"] is False


def test_gif_frame_not_object_rejected(client, uploaded):
    payload = base_payload(uploaded["id"])
    payload["frames"][1] = "not-an-object"
    response = client.post("/api/animations/gif", json=payload)
    assert response.status_code == 400


def test_gif_boundary_values_accepted(client, uploaded):
    other = upload(client, solid_rgba((8, 8), (1, 2, 3)), name="other.png")
    payload = {
        "frames": [
            {"asset_id": uploaded["id"], "duration_ms": 10},
            {"asset_id": other["id"], "duration_ms": 10000},
        ],
        "width": 1,
        "height": 1,
        "background": "#aAbBcc",
        "loop": 0,
    }
    body, buffer = gif_bytes(client, payload)
    assert body["total_duration_ms"] == 10010
    image = open_gif(buffer)
    assert image.size == (1, 1)
    assert image.info["loop"] == 0
    image.seek(1)
    assert image.info["duration"] == 10000
    # normalized lowercase background stored in options
    assert body["export"]["options"]["background"] == "#aabbcc"


def test_gif_unknown_field_and_frame_field_rejected(client, uploaded):
    payload = base_payload(uploaded["id"])
    payload["extra"] = 1
    assert client.post("/api/animations/gif", json=payload).status_code == 400
    payload = base_payload(uploaded["id"])
    payload["frames"][0]["extra"] = 1
    assert client.post("/api/animations/gif", json=payload).status_code == 400


def test_gif_non_json_body_rejected(client):
    response = client.post("/api/animations/gif", data="not-json",
                           content_type="text/plain")
    assert 400 <= response.status_code < 500


def test_gif_restart_persists_export_record(app, client, uploaded):
    body, buffer = gif_bytes(client, base_payload(uploaded["id"]))
    root = app.extensions["studio"]["settings"].root
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(root, testing=True)
    try:
        second_client = second.test_client()
        record = second_client.get(body["export"]["url"])
        assert record.status_code == 200
        assert record.mimetype == "image/gif"
    finally:
        second.extensions["studio"]["jobs"].shutdown()
