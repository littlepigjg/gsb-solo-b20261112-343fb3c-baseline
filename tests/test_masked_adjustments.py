"""POST /api/masked-adjustments: alpha/luminance masks, inversion, feather and blending."""
import io
import numpy as np
from PIL import Image, ImageFilter


def upload(client, image, name):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def png_bytes(client, asset):
    return client.get(asset["url"]).data


def pixels(client, asset):
    with Image.open(io.BytesIO(png_bytes(client, asset))) as image:
        return np.asarray(image.convert("RGBA"))


def run(client, original, mask, **options):
    payload = {
        "asset_id": original["id"],
        "mask_id": mask["id"],
        "operation": "color_exposure",
        "params": {"stops": 1.0},
    }
    payload.update(options)
    return client.post("/api/masked-adjustments", json=payload)


def test_alpha_mask_transparent_areas_keep_original_rgb(client):
    source = Image.new("RGBA", (8, 8), (80, 120, 200, 255))
    mask = Image.new("RGBA", (8, 8), (255, 255, 255, 0))
    original = upload(client, source, "original.png")
    masked = upload(client, mask, "mask.png")
    before = png_bytes(client, original)
    mask_before = png_bytes(client, masked)

    response = run(client, original, masked, mode="alpha", strength=1.0)
    assert response.status_code == 201, response.json
    asset = response.json["data"]["asset"]
    assert asset["parent_id"] == original["id"]
    assert asset["root_id"] == original["id"]
    assert len(client.get(f"/api/assets/{asset['id']}/history").json["data"]) == 2
    assert np.array_equal(pixels(client, asset), np.asarray(source))
    assert png_bytes(client, original) == before
    assert png_bytes(client, masked) == mask_before


def test_luminance_mode_uses_lightness_and_alpha(client):
    source = Image.new("RGBA", (4, 1), (10, 20, 30, 255))
    mask = Image.new("RGBA", (4, 1))
    mask.putpixel((0, 0), (0, 0, 0, 255))
    mask.putpixel((1, 0), (255, 255, 255, 255))
    mask.putpixel((2, 0), (255, 255, 255, 0))
    mask.putpixel((3, 0), (128, 128, 128, 255))
    original = upload(client, source, "original.png")
    masked = upload(client, mask, "mask.png")

    response = run(client, original, masked, mode="luminance", feather=0, strength=1.0)
    assert response.status_code == 201, response.json
    result = pixels(client, response.json["data"]["asset"])
    # Black mask hides, white opaque applies fully, transparent hides.
    assert np.array_equal(result[0, 0, :3], [10, 20, 30])
    assert np.array_equal(result[0, 2, :3], [10, 20, 30])
    assert not np.array_equal(result[0, 1, :3], [10, 20, 30])
    assert np.all(result[..., 3] == 255)


def test_invert_swaps_covered_and_uncovered_areas(client):
    source = Image.new("RGBA", (4, 1), (10, 20, 30, 255))
    mask = Image.new("RGBA", (4, 1))
    mask.putpixel((0, 0), (255, 255, 255, 255))
    mask.putpixel((1, 0), (255, 255, 255, 0))
    mask.putpixel((2, 0), (255, 255, 255, 255))
    mask.putpixel((3, 0), (255, 255, 255, 0))
    original = upload(client, source, "original.png")
    masked = upload(client, mask, "mask.png")

    response = run(client, original, masked, mode="alpha", invert=True, strength=1.0)
    assert response.status_code == 201, response.json
    result = pixels(client, response.json["data"]["asset"])
    # Opaque mask becomes weight 0; transparent mask becomes weight 255.
    assert np.array_equal(result[0, 0, :3], [10, 20, 30])
    assert np.array_equal(result[0, 2, :3], [10, 20, 30])
    assert not np.array_equal(result[0, 1, :3], [10, 20, 30])
    assert not np.array_equal(result[0, 3, :3], [10, 20, 30])


