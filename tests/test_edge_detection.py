"""Sobel / Prewitt / Scharr 修复后的行为契约。

要点：
- 各自使用标准 3×3 方向核、edge 边界；
- 梯度在完整图像上计算，1%/99% 百分位拉伸也是全局一次完成，
  不再按 32 行水平条带分别归一化（旧实现会把弱纹理放大成强边并产生接缝）；
- direction 选择 |gx|、|gy| 或 hypot；
- 百分位范围相等时输出零；之后再乘 gain、clip，invert 最后反相；
- 保留原始 alpha，float32 -> uint8 允许至多 1 单位误差。
"""
import io

import numpy as np
import pytest
from PIL import Image

from backend.registry import Registry, OperationContext
from backend.errors import StudioError
from backend.settings import Settings
from backend.imaging import convolution, percentile_stretch


EDGE_OPS = ["edge_sobel", "edge_prewitt", "edge_scharr"]

KERNELS = {
    "edge_sobel": (
        [[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]],
        [[-1, -2, -1], [0, 0, 0], [1, 2, 1]],
    ),
    "edge_prewitt": (
        [[-1, 0, 1], [-1, 0, 1], [-1, 0, 1]],
        [[-1, -1, -1], [0, 0, 0], [1, 1, 1]],
    ),
    "edge_scharr": (
        [[-3, 0, 3], [-10, 0, 10], [-3, 0, 3]],
        [[-3, -10, -3], [0, 0, 0], [3, 10, 3]],
    ),
}


@pytest.fixture(scope="module")
def context(tmp_path_factory):
    return OperationContext(Settings(root=tmp_path_factory.mktemp("edges")))


@pytest.fixture(scope="module")
def registry():
    return Registry()


def gray_of(image):
    return np.asarray(image.convert("L"), dtype=np.float32)


def reference_gradient(image, horizontal, vertical, direction, gain, invert):
    """与修复后规范等价的独立参考实现。"""
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    gx = convolution(gray, horizontal)
    gy = convolution(gray, vertical)
    if direction == "horizontal":
        value = np.abs(gx)
    elif direction == "vertical":
        value = np.abs(gy)
    else:
        value = np.hypot(gx, gy)
    magnitude = percentile_stretch(value) * gain
    if invert:
        magnitude = 255 - np.clip(magnitude, 0, 255)
    return magnitude


def make_image(pixels, alpha=None):
    pixels = np.asarray(pixels, dtype=np.uint8)
    if pixels.ndim == 2:
        rgba = np.repeat(pixels[..., None], 4, axis=2)
    else:
        rgba = pixels.copy()
    rgba[..., 3] = 255 if alpha is None else alpha
    return Image.fromarray(rgba, "RGBA")


@pytest.mark.parametrize("operation", EDGE_OPS)
@pytest.mark.parametrize("direction", ["horizontal", "vertical", "both"])
def test_matches_standard_kernels_and_global_stretch(operation, direction, registry, context):
    rng = np.random.default_rng(11)
    gray = rng.integers(0, 256, size=(48, 40)).astype(np.uint8)
    image = make_image(gray)
    horizontal, vertical = KERNELS[operation]

    output = registry.apply(
        image,
        {"operation": operation, "params": {"direction": direction, "gain": 1.0, "invert": False}},
        context,
    )
    expected = reference_gradient(image, horizontal, vertical, direction, 1.0, False)
    diff = np.abs(gray_of(output) - expected)
    assert diff.max() <= 1.0


@pytest.mark.parametrize("operation", EDGE_OPS)
def test_weak_texture_is_not_amplified_per_region(operation, registry, context):
    """同一张图中弱纹理区域不应因为局部归一化而被拉到 255。"""
    height, width = 64, 64
    gray = np.zeros((height, width), dtype=np.float32)
    gray[:32, :] = 128 + 10 * np.where(np.arange(width) % 2, 1, -1)[None, :]
    gray[32:, :] = 128 + 1 * np.where(np.arange(width) % 2, 1, -1)[None, :]
    image = make_image(gray)

    output = registry.apply(
        image,
        {"operation": operation, "params": {"direction": "horizontal", "gain": 1.0, "invert": False}},
        context,
    )
    result = gray_of(output)
    # 强纹理区域达到满量程，弱纹理区域应保持其真实梯度比例（约为强区的 1/10）。
    assert result[:32].max() == pytest.approx(255, abs=1)
    assert result[34:62].max() < 60


