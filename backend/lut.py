"""Adobe Cube 3D LUT parsing and alpha-preserving trilinear application.

The parser implements the subset of the .cube format used by this service:
TITLE, LUT_3D_SIZE, DOMAIN_MIN and DOMAIN_MAX headers plus data lines ordered
with red changing fastest, then green, then blue. 1D LUTs and unknown headers
are rejected so that malformed files never create a derived asset.
"""
import math
import re
import threading
import numpy as np
from PIL import Image
from .errors import StudioError
from .validation import identifier, number_value

CUBE_LIMIT_BYTES = 1024 * 1024
SIZE_MIN = 2
SIZE_MAX = 17
HEADERS = {"TITLE", "LUT_3D_SIZE", "DOMAIN_MIN", "DOMAIN_MAX"}
INTEGER_TOKEN = re.compile(r"[0-9]+")

# Bound temporary arrays for 16-megapixel uploads while keeping float64
# precision for the interpolation and the final rounding step.
CHUNK_PIXELS = 500_000


class CubeLut:
    def __init__(self, size, table, domain_min, domain_max, title=None):
        self.size = size
        self.table = table
        self.domain_min = domain_min
        self.domain_max = domain_max
        self.title = title

    def apply(self, rgb, strength):
        """Apply the LUT to an H×W×3 uint8 array and return uint8 RGB."""
        height, width = rgb.shape[:2]
        output = np.empty((height, width, 3), dtype=np.uint8)
        rows = max(1, CHUNK_PIXELS // max(width, 1))
        for start in range(0, height, rows):
            stop = min(height, start + rows)
            output[start:stop] = self._apply_chunk(rgb[start:stop], strength)
        return output

    def _apply_chunk(self, rgb, strength):
        encoded = rgb.astype(np.float64) / 255.0
        coordinates = (encoded - self.domain_min) / (self.domain_max - self.domain_min)
        np.clip(coordinates, 0.0, 1.0, out=coordinates)
        interpolated = self._trilinear(coordinates)
        np.clip(interpolated, 0.0, 1.0, out=interpolated)
        if strength == 0.0:
            blended = encoded
        elif strength == 1.0:
            blended = interpolated
        else:
            blended = interpolated * strength
            blended += encoded * (1.0 - strength)
        values = np.floor(blended * 255.0 + 0.5)
        return np.clip(values, 0.0, 255.0).astype(np.uint8)

    def _trilinear(self, coordinates):
        """Sample the table at normalized coordinates in [0, 1]."""
        scaled = coordinates * (self.size - 1)
        low = np.floor(scaled).astype(np.intp)
        high = np.minimum(low + 1, self.size - 1)
        fraction = scaled - low
        r0 = low[..., 0]
        r1 = high[..., 0]
        g0 = low[..., 1]
        g1 = high[..., 1]
        b0 = low[..., 2]
        b1 = high[..., 2]
        fr = fraction[..., 0:1]
        fg = fraction[..., 1:2]
        fb = fraction[..., 2:3]
        table = self.table

        c000 = table[b0, g0, r0]
        c001 = table[b0, g0, r1]
        c010 = table[b0, g1, r0]
        c011 = table[b0, g1, r1]
        c100 = table[b1, g0, r0]
        c101 = table[b1, g0, r1]
        c110 = table[b1, g1, r0]
        c111 = table[b1, g1, r1]

        c00 = c000 + (c001 - c000) * fr
        c01 = c010 + (c011 - c010) * fr
        c10 = c100 + (c101 - c100) * fr
        c11 = c110 + (c111 - c110) * fr
        c0 = c00 + (c01 - c00) * fg
        c1 = c10 + (c11 - c10) * fg
        return c0 + (c1 - c0) * fb


def _finite_triplet(tokens, label, line_number):
    if len(tokens) != 3:
        raise StudioError(
            f"{label} 必须包含三个有限数字",
            details={"line": line_number},
        )
    values = []
    for token in tokens:
        try:
            value = float(token)
        except ValueError as error:
            raise StudioError(
                f"{label} 必须包含三个有限数字",
                details={"line": line_number},
            ) from error
        if not math.isfinite(value):
            raise StudioError(
                f"{label} 不能包含 NaN 或 Infinity",
                details={"line": line_number},
            )
        values.append(value)
    return values


def parse_cube(text):
    if not isinstance(text, str):
        raise StudioError("cube 必须是 .cube 文本")
    try:
        text_bytes = len(text.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise StudioError("cube 文本编码不正确") from error
    if text_bytes > CUBE_LIMIT_BYTES:
        raise StudioError("LUT 文本不能超过 1 MiB", code="lut_too_large")
    if text.startswith("﻿"):
        text = text[1:]

    headers = {}
    data = []
    in_data = False
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if not in_data:
            tokens = line.split()
            key = tokens[0].upper()
            if key == "LUT_1D_SIZE":
                raise StudioError("不支持 1D LUT", details={"line": line_number})
            if key in HEADERS:
                if key in headers:
                    raise StudioError(
                        f"LUT 头 {key} 重复",
                        details={"line": line_number},
                    )
                if key == "TITLE":
                    headers[key] = line[len(tokens[0]):].strip()
                elif key == "LUT_3D_SIZE":
                    if len(tokens) != 2 or not INTEGER_TOKEN.fullmatch(tokens[1]):
                        raise StudioError(
                            "LUT_3D_SIZE 必须是 2–17 的整数",
                            details={"line": line_number},
                        )
                    size = int(tokens[1])
                    if not SIZE_MIN <= size <= SIZE_MAX:
                        raise StudioError(
                            f"LUT_3D_SIZE 必须在 {SIZE_MIN}–{SIZE_MAX} 之间",
                            details={"line": line_number},
                        )
                    headers[key] = size
                else:
                    headers[key] = _finite_triplet(tokens[1:], key, line_number)
                continue

            try:
                float(tokens[0])
            except ValueError as error:
                raise StudioError(
                    f"未知的 LUT 头 {tokens[0]}",
                    details={"line": line_number},
                ) from error
            in_data = True

        tokens = line.split()
        if "LUT_1D_SIZE" == tokens[0].upper():
            raise StudioError("不支持 1D LUT", details={"line": line_number})
        if tokens[0].upper() in HEADERS:
            raise StudioError(
                "LUT 头必须位于数据行之前",
                details={"line": line_number},
            )
        if len(tokens) != 3:
            raise StudioError(
                "LUT 数据行必须包含三个有限数字",
                details={"line": line_number},
            )
        try:
            values = [float(token) for token in tokens]
        except ValueError as error:
            raise StudioError(
                "LUT 数据行必须包含三个有限数字",
                details={"line": line_number},
            ) from error
        if not all(math.isfinite(value) for value in values):
            raise StudioError(
                "LUT 数据不能包含 NaN 或 Infinity",
                details={"line": line_number},
            )
        data.append(values)

    size = headers.get("LUT_3D_SIZE")
    if size is None:
        raise StudioError("缺少 LUT_3D_SIZE")
    expected = size ** 3
    if len(data) != expected:
        raise StudioError(
            f"LUT 数据行数量必须是 {expected}，实际为 {len(data)}",
            details={"expected": expected, "actual": len(data)},
        )

    domain_min = np.asarray(headers.get("DOMAIN_MIN", [0.0, 0.0, 0.0]), dtype=np.float64)
    domain_max = np.asarray(headers.get("DOMAIN_MAX", [1.0, 1.0, 1.0]), dtype=np.float64)
    if np.any(domain_max <= domain_min):
        raise StudioError("DOMAIN_MAX 每个通道都必须大于 DOMAIN_MIN")

    table = np.asarray(data, dtype=np.float64).reshape(size, size, size, 3)
    return CubeLut(size, table, domain_min, domain_max, headers.get("TITLE", ""))


class LutService:
    def __init__(self, settings, store, assets):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.lock = threading.Lock()

    def apply(self, asset_id, cube, strength):
        identifier(asset_id)
        strength = number_value(strength, "strength", 0.0, 1.0)
        lut = parse_cube(cube)
        with self.lock:
            image, parent = self.assets.load(asset_id)
            rgb = np.asarray(image, dtype=np.uint8)[..., :3]
            result_rgb = lut.apply(rgb, strength)
            result = _image_from_rgb(result_rgb, image.getchannel("A"))
            base = parent["name"].rsplit(".", 1)[0][:150].strip() or "LUT"
            name = f"{base} · 3D LUT"[:180]
            record = self.assets.save(
                result,
                name=name,
                parent=parent,
                steps=[],
                metadata={
                    "lut": {
                        "title": lut.title,
                        "size": lut.size,
                        "strength": strength,
                        "domain_min": lut.domain_min.tolist(),
                        "domain_max": lut.domain_max.tolist(),
                    }
                },
            )
        self.store.event("lut_apply", f"应用 3D LUT：{name}", {
            "source_id": asset_id,
            "result_id": record["id"],
            "size": lut.size,
            "strength": strength,
        })
        return {
            "asset": record,
            "size": {"width": record["width"], "height": record["height"]},
        }


def _image_from_rgb(rgb, alpha):
    output = Image.fromarray(rgb, mode="RGB").convert("RGBA")
    output.putalpha(alpha)
    return output
