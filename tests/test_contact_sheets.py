import io

import pytest
from PIL import Image


def upload(client, size, color, name):
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def sheet_payload(asset_ids, **overrides):
    payload = {
        "asset_ids": asset_ids,
        "columns": 2,
        "cell_width": 100,
        "cell_height": 80,
        "gap": 10,
        "margin": 5,
        "background": "#123456",
    }
    payload.update(overrides)
    return payload


def test_contact_sheet_layout_and_png(client, uploaded, sample_image):
    second = upload(client, (32, 32), (0, 0, 255, 255), "蓝色.png")
    third = upload(client, (200, 100), (255, 0, 0, 255), "红色.png")
    before = client.get(uploaded["url"]).data

    response = client.post("/api/contact-sheets", json=sheet_payload([uploaded["id"], second["id"], third["id"]]))
    assert response.status_code == 201, response.json
    data = response.json["data"]
    asset = data["asset"]

    assert asset["kind"] == "result"
    assert asset["width"] == 2 * 5 + 2 * 100 + 10
    assert asset["height"] == 2 * 5 + 2 * 80 + 10
    assert client.get(asset["url"]).data[:8] == b"\x89PNG\r\n\x1a\n"

    layout = data["layout"]
    assert [item["index"] for item in layout] == [0, 1, 2]
    assert [item["asset_id"] for item in layout] == [uploaded["id"], second["id"], third["id"]]
    assert layout[0]["cell"] == {"x": 5, "y": 5, "w": 100, "h": 80}
    assert layout[1]["cell"] == {"x": 115, "y": 5, "w": 100, "h": 80}
    assert layout[2]["cell"] == {"x": 5, "y": 95, "w": 100, "h": 80}

    # 384x320 contains to 96x80; 32x32 contains to 80x80; 200x100 to 100x50.
    assert layout[0]["image_box"] == {"x": 7, "y": 5, "w": 96, "h": 80}
    assert layout[1]["image_box"] == {"x": 125, "y": 5, "w": 80, "h": 80}
    assert layout[2]["image_box"] == {"x": 5, "y": 110, "w": 100, "h": 50}

    with Image.open(io.BytesIO(client.get(asset["url"]).data)) as rendered:
        assert rendered.size == (asset["width"], asset["height"])
        assert rendered.convert("RGB").getpixel((0, 0)) == (0x12, 0x34, 0x56)
        # The fourth cell is unused and must remain background.
        assert rendered.convert("RGB").getpixel((160, 100)) == (0x12, 0x34, 0x56)

    assert client.get(uploaded["url"]).data == before


def test_alpha_is_composited_onto_background(client):
    transparent = upload(client, (40, 40), (0, 0, 0, 0), "透明.png")
    response = client.post("/api/contact-sheets", json=sheet_payload(
        [transparent["id"]], columns=1, cell_width=40, cell_height=40, gap=0, margin=8, background="#abcdef"
    ))
    assert response.status_code == 201, response.json
    asset = response.json["data"]["asset"]
    layout = response.json["data"]["layout"]
    assert layout[0]["image_box"] == {"x": 8, "y": 8, "w": 40, "h": 40}
    with Image.open(io.BytesIO(client.get(asset["url"]).data)) as rendered:
        # Fully transparent source leaves the configured background untouched.
        assert rendered.convert("RGB").getpixel((8, 8)) == (0xab, 0xcd, 0xef)
        assert rendered.convert("RGB").getpixel((0, 0)) == (0xab, 0xcd, 0xef)


def test_contact_sheet_preserves_duplicates_and_order(client, uploaded):
    response = client.post("/api/contact-sheets", json=sheet_payload([uploaded["id"], uploaded["id"]], columns=2))
    assert response.status_code == 201, response.json
    layout = response.json["data"]["layout"]
    assert len(layout) == 2
    assert [item["asset_id"] for item in layout] == [uploaded["id"], uploaded["id"]]
    assert [item["index"] for item in layout] == [0, 1]
    assert layout[0]["cell"]["x"] == 5
    assert layout[1]["cell"]["x"] == 115


@pytest.mark.parametrize("overrides", [
    {"columns": True},
    {"cell_width": True},
    {"cell_width": 7},
    {"cell_width": 1025},
    {"cell_height": 8.5},
    {"gap": 65},
    {"margin": -1},
    {"columns": 9},
    {"background": "red"},
    {"extra": "unknown"},
])
def test_contact_sheet_rejects_bad_parameters(client, uploaded, overrides):
    before = client.get("/api/assets").json["data"]["total"]
    payload = sheet_payload([uploaded["id"]], **overrides)
    response = client.post("/api/contact-sheets", json=payload)
    assert 400 <= response.status_code < 500, response.json
    assert response.json["ok"] is False
    assert client.get("/api/assets").json["data"]["total"] == before


def test_contact_sheet_rejects_missing_and_unknown_assets(client, uploaded):
    before = client.get("/api/assets").json["data"]["total"]
    for asset_ids in ([], ["0" * 32], ["not-an-id"], [uploaded["id"]] * 25, [uploaded["id"], "0" * 32]):
        response = client.post("/api/contact-sheets", json=sheet_payload(asset_ids))
        assert 400 <= response.status_code < 500, response.json
        assert client.get("/api/assets").json["data"]["total"] == before


def test_contact_sheet_rejects_oversized_result(client, uploaded):
    before = client.get("/api/assets").json["data"]["total"]
    # 8 columns * 1024 + 7 * 64 + 2 * 64 = 8768 pixels on the long edge.
    response = client.post("/api/contact-sheets", json=sheet_payload(
        [uploaded["id"]] * 8,
        columns=8,
        cell_width=1024,
        cell_height=1024,
        gap=64,
        margin=64,
    ))
    assert response.status_code == 400, response.json

    # 8192x2048 stays within the per-side limit but exceeds 16,000,000 pixels.
    response = client.post("/api/contact-sheets", json=sheet_payload(
        [uploaded["id"]] * 16,
        columns=8,
        cell_width=1024,
        cell_height=1024,
        gap=0,
        margin=0,
    ))
    assert response.status_code == 400, response.json
    assert client.get("/api/assets").json["data"]["total"] == before
