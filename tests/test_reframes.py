import io
import pytest
from PIL import Image

from backend.app import create_app


def reframe(client, uploaded, targets, focus=None, **extra):
    payload = {"asset_id": uploaded["id"], "focus": focus or [40, 30, 120, 100], "targets": targets}
    payload.update(extra)
    return client.post("/api/reframes", json=payload)


def test_reframe_happy_path_geometry_order_and_lineage(client, uploaded):
    response = reframe(client, uploaded, [
        {"name": "横版", "width": 200, "height": 100},
        {"name": "竖版", "width": 100, "height": 200},
    ])
    assert response.status_code == 201, response.json
    data = response.json["data"]
    assert data["source_id"] == uploaded["id"]
    results = data["results"]
    assert [item["name"] for item in results] == ["横版", "竖版"]

    source_width, source_height = uploaded["width"], uploaded["height"]
    focus_left, focus_top, focus_width, focus_height = 40, 30, 120, 100
    center_x = focus_left + focus_width / 2
    center_y = focus_top + focus_height / 2
    expected_windows = (
        (384.0, 192.0),   # 2:1 横版：受源宽限制，窗口 384×192
        (160.0, 320.0),   # 1:2 竖版：受源高限制，窗口 160×320
    )
    for item, target, expected in zip(results, ({"width": 200, "height": 100}, {"width": 100, "height": 200}), expected_windows):
        box = item["box"]
        window_width = box[2] - box[0]
        window_height = box[3] - box[1]
        assert (window_width, window_height) == pytest.approx(expected)
        assert window_width / window_height == pytest.approx(target["width"] / target["height"])
        assert window_width <= source_width and window_height <= source_height
        # 连续窗口不预先取整。
        assert any(isinstance(component, float) for component in box)
        # 窗口完整包含 focus 且位于源图内部。
        assert box[0] <= focus_left and box[1] <= focus_top
        assert box[2] >= focus_left + focus_width and box[3] >= focus_top + focus_height
        assert 0 <= box[0] and 0 <= box[1]
        assert box[2] <= source_width and box[3] <= source_height
        # 默认 anchor [0.5, 0.5]：理想位置即 focus 中心对齐窗口中心；
        # 被钳制的轴则贴到允许区间边界，未被钳制的轴保持理想位置。
        min_left = max(0.0, focus_left + focus_width - window_width)
        max_left = min(float(focus_left), source_width - window_width)
        min_top = max(0.0, focus_top + focus_height - window_height)
        max_top = min(float(focus_top), source_height - window_height)
        ideal_left = min(max(center_x - window_width / 2, min_left), max_left)
        ideal_top = min(max(center_y - window_height / 2, min_top), max_top)
        assert box[0] == pytest.approx(ideal_left)
        assert box[1] == pytest.approx(ideal_top)
        asset = item["asset"]
        assert asset["width"] == target["width"]
        assert asset["height"] == target["height"]
        assert asset["name"] == item["name"]
        assert asset["parent_id"] == uploaded["id"]
        assert asset["root_id"] == uploaded["id"]
        assert asset["mode"] == "RGBA"
        assert client.get(asset["url"]).status_code == 200
        history = client.get(f"/api/assets/{asset['id']}/history").json["data"]
        assert [record["id"] for record in history] == [uploaded["id"], asset["id"]]
        assert asset["metadata"]["reframe"]["box"] == box
        assert asset["steps"][0]["operation"] == "reframe"

    assert client.get("/api/assets").json["data"]["total"] == 3


