"""GIMP Palette (GPL) parsing, canonical normalization and swatch-card PNGs.

The parser is deliberately strict: a rejected document never reaches the
asset library, so invalid input cannot leave half-finished artifacts behind.
"""
import math
import re
import unicodedata
from PIL import Image, ImageDraw
from .errors import StudioError


MAX_TEXT_BYTES = 256 * 1024
MAX_COLORS = 256
MIN_COLUMNS, MAX_COLUMNS = 1, 16
DEFAULT_COLUMNS = 4
DEFAULT_NAME = "Imported"
MIN_SWATCH_SIZE, MAX_SWATCH_SIZE = 8, 128

_INTEGER = re.compile(r"0|[1-9][0-9]*")
_HEADER = re.compile(r"([A-Za-z][A-Za-z ]*):[ \t]*(.*)")
_COLOR = re.compile(r"[ \t]*(\S+)[ \t]+(\S+)[ \t]+(\S+)(?:[ \t]+(.*?))?[ \t]*$")


def parse_gpl(text):
    """Parse UTF-8 GIMP Palette text into ``(name, columns, colors)``.

    ``colors`` keeps insertion order and duplicates, as ``(r, g, b, name)``.
    Raises :class:`StudioError` for every structural or value violation.
    """
    if not isinstance(text, str):
        raise StudioError("text 必须是 UTF-8 GIMP Palette 文本")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise StudioError("调色板文本不能超过 256 KiB", code="payload_too_large", status=413)

    text = text.replace("\r\n", "\n")
    for char in text:
        if unicodedata.category(char) == "Cc" and char not in "\t\n":
            raise StudioError("调色板文本包含异常控制字符")

    lines = text.split("\n")
    if not lines or lines[0] != "GIMP Palette":
        raise StudioError("首行必须是 GIMP Palette")

    name = DEFAULT_NAME
    columns = DEFAULT_COLUMNS
    seen_headers = set()
    colors = []
    data_started = False

    for line_number, line in enumerate(lines[1:], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        header = _HEADER.fullmatch(stripped)
        if header and not data_started:
            key, value = header.group(1), header.group(2).strip()
            if key not in {"Name", "Columns"}:
                raise StudioError(f"第 {line_number} 行包含未知头部：{key}")
            if key in seen_headers:
                raise StudioError(f"第 {line_number} 行重复出现 {key} 头部")
            seen_headers.add(key)
            if key == "Name":
                if not value:
                    raise StudioError(f"第 {line_number} 行 Name 头部不能为空")
                name = value
            else:
                if not _INTEGER.fullmatch(value):
                    raise StudioError(f"第 {line_number} 行 Columns 必须是整数")
                columns = int(value)
                if not MIN_COLUMNS <= columns <= MAX_COLUMNS:
                    raise StudioError(f"第 {line_number} 行 Columns 必须在 1–16 之间")
            continue

        if header and data_started:
            raise StudioError(f"第 {line_number} 行的头部出现在颜色数据之后")

        match = _COLOR.fullmatch(line)
        if not match:
            raise StudioError(f"第 {line_number} 行不是有效的颜色行")
        channels = []
        for token in match.groups()[:3]:
            if not _INTEGER.fullmatch(token):
                raise StudioError(f"第 {line_number} 行颜色通道必须是 0–255 的整数")
            channels.append(int(token))
        if any(not 0 <= channel <= 255 for channel in channels):
            raise StudioError(f"第 {line_number} 行颜色通道超出 0–255 范围")

        color_name = match.group(4)
        color_name = color_name.strip() if color_name else ""
        if not color_name:
            color_name = f"Color {len(colors) + 1}"
        colors.append((channels[0], channels[1], channels[2], color_name))
        data_started = True
        if len(colors) > MAX_COLORS:
            raise StudioError("调色板颜色数量不能超过 256 种")

    if not colors:
        raise StudioError("调色板必须包含 1–256 种颜色")
    return name, columns, colors


def render_gpl(name, columns, colors):
    """Render canonical GPL text; re-importing it yields identical data."""
    lines = ["GIMP Palette", f"Name: {name}", f"Columns: {columns}", "#"]
    lines.extend(f"{r} {g} {b}\t{color_name}" for r, g, b, color_name in colors)
    return "\n".join(lines) + "\n"


def render_swatch_card(colors, columns, swatch_size):
    """Build a row-major RGBA swatch card; unfilled cells stay transparent."""
    rows = math.ceil(len(colors) / columns)
    image = Image.new("RGBA", (columns * swatch_size, rows * swatch_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index, (r, g, b, _name) in enumerate(colors):
        x0 = (index % columns) * swatch_size
        y0 = (index // columns) * swatch_size
        draw.rectangle(
            (x0, y0, x0 + swatch_size - 1, y0 + swatch_size - 1),
            fill=(r, g, b, 255),
        )
    return image


class PaletteService:
    def __init__(self, assets, store):
        self.assets = assets
        self.store = store

    def convert(self, text, swatch_size):
        # Parse fully before touching the filesystem so invalid text creates
        # no asset record, media file or event at all.
        name, columns, colors = parse_gpl(text)
        card = render_swatch_card(colors, columns, swatch_size)
        record = self.assets.save(
            card,
            f"{name[:170]} 色卡",
            kind="result",
            metadata={
                "source": "gpl_convert",
                "color_count": len(colors),
                "columns": columns,
                "swatch_size": swatch_size,
            },
            enforce_limits=False,
        )
        gpl = render_gpl(name, columns, colors)
        self.store.event(
            "palette_convert",
            f"转换调色板 {name}",
            {"asset_id": record["id"], "colors": len(colors), "columns": columns},
        )
        return {
            "name": name,
            "columns": columns,
            "colors": [{"rgb": [r, g, b], "name": color_name} for r, g, b, color_name in colors],
            "gpl": gpl,
            "asset": record,
        }
