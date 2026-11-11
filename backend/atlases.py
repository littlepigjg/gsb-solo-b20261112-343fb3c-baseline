"""Deterministic sprite-sheet packing for existing image assets.

Sprites keep their original pixels (no rotation, scaling or resampling).
Items are placed in input order on horizontal shelves; every item's
footprint includes its padding, so tiles can never overlap.
"""
import numpy as np
from PIL import Image
from .errors import StudioError
from .validation import (
    boolean_value,
    identifier,
    list_value,
    number_value,
    object_value,
    reject_unknown,
    text_value,
)


class AtlasService:
    def __init__(self, settings, store, assets):
        self.settings = settings
        self.store = store
        self.assets = assets

    def _validate(self, data):
        raw_items = list_value(data.get("items"), "图集条目", minimum=1, maximum=24)
        items = []
        names = []
        for index, raw in enumerate(raw_items, start=1):
            label = f"第 {index} 个图集条目"
            object_value(raw, label)
            reject_unknown(raw, {"asset_id", "name"}, label)
            asset_id = identifier(raw.get("asset_id"), f"第 {index} 个素材 ID")
            name = text_value(raw.get("name"), f"第 {index} 个名称", 180, 1)
            if name in names:
                raise StudioError(
                    "图集条目名称必须唯一",
                    details={"name": name, "index": index},
                )
            names.append(name)
            items.append({"asset_id": asset_id, "name": name})
        options = {
            "max_width": number_value(data.get("max_width", 4096), "图集最大宽度", 8, 4096, integer=True),
            "max_height": number_value(data.get("max_height", 4096), "图集最大高度", 8, 4096, integer=True),
            "padding": number_value(data.get("padding", 0), "图集间距", 0, 16, integer=True),
            "trim": boolean_value(data.get("trim", False), "裁剪透明边"),
            "extrude": boolean_value(data.get("extrude", False), "边缘延伸"),
        }
        return items, options

    @staticmethod
    def _trim(image, trim):
        """Return (content, source_rect, empty) without touching any pixel."""
        alpha = np.asarray(image.getchannel("A"))
        visible = alpha > 0
        if trim:
            coordinates = np.argwhere(visible)
            if coordinates.size == 0:
                # Fully transparent assets keep a transparent 1x1 placeholder.
                return Image.new("RGBA", (1, 1), (0, 0, 0, 0)), [0, 0, 0, 0], True
            top, left = (int(value) for value in coordinates.min(axis=0))
            bottom, right = (int(value) for value in coordinates.max(axis=0) + 1)
            content = image.crop((left, top, right, bottom))
            return content, [left, top, right - left, bottom - top], False
        if not bool(visible.any()):
            # A fully transparent sprite has no source crop, even in full-frame mode.
            return image, [0, 0, 0, 0], True
        return image, [0, 0, image.width, image.height], False

    @staticmethod
    def _layout(tiles, options):
        """Assign every tile a shelf position, failing before any write."""
        max_width = options["max_width"]
        max_height = options["max_height"]
        padding = options["padding"]
        placements = []
        x = y = row_height = 0
        for index, tile in enumerate(tiles, start=1):
            width = tile["content"].width + padding * 2
            height = tile["content"].height + padding * 2
            if width > max_width:
                raise StudioError(
                    f"第 {index} 个精灵连同间距宽 {width} 像素，超过图集最大宽度 {max_width}",
                    details={"index": index, "width": width, "max_width": max_width},
                )
            if height > max_height:
                raise StudioError(
                    f"第 {index} 个精灵连同间距高 {height} 像素，超过图集最大高度 {max_height}",
                    details={"index": index, "height": height, "max_height": max_height},
                )
            if x and x + width > max_width:
                y += row_height
                x = 0
                row_height = 0
            placements.append({"x": x, "y": y, "width": width, "height": height})
            x += width
            row_height = max(row_height, height)
            if y + row_height > max_height:
                raise StudioError(
                    f"图集总高 {y + row_height} 像素，超过最大高度 {max_height}",
                    details={"height": y + row_height, "max_height": max_height},
                )
        return placements, y + row_height

    @staticmethod
    def _tile(content, padding, extrude):
        if padding == 0:
            return content
        if extrude:
            array = np.asarray(content)
            array = np.pad(
                array,
                ((padding, padding), (padding, padding), (0, 0)),
                mode="edge",
            )
            return Image.fromarray(array, "RGBA")
        result = Image.new(
            "RGBA",
            (content.width + padding * 2, content.height + padding * 2),
            (0, 0, 0, 0),
        )
        result.paste(content, (padding, padding))
        return result

    def create(self, data):
        items, options = self._validate(data)
        padding = options["padding"]
        sources = {}
        tiles = []
        with self.assets.lock:
            for item in items:
                asset_id = item["asset_id"]
                if asset_id not in sources:
                    sources[asset_id] = self.assets.load(asset_id)
                image, record = sources[asset_id]
                content, source_rect, empty = self._trim(image, options["trim"])
                tiles.append({
                    "name": item["name"],
                    "asset_id": asset_id,
                    "content": content,
                    "source_rect": source_rect,
                    "source_size": [int(record["width"]), int(record["height"])],
                    "empty": empty,
                })

            placements, atlas_height = self._layout(tiles, options)
            atlas = Image.new(
                "RGBA",
                (options["max_width"], atlas_height),
                (0, 0, 0, 0),
            )
            sprites = []
            for tile, placement in zip(tiles, placements):
                piece = self._tile(tile["content"], padding, options["extrude"])
                atlas.paste(piece, (placement["x"], placement["y"]))
                sprites.append({
                    "name": tile["name"],
                    "asset_id": tile["asset_id"],
                    "rect": [
                        placement["x"] + padding,
                        placement["y"] + padding,
                        tile["content"].width,
                        tile["content"].height,
                    ],
                    "source_size": tile["source_size"],
                    "source_rect": tile["source_rect"],
                    "empty": tile["empty"],
                })

            metadata = {
                "type": "atlas",
                "sprite_count": len(sprites),
                "max_width": options["max_width"],
                "max_height": options["max_height"],
                "padding": padding,
                "trim": options["trim"],
                "extrude": options["extrude"],
            }
            asset = self.assets.save(
                atlas,
                name="精灵图集.png",
                kind="result",
                metadata=metadata,
                enforce_limits=False,
            )
        self.store.event(
            "atlas",
            f"打包 {len(sprites)} 个精灵",
            {"asset_id": asset["id"], "sprite_count": len(sprites)},
        )
        return {"asset": asset, "sprites": sprites}
