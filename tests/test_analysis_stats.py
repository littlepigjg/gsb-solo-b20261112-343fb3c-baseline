"""Inspector 总体统计语义回归测试。

统计口径（与 backend/analysis.py 保持一致）：
- 先按现有 Lanczos 缩略到 768x768 范围内；
- alpha>0 的像素参与 RGB 均值与总体标准差；
- 亮度取 Pillow L 值的可见总体，反差为总体标准差；
- 百分位为 1/5/25/50/75/95/99 的线性插值；
- 熵从总体 256 级亮度直方图计算 log2；
- 全透明图按单个零像素回退。
"""
import io

import numpy as np
from PIL import Image


PERCENTILE_KEYS = (1, 5, 25, 50, 75, 95, 99)


def upload(client, array):
    buffer = io.BytesIO()
    Image.fromarray(array, "RGBA").save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, "case.png")})
    assert response.status_code == 201, response.json
    return response.json["data"]["id"]


def analysis(client, asset_id):
    response = client.get(f"/api/assets/{asset_id}/analysis")
    assert response.status_code == 200
    body = response.json
    assert body["ok"] is True
    return body["data"]


def expected_stats(array):
    """独立实现一遍规格，作为期望值来源。"""
    image = Image.fromarray(array, "RGBA")
    image.thumbnail((768, 768), Image.Resampling.LANCZOS)
    rgba = np.asarray(image, dtype=np.uint8)
    visible = rgba[..., 3] > 0
    rgb = rgba[..., :3][visible]
    if rgb.shape[0] == 0:
        rgb = np.zeros((1, 3), dtype=np.uint8)
        gray_visible = np.zeros(1, dtype=np.float64)
    else:
        gray_visible = np.asarray(image.convert("L"), dtype=np.float64)[visible]

    hists = {name: np.bincount(rgb[:, i], minlength=256)
             for i, name in enumerate(("red", "green", "blue"))}
    luma = np.bincount(gray_visible.astype(np.uint8), minlength=256)
    probs = luma[luma > 0] / luma.sum()
    entropy = round(max(0.0, float(-(probs * np.log2(probs)).sum())), 3)
    return {
        "sample_size": list(image.size),
        "histograms": {**{k: v.tolist() for k, v in hists.items()},
                       "luminance": luma.tolist()},
        "mean_rgb": [round(float(v), 2) for v in rgb.mean(axis=0)],
        "std_rgb": [round(float(v), 2) for v in rgb.std(axis=0)],
        "brightness": round(float(gray_visible.mean()), 2),
        "contrast": round(float(gray_visible.std()), 2),
        "entropy_bits": entropy,
        "percentiles": {str(k): round(float(v), 2) for k, v in
                        zip(PERCENTILE_KEYS,
                            np.percentile(gray_visible, PERCENTILE_KEYS))},
    }


def assert_matches_spec(client, array):
    data = analysis(client, upload(client, array))
    want = expected_stats(array)
    assert data["sample_size"] == want["sample_size"]
    assert max(data["sample_size"]) <= 768
    for channel in ("red", "green", "blue", "luminance"):
        assert data["histograms"][channel] == want["histograms"][channel]
    assert data["mean_rgb"] == want["mean_rgb"]
    assert data["std_rgb"] == want["std_rgb"]
    assert data["brightness"] == want["brightness"]
    assert data["contrast"] == want["contrast"]
    assert data["entropy_bits"] == want["entropy_bits"]
    assert data["percentiles"] == want["percentiles"]
    return data


def test_stats_match_histograms_on_partial_alpha(client):
    rng = np.random.default_rng(7)
    array = np.zeros((120, 200, 4), dtype=np.uint8)
    array[..., :3] = rng.integers(0, 256, (120, 200, 3), dtype=np.uint16)
    array[..., 3] = 255
    array[:30, :, 3] = 0      # 全透明条带，不参与统计
    array[30:60, :, 3] = 128  # 半透明条带，alpha>0 仍参与
    data = assert_matches_spec(client, array)

    # 统计必须能由返回的直方图直接推出
    levels = np.arange(256, dtype=np.float64)
    for i, channel in enumerate(("red", "green", "blue")):
        hist = np.array(data["histograms"][channel], dtype=np.float64)
        mean = round(float((levels * hist).sum() / hist.sum()), 2)
        std = round(float(np.sqrt(((levels - mean) ** 2 * hist).sum() / hist.sum())), 2)
        assert data["mean_rgb"][i] == mean
        assert data["std_rgb"][i] == std
    luma = np.array(data["histograms"]["luminance"], dtype=np.float64)
    probs = luma[luma > 0] / luma.sum()
    assert data["entropy_bits"] == round(float(-(probs * np.log2(probs)).sum()), 3)
    assert data["transparent_percent"] > 0


def test_regions_of_different_sizes(client):
    # 10x10 黑块 + 90x100 白底：区域大小不同也按总体计数
    array = np.full((100, 100, 4), 255, dtype=np.uint8)
    array[:10, :10, :3] = 0
    assert_matches_spec(client, array)


