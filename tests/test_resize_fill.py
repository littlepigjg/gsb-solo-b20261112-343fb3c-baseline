"""resize_fill：连续坐标等比 cover 裁剪与透明感知 Lanczos 的专项测试。"""
import numpy as np
import pytest
from PIL import Image

from backend.registry import Registry, OperationContext
from backend.settings import Settings
from tests.lanczos_reference import premultiply, reference_fill


@pytest.fixture(scope="module")
def registry():
    return Registry()


def _context(tmp_path):
    return OperationContext(Settings(root=tmp_path))


def _fill(registry, image, tmp_path, width, height, anchor_x=50, anchor_y=50):
    step = {"operation": "resize_fill",
            "params": {"width": width, "height": height,
                       "anchor_x": anchor_x, "anchor_y": anchor_y}}
    return registry.apply(image, step, _context(tmp_path))


def _expected_window(source_width, source_height, target_width, target_height,
                     anchor_x, anchor_y):
    """与生产实现相同的连续窗口（含 float32 贴边钳制）。"""
    scale = max(target_width / source_width, target_height / source_height)
    window_width = target_width / scale
    window_height = target_height / scale
    left = (source_width - window_width) * anchor_x / 100
    top = (source_height - window_height) * anchor_y / 100
    left = min(max(left, 0.0), source_width - window_width)
    top = min(max(top, 0.0), source_height - window_height)
    box_left = max(float(np.float32(left)), 0.0)
    box_top = max(float(np.float32(top)), 0.0)
    box_right = min(float(np.float32(left + window_width)), float(source_width))
    box_bottom = min(float(np.float32(top + window_height)), float(source_height))
    return (box_left, box_top, box_right, box_bottom)


def _gradient(width, height):
    image = np.zeros((height, width, 4), dtype=np.uint8)
    image[..., 0] = np.linspace(0, 255, width)[None, :].repeat(height, 0)
    image[..., 1] = np.linspace(0, 255, height)[:, None].repeat(width, 1)
    image[..., 2] = 90
    image[..., 3] = 255
    return Image.fromarray(image, "RGBA")


def test_output_dimensions_and_mode(registry, tmp_path):
    image = _gradient(71, 53)
    for width, height in [(100, 100), (33, 99), (1, 1), (71, 53), (200, 50), (50, 200)]:
        output = _fill(registry, image, tmp_path, width, height)
        assert output.size == (width, height)
        assert output.mode == "RGBA"


def test_window_matches_target_aspect_ratio(registry, tmp_path):
    source_width, source_height = 333, 217
    image = _gradient(source_width, source_height)
    for target_width, target_height in [(400, 100), (100, 400), (257, 131), (64, 64)]:
        box = _expected_window(source_width, source_height,
                               target_width, target_height, 37, 63)
        window_ratio = (box[2] - box[0]) / (box[3] - box[1])
        assert window_ratio == pytest.approx(target_width / target_height, rel=1e-6)
        output = _fill(registry, image, tmp_path, target_width, target_height, 37, 63)
        assert output.size == (target_width, target_height)


def test_extreme_anchors_select_opposite_content(registry, tmp_path):
    image = _gradient(40, 30)
    left = np.asarray(_fill(registry, image, tmp_path, 10, 30, anchor_x=0))
    right = np.asarray(_fill(registry, image, tmp_path, 10, 30, anchor_x=100))
    assert right[..., 0].mean() > left[..., 0].mean()

    top = np.asarray(_fill(registry, image, tmp_path, 40, 10, anchor_y=0))
    bottom = np.asarray(_fill(registry, image, tmp_path, 40, 10, anchor_y=100))
    assert bottom[..., 1].mean() > top[..., 1].mean()


def test_anchor_position_is_monotonic(registry, tmp_path):
    image = _gradient(40, 30)
    means = [np.asarray(_fill(registry, image, tmp_path, 10, 30, anchor_x=anchor))[..., 0].mean()
             for anchor in (0, 25, 50, 75, 100)]
    assert all(means[i] < means[i + 1] for i in range(len(means) - 1))


def test_fractional_anchor_uses_continuous_offset(registry, tmp_path):
    # 源宽 10、窗口宽 7，水平偏移量为 3 * anchor/100；anchor=33 -> 0.99 像素，
    # 整数裁剪会落到 1，连续窗口应保留亚像素偏移，与 Pillow box 重采样一致。
    image = _gradient(10, 10)
    output = _fill(registry, image, tmp_path, 7, 10, anchor_x=33, anchor_y=50)
    box = _expected_window(10, 10, 7, 10, 33, 50)
    expected = image.resize((7, 10), Image.Resampling.LANCZOS, box=box)
    assert np.array_equal(np.asarray(output), np.asarray(expected))
    assert 0.0 < box[0] < 1.0


