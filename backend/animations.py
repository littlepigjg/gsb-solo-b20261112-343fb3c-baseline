"""Multi-frame GIF animation export with one animation-wide color palette."""
import io
import os
from PIL import Image, ImageColor
from .errors import StudioError
from .storage import new_id
from .validation import (
    object_value,
    reject_unknown,
    number_value,
    color_value,
    list_value,
    identifier,
)


FRAME_MIN, FRAME_MAX = 2, 24
DURATION_MIN, DURATION_MAX, DURATION_STEP = 10, 10000, 10
DIMENSION_MIN, DIMENSION_MAX = 1, 1024
LOOP_MIN, LOOP_MAX = 0, 65535
PALETTE_LIMIT = 256


class AnimationService:
    """Builds real GIF files from existing assets without creating new images."""

    def __init__(self, settings, store, assets, processing):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.processing = processing

    def validate(self, data):
        object_value(data, "请求")
        reject_unknown(data, {"frames", "width", "height", "background", "loop"}, "请求")

        frames = list_value(data.get("frames"), "帧", minimum=FRAME_MIN, maximum=FRAME_MAX)
        validated_frames = []
        for index, frame in enumerate(frames):
            label = f"第 {index + 1} 帧"
            object_value(frame, label)
            reject_unknown(frame, {"asset_id", "duration_ms"}, label)
            asset_id = frame.get("asset_id")
            identifier(asset_id, f"{label}素材ID")
            duration = number_value(
                frame.get("duration_ms"),
                f"{label}时长",
                DURATION_MIN,
                DURATION_MAX,
                integer=True,
            )
            if duration % DURATION_STEP != 0:
                raise StudioError(f"{label}时长必须是 {DURATION_STEP} 的倍数")
            validated_frames.append({"asset_id": asset_id, "duration_ms": duration})

        width = number_value(data.get("width"), "宽度", DIMENSION_MIN, DIMENSION_MAX, integer=True)
        height = number_value(data.get("height"), "高度", DIMENSION_MIN, DIMENSION_MAX, integer=True)
        background = color_value(data.get("background"), "背景颜色")
        loop = number_value(data.get("loop"), "循环次数", LOOP_MIN, LOOP_MAX, integer=True)

        return {
            "frames": validated_frames,
            "width": width,
            "height": height,
            "background": background,
            "loop": loop,
        }

    def compose_frame(self, image, width, height, background_rgb):
        """Lanczos contain (upscale allowed), centered, alpha composited on background."""
        scale = min(width / image.width, height / image.height)
        target_width = max(1, int(round(image.width * scale)))
        target_height = max(1, int(round(image.height * scale)))
        resized = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
        offset_x = (width - target_width) // 2
        offset_y = (height - target_height) // 2
        canvas = Image.new("RGB", (width, height), background_rgb)
        canvas.paste(resized, (offset_x, offset_y), resized)
        return canvas

    def encode_gif(self, rgb_frames, durations, loop):
        """Quantize once against a shared palette and save a real animated GIF."""
        width, height = rgb_frames[0].size
        strip = Image.new("RGB", (width, height * len(rgb_frames)))
        for index, frame in enumerate(rgb_frames):
            strip.paste(frame, (0, index * height))
        shared = strip.quantize(
            colors=PALETTE_LIMIT,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.NONE,
        )
        palette = bytes(shared.getpalette())
        quantized = [
            frame.quantize(palette=shared, dither=Image.Dither.NONE)
            for frame in rgb_frames
        ]
        buffer = io.BytesIO()
        quantized[0].save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=quantized[1:],
            duration=list(durations),
            loop=loop,
            optimize=False,
            palette=palette,
        )
        return buffer.getvalue()

    def create(self, data):
        spec = self.validate(data)
        width, height = spec["width"], spec["height"]
        background_rgb = ImageColor.getrgb(spec["background"])
        durations = [frame["duration_ms"] for frame in spec["frames"]]

        # All inputs are validated and every asset is resolved before any export record is written.
        with self.processing.compute_lock:
            loaded = [self.assets.load(frame["asset_id"]) for frame in spec["frames"]]
            first_asset = loaded[0][1]
            rgb_frames = [
                self.compose_frame(image, width, height, background_rgb)
                for image, _record in loaded
            ]
            payload = self.encode_gif(rgb_frames, durations, spec["loop"])

        export_id = new_id()
        filename = f"{export_id}.gif"
        path = self.settings.exports / filename
        temporary = path.with_suffix(".tmp")
        options = {
            "format": "GIF",
            "kind": "animation",
            "width": width,
            "height": height,
            "background": spec["background"],
            "loop": spec["loop"],
            "frames": spec["frames"],
        }
        base_name = first_asset["name"].rsplit(".", 1)[0][:100]
        download_name = f"{base_name}.gif"
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
            record = self.store.insert("exports", {
                "id": export_id,
                "filename": filename,
                "download_name": download_name,
                "bytes": len(payload),
                "asset_ids": [frame["asset_id"] for frame in spec["frames"]],
                "options": options,
                "kind": "animation",
            })
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        record["url"] = f"/api/exports/{export_id}/download"
        self.store.event("animation", f"生成 GIF 动画 {download_name}", {"export_id": export_id})
        return {
            "export": record,
            "frame_count": len(spec["frames"]),
            "total_duration_ms": sum(durations),
        }
