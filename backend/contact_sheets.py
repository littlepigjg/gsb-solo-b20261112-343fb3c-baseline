"""Contact-sheet composition: validated grid layout rendered as a real PNG asset."""
import math
from PIL import Image, ImageColor, ImageOps
from .validation import (
    color_value,
    dimension_guard,
    identifier,
    list_value,
    number_value,
)


class ContactSheetService:
    def __init__(self, settings, store, assets, processing):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.processing = processing

    def _validate(self, asset_ids, columns, cell_width, cell_height, gap, margin, background):
        ids = list_value(asset_ids, "素材列表", minimum=1, maximum=self.settings.max_batch)
        ids = [identifier(item, "素材ID") for item in ids]
        columns = number_value(columns, "列数", 1, 8, integer=True)
        cell_width = number_value(cell_width, "单元格宽度", 8, 1024, integer=True)
        cell_height = number_value(cell_height, "单元格高度", 8, 1024, integer=True)
        gap = number_value(gap, "单元格间距", 0, 64, integer=True)
        margin = number_value(margin, "外边距", 0, 64, integer=True)
        background = color_value(background, "背景颜色")
        return ids, columns, cell_width, cell_height, gap, margin, background

    def _layout(self, asset_ids, columns, cell_width, cell_height, gap, margin):
        rows = math.ceil(len(asset_ids) / columns)
        width = 2 * margin + columns * cell_width + (columns - 1) * gap
        height = 2 * margin + rows * cell_height + (rows - 1) * gap
        dimension_guard(width, height, self.settings)
        entries = []
        for index, asset_id in enumerate(asset_ids):
            column = index % columns
            row = index // columns
            x = margin + column * (cell_width + gap)
            y = margin + row * (cell_height + gap)
            entries.append({
                "asset_id": asset_id,
                "index": index,
                "cell": {"x": x, "y": y, "w": cell_width, "h": cell_height},
                "image_box": None,
            })
        return entries, width, height, rows

    def _render(self, entries, width, height, background, cell_width, cell_height):
        canvas = Image.new("RGBA", (width, height), ImageColor.getrgb(background) + (255,))
        for entry in entries:
            image, _record = self.assets.load(entry["asset_id"])
            fitted = ImageOps.contain(image, (cell_width, cell_height), Image.Resampling.LANCZOS)
            x = entry["cell"]["x"] + (cell_width - fitted.width) // 2
            y = entry["cell"]["y"] + (cell_height - fitted.height) // 2
            entry["image_box"] = {"x": x, "y": y, "w": fitted.width, "h": fitted.height}
            canvas.alpha_composite(fitted, (x, y))
        return canvas

    def create(self, asset_ids, columns, cell_width, cell_height, gap, margin, background):
        ids, columns, cell_width, cell_height, gap, margin, background = self._validate(
            asset_ids, columns, cell_width, cell_height, gap, margin, background
        )
        entries, width, height, rows = self._layout(
            ids, columns, cell_width, cell_height, gap, margin
        )
        # Preflight every referenced file before any composite work so a bad
        # reference cannot leave a partially rendered sheet behind.
        for asset_id in ids:
            self.assets.file(asset_id)
        name = f"联系印样 · {len(ids)} 张"
        with self.processing.compute_lock:
            canvas = self._render(entries, width, height, background, cell_width, cell_height)
            asset = self.assets.save(
                canvas,
                name=name,
                kind="result",
                metadata={
                    "contact_sheet": True,
                    "columns": columns,
                    "rows": rows,
                    "count": len(ids),
                    "source_ids": ids,
                },
            )
        self.store.event("contact_sheet", f"生成 {name}", {
            "asset_id": asset["id"],
            "count": len(ids),
            "columns": columns,
        })
        return {"asset": asset, "layout": entries}