def test_feather_blurs_the_mask(client):
    source = Image.new("RGBA", (15, 15), (10, 20, 30, 255))
    mask = Image.new("RGBA", (15, 15), (0, 0, 0, 0))
    mask.paste((255, 255, 255, 255), (7, 7, 8, 8))
    original = upload(client, source, "original.png")
    masked = upload(client, mask, "mask.png")

    response = run(client, original, masked, mode="alpha", feather=3, strength=1.0)
    assert response.status_code == 201, response.json
    result = pixels(client, response.json["data"]["asset"])
    # The blurred mask covers a wider area than the single source pixel.
    covered = np.count_nonzero(result[..., 0])
    assert covered > 1
    assert result[7, 7, 0] > 0
    neighbours = [result[7, 6, 0], result[7, 8, 0], result[6, 7, 0], result[8, 7, 0]]
    assert all(value > 0 for value in neighbours)
    assert all(value < 255 for value in neighbours)


def test_strength_zero_is_an_exact_copy_and_one_matches_full_operation(client):
    source = Image.new("RGBA", (10, 10), (30, 60, 90, 255))
    mask = Image.new("RGBA", (10, 10), (255, 255, 255, 255))
    original = upload(client, source, "original.png")
    masked = upload(client, mask, "mask.png")

    zero = run(client, original, masked, mode="alpha", strength=0.0)
    assert zero.status_code == 201, zero.json
    assert np.array_equal(pixels(client, zero.json["data"]["asset"]), np.asarray(source))

    full = run(client, original, masked, mode="alpha", strength=1.0)
    assert full.status_code == 201, full.json
    expected = client.post("/api/process", json={
        "asset_id": original["id"],
        "steps": [{"operation": "color_exposure", "params": {"stops": 1.0}}],
    }).json["data"]
    assert np.array_equal(pixels(client, full.json["data"]["asset"]), pixels(client, expected))


def test_different_mask_size_rejected_before_copy(client):
    source = Image.new("RGBA", (10, 10), (1, 2, 3, 255))
    mask = Image.new("RGBA", (9, 10), (255, 255, 255, 255))
    original = upload(client, source, "original.png")
    masked = upload(client, mask, "small-mask.png")
    total = client.get("/api/assets").json["data"]["total"]

    response = run(client, original, masked)
    assert response.status_code == 400
    assert response.json["ok"] is False
    assert client.get("/api/assets").json["data"]["total"] == total


def test_invalid_operation_params_and_assets_rejected_without_copy(client):
    source = Image.new("RGBA", (4, 4), (1, 2, 3, 255))
    mask = Image.new("RGBA", (4, 4), (255, 255, 255, 255))
    original = upload(client, source, "original.png")
    masked = upload(client, mask, "mask.png")
    total = client.get("/api/assets").json["data"]["total"]

    cases = [
        {"operation": "filter_sepia"},
        {"params": {"stops": 99}},
        {"params": {"unknown": 1}},
        {"mode": "rgb"},
        {"invert": 1},
        {"feather": 9},
        {"strength": 1.5},
        {"asset_id": "0" * 32},
        {"mask_id": "../../etc/passwd"},
    ]
    for change in cases:
        payload = {
            "asset_id": original["id"],
            "mask_id": masked["id"],
            "operation": "color_exposure",
            "params": {"stops": 0.5},
        }
        payload.update(change)
        response = client.post("/api/masked-adjustments", json=payload)
        assert 400 <= response.status_code < 500, (change, response.json)
        assert response.json["ok"] is False
    assert client.get("/api/assets").json["data"]["total"] == total


def test_missing_parameters_use_catalog_defaults(client):
    source = Image.new("RGBA", (6, 6), (50, 100, 150, 255))
    mask = Image.new("RGBA", (6, 6), (255, 255, 255, 255))
    original = upload(client, source, "original.png")
    masked = upload(client, mask, "mask.png")
    response = client.post("/api/masked-adjustments", json={
        "asset_id": original["id"],
        "mask_id": masked["id"],
        "operation": "color_brightness",
    })
    assert response.status_code == 201, response.json
    assert response.json["data"]["asset"]["width"] == 6
