"""Classical edge detectors with explicit border handling."""
import numpy as np
from PIL import Image, ImageFilter, ImageOps
from ..imaging import gray_array, rgb_array, from_array, convolution, percentile_stretch


def _gradient(image, horizontal, vertical, p):
    gray = gray_array(image)
    gx = convolution(gray, horizontal)
    gy = convolution(gray, vertical)
    if p["direction"] == "horizontal":
        value = np.abs(gx)
    elif p["direction"] == "vertical":
        value = np.abs(gy)
    else:
        value = np.hypot(gx, gy)
    magnitude = percentile_stretch(value)
    magnitude = magnitude * p["gain"]
    if p["invert"]:
        magnitude = 255 - np.clip(magnitude, 0, 255)
    return from_array(magnitude, image)


def edge_sobel(image, p, context):
    horizontal = [[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]]
    vertical = [[-1, -2, -1], [0, 0, 0], [1, 2, 1]]
    return _gradient(image, horizontal, vertical, p)


def edge_prewitt(image, p, context):
    horizontal = [[-1, 0, 1], [-1, 0, 1], [-1, 0, 1]]
    vertical = [[-1, -1, -1], [0, 0, 0], [1, 1, 1]]
    return _gradient(image, horizontal, vertical, p)


def edge_scharr(image, p, context):
    horizontal = [[-3, 0, 3], [-10, 0, 10], [-3, 0, 3]]
    vertical = [[-3, -10, -3], [0, 0, 0], [3, 10, 3]]
    return _gradient(image, horizontal, vertical, p)


def edge_laplacian(image, p, context):
    gray = image.convert("L").filter(ImageFilter.GaussianBlur(p["blur"]))
    array = np.asarray(gray, dtype=np.float32)
    kernel = [[0, 1, 0], [1, -4, 1], [0, 1, 0]]
    result = np.abs(convolution(array, kernel)) * p["gain"]
    if p["invert"]:
        result = 255 - np.clip(result, 0, 255)
    return from_array(result, image)


def edge_roberts(image, p, context):
    gray = gray_array(image)
    padded = np.pad(gray, ((0, 1), (0, 1)), mode="edge")
    a = padded[:-1, :-1] - padded[1:, 1:]
    b = padded[1:, :-1] - padded[:-1, 1:]
    magnitude = percentile_stretch(np.hypot(a, b)) * p["gain"]
    if p["invert"]:
        magnitude = 255 - np.clip(magnitude, 0, 255)
    return from_array(magnitude, image)


def edge_contour(image, p, context):
    gray = image.convert("L").filter(ImageFilter.GaussianBlur(p["blur"]))
    result = gray.filter(ImageFilter.CONTOUR)
    if p["invert"]:
        result = ImageOps.invert(result)
    return from_array(np.asarray(result), image)


def edge_binary(image, p, context):
    source = image.filter(ImageFilter.GaussianBlur(p["blur"]))
    gradient = edge_sobel(source, {"direction": "both", "gain": 1, "invert": False}, context)
    array = gray_array(gradient)
    binary = np.where(array >= p["threshold"], 255, 0)
    if p["invert"]:
        binary = 255 - binary
    return from_array(binary, image)


def edge_overlay(image, p, context):
    gradient = edge_sobel(image, {"direction": "both", "gain": 1, "invert": False}, context)
    intensity = gray_array(gradient) / 255 * p["strength"]
    from PIL import ImageColor
    color = np.asarray(ImageColor.getrgb(p["color"]), dtype=np.float32)
    base = rgb_array(image)
    result = base * (1 - intensity[..., None]) + color * intensity[..., None]
    return from_array(result, image)

