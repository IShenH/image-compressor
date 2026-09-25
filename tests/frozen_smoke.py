# -*- coding: utf-8 -*-
"""打包产物冒烟测试：确认「冻结之后」的程序仍然具备完整的图片编解码能力。

为什么需要它：交付给用户的界面程序是用 `--windowed` 打的，**没有任何控制台输出**。
万一 Pillow 的 WebP 支持（依赖额外 DLL）没被正确打包进去，界面照样能开，
只是用户一用到那条路径就失败 —— 而且在开发机上完全看不出来。

所以这个脚本单独以 `--console` 打包、单独运行，把每条编码路径都实跑一遍并打印结果。
它不启动界面，因此也不会弹窗。

打包与运行方式见 README。
"""

import os
import shutil
import sys
import tempfile

# 未打包时（直接 python tests/frozen_smoke.py 运行）需要手动补上项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, features

import compressor

results = []


def check(label, condition, detail=""):
    results.append((label, condition, detail))
    print("  %s  %s%s" % ("PASS" if condition else "FAIL", label,
                          ("  " + detail) if detail else ""))


def main():
    print("=" * 76)
    print("运行环境")
    print("-" * 76)
    print("  Python      : %s" % sys.version.split()[0])
    print("  是否冻结    : %s" % getattr(sys, "frozen", False))
    print("  可执行文件  : %s" % sys.executable)
    print("  Pillow      : %s" % getattr(Image, "__version__", "?"))
    print()
    print("编解码能力")
    print("-" * 76)
    for name in ("jpg", "zlib", "webp", "libtiff"):
        check("支持 %s" % name, bool(features.check(name)),
              "缺失会导致对应格式不可用" if not features.check(name) else "")

    work = tempfile.mkdtemp(prefix="smoke_")
    try:
        # 造样本：一张类照片的 JPEG、一张类截图的 PNG
        photo = os.path.join(work, "photo.jpg")
        base = Image.new("RGB", (600, 400))
        draw = ImageDraw.Draw(base)
        for y in range(0, 400, 4):
            draw.line([(0, y), (600, y)], fill=(y % 255, (y * 2) % 255, 128))
        base.save(photo, "JPEG", quality=92, optimize=True)

        shot = os.path.join(work, "shot.png")
        graphic = Image.new("RGB", (600, 400), (255, 255, 255))
        draw = ImageDraw.Draw(graphic)
        draw.rectangle([0, 0, 600, 40], fill=(32, 36, 44))
        for row in range(8):
            y = 60 + row * 40
            draw.rectangle([40, y, 300, y + 10], fill=(60, 66, 78))
            draw.rectangle([40, y + 16, 520, y + 22], fill=(180, 186, 196))
        graphic.save(shot, "PNG")

        print()
        print("各条编码路径（这才是真正的验证）")
        print("-" * 76)
        source_size = os.path.getsize(photo)

        for level in compressor.LEVELS:
            out = os.path.join(work, "p_%s.jpg" % level)
            result = compressor.compress(photo, out, level)
            check("JPEG 三档 · %s" % level,
                  result.output_size_bytes < source_size and result.output_format == "JPEG",
                  "%d → %d 字节" % (source_size, result.output_size_bytes))

        out = os.path.join(work, "p.webp")
        result = compressor.compress(photo, out, "balanced", output_format="WEBP")
        check("WebP 有损编码", result.output_format == "WEBP" and result.output_size_bytes > 0,
              "%d 字节" % result.output_size_bytes)

        out = os.path.join(work, "g.png")
        result = compressor.compress(shot, out, "balanced")
        check("PNG 减色（需要调色板量化）",
              result.quantized_colors is not None and result.output_size_bytes > 0,
              "%d 色，%d 字节" % (result.quantized_colors or 0, result.output_size_bytes))

        out = os.path.join(work, "s.jpg")
        result = compressor.compress(photo, out, "balanced", max_dimension=200)
        check("尺寸限制 + 重采样", result.resized and max(result.output_width,
                                                        result.output_height) == 200,
              "%dx%d" % (result.output_width, result.output_height))

        # 每个产物都要能重新打开 —— 能写出来不等于写对了
        ok = True
        detail = ""
        for name in sorted(os.listdir(work)):
            path = os.path.join(work, name)
            with open(path, "rb") as handle:
                if len(handle.read()) == 0:
                    ok, detail = False, name + " 是空文件"
            with Image.open(path) as reopened:
                reopened.verify()
        check("全部产物都能正常打开", ok, detail)

    except Exception as exc:  # 任何意外都要在这里被看见，而不是悄悄退出
        check("冒烟测试未抛异常", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    failed = [item for item in results if not item[1]]
    print()
    print("=" * 76)
    print("共 %d 项，失败 %d 项" % (len(results), len(failed)))
    print("=" * 76)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