@pytest.mark.parametrize("operation", EDGE_OPS)
def test_no_seam_between_historical_stripe_bands(operation, registry, context):
    """跨越旧实现 32 行条带边界的结构，两侧必须连续、无接缝跳变。"""
    height, width = 97, 48
    ramp = np.tile(np.linspace(0, 255, width, dtype=np.float32), (height, 1))
    # 水平亮带同时覆盖第 31/32、63/64 行两个历史条带边界。
    ramp[30:34, :] += 60
    ramp[62:66, :] += 60
    image = make_image(np.clip(ramp, 0, 255))

    output = registry.apply(
        image,
        {"operation": operation, "params": {"direction": "both", "gain": 1.0, "invert": False}},
        context,
    )
    result = gray_of(output)
    row_diff = np.abs(np.diff(result, axis=0))
    seam_jump = max(float(row_diff[31].max()), float(row_diff[63].max()))
    interior_jump = float(np.delete(row_diff, [31, 63], axis=0).max())
    # 条带边界处的跳变不得明显大于图像内部的普通行跳变。
    assert seam_jump <= interior_jump + 2.0


@pytest.mark.parametrize("operation", EDGE_OPS)
@pytest.mark.parametrize("direction", ["horizontal", "vertical", "both"])
def test_equal_percentile_range_produces_zeros(operation, direction, registry, context):
    image = make_image(np.full((40, 40), 137, dtype=np.uint8))
    output = registry.apply(
        image,
        {"operation": operation, "params": {"direction": direction, "gain": 2.0, "invert": False}},
        context,
    )
    assert np.count_nonzero(gray_of(output)) == 0


@pytest.mark.parametrize("operation", EDGE_OPS)
def test_gain_applied_after_stretch_and_clipped(operation, registry, context):
    rng = np.random.default_rng(23)
    image = make_image(rng.integers(0, 256, size=(36, 36)).astype(np.uint8))
    horizontal, vertical = KERNELS[operation]
    # 参考值直接在 float32 域完成“拉伸 -> 乘 gain -> clip”，只在末尾取整一次。
    expected = np.clip(
        reference_gradient(image, horizontal, vertical, "both", 1.0, False) * 2.5, 0, 255
    )
    boosted = registry.apply(
        image,
        {"operation": operation, "params": {"direction": "both", "gain": 2.5, "invert": False}},
        context,
    )
    boosted_gray = gray_of(boosted)
    assert boosted_gray.max() <= 255
    assert np.abs(boosted_gray - expected).max() <= 1.0


@pytest.mark.parametrize("operation", EDGE_OPS)
@pytest.mark.parametrize("direction", ["horizontal", "vertical", "both"])
def test_invert_is_applied_last(operation, direction, registry, context):
    rng = np.random.default_rng(31)
    image = make_image(rng.integers(0, 256, size=(44, 44)).astype(np.uint8))
    normal = registry.apply(
        image,
        {"operation": operation, "params": {"direction": direction, "gain": 1.7, "invert": False}},
        context,
    )
    inverted = registry.apply(
        image,
        {"operation": operation, "params": {"direction": direction, "gain": 1.7, "invert": True}},
        context,
    )
    assert np.abs(gray_of(inverted) - (255 - gray_of(normal))).max() <= 1.0


@pytest.mark.parametrize("operation", EDGE_OPS)
def test_flat_inverted_image_is_uniform(operation, registry, context):
    image = make_image(np.full((24, 24), 90, dtype=np.uint8))
    inverted = registry.apply(
        image,
        {"operation": operation, "params": {"direction": "both", "gain": 1.0, "invert": True}},
        context,
    )
    result = gray_of(inverted)
    assert np.all(result == 255)


