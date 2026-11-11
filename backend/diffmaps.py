"""World-space diff maps between two RGBA assets."""
import numpy as np
from PIL import Image, ImageColor
from .validation import dimension_guard


class DiffMapService:
    """Composite both assets onto one background, then diff their visible RGB."""

    def __init__(self, assets, processing):
        self.assets = assets
        self.settings = assets.settings
        self.processing = processing

    @staticmethod
    def _compose(image, position, canvas_size, background):
        """Alpha-composite one image onto an opaque background canvas."""
        canvas_width, canvas_height = canvas_size
        canvas = np.empty((canvas_height, canvas_width, 3), dtype=np.uint8)
        canvas[...] = background
        source = image.convert("RGBA")
        patch = Image.new("RGBA", source.size, tuple(background) + (255,))
        patch.alpha_composite(source)
        x, y = position
        width, height = source.size
        canvas[y:y + height, x:x + width] = np.asarray(patch.convert("RGB"), dtype=np.uint8)
        return canvas

    def build(self, first_id, second_id, offset_x=0, offset_y=0, tolerance=0, background="#ffffff"):
        background_rgb = ImageColor.getrgb(background)
        with self.processing.compute_lock:
            first, first_record = self.assets.load(first_id)
            second, second_record = self.assets.load(second_id)
            first_width, first_height = first.size
            second_width, second_height = second.size

            left = min(0, offset_x)
            top = min(0, offset_y)
            right = max(first_width, offset_x + second_width)
            bottom = max(first_height, offset_y + second_height)
            canvas_size = (right - left, bottom - top)
            dimension_guard(*canvas_size, self.settings)

            first_canvas = self._compose(
                first, (0 - left, 0 - top), canvas_size, background_rgb)
            second_canvas = self._compose(
                second, (offset_x - left, offset_y - top), canvas_size, background_rgb)
            difference = np.abs(first_canvas.astype(np.int16) - second_canvas.astype(np.int16))
            max_difference = difference.max(axis=2)
            changed = max_difference > tolerance

            diff_pixels = np.zeros((canvas_size[1], canvas_size[0], 4), dtype=np.uint8)
            diff_pixels[changed] = (255, 0, 0, 255)
            diff_image = Image.fromarray(diff_pixels, "RGBA")

            changed_pixels = int(changed.sum())
            area = canvas_size[0] * canvas_size[1]
            changed_percent = round(changed_pixels * 100 / area, 2)
            columns = np.flatnonzero(changed.any(axis=0))
            rows = np.flatnonzero(changed.any(axis=1))
            bbox = None
            if columns.size:
                bbox = [
                    int(columns[0]), int(rows[0]),
                    int(columns[-1]) + 1, int(rows[-1]) + 1,
                ]
            maximum = int(max_difference.max())

            first_name = first_record["name"].rsplit(".", 1)[0][:68]
            second_name = second_record["name"].rsplit(".", 1)[0][:68]
            name = f"差异图 {first_name} vs {second_name}.png"
            asset = self.assets.save(
                diff_image,
                name=name,
                kind="result",
                metadata={
                    "operation": "diff_map",
                    "first_id": first_id,
                    "second_id": second_id,
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "tolerance": tolerance,
                    "background": background,
                    "origin": [left, top],
                    "changed_pixels": changed_pixels,
                    "changed_percent": changed_percent,
                    "bbox": bbox,
                    "max_difference": maximum,
                },
            )

        self.assets.store.event("diff_map", f"生成 {name}", {
            "first_id": first_id,
            "second_id": second_id,
            "result_id": asset["id"],
        })
        return {
            "asset": asset,
            "origin": [left, top],
            "changed_pixels": changed_pixels,
            "changed_percent": changed_percent,
            "bbox": bbox,
            "max_difference": maximum,
        }
