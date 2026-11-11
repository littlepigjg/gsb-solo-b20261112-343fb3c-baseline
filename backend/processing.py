"""Validated non-destructive processing pipelines shared by UI and jobs."""
import threading
import time
import numpy as np
from PIL import Image, ImageFilter
from .registry import OperationContext
from .errors import StudioError
from .validation import (
    boolean_value,
    choice_value,
    identifier,
    number_value,
    text_value,
    validate_parameters,
)


MASKED_OPERATIONS = ("color_gamma", "color_exposure", "color_brightness")
MASKED_MODES = ("alpha", "luminance")


class ProcessingService:
    def __init__(self, settings, store, assets, registry):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.registry = registry
        self.compute_lock = threading.Lock()
        self.context = OperationContext(settings=settings, assets=assets)

    def validate(self, steps):
        return self.registry.validate_pipeline(steps, maximum=self.settings.max_steps)

    def apply_steps(self, image, steps, progress=None, cancelled=None):
        timings = []
        for index, step in enumerate(steps):
            if cancelled and cancelled():
                raise StudioError("任务已取消", code="cancelled", status=409)
            start = time.perf_counter()
            image = self.registry.apply(image, step, self.context)
            timings.append({
                "operation": step["operation"],
                "milliseconds": round((time.perf_counter() - start) * 1000, 2),
            })
            if progress:
                progress(index + 1, len(steps))
        return image, timings

    def process(self, asset_id, steps, name=None, progress=None, cancelled=None):
        steps = self.validate(steps)
        if name is not None:
            name = text_value(name, "结果名称", 180, 1)
        started = time.perf_counter()
        with self.compute_lock:
            image, parent = self.assets.load(asset_id)
            image, timings = self.apply_steps(image, steps, progress, cancelled)
            if cancelled and cancelled():
                raise StudioError("任务已取消", code="cancelled", status=409)
            last = self.registry.spec(steps[-1]["operation"])
            if name is None:
                base = parent["name"].rsplit(".", 1)[0][:100]
                name = f"{base} · {last['title']}"
            result = self.assets.save(
                image,
                name=name,
                parent=parent,
                steps=steps,
                metadata={
                    "timings": timings,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        self.store.event("process", f"完成 {name}", {
            "source_id": asset_id,
            "result_id": result["id"],
            "operations": [step["operation"] for step in steps],
        })
        return result

    def preview(self, asset_id, steps):
        """Preview is ephemeral and never creates a JSON record."""
        from PIL import Image
        import io
        steps = self.validate(steps)
        with self.compute_lock:
            image, parent = self.assets.load(asset_id)
            image, timings = self.apply_steps(image, steps)
            image.thumbnail((1200, 1000), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
        buffer.seek(0)
        return buffer

    def masked_adjustment(self, asset_id, mask_id, operation, params=None,
                          mode="alpha", invert=False, feather=0, strength=1.0):
        """Apply one color operation through a same-size alpha/luminance mask.

        Every argument and both source assets are validated before the result
        copy is written. The original and the mask are only read.
        """
        identifier(asset_id, "原图ID")
        identifier(mask_id, "蒙版ID")
        operation = choice_value(operation, MASKED_OPERATIONS, "蒙版调整操作")
        spec = self.registry.spec(operation)
        params = validate_parameters(spec, {} if params is None else params)
        mode = choice_value(mode, MASKED_MODES, "蒙版模式")
        invert = boolean_value(invert, "反相")
        feather = number_value(feather, "羽化半径", 0, 8)
        strength = number_value(strength, "效果强度", 0, 1)

        started = time.perf_counter()
        with self.compute_lock:
            image, parent = self.assets.load(asset_id)
            mask, _ = self.assets.load(mask_id)
            if mask.size != image.size:
                raise StudioError(
                    "蒙版尺寸必须与原图一致",
                    details={"asset": list(image.size), "mask": list(mask.size)},
                )
            adjusted = self.registry.apply(
                image,
                {"operation": operation, "params": params},
                self.context,
            )
            weight = self._mask_weight(mask, mode, invert, feather, strength)
            output = self._masked_blend(image, adjusted, weight)
            base_name = parent["name"].rsplit(".", 1)[0][:100]
            name = f"{base_name} · 蒙版{spec['title']}"
            result = self.assets.save(
                output,
                name=name,
                parent=parent,
                steps=[{"operation": operation, "params": params}],
                metadata={
                    "operation": operation,
                    "params": params,
                    "mask_id": mask_id,
                    "mode": mode,
                    "invert": invert,
                    "feather": feather,
                    "strength": strength,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        self.store.event("masked_adjustment", f"完成 {name}", {
            "source_id": asset_id,
            "mask_id": mask_id,
            "result_id": result["id"],
            "operation": operation,
        })
        return result

    @staticmethod
    def _mask_weight(mask, mode, invert, feather, strength):
        rgba = mask.convert("RGBA")
        alpha = np.asarray(rgba.getchannel("A"), dtype=np.float64)
        if mode == "alpha":
            weight = alpha / 255.0
        else:
            light = np.asarray(rgba.convert("L"), dtype=np.float64)
            weight = light * alpha / (255.0 * 255.0)
        rounded = np.floor(weight * 255.0 + 0.5)
        rounded = np.clip(rounded, 0, 255).astype(np.uint8)
        if invert:
            rounded = (255 - rounded).astype(np.uint8)
        label = Image.fromarray(rounded, mode="L")
        if feather > 0:
            label = label.filter(ImageFilter.GaussianBlur(radius=feather))
        blurred = np.asarray(label, dtype=np.float64)
        return blurred / 255.0 * strength

    @staticmethod
    def _masked_blend(original, adjusted, weight):
        base = np.asarray(original.convert("RGB"), dtype=np.float64)
        effect = np.asarray(adjusted.convert("RGB"), dtype=np.float64)
        mixed = base + (effect - base) * weight[..., None]
        quantized = np.clip(np.floor(mixed + 0.5), 0, 255).astype(np.uint8)
        output = Image.fromarray(quantized, mode="RGB").convert("RGBA")
        output.putalpha(original.convert("RGBA").getchannel("A"))
        return output
