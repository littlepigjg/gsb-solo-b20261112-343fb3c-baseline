"""Cropping, resampling, canvas changes and affine transformations."""
import math
import numpy as np
from PIL import Image, ImageOps, ImageChops, ImageDraw, ImageColor
from ..errors import StudioError
from ..imaging import checked_size, RESAMPLING


def crop_rect(image, p, context):
    left = p["left"]
    top = p["top"]
    right = left + p["width"]
    bottom = top + p["height"]
    if right > image.width or bottom > image.height:
        raise StudioError("裁剪区域超出了原图，请调整坐标和尺寸")
    return image.crop((left, top, right, bottom))


def crop_center(image, p, context):
    width = max(1, round(image.width * p["percent"] / 100))
    height = max(1, round(image.height * p["percent"] / 100))
    left = (image.width - width) // 2
    top = (image.height - height) // 2
    return image.crop((left, top, left + width, top + height))


def crop_ratio(image, p, context):
    ratio = p["ratio_width"] / p["ratio_height"]
    if image.width / image.height > ratio:
        width = max(1, round(image.height * ratio))
        height = image.height
    else:
        width = image.width
        height = max(1, round(image.width / ratio))
    horizontal = p["anchor_x"] / 100
    vertical = p["anchor_y"] / 100
    left = round((image.width - width) * horizontal)
    top = round((image.height - height) * vertical)
    return image.crop((left, top, left + width, top + height))


def crop_square(image, p, context):
    size = min(image.size)
    anchors = {"start": 0, "center": 0.5, "end": 1}
    offset = anchors[p["anchor"]]
    left = round((image.width - size) * offset)
    top = round((image.height - size) * offset)
    return image.crop((left, top, left + size, top + size))


def crop_circle(image, p, context):
    source = crop_square(image, {"anchor": "center"}, context)
    mask = Image.new("L", source.size, 0)
    draw = ImageDraw.Draw(mask)
    margin = round(source.width * p["inset"] / 100)
    draw.ellipse((margin, margin, source.width - margin - 1,
                  source.height - margin - 1), fill=255)
    if p["feather"]:
        from PIL import ImageFilter
        mask = mask.filter(ImageFilter.GaussianBlur(p["feather"]))
    alpha = ImageChops.multiply(source.getchannel("A"), mask)
    source.putalpha(alpha)
    return source


def crop_trim(image, p, context):
    background = Image.new("RGB", image.size, p["background"])
    difference = ImageChops.difference(image.convert("RGB"), background)
    channels = difference.split()
    difference = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    mask = difference.point(lambda value: 255 if value > p["tolerance"] else 0)
    box = mask.getbbox()
    if box is None:
        raise StudioError("未检测到与背景不同的内容")
    padding = p["padding"]
    left = max(0, box[0] - padding)
    top = max(0, box[1] - padding)
    right = min(image.width, box[2] + padding)
    bottom = min(image.height, box[3] + padding)
    return image.crop((left, top, right, bottom))


def crop_alpha(image, p, context):
    mask = image.getchannel("A").point(lambda value: 255 if value > p["threshold"] else 0)
    box = mask.getbbox()
    if box is None:
        raise StudioError("图片完全透明，没有可裁剪的内容")
    return image.crop(box)


def resize_exact(image, p, context):
    size = checked_size(p["width"], p["height"], context)
    return image.resize(size, RESAMPLING[p["resampling"]])


def resize_percent(image, p, context):
    factor = p["percent"] / 100
    size = checked_size(max(1, image.width * factor), max(1, image.height * factor), context)
    return image.resize(size, RESAMPLING[p["resampling"]])


def resize_fit(image, p, context):
    size = checked_size(p["width"], p["height"], context)
    scale = min(size[0] / image.width, size[1] / image.height)
    if not p["upscale"]:
        scale = min(scale, 1)
    target = checked_size(max(1, image.width * scale), max(1, image.height * scale), context)
    return image.resize(target, Image.Resampling.LANCZOS)


def resize_fill(image, p, context):
    """等比 cover：连续坐标窗口 + 透明感知 Lanczos。

    先按目标宽高比取源图中最大的等比窗口（窗口比例与目标完全一致），
    窗口位置由锚点百分比在连续坐标上决定；再把该浮点窗口通过单遍
    Lanczos 重采样到目标尺寸，避免“先整数裁剪再缩放”带来的构图偏移。

    Alpha 语义与 Pillow 一致：RGBA 经预乘（RGBa）后重采样再还原，
    全透明像素的隐藏色不会污染半透明边缘。
    """
    width, height = checked_size(p["width"], p["height"], context)
    scale = max(width / image.width, height / image.height)
    window_width = width / scale
    window_height = height / scale
    left = (image.width - window_width) * p["anchor_x"] / 100
    top = (image.height - window_height) * p["anchor_y"] / 100
    # Pillow 的 C 层以 32 位浮点接收并校验 box。浮点误差可能让贴边窗口
    # 越界（例如窗口高比源高多 2e-15、top 变成 -1.8e-15，截断为 float32
    # 后被判定为负偏移）。先在连续坐标上定位，再把最终的 float32 分量
    # 钳制进 [0, 源尺寸]，保证窗口合法且保持目标宽高比。
    left = min(max(left, 0.0), image.width - window_width)
    top = min(max(top, 0.0), image.height - window_height)
    box_left = max(float(np.float32(left)), 0.0)
    box_top = max(float(np.float32(top)), 0.0)
    box_right = min(float(np.float32(left + window_width)), float(image.width))
    box_bottom = min(float(np.float32(top + window_height)), float(image.height))
    box = (box_left, box_top, box_right, box_bottom)
    return image.resize((width, height), Image.Resampling.LANCZOS, box=box)