def test_bit_exact_against_pillow_continuous_window(registry, tmp_path):
    rng = np.random.default_rng(2026)
    for _ in range(60):
        source_width, source_height = rng.integers(3, 120, 2)
        target_width, target_height = rng.integers(1, 120, 2)
        pixels = rng.integers(0, 256, (source_height, source_width, 4), dtype=np.uint8)
        pixels[..., 3] = rng.choice([0, 1, 30, 128, 200, 255],
                                    (source_height, source_width))
        image = Image.fromarray(pixels, "RGBA")
        anchor_x, anchor_y = rng.integers(0, 101, 2)
        output = _fill(registry, image, tmp_path, int(target_width), int(target_height),
                       int(anchor_x), int(anchor_y))
        box = _expected_window(source_width, source_height, target_width, target_height,
                               anchor_x, anchor_y)
        expected = image.resize((int(target_width), int(target_height)),
                                Image.Resampling.LANCZOS, box=box)
        assert np.array_equal(np.asarray(output), np.asarray(expected))


def test_matches_independent_numpy_reference_within_one_unit(registry, tmp_path):
    # 独立 NumPy 复现采用双精度几何，而 Pillow 的 C 层用 float32；为隔离算法
    # 本身（Lanczos 系数、定点累加、预乘/反预乘），这里使用整数对齐的全幅窗口，
    # 两条实现的几何输入完全相同，关键通道必须落在 1 个量化级以内。
    rng = np.random.default_rng(11)
    worst_premultiplied = 0
    worst_alpha = 0
    worst_opaque_rgb = 0
    for _ in range(40):
        source_width, source_height = rng.integers(4, 90, 2)
        target_width, target_height = rng.integers(2, 90, 2)
        pixels = rng.integers(0, 256, (source_height, source_width, 4), dtype=np.uint8)
        pixels[..., 3] = rng.choice([0, 1, 30, 128, 200, 255],
                                    (source_height, source_width))
        image = Image.fromarray(pixels, "RGBA")
        box = (0.0, 0.0, float(source_width), float(source_height))
        output = image.resize((int(target_width), int(target_height)),
                              Image.Resampling.LANCZOS, box=box)
        reference = reference_fill(image, (int(target_width), int(target_height)), box)
        # 预乘空间衡量真实重采样误差，规避反预乘在极低 alpha 上的放大效应。
        premul_diff = np.abs(
            np.asarray(output.convert("RGBa"), dtype=np.int64)
            - np.asarray(reference.convert("RGBa"), dtype=np.int64)
        )
        rgba_diff = np.abs(np.asarray(output, dtype=np.int64)
                           - np.asarray(reference, dtype=np.int64))
        worst_premultiplied = max(worst_premultiplied, premul_diff.max())
        worst_alpha = max(worst_alpha, rgba_diff[..., 3].max())
        opaque = np.asarray(output)[..., 3] >= 250
        if opaque.any():
            worst_opaque_rgb = max(worst_opaque_rgb, rgba_diff[..., :3][opaque].max())
    assert worst_premultiplied <= 1
    assert worst_alpha <= 1
    assert worst_opaque_rgb <= 1


def test_fractional_window_reference_remains_close(registry, tmp_path):
    # 分数窗口下双精度与 float32 几何存在亚像素级系数差异：生产实现始终与
    # Pillow 原生结果逐位一致，与独立参考仅允许极少数像素相差一个量化级。
    rng = np.random.default_rng(23)
    for _ in range(30):
        source_width, source_height = rng.integers(6, 80, 2)
        target_width, target_height = rng.integers(2, 80, 2)
        pixels = rng.integers(0, 256, (source_height, source_width, 4), dtype=np.uint8)
        pixels[..., 3] = rng.choice([0, 30, 128, 255], (source_height, source_width))
        image = Image.fromarray(pixels, "RGBA")
        anchor_x, anchor_y = (37, 63)
        output = _fill(registry, image, tmp_path, int(target_width), int(target_height),
                       anchor_x, anchor_y)
        box = _expected_window(source_width, source_height, target_width, target_height,
                               anchor_x, anchor_y)
        pillow = image.resize((int(target_width), int(target_height)),
                              Image.Resampling.LANCZOS, box=box)
        assert np.array_equal(np.asarray(output), np.asarray(pillow))
        reference = reference_fill(image, (int(target_width), int(target_height)), box)
        premul_diff = np.abs(
            np.asarray(output.convert("RGBa"), dtype=np.int64)
            - np.asarray(reference.convert("RGBa"), dtype=np.int64)
        )
        assert premul_diff.max() <= 1 or np.count_nonzero(premul_diff > 1) <= 2