def test_constant_regions_preserve_global_variance(client):
    # 每个区域内部常量，但全图存在差异：旧的分块平均会把反差抹平
    array = np.zeros((100, 100, 4), dtype=np.uint8)
    array[..., 3] = 255
    array[:50, :, :3] = (40, 40, 40)
    array[50:, :, :3] = (210, 210, 210)
    data = assert_matches_spec(client, array)
    assert data["mean_rgb"] == [125.0, 125.0, 125.0]
    assert data["std_rgb"] == [85.0, 85.0, 85.0]
    assert data["brightness"] == 125.0
    assert data["contrast"] == 85.0
    assert data["entropy_bits"] == 1.0
    assert data["percentiles"]["50"] == 125.0


def test_transparent_gaps_excluded(client):
    # 两块不透明常量区域，中间与四周全透明
    array = np.zeros((100, 100, 4), dtype=np.uint8)
    array[10:30, 10:90, :3] = (200, 100, 50)
    array[10:30, 10:90, 3] = 255
    array[60:90, 10:90, :3] = (10, 200, 90)
    array[60:90, 10:90, 3] = 255
    data = assert_matches_spec(client, array)
    first = np.tile(np.array([200, 100, 50]), (20 * 80, 1))
    second = np.tile(np.array([10, 200, 90]), (30 * 80, 1))
    merged = np.concatenate([first, second])
    assert data["mean_rgb"] == [round(float(v), 2) for v in merged.mean(axis=0)]
    assert data["std_rgb"] == [round(float(v), 2) for v in merged.std(axis=0)]
    # 仅 4000/10000 个像素不透明
    assert data["transparent_percent"] == round(6000 / 10000 * 100, 2)


def test_distinct_rgb_distributions(client):
    array = np.zeros((256, 256, 4), dtype=np.uint8)
    array[..., 0] = np.tile(np.arange(256, dtype=np.uint8), (256, 1))
    array[..., 1] = np.tile(np.arange(256, dtype=np.uint8).reshape(256, 1), (1, 256))
    array[..., 2] = ((array[..., 0].astype(np.uint16)
                      + array[..., 1].astype(np.uint16)) % 256).astype(np.uint8)
    array[..., 3] = 255
    assert_matches_spec(client, array)


def test_fully_transparent_falls_back_to_single_zero_pixel(client):
    array = np.zeros((48, 64, 4), dtype=np.uint8)
    data = assert_matches_spec(client, array)
    assert data["mean_rgb"] == [0.0, 0.0, 0.0]
    assert data["std_rgb"] == [0.0, 0.0, 0.0]
    assert data["brightness"] == 0.0
    assert data["contrast"] == 0.0
    assert data["entropy_bits"] == 0.0  # 不得出现 -0.0
    assert list(data["percentiles"].values()) == [0.0] * 7
    assert sum(data["histograms"]["luminance"]) == 1
    assert data["histograms"]["luminance"][0] == 1
    assert data["transparent_percent"] == 100.0
    # 其余公开字段保持不变
    for field in ("asset_id", "width", "height", "aspect_ratio", "megapixels",
                  "laplacian_variance", "dark_clipping_percent",
                  "light_clipping_percent", "palette", "suggestions"):
        assert field in data


def test_large_image_uses_lanczos_thumbnail(client):
    rng = np.random.default_rng(11)
    array = np.zeros((1200, 800, 4), dtype=np.uint8)
    array[..., :3] = rng.integers(0, 256, (1200, 800, 3), dtype=np.uint16)
    array[..., 3] = 255
    data = assert_matches_spec(client, array)
    assert data["sample_size"] == [512, 768]
    assert data["width"] == 800 and data["height"] == 1200


def test_percentiles_use_linear_interpolation(client):
    # 偶数个互不相同的灰度级，线性插值中位数落在两级之间（半点）。
    # 选 0,2,...,198 共 100 级：中间两级为 98 与 100，中位数必须是 99.0 之外的半点；
    # 改用 0..99 共 100 级：中间两级 49、50 -> 中位数 49.5，证明不是最近邻取值。
    gray = np.arange(100, dtype=np.uint8)
    array = np.zeros((1, 100, 4), dtype=np.uint8)
    array[0, :, 0] = gray
    array[0, :, 1] = gray
    array[0, :, 2] = gray
    array[0, :, 3] = 255
    data = assert_matches_spec(client, array)
    assert data["percentiles"]["50"] == 49.5
    assert data["percentiles"]["1"] == round(float(np.percentile(gray.astype(float), 1)), 2)
    assert data["percentiles"]["99"] == round(float(np.percentile(gray.astype(float), 99)), 2)


def test_constant_image_zero_entropy_and_contrast(client):
    array = np.zeros((32, 48, 4), dtype=np.uint8)
    array[..., :3] = (120, 60, 200)
    array[..., 3] = 255
    data = assert_matches_spec(client, array)
    assert data["contrast"] == 0.0
    assert data["entropy_bits"] == 0.0
    assert data["std_rgb"] == [0.0, 0.0, 0.0]
    assert list({v for v in data["percentiles"].values()}) == [data["brightness"]]
