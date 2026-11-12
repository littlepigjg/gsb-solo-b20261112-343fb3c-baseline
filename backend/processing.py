"""Validated non-destructive processing pipelines shared by UI and jobs."""
import threading
import time
from .registry import OperationContext
from .errors import StudioError
from .validation import text_value


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
            # 声明源素材使用权：从读取源图到副本发布期间，删除必须等待，
            # 随后按后代保护规则拒绝；若源素材已先被删除，则在此明确失败，
            # 不会发布父级缺失的悬空副本。
            with self.assets.usage_claim(asset_id):
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

