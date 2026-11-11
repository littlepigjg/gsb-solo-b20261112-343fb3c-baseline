"""Bounded diagnostics for the inspector: histograms, palette and quality."""
import math
import numpy as np
from PIL import Image
from .imaging import convolution


class AnalysisService:
    def __init__(self, assets):
        self.assets = assets

    def inspect(self, asset_id):
        image, record = self.assets.load(asset_id)
        original_size = image.size
        image.thumbnail((768, 768), Image.Resampling.LANCZOS)
        rgba = np.asarray(image, dtype=np.uint8)
        rgb = rgba[..., :3]
        visible = rgba[..., 3] > 0
        pixels = rgb[visible]
        if not len(pixels):
            pixels = np.zeros((1, 3), dtype=np.uint8)
        histograms = {}
        channel_names = ("red", "green", "blue")
        for index, channel in enumerate(channel_names):
            counts = np.bincount(pixels[:, index], minlength=256)
            histograms[channel] = counts.tolist()
        gray = np.asarray(image.convert("L"), dtype=np.float32)
        gray_visible = gray[visible] if visible.any() else np.array([0], dtype=np.float32)
        luma_histogram = np.bincount(gray_visible.astype(np.uint8), minlength=256)
        histograms["luminance"] = luma_histogram.tolist()
        laplacian = convolution(gray, [[0, 1, 0], [1, -4, 1], [0, 1, 0]])
        # 总体统计：全部可见像素（alpha>0）参与，与直方图同源，
        # 不按分块平均，避免抹掉不同区域之间的差异。
        means = pixels.mean(axis=0)
        std = pixels.std(axis=0)
        brightness = float(gray_visible.mean())
        contrast = float(gray_visible.std())
        percentiles = np.percentile(gray_visible, [1, 5, 25, 50, 75, 95, 99])
        # 熵由总体 256 级亮度直方图计算，保证与 luminance 直方图一致。
        luma_probabilities = luma_histogram[luma_histogram > 0] / max(1, luma_histogram.sum())
        # 全透明回退为单个零像素时直方图无正概率，空和会得到 -0.0，归一化为 0。
        entropy = max(0.0, float(-(luma_probabilities * np.log2(luma_probabilities)).sum()))
        palette_image = image.convert("RGB").quantize(colors=6, method=Image.Quantize.MEDIANCUT)
        palette = palette_image.getpalette()
        colors = sorted(palette_image.getcolors() or [], reverse=True)
        dominant = []
        for count, index in colors:
            channels = palette[index * 3:index * 3 + 3]
            dominant.append({
                "hex": "#" + "".join(f"{value:02x}" for value in channels),
                "rgb": channels,
                "percentage": round(count / (image.width * image.height) * 100, 1),
            })
        suggestions = []
        if brightness < 65:
            suggestions.append({"message": "画面偏暗，可尝试曝光补偿或阴影提亮。", "operation": "color_exposure"})
        elif brightness > 195:
            suggestions.append({"message": "画面偏亮，可适当降低曝光。", "operation": "color_exposure"})
        if gray_visible.std() < 35:
            suggestions.append({"message": "明暗范围较窄，可尝试自动对比度。", "operation": "color_autocontrast"})
        if float(laplacian.var()) < 100:
            suggestions.append({"message": "细节变化较少，可尝试精细锐化；此指标不代表美学质量。", "operation": "enhance_unsharp"})
        if not suggestions:
            suggestions.append({"message": "亮度与反差较均衡，可以从风格滤镜开始探索。", "operation": "filter_vintage"})
        divisor = math.gcd(*original_size)
        return {
            "asset_id": asset_id,
            "width": original_size[0],
            "height": original_size[1],
            "aspect_ratio": f"{original_size[0] // divisor}:{original_size[1] // divisor}",
            "megapixels": round(original_size[0] * original_size[1] / 1_000_000, 2),
            "sample_size": list(image.size),
            "histograms": histograms,
            "mean_rgb": [round(float(value), 2) for value in means],
            "std_rgb": [round(float(value), 2) for value in std],
            "brightness": round(brightness, 2),
            "contrast": round(contrast, 2),
            "entropy_bits": round(entropy, 3),
            "laplacian_variance": round(float(laplacian.var()), 2),
            "percentiles": {str(key): round(float(value), 2) for key, value in zip([1, 5, 25, 50, 75, 95, 99], percentiles)},
            "dark_clipping_percent": round(float((gray_visible <= 3).mean()) * 100, 2),
            "light_clipping_percent": round(float((gray_visible >= 252).mean()) * 100, 2),
            "transparent_percent": round(float((rgba[..., 3] < 255).mean()) * 100, 2),
            "palette": dominant,
            "suggestions": suggestions,
        }

    def compare(self, first_id, second_id):
        first, first_record = self.assets.load(first_id)
        second, second_record = self.assets.load(second_id)
        original_equal = first.size == second.size
        first.thumbnail((768, 768), Image.Resampling.LANCZOS)
        second = second.resize(first.size, Image.Resampling.LANCZOS)
        a = np.asarray(first.convert("RGB"), dtype=np.float32)
        b = np.asarray(second.convert("RGB"), dtype=np.float32)
        difference = a - b
        mse = float(np.mean(difference ** 2))
        mae = float(np.mean(np.abs(difference)))
        psnr = None if mse == 0 else 10 * math.log10(255 ** 2 / mse)
        return {
            "first_id": first_id,
            "second_id": second_id,
            "same_dimensions": original_equal,
            "sample_size": list(first.size),
            "mean_absolute_error": round(mae, 4),
            "mean_squared_error": round(mse, 4),
            "psnr_db": round(psnr, 4) if psnr is not None else None,
            "identical_sample": mse == 0,
            "note": "指标在缩略采样上计算；尺寸不同则先对齐，不用于无损验证。",
        }