def test_reframe_anchor_shifts_and_clamps(client, uploaded):
    # 横版 2:1 窗口 384×192，水平允许区间退化为单点 left=0；
    # anchor 0 表示 focus 中心对齐窗口起点：理想 top=80，钳制到 max_top=30。
    response = reframe(client, uploaded, [{"name": "锚点0", "width": 200, "height": 100}], anchor=[0.0, 0.0])
    assert response.status_code == 201, response.json
    box = response.json["data"]["results"][0]["box"]
    assert box == pytest.approx([0.0, 30.0, 384.0, 222.0])

    # anchor 1 表示 focus 中心对齐窗口末端：理想 top=80-192=-112，钳制到 min_top=0。
    response = reframe(client, uploaded, [{"name": "锚点1", "width": 200, "height": 100}], anchor=[1.0, 1.0])
    assert response.status_code == 201, response.json
    box = response.json["data"]["results"][0]["box"]
    assert box == pytest.approx([0.0, 0.0, 384.0, 192.0])

    # 竖版 1:2 窗口 160×320，垂直允许区间退化为单点 top=0；
    # anchor 0 时理想 left=100 落在 [0, 40] 之外，钳制到 40；anchor 1 时理想 left=-60，钳制到 0。
    response = reframe(client, uploaded, [{"name": "竖锚点0", "width": 100, "height": 200}], anchor=[0.0, 0.0])
    assert response.status_code == 201, response.json
    box = response.json["data"]["results"][0]["box"]
    assert box == pytest.approx([40.0, 0.0, 200.0, 320.0])

    response = reframe(client, uploaded, [{"name": "竖锚点1", "width": 100, "height": 200}], anchor=[1.0, 1.0])
    assert response.status_code == 201, response.json
    box = response.json["data"]["results"][0]["box"]
    assert box == pytest.approx([0.0, 0.0, 160.0, 320.0])

    # 竖版窗口 160×320 的垂直区间退化为 top=0，水平区间 [0, 40]；
    # anchor 0.5 时水平理想 left=20 未被钳制，窗口中心正好对齐 focus 中心。
    response = reframe(client, uploaded, [{"name": "竖居中", "width": 100, "height": 200}], anchor=[0.5, 0.5])
    assert response.status_code == 201, response.json
    box = response.json["data"]["results"][0]["box"]
    assert box == pytest.approx([20.0, 0.0, 180.0, 320.0])
    assert box[0] + (box[2] - box[0]) / 2 == pytest.approx(100.0)


def test_reframe_infeasible_target_rejects_whole_request(client, uploaded):
    before = client.get("/api/assets").json["data"]["total"]
    original_bytes = client.get(uploaded["url"]).data
    # 1:3 竖版窗口宽约 106.7，小于 focus 宽 120，水平允许区间为空，该目标不可行。
    response = reframe(client, uploaded, [
        {"name": "可行", "width": 200, "height": 100},
        {"name": "不可行", "width": 100, "height": 300},
    ])
    assert response.status_code == 400
    assert response.json["ok"] is False
    assert response.json["error"]["code"] == "infeasible_target"
    assert response.json["error"]["details"]["name"] == "不可行"
    # 末项不可行不能留下前面的结果。
    assert client.get("/api/assets").json["data"]["total"] == before
    assert client.get(uploaded["url"]).data == original_bytes
    diagnostics = client.get("/api/diagnostics").json["data"]
    assert diagnostics["healthy"] is True
    assert diagnostics["orphan_media_count"] == 0


def test_reframe_pixel_exact_integer_window(client, uploaded):
    # 正方形源图、正方形 focus、正方形目标：窗口与映射均为整数，像素应与直接缩放完全一致。
    buffer = io.BytesIO()
    Image.new("RGBA", (320, 320), (12, 34, 56, 255)).save(buffer, "PNG")
    buffer.seek(0)
    square = client.post("/api/assets", data={"file": (buffer, "square.png")}).json["data"]
    response = client.post("/api/reframes", json={
        "asset_id": square["id"],
        "focus": [40, 40, 100, 100],
        "targets": [{"name": "整窗", "width": 160, "height": 160}],
    })
    assert response.status_code == 201, response.json
    result = response.json["data"]["results"][0]
    assert result["box"] == pytest.approx([0.0, 0.0, 320.0, 320.0])
    with Image.open(io.BytesIO(client.get(result["asset"]["url"]).data)) as image:
        assert image.size == (160, 160)
        assert image.mode == "RGBA"
        assert image.getpixel((10, 20)) == (12, 34, 56, 255)


