import io
import math
import numpy as np
import pytest
from PIL import Image


def upload(client, rgb, alpha=None, name="lut-source.png"):
    rgba = np.empty((*rgb.shape[:2], 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = 255 if alpha is None else alpha
    buffer = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def cube_text(size, function, domain_min=None, domain_max=None, title=None, comments=True):
    lines = []
    if comments:
        lines.append("# generated test LUT")
    if title is not None:
        lines.append(f"TITLE {title}")
    if domain_min is not None:
        lines.append("DOMAIN_MIN " + " ".join(repr(value) for value in domain_min))
    if domain_max is not None:
        lines.append("DOMAIN_MAX " + " ".join(repr(value) for value in domain_max))
    lines.append(f"LUT_3D_SIZE {size}")
    if comments:
        lines.append("# data: red fastest, then green, then blue")
    for b in range(size):
        for g in range(size):
            for r in range(size):
                rr = r / (size - 1)
                gg = g / (size - 1)
                bb = b / (size - 1)
                values = function(rr, gg, bb)
                lines.append(" ".join(repr(float(value)) for value in values))
    return "\n".join(lines) + "\n"


def download_rgba(client, asset):
    with Image.open(io.BytesIO(client.get(asset["url"]).data)) as image:
        return np.asarray(image.convert("RGBA"))


def apply(client, asset, cube, strength=1.0):
    return client.post("/api/lut/apply", json={"asset_id": asset["id"], "cube": cube, "strength": strength})


def test_identity_lut_reproduces_pixels_and_alpha(client):
    rgb = np.array([
        [[0, 0, 0], [255, 255, 255], [1, 128, 254]],
        [[17, 90, 201], [64, 200, 3], [250, 10, 77]],
    ], dtype=np.uint8)
    alpha = np.array([[0, 120, 40], [255, 255, 255]], dtype=np.uint8)
    source = upload(client, rgb, alpha)
    response = apply(client, source, cube_text(2, lambda r, g, b: (r, g, b)))
    assert response.status_code == 201, response.json
    result = response.json["data"]["asset"]
    assert result["id"] != source["id"]
    assert result["parent_id"] == source["id"]
    assert result["root_id"] == source["id"]
    assert result["format"] == "PNG"
    assert response.json["data"]["size"] == {"width": 3, "height": 2}
    pixels = download_rgba(client, result)
    assert np.array_equal(pixels[..., :3], rgb)
    assert np.array_equal(pixels[..., 3], alpha)
    assert len(client.get(f"/api/assets/{result['id']}/history").json["data"]) == 2


def test_strength_zero_still_creates_identical_copy(client):
    rgb = np.arange(2 * 2 * 3, dtype=np.uint8).reshape(2, 2, 3)
    source = upload(client, rgb, alpha=np.full((2, 2), 77, dtype=np.uint8))
    before = client.get("/api/assets").json["data"]["total"]
    response = apply(client, source, cube_text(2, lambda r, g, b: (1 - r, 1 - g, 1 - b)), strength=0)
    assert response.status_code == 201, response.json
    result = response.json["data"]["asset"]
    assert client.get("/api/assets").json["data"]["total"] == before + 1
    pixels = download_rgba(client, result)
    assert np.array_equal(pixels[..., :3], rgb)
    assert np.all(pixels[..., 3] == 77)


def test_non_default_domain_and_linear_interpolation(client):
    rgb = np.array([
        [[0, 0, 0], [255, 255, 255], [64, 200, 3]],
        [[180, 30, 240], [128, 128, 128], [7, 99, 177]],
    ], dtype=np.uint8)
    alpha = np.array([[0, 200, 100], [50, 255, 255]], dtype=np.uint8)
    source = upload(client, rgb, alpha)
    domain_min = (0.1, -0.2, 0.25)
    domain_max = (0.9, 0.6, 1.1)

    def linear(r, g, b):
        return (
            0.10 + 0.22 * r + 0.31 * g + 0.15 * b,
            0.05 + 0.11 * r + 0.40 * g + 0.25 * b,
            0.30 + 0.18 * r + 0.07 * g + 0.35 * b,
        )

    response = apply(client, source, cube_text(
        4, linear, domain_min=domain_min, domain_max=domain_max,
        title="倾斜通道",
    ))
    assert response.status_code == 201, response.json
    result = response.json["data"]["asset"]
    pixels = download_rgba(client, result)

    encoded = rgb.astype(np.float64) / 255.0
    coordinates = np.clip(
        (encoded - np.asarray(domain_min)) / (np.asarray(domain_max) - np.asarray(domain_min)),
        0.0, 1.0,
    )
    expected = np.stack([
        0.10 + 0.22 * coordinates[..., 0] + 0.31 * coordinates[..., 1] + 0.15 * coordinates[..., 2],
        0.05 + 0.11 * coordinates[..., 0] + 0.40 * coordinates[..., 1] + 0.25 * coordinates[..., 2],
        0.30 + 0.18 * coordinates[..., 0] + 0.07 * coordinates[..., 1] + 0.35 * coordinates[..., 2],
    ], axis=-1)
    expected = np.floor(np.clip(expected, 0, 1) * 255.0 + 0.5).astype(np.uint8)
    assert np.array_equal(pixels[..., :3], expected)
    assert np.array_equal(pixels[..., 3], alpha)


def test_corners_endpoints_and_strength_blend(client):
    rgb = np.array([
        [[0, 0, 0], [255, 0, 0], [0, 255, 0], [0, 0, 255]],
        [[255, 255, 0], [255, 0, 255], [0, 255, 255], [255, 255, 255]],
    ], dtype=np.uint8)
    source = upload(client, rgb, alpha=np.array([[0, 10, 20, 30], [40, 50, 60, 70]], dtype=np.uint8))

    # Red channel is inverted; green/blue are shifted by +0.25. At endpoints
    # the grid corners are exact, and the mid grid point is exact too.
    response = apply(
        client,
        source,
        cube_text(3, lambda r, g, b: (1 - r, g + 0.25, b + 0.25)),
        strength=0.5,
    )
    assert response.status_code == 201, response.json
    result = response.json["data"]["asset"]
    pixels = download_rgba(client, result)

    encoded = rgb.astype(np.float64) / 255.0
    lut_output = np.stack([
        1 - encoded[..., 0],
        encoded[..., 1] + 0.25,
        encoded[..., 2] + 0.25,
    ], axis=-1)
    blended = 0.5 * encoded + 0.5 * np.clip(lut_output, 0, 1)
    expected = np.floor(blended * 255.0 + 0.5).astype(np.uint8)
    assert np.array_equal(pixels[..., :3], expected)
    assert np.array_equal(pixels[..., 3], np.array([[0, 10, 20, 30], [40, 50, 60, 70]], dtype=np.uint8))


@pytest.mark.parametrize("size", [2, 17])
def test_size_boundaries_are_accepted(client, size):
    source = upload(client, np.array([[[13, 44, 99]]], dtype=np.uint8))
    response = apply(client, source, cube_text(size, lambda r, g, b: (r, g, b)))
    assert response.status_code == 201, response.json
    pixels = download_rgba(client, response.json["data"]["asset"])
    assert np.array_equal(pixels[..., :3], np.array([[[13, 44, 99]]], dtype=np.uint8))


def base_cube(size=2):
    return cube_text(size, lambda r, g, b: (r, g, b))


def replace_line(text, old, new):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(old):
            lines[index] = new
            return "\n".join(lines) + "\n"
    raise AssertionError(old)


def test_parse_failures_never_create_assets(client):
    source = upload(client, np.zeros((2, 2, 3), dtype=np.uint8))
    identity = base_cube()
    data_line = "0.0 0.0 0.0"
    bad_cubes = [
        replace_line(identity, "LUT_3D_SIZE", "LUT_3D_SIZE 1"),
        replace_line(identity, "LUT_3D_SIZE", "LUT_3D_SIZE 18"),
        replace_line(identity, "LUT_3D_SIZE", "LUT_3D_SIZE 2\nLUT_3D_SIZE 2"),
        identity + "FOO 1 2 3\n",
        identity.replace(data_line, "nan 0.0 0.0", 1),
        identity.replace("0.0 0.0 1.0", "inf 0.0 1.0", 1),
        identity + data_line + "\n",
        identity.replace(data_line, "0.0 0.0\n", 1),
        identity.replace(data_line, "0.0 0.0 0.0 1.0\n", 1),
        identity.replace("LUT_3D_SIZE 2", "LUT_1D_SIZE 2\nLUT_3D_SIZE 2"),
        identity.replace("LUT_3D_SIZE 2", "LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MIN 0 0 0"),
        identity.replace("LUT_3D_SIZE 2", "BAD_HEADER 1 2 3\nLUT_3D_SIZE 2", 1),
        identity + "DOMAIN_MIN 0 0 0\n",
        replace_line(identity, "LUT_3D_SIZE", "LUT_3D_SIZE 2\nTITLE 重复标题\nTITLE 重复标题"),
        identity.replace("LUT_3D_SIZE 2", "LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 0 1 1"),
        identity.replace("LUT_3D_SIZE 2", "LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 nan"),
        "LUT_3D_SIZE 2\n" + "0 0 0\n" * 4,
        "",
        "# only a comment\n",
    ]
    before = client.get("/api/assets").json["data"]["total"]
    for bad in bad_cubes:
        response = apply(client, source, bad, strength=1.0)
        assert response.status_code == 400, bad[:120] + " -> " + response.get_data(as_text=True)
        assert client.get("/api/assets").json["data"]["total"] == before


def test_missing_size_and_one_dimensional_lut_rejected(client):
    source = upload(client, np.zeros((1, 1, 3), dtype=np.uint8))
    assert apply(client, source, "0 0 0\n1 1 1\n").status_code == 400
    assert apply(client, source, "LUT_1D_SIZE 4\n" + "0 0 0\n" * 4).status_code == 400


def test_oversized_cube_and_bad_request_fields_rejected(client):
    source = upload(client, np.zeros((1, 1, 3), dtype=np.uint8))
    oversized = "#" * (1024 * 1024 + 1)
    response = apply(client, source, oversized)
    assert response.status_code == 400
    assert "1 MiB" in response.json["error"]["message"]
    assert apply(client, source, base_cube(), strength=1.5).status_code == 400
    assert apply(client, source, base_cube(), strength=-0.1).status_code == 400
    assert client.post("/api/lut/apply", json={
        "asset_id": source["id"], "cube": base_cube(), "strength": 1, "extra": True,
    }).status_code == 400
    assert client.post("/api/lut/apply", json={"cube": base_cube(), "strength": 1}).status_code == 400


def test_lut_preserves_existing_lineage(client):
    source = upload(client, np.array([[[10, 20, 30]]], dtype=np.uint8))
    first = client.post("/api/process", json={
        "asset_id": source["id"],
        "steps": [{"operation": "color_invert", "params": {}}],
    }).json["data"]
    response = apply(client, first, base_cube())
    assert response.status_code == 201, response.json
    result = response.json["data"]["asset"]
    assert result["parent_id"] == first["id"]
    assert result["root_id"] == source["id"]
    history = client.get(f"/api/assets/{result['id']}/history").json["data"]
    assert [item["id"] for item in history] == [source["id"], first["id"], result["id"]]


def test_headers_comments_and_title_are_accepted(client):
    source = upload(client, np.array([[[10, 20, 30]]], dtype=np.uint8))
    text = (
        "﻿# leading comment\n"
        "   \n"
        "title My Film Look\n"
        "# another comment\n"
        "DOMAIN_MAX 1.0 1.0 1.0\n"
        "LUT_3D_SIZE 2\n"
        "# inline comment between headers and data\n"
        "0.0 0.0 0.0\n"
        "1.0 0.0 0.0\n"
        "0.0 1.0 0.0\n"
        "1.0 1.0 0.0\n"
        "0.0 0.0 1.0\n"
        "1.0 0.0 1.0\n"
        "0.0 1.0 1.0\n"
        "1.0 1.0 1.0\n"
    )
    response = apply(client, source, text)
    assert response.status_code == 201, response.json
