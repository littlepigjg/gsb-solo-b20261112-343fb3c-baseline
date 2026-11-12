"""独立于生产代码的 Lanczos 透明感知重采样参考实现。

严格对照 Pillow 13.0.0 的 C 源码编写，用于交叉验证
``resize_fill`` 的连续窗口 cover 裁剪：

- ``src/libImaging/Resample.c``：Lanczos 滤波器、系数预计算、8 位定点
  累加（``PRECISION_BITS = 22``）、横向/纵向 pass 顺序与跳过规则；
- ``src/libImaging/Convert.c``：RGBA -> RGBa 预乘（``MULDIV255``）与
  RGBa -> RGBA 反预乘（``CLIP8(255 * p / a)``，alpha 为 0/255 时直通）。

生产实现直接调用 Pillow 原生 ``Image.resize(..., box=...)``，与任何
基于 Pillow 的参考逐位一致；本模块用 NumPy 独立复现同一算法，几何系数
采用双精度（C 层为 float32），因此个别分数窗口可能有 1 个量化级的
正常偏差，不应对拍逐位相等，而以关键通道的 1 单位容差断言。
"""
import numpy as np
from PIL import Image

PRECISION_BITS = 22  # 32 - 8 - 2，见 Resample.c
ONE = 1 << PRECISION_BITS
HALF = 1 << (PRECISION_BITS - 1)
SUPPORT = 3.0  # LANCZOS 滤波器支撑半径


def _sinc(t):
    t = np.asarray(t, dtype=np.float64)
    out = np.ones_like(t)
    nz = t != 0.0
    ang = t[nz] * np.pi
    out[nz] = np.sin(ang) / ang
    return out


def _lanczos(x):
    # 与 C 的 lanczos_filter 一致：区间 [-3, 3)，含 -3 不含 3。
    x = np.asarray(x, dtype=np.float64)
    out = np.zeros_like(x)
    mask = (x >= -SUPPORT) & (x < SUPPORT)
    out[mask] = _sinc(x[mask]) * _sinc(x[mask] / SUPPORT)
    return out


def _coeffs(in_size, in0, in1, out_size):
    scale = (in1 - in0) / out_size
    filterscale = max(scale, 1.0)
    support_scaled = SUPPORT * filterscale
    ksize = int(np.ceil(support_scaled)) * 2 + 1
    bounds = np.zeros((out_size, 2), dtype=np.int64)
    kk = np.zeros((out_size, ksize), dtype=np.float64)
    inv = 1.0 / filterscale
    for xx in range(out_size):
        center = in0 + (xx + 0.5) * scale
        xmin = max(0, int(center - support_scaled + 0.5))
        xmax = min(in_size, int(center + support_scaled + 0.5))
        xs = np.arange(xmin, xmax)
        w = _lanczos((xs - center + 0.5) * inv)
        ww = w.sum()
        if ww != 0.0:
            w = w / ww
        kk[xx, : len(w)] = w
        bounds[xx] = (xmin, xmax - xmin)
    # normalize_coeffs_8bpc：负值向下、正值向上的定点四舍五入。
    ki = np.where(kk < 0, (-0.5 + kk * ONE).astype(np.int64),
                  (0.5 + kk * ONE).astype(np.int64))
    return bounds, ki


def _apply_axis(src, coeffs, out_size, axis):
    bounds, ki = coeffs
    if axis == 1:  # 水平
        h, _ = src.shape
        out = np.empty((h, out_size), dtype=np.int64)
        for xx in range(out_size):
            xmin, length = bounds[xx]
            k = ki[xx, :length]
            out[:, xx] = HALF + (src[:, xmin:xmin + length] * k).sum(axis=1)
    else:  # 纵向
        _, w = src.shape
        out = np.empty((out_size, w), dtype=np.int64)
        for yy in range(out_size):
            ymin, length = bounds[yy]
            k = ki[yy, :length]
            out[yy, :] = HALF + (src[ymin:ymin + length, :] * k[:, None]).sum(axis=0)
    return np.clip(out >> PRECISION_BITS, 0, 255).astype(np.uint8)


def _shift_for_div255(values):
    values = values.astype(np.int64)
    return ((values >> 8) + values) >> 8


def premultiply(rgba):
    """RGBA -> RGBa，等价 Convert.c 的 MULDIV255(c, a)。"""
    alpha = rgba[..., 3:4].astype(np.int64)
    rgb = rgba[..., :3].astype(np.int64)
    return np.concatenate([_shift_for_div255(rgb * alpha + 128), alpha], axis=2)


def unpremultiply(premultiplied):
    """RGBa -> RGBA，等价 Convert.c 的 rgba2rgbA。"""
    premultiplied = premultiplied.astype(np.int64)
    alpha = premultiplied[..., 3:4]
    rgb = premultiplied[..., :3]
    scaled = np.clip((255 * rgb) // np.maximum(alpha, 1), 0, 255)
    out_rgb = np.where((alpha == 0) | (alpha == 255), rgb, scaled)
    return np.concatenate([out_rgb, alpha], axis=2).astype(np.uint8)


def reference_fill(rgba_image, size, box):
    """对给定连续窗口执行透明感知 Lanczos 重采样，返回 RGBA 图像。"""
    width, height = size
    sw, sh = rgba_image.size
    pm = premultiply(np.asarray(rgba_image.convert("RGBA"), dtype=np.uint8))
    need_h = width != sw or box[0] != 0.0 or box[2] != width
    need_v = height != sh or box[1] != 0.0 or box[3] != height
    # ImagingResampleInner 的 pass 顺序判定。
    horizontal_first = not ((sh - height) > 0 and (sh - height) > (sw - width) * 2)
    bands = []
    for channel in range(4):
        plane = pm[..., channel]
        if horizontal_first:
            if need_h:
                plane = _apply_axis(plane, _coeffs(sw, box[0], box[2], width), width, 1)
            if need_v:
                plane = _apply_axis(plane, _coeffs(sh, box[1], box[3], height), height, 0)
        else:
            if need_v:
                plane = _apply_axis(plane, _coeffs(sh, box[1], box[3], height), height, 0)
            if need_h:
                plane = _apply_axis(plane, _coeffs(sw, box[0], box[2], width), width, 1)
        bands.append(plane)
    return Image.fromarray(unpremultiply(np.stack(bands, axis=2)), "RGBA")
