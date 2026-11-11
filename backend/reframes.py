"""All-or-nothing anchored reframes: largest aspect-ratio window containing a focus box.

Each target requests an output size. The largest continuous window of the source
sharing that aspect ratio is chosen, constrained to fully contain ``focus`` and
to stay inside the source. The anchor places the focus center inside the window
before the window is clamped into the allowed range. Geometry for every target
is validated before any asset record is created, so an infeasible last target
never leaves earlier results behind. The continuous (fractional) window is
handed straight to Pillow's Lanczos resampler through its float ``box`` argument,
so the window is never rounded to an integer crop first; RGBA goes through the
premultiplied-alpha path and keeps its transparency semantics.
"""
import threading
from PIL import Image
from .errors import StudioError
from .validation import identifier, list_value, number_value, object_value, reject_unknown, text_value


class ReframeService:
    MAX_TARGETS = 12
    MAX_SIDE = 4096
    EPSILON = 1e-9

    def __init__(self, settings, store, assets):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.compute_lock = threading.Lock()

    def _rect_value(self, value, label):
        list_value(value, label, minimum=4, maximum=4)
        return [number_value(component, f"{label}分量", integer=True) for component in value]

    def _point_value(self, value, label):
        list_value(value, label, minimum=2, maximum=2)
        return [number_value(component, f"{label}分量", 0.0, 1.0) for component in value]

    def _validate_focus(self, focus, width, height):
        left, top, box_width, box_height = self._rect_value(focus, "focus")
        if box_width < 1 or box_height < 1:
            raise StudioError("focus 必须是非空矩形，宽高至少为 1")
        right = left + box_width
        bottom = top + box_height
        if left < 0 or top < 0 or right > width or bottom > height:
            raise StudioError("focus 必须完整位于源图内部")
        return {"left": left, "top": top, "right": right, "bottom": bottom,
                "width": box_width, "height": box_height}

    def _validate_target(self, target, used_names):
        object_value(target, "目标项")
        reject_unknown(target, {"name", "width", "height"}, "目标项")
        if "name" not in target or "width" not in target or "height" not in target:
            raise StudioError("每个目标都必须包含 name、width、height")
        name = text_value(target["name"], "目标名称", 180, 1)
        if name in used_names:
            raise StudioError("目标名称必须唯一", details={"name": name})
        used_names.add(name)
        width = number_value(target["width"], "目标宽度", 1, self.MAX_SIDE, integer=True)
        height = number_value(target["height"], "目标高度", 1, self.MAX_SIDE, integer=True)
        return {"name": name, "width": width, "height": height}

    def _plan_target(self, target, source_size, focus, anchor):
        anchor_x, anchor_y = anchor
        source_width, source_height = source_size
        ratio = target["width"] / target["height"]
        # Largest continuous window with the target aspect ratio that fits the source.
        window_width = min(float(source_width), source_height * ratio)
        window_height = window_width / ratio
        if window_height > source_height + self.EPSILON:
            window_height = float(source_height)
            window_width = window_height * ratio

        # Feasibility: the focus box must fit inside the window.
        min_left = max(0.0, focus["right"] - window_width)
        max_left = min(float(focus["left"]), source_width - window_width)
        min_top = max(0.0, focus["bottom"] - window_height)
        max_top = min(float(focus["top"]), source_height - window_height)
        if min_left > max_left + self.EPSILON or min_top > max_top + self.EPSILON:
            raise StudioError(
                "目标长宽比下没有能完整包含 focus 的裁剪窗口，该目标不可行",
                code="infeasible_target",
                details={"name": target["name"]},
            )

        focus_center_x = focus["left"] + focus["width"] / 2.0
        focus_center_y = focus["top"] + focus["height"] / 2.0
        left = min(max(focus_center_x - anchor_x * window_width, min_left), max_left)
        top = min(max(focus_center_y - anchor_y * window_height, min_top), max_top)
        return {
            "name": target["name"],
            "width": target["width"],
            "height": target["height"],
            "box": [left, top, left + window_width, top + window_height],
        }

    def _resample(self, image, plan):
        # Pillow accepts fractional box coordinates and runs Lanczos directly on
        # the continuous window; RGBA is resampled in premultiplied space so
        # transparent pixels never bleed colour into their neighbours.
        return image.resize(
            (plan["width"], plan["height"]),
            Image.Resampling.LANCZOS,
            box=tuple(plan["box"]),
        )

    def create(self, asset_id, focus, anchor, targets):
        identifier(asset_id, "asset_id")
        anchor = self._point_value(anchor, "anchor") if anchor is not None else [0.5, 0.5]
        target_items = list_value(targets, "目标列表", minimum=1, maximum=self.MAX_TARGETS)
        used_names = set()
        validated_targets = [self._validate_target(item, used_names) for item in target_items]

        with self.compute_lock:
            image, parent = self.assets.load(asset_id)
            focus_box = self._validate_focus(focus, image.width, image.height)
            # All geometry (names, sizes, aspect windows, feasibility) is settled
            # before the first record is generated.
            plans = [self._plan_target(target, image.size, focus_box, anchor) for target in validated_targets]

            results = []
            created = []
            try:
                for plan in plans:
                    reframed = self._resample(image, plan)
                    record = self.assets.save(
                        reframed,
                        name=plan["name"],
                        parent=parent,
                        steps=[{"operation": "reframe", "params": {
                            "source_id": parent["id"],
                            "focus": [focus_box["left"], focus_box["top"], focus_box["width"], focus_box["height"]],
                            "anchor": anchor,
                            "width": plan["width"],
                            "height": plan["height"],
                            "box": plan["box"],
                        }}],
                        metadata={
                            "reframe": {
                                "source_id": parent["id"],
                                "focus": [focus_box["left"], focus_box["top"], focus_box["width"], focus_box["height"]],
                                "anchor": anchor,
                                "box": plan["box"],
                            }
                        },
                        enforce_limits=False,
                    )
                    created.append(record["id"])
                    results.append({"name": plan["name"], "box": plan["box"], "asset": record})
            except Exception:
                # Defensive: no partial family survives a mid-flight failure.
                for asset_id_created in created:
                    self._discard(asset_id_created)
                raise
        self.store.event("reframe", f"重构 {parent['name']} 的 {len(results)} 个目标", {
            "source_id": parent["id"],
            "result_ids": [item["asset"]["id"] for item in results],
        })
        return {"source_id": parent["id"], "results": results}

    def _discard(self, asset_id):
        self.assets.discard(asset_id)