def test_transparent_hidden_color_does_not_pollute_edge(registry, tmp_path):
    # 左半为不透明纯红，右半全透明但隐藏色为鲜绿；脏边会把绿色带进不透明区。
    width = 14
    pixels = np.zeros((4, width, 4), dtype=np.uint8)
    pixels[:, : width // 2] = (255, 0, 0, 255)
    pixels[:, width // 2:] = (0, 255, 0, 0)
    image = Image.fromarray(pixels, "RGBA")
    output = np.asarray(_fill(registry, image, tmp_path, 7, 4))
    solid = output[..., 3] > 200
    assert output[..., 1][solid].max() <= 1
    # 全透明输出像素的隐藏 RGB 必须被清零，而不是保留源图的绿色。
    transparent = output[..., 3] == 0
    assert (output[transparent][..., :3] == 0).all()


def test_semitransparent_blend_is_premultiplied(registry, tmp_path):
    # 单个半透明像素放大后，预乘语义下颜色随 alpha 等比衰减。
    pixels = np.zeros((2, 2, 4), dtype=np.uint8)
    pixels[:] = (200, 100, 50, 128)
    image = Image.fromarray(pixels, "RGBA")
    output = _fill(registry, image, tmp_path, 8, 8)
    result = np.asarray(output)
    expected_premul = premultiply(pixels)[0, 0, :3]
    center = result[4, 4]
    assert center[3] == 128
    assert np.abs(center[:3].astype(np.int64) - expected_premul * 255 // 128).max() <= 1


def test_same_size_is_identity(registry, tmp_path):
    image = _gradient(40, 30)
    assert np.array_equal(np.asarray(_fill(registry, image, tmp_path, 40, 30)),
                          np.asarray(image))
    # 同尺寸且全透明：隐藏色必须原样保留（Pillow 的同尺寸 copy 快速路径）。
    transparent = np.zeros((12, 12, 4), dtype=np.uint8)
    transparent[..., :3] = 123
    transparent_image = Image.fromarray(transparent, "RGBA")
    assert np.array_equal(
        np.asarray(_fill(registry, transparent_image, tmp_path, 12, 12)), transparent)


def test_odd_non_square_sources_and_targets(registry, tmp_path):
    rng = np.random.default_rng(5)
    for source_size, target_size in [((37, 23), (11, 29)), ((23, 37), (29, 11)),
                                     ((3, 3), (2, 2)), ((101, 77), (33, 99)),
                                     ((5, 7), (7, 5))]:
        pixels = rng.integers(0, 256, (source_size[1], source_size[0], 4), dtype=np.uint8)
        image = Image.fromarray(pixels, "RGBA")
        output = _fill(registry, image, tmp_path, target_size[0], target_size[1], 33, 71)
        assert output.size == target_size and output.mode == "RGBA"


def test_edge_anchors_never_overflow_on_extreme_shapes(registry, tmp_path):
    rng = np.random.default_rng(9)
    for source_width, source_height in [(15, 55), (7, 11), (3, 3), (100, 2), (2, 100)]:
        pixels = rng.integers(0, 256, (source_height, source_width, 4), dtype=np.uint8)
        image = Image.fromarray(pixels, "RGBA")
        for target_width, target_height in [(1, 1), (2, 2), (source_width, source_height)]:
            for anchor_x in (0, 100):
                for anchor_y in (0, 100):
                    output = _fill(registry, image, tmp_path,
                                   target_width, target_height, anchor_x, anchor_y)
                    assert output.size == (target_width, target_height)


def test_opaque_and_rgb_like_input(registry, tmp_path):
    pixels = np.zeros((16, 24, 4), dtype=np.uint8)
    pixels[..., :3] = np.mgrid[0:16, 0:24][1][..., None]
    pixels[..., 3] = 255
    image = Image.fromarray(pixels, "RGBA")
    output = np.asarray(_fill(registry, image, tmp_path, 20, 20))
    assert (output[..., 3] == 255).all()


def test_dimension_guard_still_enforced(registry, tmp_path):
    from backend.errors import StudioError
    image = _gradient(64, 64)
    with pytest.raises(StudioError):
        _fill(registry, image, tmp_path, 8192, 8192)
    with pytest.raises(StudioError):
        registry.validate_step(
            {"operation": "resize_fill",
             "params": {"width": 100, "height": 100, "anchor_x": 101, "anchor_y": 50}})
