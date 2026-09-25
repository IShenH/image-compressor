# -*- coding: utf-8 -*-
"""测试用的公共工具。

两件事：
1. **生成样本图片** —— 测试不依赖任何个人照片，全部现场合成，放在临时目录里。
2. **驱动界面** —— tkinter 没法用鼠标点，改成直接调方法 + 泵事件循环。
"""

import math
import os
import random
import sys
import time

# 让测试能 import 到项目根目录下的 compressor / main
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw


def make_photo(path, size=(900, 560), quality=90):
    """造一张「类照片」的 JPEG：平滑渐变 + 高频噪点，编码特性接近真实照片。"""
    width, height = size
    small = Image.new("RGB", (48, 27))
    pixels = small.load()
    for y in range(27):
        for x in range(48):
            pixels[x, y] = (int(120 + 80 * math.sin(x / 6)),
                            int(100 + 70 * math.cos(y / 5)),
                            int(140 + 60 * math.sin((x + y) / 8)))
    base = small.resize((width, height), Image.BICUBIC)
    noise = Image.effect_noise((width, height), 48).convert("L")
    image = Image.blend(base, Image.merge("RGB", (noise, noise, noise)), 0.35)
    random.seed(7)
    draw = ImageDraw.Draw(image)
    for _ in range(8):
        x0 = random.randint(0, max(1, width - 100))
        y0 = random.randint(0, max(1, height - 100))
        draw.ellipse([x0, y0, x0 + 60, y0 + 60],
                     fill=tuple(random.randint(0, 255) for _ in range(3)))
    image.save(path, "JPEG", quality=quality, optimize=True)
    return path


def make_photo_png(path, size=(900, 560)):
    """同一张「类照片」但存成 PNG —— 用来测「保持 PNG 格式」的路径。"""
    jpg = os.path.splitext(path)[0] + "_tmp.jpg"
    make_photo(jpg, size)
    with Image.open(jpg) as image:
        image.save(path, "PNG")
    os.remove(jpg)
    return path


def make_graphic(path, size=(900, 560)):
    """造一张「类截图」的 PNG：大片纯色 + 线条 + 色块，属于扁平图形。"""
    width, height = size
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, int(height * 0.09)], fill=(32, 36, 44))
    draw.rectangle([0, int(height * 0.09), int(width * 0.17), height], fill=(245, 246, 248))
    for i in range(10):
        y = int(height * 0.13) + i * int(height * 0.032)
        draw.rectangle([int(width * 0.01), y, int(width * 0.11), y + 6], fill=(205, 210, 218))
    for row in range(9):
        y = int(height * 0.12) + row * int(height * 0.055)
        draw.rectangle([int(width * 0.2), y, int(width * 0.42), y + 8], fill=(60, 66, 78))
        draw.rectangle([int(width * 0.2), y + 12, int(width * 0.68), y + 18], fill=(180, 186, 196))
        for c in range(3):
            x = int(width * 0.72) + c * 40
            draw.rectangle([x, y - 4, x + 32, y + 24],
                           fill=[(66, 133, 244), (52, 168, 83), (234, 67, 53)][c])
    image.save(path, "PNG", optimize=True)
    return path


def make_icon(path, size=(300, 300)):
    """造一个二元透明的 PNG：alpha 只有 0 和 255 两种（典型图标 / logo）。

    颜色要够丰富 —— 否则 PNG 本来就能把它压到极致，减色没有收益空间，
    就测不到「可以减色」这条路径了。故意不做 optimize 保存，留出优化余地。
    """
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    w, h = size
    center_x, center_y = w // 2, h // 2
    for radius in range(min(center_x, center_y) - 6, 0, -4):
        angle = radius / 12.0
        color = (int(128 + 120 * math.sin(angle)),
                 int(128 + 120 * math.cos(angle * 1.3)),
                 int(128 + 120 * math.sin(angle * 0.7)))
        draw.ellipse([center_x - radius, center_y - radius,
                      center_x + radius, center_y + radius],
                     fill=color + (255,))
    image.save(path, "PNG")
    return path


def make_soft_alpha(path, size=(300, 300)):
    """造一个带 alpha 渐变的 PNG —— 用来验证「透明太复杂就别减色」的保护。

    特意**不做** optimize 保存：这类图会被跳过减色、退回无损优化，
    如果原文件已经是优化过的，就没什么可省的了，测不出降级路径。
    """
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    w, h = size
    # 半径要留够余量，否则左上角坐标会超过右下角（Pillow 会直接报错）
    steps = max(1, min(180, w // 2 - 20))
    for i in range(steps):
        draw.ellipse([8 + i, 8 + i, w - 8 - i, h - 8 - i],
                     fill=(220, 60, 60, 255 - i))
    image.save(path, "PNG")
    return path


def make_broken_jpeg(path, source):
    """把一个正常 JPEG 砍掉一半，制造「文件头正常、数据截断」的坏文件。"""
    with open(source, "rb") as handle:
        raw = handle.read()
    with open(path, "wb") as handle:
        handle.write(raw[: len(raw) // 2])
    return path


def wait_until(predicate, timeout=60.0, pump=None):
    """泵事件循环直到 predicate 成立或超时。

    后台线程跑完后，结果要通过 tkinter 的 after 回调回到主线程，
    所以测试必须真的让事件循环转起来 —— 光 sleep 是没用的。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pump is not None:
            pump()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
