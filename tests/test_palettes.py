import io
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.serving import make_server
from backend.palettes import parse_gpl, render_gpl, render_swatch_card, MAX_TEXT_BYTES
from backend.errors import StudioError


BASE_GPL = (
    "GIMP Palette\r\n"
    "Name: 测试色板\r\n"
    "Columns: 3\r\n"
    "# 这是注释\r\n"
    "\r\n"
    "  0   0   0\t黑色\r\n"
    "255 0 0 Red\r\n"
    "0 255 0   绿色 \r\n"
    "0 0 255\r\n"
    "10 20 30 重复\r\n"
    "10 20 30 重复\r\n"
)


def post_convert(client, text, swatch_size=16):
    return client.post("/api/palettes/convert", json={"text": text, "swatch_size": swatch_size})


def test_convert_basic_and_asset_persisted(client):
    response = post_convert(client, BASE_GPL, swatch_size=24)
    assert response.status_code == 201, response.json
    data = response.json["data"]
    assert data["name"] == "测试色板"
    assert data["columns"] == 3
    assert data["colors"] == [
        {"rgb": [0, 0, 0], "name": "黑色"},
        {"rgb": [255, 0, 0], "name": "Red"},
        {"rgb": [0, 255, 0], "name": "绿色"},
        {"rgb": [0, 0, 255], "name": "Color 4"},
        {"rgb": [10, 20, 30], "name": "重复"},
        {"rgb": [10, 20, 30], "name": "重复"},
    ]
    asset = data["asset"]
    assert asset["width"] == 3 * 24
    assert asset["height"] == 2 * 24
    assert asset["kind"] == "result"
    assert asset["metadata"]["color_count"] == 6

    listing = client.get("/api/assets").json["data"]
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == asset["id"]

    download = client.get(asset["url"])
    assert download.status_code == 200
    assert download.mimetype == "image/png"
    with Image.open(io.BytesIO(download.data)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"
        assert image.size == (72, 48)
        assert image.getpixel((0, 0)) == (0, 0, 0, 255)
        assert image.getpixel((23, 23)) == (0, 0, 0, 255)
        assert image.getpixel((24, 0)) == (255, 0, 0, 255)
        assert image.getpixel((48, 0)) == (0, 255, 0, 255)
        assert image.getpixel((0, 47)) == (0, 0, 255, 255)
        assert image.getpixel((48, 47)) == (10, 20, 30, 255)
    assert client.get(asset["thumbnail_url"]).status_code == 200


def test_swatch_card_has_no_stroke_and_row_major_order():
    colors = [(1, 2, 3, "a"), (4, 5, 6, "b"), (5, 5, 5, "c")]
    image = render_swatch_card(colors, columns=2, swatch_size=8)
    assert image.size == (16, 16)
    # Whole cell including both edges is the fill color: no outline.
    assert image.getpixel((0, 0)) == (1, 2, 3, 255)
    assert image.getpixel((7, 7)) == (1, 2, 3, 255)
    assert image.getpixel((8, 0)) == (4, 5, 6, 255)
    assert image.getpixel((0, 8)) == (5, 5, 5, 255)
    assert image.getpixel((15, 15)) == (0, 0, 0, 0)


def test_default_headers_and_default_names(client):
    text = "GIMP Palette\n1 2 3\n4 5 6\n"
    data = post_convert(client, text).json["data"]
    assert data["name"] == "Imported"
    assert data["columns"] == 4
    assert [item["name"] for item in data["colors"]] == ["Color 1", "Color 2"]


def test_canonical_gpl_format_and_round_trip(client):
    data = post_convert(client, BASE_GPL).json["data"]
    gpl = data["gpl"]
    assert gpl.endswith("\n")
    assert not gpl.endswith("\n\n")
    assert "\r" not in gpl
    lines = gpl.split("\n")
    assert lines[0] == "GIMP Palette"
    assert lines[1] == "Name: 测试色板"
    assert lines[2] == "Columns: 3"
    assert lines[3] == "#"
    assert lines[4] == "0 0 0\t黑色"
    assert lines[5] == "255 0 0\tRed"
    assert lines[6] == "0 255 0\t绿色"
    assert lines[7] == "0 0 255\tColor 4"
    assert lines[8] == "10 20 30\t重复"
    assert lines[9] == "10 20 30\t重复"
    assert lines[10] == ""

    again = post_convert(client, gpl).json["data"]
    assert again["name"] == data["name"]
    assert again["columns"] == data["columns"]
    assert again["colors"] == data["colors"]
    assert again["gpl"] == gpl


def test_unit_parser_round_trip_is_stable():
    name, columns, colors = parse_gpl(BASE_GPL)
    canonical = render_gpl(name, columns, colors)
    assert parse_gpl(canonical) == (name, columns, colors)


def test_color_count_boundaries_accepted(client):
    one = "GIMP Palette\n1 2 3\n"
    assert post_convert(client, one).status_code == 201
    many = "GIMP Palette\nColumns: 16\n" + "".join(
        f"{i % 256} {i % 128} {255 - i % 256}\n" for i in range(256)
    )
    response = post_convert(client, many, swatch_size=8)
    assert response.status_code == 201, response.json
    assert len(response.json["data"]["colors"]) == 256
    assert response.json["data"]["asset"]["width"] == 128
    assert response.json["data"]["asset"]["height"] == 16 * 8


def test_invalid_text_leaves_no_artifacts(client):
    before = client.get("/api/stats").json["data"]
    media_before = client.get("/api/diagnostics").json["data"]["media_file_count"]
    for bad_text in (
        "Not A Palette\n0 0 0\n",
        "GIMP Palette\n256 0 0\n",
        "GIMP Palette\n0 0 -1\n",
        "GIMP Palette\n0 0 1.5\n",
        "GIMP Palette\n0 0\n",
        "GIMP Palette\nName: A\nName: B\n0 0 0\n",
        "GIMP Palette\n0 0 0\nName: Late\n",
        "GIMP Palette\nFoo: Bar\n0 0 0\n",
        "GIMP Palette\nColumns: 0\n0 0 0\n",
        "GIMP Palette\nColumns: 17\n0 0 0\n",
        "GIMP Palette\nColumns: 4.0\n0 0 0\n",
        "GIMP Palette\nName:\n0 0 0\n",
        "GIMP Palette\n",
        "GIMP Palette\n# only comments\n\n",
        "GIMP Palette\n0 0 0 extra\x00name\n",
        "GIMP Palette\n0 0 0 bell\x07\n",
        "GIMP Palette\r0 0 0\n",
        "GIMP Palette\n0 0 0\nbogus line\n",
    ):
        response = post_convert(client, bad_text)
        assert response.status_code == 400, bad_text
        assert response.json["ok"] is False
        assert response.json["error"]["details"] == {}
    after = client.get("/api/stats").json["data"]
    assert after["assets"] == before["assets"] == 0
    diagnostics = client.get("/api/diagnostics").json["data"]
    assert diagnostics["media_file_count"] == media_before == 0
    assert diagnostics["healthy"] is True
    assert client.get("/api/events").json["data"] == []


def test_too_many_colors_rejected(client):
    text = "GIMP Palette\n" + "".join(f"0 0 {i % 256}\n" for i in range(257))
    response = post_convert(client, text)
    assert response.status_code == 400
    assert client.get("/api/assets").json["data"]["total"] == 0


def test_oversize_text_rejected_with_413(client):
    padding = "# " + "x" * (MAX_TEXT_BYTES) + "\n"
    response = post_convert(client, "GIMP Palette\n" + padding + "0 0 0\n")
    assert response.status_code == 413
    assert response.json["error"]["code"] == "payload_too_large"
    assert client.get("/api/assets").json["data"]["total"] == 0


@pytest.mark.parametrize("size", [7, 129, 8.5, "16", True, None])
def test_swatch_size_validation(client, size):
    payload = {"text": "GIMP Palette\n0 0 0\n"}
    if size is not None:
        payload["swatch_size"] = size
    response = client.post("/api/palettes/convert", json=payload)
    assert 400 <= response.status_code < 500
    assert response.json["ok"] is False


@pytest.mark.parametrize("size", [8, 128])
def test_swatch_size_boundaries(client, size):
    assert post_convert(client, "GIMP Palette\n0 0 0\n", swatch_size=size).status_code == 201


def test_request_field_validation(client):
    assert client.post("/api/palettes/convert", json={"text": 123, "swatch_size": 16}).status_code == 400
    response = client.post(
        "/api/palettes/convert",
        json={"text": "GIMP Palette\n0 0 0\n", "swatch_size": 16, "extra": 1},
    )
    assert response.status_code == 400
    assert "extra" in response.json["error"]["message"] + str(response.json["error"]["details"])
    assert client.post("/api/palettes/convert", json=[]).status_code == 400


def test_text_not_string_is_studio_error():
    with pytest.raises(StudioError):
        parse_gpl(123)


def test_whitespace_and_chinese_names_preserved(client):
    text = "GIMP Palette\nName: 带 空格 的名字\nColumns: 1\n10 20 30  红色 一号 \n"
    data = post_convert(client, text, swatch_size=8).json["data"]
    assert data["name"] == "带 空格 的名字"
    assert data["colors"][0]["name"] == "红色 一号"
    assert data["gpl"].split("\n")[1] == "Name: 带 空格 的名字"


def test_bom_and_leading_blank_lines_rejected():
    with pytest.raises(StudioError):
        parse_gpl("﻿GIMP Palette\n0 0 0\n")
    with pytest.raises(StudioError):
        parse_gpl("\nGIMP Palette\n0 0 0\n")


def test_interop_example_round_trip_over_http(app, tmp_path):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        five_color_gpl = "GIMP Palette\nName: 测试色板\nColumns: 3\n# 注释\n0 0 0 黑色\n255 0 0 Red\n0 255 0 绿色\n0 0 255\n10 20 30 重复\n"
        gpl_path = tmp_path / "sample.gpl"
        gpl_path.write_text(five_color_gpl, encoding="utf-8", newline="")
        script = Path(__file__).parents[1] / "examples" / "palette_interop.py"
        result = subprocess.run(
            [sys.executable, "-I", str(script), str(gpl_path),
             "--swatch-size", "12", "--base-url", f"http://127.0.0.1:{port}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "round-trip 校验通过" in result.stdout
        canonical = tmp_path / "sample.canonical.gpl"
        swatch = tmp_path / "sample.swatch.png"
        assert canonical.read_text(encoding="utf-8").startswith("GIMP Palette\nName: 测试色板\n")
        with Image.open(swatch) as image:
            assert image.format == "PNG"
            assert image.size == (36, 24)
            assert image.getpixel((35, 23)) == (0, 0, 0, 0)
    finally:
        server.shutdown()
        thread.join(timeout=5)