@pytest.mark.parametrize("operation", EDGE_OPS)
def test_alpha_channel_is_preserved(operation, registry, context):
    rng = np.random.default_rng(41)
    height, width = 50, 38
    gray = rng.integers(0, 256, size=(height, width)).astype(np.uint8)
    alpha = rng.integers(0, 256, size=(height, width)).astype(np.uint8)
    image = make_image(gray, alpha=alpha)

    output = registry.apply(
        image,
        {"operation": operation, "params": {"direction": "both", "gain": 1.0, "invert": False}},
        context,
    )
    assert output.mode == "RGBA"
    assert np.array_equal(np.asarray(output.getchannel("A")), alpha)


@pytest.mark.parametrize("operation", EDGE_OPS)
def test_edge_padding_keeps_border_defined(operation, registry, context):
    """edge 边界下，纯色图与仅在角点变化的图都不应出现 NaN/未定义输出。"""
    gray = np.full((20, 20), 100, dtype=np.uint8)
    gray[0, 0] = 200
    image = make_image(gray)
    output = registry.apply(
        image,
        {"operation": operation, "params": {"direction": "both", "gain": 1.0, "invert": False}},
        context,
    )
    result = gray_of(output)
    assert np.isfinite(result).all()
    assert result.min() >= 0 and result.max() <= 255


def test_horizontal_and_vertical_directions_are_distinct(registry, context):
    """水平方向核只响应纵向边界，垂直方向核只响应横向边界。"""
    image = make_image(np.full((32, 32), 128, dtype=np.uint8))
    # 一条纵向黑边（沿 y 延伸）：只有水平方向 |gx| 应产生强响应。
    source = np.array(image)
    source[:, 16, :3] = 0
    image = Image.fromarray(source)

    horizontal = registry.apply(
        image,
        {"operation": "edge_sobel", "params": {"direction": "horizontal", "gain": 1.0, "invert": False}},
        context,
    )
    vertical = registry.apply(
        image,
        {"operation": "edge_sobel", "params": {"direction": "vertical", "gain": 1.0, "invert": False}},
        context,
    )
    assert gray_of(horizontal).max() > 200
    assert gray_of(vertical).max() == 0


def test_both_combines_two_axes(registry, context):
    rng = np.random.default_rng(53)
    image = make_image(rng.integers(0, 256, size=(40, 40)).astype(np.uint8))
    horizontal, vertical = KERNELS["edge_sobel"]
    expected = reference_gradient(image, horizontal, vertical, "both", 1.0, False)
    output = registry.apply(
        image,
        {"operation": "edge_sobel", "params": {"direction": "both", "gain": 1.0, "invert": False}},
        context,
    )
    assert np.abs(gray_of(output) - expected).max() <= 1.0


def test_invalid_direction_rejected(registry, context):
    image = make_image(np.full((16, 16), 128, dtype=np.uint8))
    with pytest.raises(StudioError):
        registry.apply(
            image,
            {"operation": "edge_sobel", "params": {"direction": "diagonal"}},
            context,
        )


def test_process_endpoint_keeps_ok_envelope_and_download(client, uploaded):
    response = client.post("/api/process", json={
        "asset_id": uploaded["id"],
        "steps": [{"operation": "edge_scharr",
                   "params": {"direction": "both", "gain": 1.2, "invert": True}}],
        "name": "边缘修复验证",
    })
    assert response.status_code == 201
    body = response.json
    assert body["ok"] is True
    result = body["data"]
    assert result["width"] == uploaded["width"]
    assert result["height"] == uploaded["height"]

    original = client.get(uploaded["url"])
    assert original.status_code == 200
    with Image.open(io.BytesIO(original.data)) as source:
        expected_alpha = np.asarray(source.getchannel("A"))

    download = client.get(result["url"])
    assert download.status_code == 200
    with Image.open(io.BytesIO(download.data)) as rendered:
        assert rendered.size == (384, 320)
        result_alpha = np.asarray(rendered.getchannel("A"))
    assert np.array_equal(result_alpha, expected_alpha)


def test_business_error_uses_error_envelope(client, uploaded):
    response = client.post("/api/process", json={
        "asset_id": uploaded["id"],
        "steps": [{"operation": "edge_sobel", "params": {"direction": "diagonal"}}],
    })
    assert response.status_code == 400
    body = response.json
    assert body["ok"] is False
    assert "error" in body and body["error"]["message"]