def test_reframe_continuous_window_matches_reference(client, sample_image, uploaded):
    target = {"name": "亚像素", "width": 150, "height": 210}
    response = reframe(client, uploaded, [target], anchor=[0.25, 0.75])
    assert response.status_code == 201, response.json
    box = response.json["data"]["results"][0]["box"]
    left, top, right, bottom = box
    # 窗口宽高带小数，证明没有先取整裁剪。
    assert right - left != int(right - left) or bottom - top != int(bottom - top)

    # 独立按题目公式重算几何。
    source_width, source_height = sample_image.size
    ratio = target["width"] / target["height"]
    window_width = min(float(source_width), source_height * ratio)
    window_height = window_width / ratio
    focus = {"left": 40, "top": 30, "right": 160, "bottom": 130}
    min_left = max(0.0, focus["right"] - window_width)
    max_left = min(float(focus["left"]), source_width - window_width)
    min_top = max(0.0, focus["bottom"] - window_height)
    max_top = min(float(focus["top"]), source_height - window_height)
    ideal_left = (focus["left"] + focus["right"]) / 2 - 0.25 * window_width
    ideal_top = (focus["top"] + focus["bottom"]) / 2 - 0.75 * window_height
    assert left == pytest.approx(min(max(ideal_left, min_left), max_left))
    assert top == pytest.approx(min(max(ideal_top, min_top), max_top))

    # 连续浮点窗口直接交给 Lanczos（不先整数裁剪），产物应与独立调用逐字节一致。
    reference = sample_image.resize(
        (target["width"], target["height"]),
        Image.Resampling.LANCZOS,
        box=(left, top, right, bottom),
    )
    with Image.open(io.BytesIO(client.get(response.json["data"]["results"][0]["asset"]["url"]).data)) as produced:
        assert produced.tobytes() == reference.tobytes()


def test_reframe_retains_rgba_alpha_semantics(client, uploaded):
    # focus 覆盖整张源图时窗口等于源图，alpha 渐变必须保留（含 0 与 120 的行）。
    response = client.post("/api/reframes", json={
        "asset_id": uploaded["id"],
        "focus": [0, 0, uploaded["width"], uploaded["height"]],
        "targets": [{"name": "等比缩放", "width": 192, "height": 160}],
    })
    assert response.status_code == 201, response.json
    result = response.json["data"]["results"][0]
    assert result["box"] == pytest.approx([0.0, 0.0, float(uploaded["width"]), float(uploaded["height"])])
    with Image.open(io.BytesIO(client.get(result["asset"]["url"]).data)) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((5, 2))[3] == 0
        assert image.getpixel((5, 7))[3] == 120


def test_reframe_supports_target_sides_up_to_4096(client, uploaded):
    response = reframe(client, uploaded, [{"name": "最大", "width": 4096, "height": 4096}])
    assert response.status_code == 201, response.json
    asset = response.json["data"]["results"][0]["asset"]
    assert (asset["width"], asset["height"]) == (4096, 4096)
    diagnostics = client.get("/api/diagnostics").json["data"]
    assert diagnostics["healthy"] is True


@pytest.mark.parametrize("asset_id, expected_code", [
    ("bad-id", 400),
    (None, 400),
    ("0123456789abcdef0123456789abcdef", 404),
], ids=["malformed_asset_id", "null_asset_id", "unknown_asset_id"])
def test_reframe_asset_id_validation(client, uploaded, asset_id, expected_code):
    payload = {
        "asset_id": asset_id,
        "focus": [40, 30, 120, 100],
        "targets": [{"name": "x", "width": 10, "height": 10}],
    }
    response = client.post("/api/reframes", json=payload)
    assert response.status_code == expected_code
    assert response.json["ok"] is False


def test_reframe_missing_asset_id_field(client, uploaded):
    response = client.post("/api/reframes", json={
        "focus": [40, 30, 120, 100],
        "targets": [{"name": "x", "width": 10, "height": 10}],
    })
    assert response.status_code == 400
    assert response.json["ok"] is False


