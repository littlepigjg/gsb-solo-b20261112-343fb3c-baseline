"""GIMP Palette 转换接口的互操作示例（仅使用 Python 标准库）。

用法::

    python examples/palette_interop.py example.gpl --swatch-size 32
    python examples/palette_interop.py example.gpl --base-url http://127.0.0.1:8765

脚本会：
1. 以 UTF-8 读取本地 GIMP Palette 文本，调用 POST /api/palettes/convert；
2. 打印解析后的 name、columns 与颜色数量；
3. 将规范化 GPL 写回 <原名>.canonical.gpl，并再次提交验证往返一致性；
4. 通过响应里的 asset.url 下载色卡 PNG，保存为 <原名>.swatch.png。

等价 curl::

    curl -s -X POST http://127.0.0.1:8765/api/palettes/convert \
        -H 'Content-Type: application/json' \
        -d '{"text": "GIMP Palette\n0 0 0 黑\n255 255 255 白\n", "swatch_size": 32}'
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def post_json(base_url, payload):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/palettes/convert",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = json.loads(error.read().decode("utf-8"))
        raise SystemExit(f"接口返回错误 {error.code}: {body['error']['message']}") from error
    if not body.get("ok"):
        raise SystemExit(f"接口返回失败: {body}")
    return body["data"]


def main():
    parser = argparse.ArgumentParser(description="GIMP Palette 转换互操作示例")
    parser.add_argument("gpl_path", type=Path, help="本地 .gpl 文件路径")
    parser.add_argument("--swatch-size", type=int, default=32, help="色块像素边长，8–128，默认 32")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="服务地址")
    args = parser.parse_args()

    text = args.gpl_path.read_text(encoding="utf-8")
    data = post_json(args.base_url, {"text": text, "swatch_size": args.swatch_size})

    print(f"name    : {data['name']}")
    print(f"columns : {data['columns']}")
    print(f"colors  : {len(data['colors'])}")
    for index, color in enumerate(data["colors"], start=1):
        r, g, b = color["rgb"]
        print(f"  {index:>3}. {r:>3} {g:>3} {b:>3}  {color['name']}")

    canonical_path = args.gpl_path.with_suffix(".canonical.gpl")
    canonical_path.write_text(data["gpl"], encoding="utf-8", newline="\n")

    # 重新导入规范化文本，结果必须与首次完全一致。
    again = post_json(args.base_url, {"text": data["gpl"], "swatch_size": args.swatch_size})
    if (again["name"], again["columns"], again["colors"], again["gpl"]) != (
        data["name"], data["columns"], data["colors"], data["gpl"],
    ):
        raise SystemExit("规范化文本重新导入的结果与首次不一致")
    print(f"round-trip 校验通过，规范化文本已写入: {canonical_path}")

    png_url = f"{args.base_url.rstrip('/')}{data['asset']['url']}"
    with urllib.request.urlopen(png_url, timeout=30) as response:
        png_bytes = response.read()
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit("下载内容不是有效的 PNG")
    png_path = args.gpl_path.with_suffix(".swatch.png")
    png_path.write_bytes(png_bytes)
    print(f"色卡 PNG 已下载: {png_path}（{data['asset']['width']}x{data['asset']['height']}）")


if __name__ == "__main__":
    main()