def resize_pad(image, p, context):
    size = checked_size(p["width"], p["height"], context)
    contained = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
    background = Image.new("RGBA", size, p["background"])
    position = ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2)
    background.alpha_composite(contained, position)
    return background


def resize_long_edge(image, p, context):
    factor = p["length"] / max(image.size)
    if not p["upscale"]:
        factor = min(factor, 1)
    size = checked_size(max(1, image.width * factor), max(1, image.height * factor), context)
    return image.resize(size, Image.Resampling.LANCZOS)


def resize_canvas(image, p, context):
    size = checked_size(p["width"], p["height"], context)
    alpha = 0 if p["transparent"] else 255
    background = ImageColor.getrgb(p["background"]) + (alpha,)
    result = Image.new("RGBA", size, background)
    x = (size[0] - image.width) // 2
    y = (size[1] - image.height) // 2
    result.alpha_composite(image, (x, y))
    return result


def transform_rotate(image, p, context):
    if p["expand"]:
        radians = math.radians(p["angle"])
        width = abs(image.width * math.cos(radians)) + abs(image.height * math.sin(radians))
        height = abs(image.width * math.sin(radians)) + abs(image.height * math.cos(radians))
        checked_size(math.ceil(width) + 2, math.ceil(height) + 2, context)
    fill = ImageColor.getrgb(p["background"]) + (0 if p["transparent"] else 255,)
    return image.rotate(p["angle"], resample=Image.Resampling.BICUBIC,
                        expand=p["expand"], fillcolor=fill)


def transform_quarter(image, p, context):
    methods = {
        "90": Image.Transpose.ROTATE_90,
        "180": Image.Transpose.ROTATE_180,
        "270": Image.Transpose.ROTATE_270,
    }
    return image.transpose(methods[p["angle"]])


def transform_flip(image, p, context):
    if p["axis"] == "horizontal":
        return ImageOps.mirror(image)
    if p["axis"] == "vertical":
        return ImageOps.flip(image)
    return ImageOps.flip(ImageOps.mirror(image))


def transform_transpose(image, p, context):
    method = Image.Transpose.TRANSPOSE if p["diagonal"] == "main" else Image.Transpose.TRANSVERSE
    return image.transpose(method)


def transform_shear(image, p, context):
    horizontal = p["horizontal"] / 100
    vertical = p["vertical"] / 100
    matrix = (1, horizontal, -horizontal * image.height / 2,
              vertical, 1, -vertical * image.width / 2)
    return image.transform(image.size, Image.Transform.AFFINE, matrix,
                           resample=Image.Resampling.BICUBIC, fillcolor=p["background"])


def transform_translate(image, p, context):
    x = round(image.width * p["x"] / 100)
    y = round(image.height * p["y"] / 100)
    if p["wrap"]:
        return ImageChops.offset(image, x, y)
    canvas = Image.new("RGBA", image.size, (0, 0, 0, 0))
    canvas.alpha_composite(image, (x, y))
    return canvas


def transform_perspective(image, p, context):
    amount = p["amount"] / 100
    delta = abs(amount) * image.width * 0.4
    if amount >= 0:
        quad = (delta, 0, 0, image.height, image.width, image.height, image.width - delta, 0)
    else:
        quad = (0, 0, delta, image.height, image.width - delta, image.height, image.width, 0)
    return image.transform(image.size, Image.Transform.QUAD, quad,
                           resample=Image.Resampling.BICUBIC)


def transform_mirror_tile(image, p, context):
    size = checked_size(image.width * 2, image.height * 2, context)
    canvas = Image.new("RGBA", size)
    top_right = ImageOps.mirror(image)
    bottom_left = ImageOps.flip(image)
    bottom_right = ImageOps.flip(top_right)
    canvas.paste(image, (0, 0))
    canvas.paste(top_right, (image.width, 0))
    canvas.paste(bottom_left, (0, image.height))
    canvas.paste(bottom_right, image.size)
    if p["keep_size"]:
        return canvas.resize(image.size, Image.Resampling.LANCZOS)
    return canvas