@pytest.mark.parametrize("payload", [
    {"asset_id": "placeholder", "focus": [40, 30, 120], "targets": [{"name": "x", "width": 10, "height": 10}]},
    {"asset_id": "placeholder", "focus": [40, 30, 0, 100], "targets": [{"name": "x", "width": 10, "height": 10}]},
    {"asset_id": "placeholder", "focus": [-1, 0, 10, 10], "targets": [{"name": "x", "width": 10, "height": 10}]},
    {"asset_id": "placeholder", "focus": [0, 0, 385, 10], "targets": [{"name": "x", "width": 10, "height": 10}]},
    {"asset_id": "placeholder", "focus": [40, 30, 120.5, 100], "targets": [{"name": "x", "width": 10, "height": 10}]},
    {"asset_id": "placeholder", "anchor": [0.5], "focus": [40, 30, 120, 100], "targets": [{"name": "x", "width": 10, "height": 10}]},
    {"asset_id": "placeholder", "anchor": [0.0, 1.5], "focus": [40, 30, 120, 100], "targets": [{"name": "x", "width": 10, "height": 10}]},
    {"asset_id": "placeholder", "focus": [40, 30, 120, 100], "targets": []},
    {"asset_id": "placeholder", "focus": [40, 30, 120, 100], "targets": [{"name": "x", "width": 10}]},
    {"asset_id": "placeholder", "focus": [40, 30, 120, 100], "targets": [{"name": "  ", "width": 10, "height": 10}]},
    {"asset_id": "placeholder", "focus": [40, 30, 120, 100],
     "targets": [{"name": "x", "width": 10, "height": 10}, {"name": "x", "width": 20, "height": 20}]},
    {"asset_id": "placeholder", "focus": [40, 30, 120, 100], "targets": [{"name": "x", "width": 0, "height": 10}]},
    {"asset_id": "placeholder", "focus": [40, 30, 120, 100], "targets": [{"name": "x", "width": 10, "height": 4097}]},
    {"asset_id": "placeholder", "focus": [40, 30, 120, 100], "targets": [{"name": "x", "width": "10.5", "height": 10}]},
    {"asset_id": "placeholder", "focus": [40, 30, 120, 100], "targets": [{"name": "x", "width": 10, "height": 10}], "extra": 1},
])
def test_reframe_request_validation(client, uploaded, payload):
    payload["asset_id"] = uploaded["id"] if payload["asset_id"] == "placeholder" else payload["asset_id"]
    before = client.get("/api/assets").json["data"]["total"]
    response = client.post("/api/reframes", json=payload)
    assert 400 <= response.status_code < 500
    assert response.json["ok"] is False
    assert client.get("/api/assets").json["data"]["total"] == before


def test_reframe_thirteen_targets_rejected(client, uploaded):
    payload = {
        "asset_id": uploaded["id"],
        "focus": [40, 30, 120, 100],
        "targets": [{"name": f"目标{index}", "width": 10, "height": 10} for index in range(13)],
    }
    response = client.post("/api/reframes", json=payload)
    assert response.status_code == 400
    assert response.json["ok"] is False
    assert client.get("/api/assets").json["data"]["total"] == 1


def test_reframe_missing_asset_returns_404_and_creates_nothing(client, uploaded):
    response = client.post("/api/reframes", json={
        "asset_id": "0" * 32,
        "focus": [40, 30, 120, 100],
        "targets": [{"name": "x", "width": 10, "height": 10}],
    })
    assert response.status_code == 404
    assert client.get("/api/assets").json["data"]["total"] == 1


def test_reframe_original_remains_unchanged(client, uploaded):
    before = client.get(uploaded["url"]).data
    response = reframe(client, uploaded, [{"name": "副本", "width": 64, "height": 64}])
    assert response.status_code == 201, response.json
    assert client.get(uploaded["url"]).data == before
    original = client.get(f"/api/assets/{uploaded['id']}").json["data"]
    assert original["parent_id"] is None
    assert original["root_id"] == uploaded["id"]


def test_reframe_rolls_back_when_a_later_save_fails(client, uploaded, monkeypatch):
    from backend.assets import AssetService

    before = client.get("/api/assets").json["data"]["total"]
    calls = {"count": 0}
    original_save = AssetService.save

    def flaky_save(self, image, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated storage failure")
        return original_save(self, image, **kwargs)

    monkeypatch.setattr(AssetService, "save", flaky_save)
    response = client.post("/api/reframes", json={
        "asset_id": uploaded["id"],
        "focus": [40, 30, 120, 100],
        "targets": [
            {"name": "第一个", "width": 64, "height": 64},
            {"name": "第二个", "width": 96, "height": 96},
            {"name": "第三个", "width": 128, "height": 128},
        ],
    })
    assert response.status_code == 500
    assert response.json["ok"] is False
    # 第一个已保存的副本必须随失败一起回滚，不留记录也不留文件。
    assert client.get("/api/assets").json["data"]["total"] == before
    assert client.get("/api/diagnostics").json["data"]["healthy"] is True


def test_reframe_unknown_field_rejected(client, uploaded):
    response = client.post("/api/reframes", json={
        "asset_id": uploaded["id"],
        "focus": [40, 30, 120, 100],
        "targets": [{"name": "x", "width": 10, "height": 10, "mode": "RGB"}],
    })
    assert response.status_code == 400
    assert "未知字段" in response.json["error"]["message"]
