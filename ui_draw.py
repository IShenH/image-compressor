# -*- coding: utf-8 -*-
"""界面绘制层 —— 纯 Pillow，不导入任何界面库。

和 compressor 与 main 的关系一样：这一层只负责「把东西画出来」，
不关心它会被放到哪儿、被谁点击。所以它能脱离界面单独验证。

**为什么用 Pillow 而不是 tkinter 的 Canvas 图元**
    Canvas 画圆角和斜线**没有抗锯齿**，在高 DPI 屏幕上是明显的锯齿。
    Pillow 可以先在 4 倍尺寸上画、再缩回来，得到平滑边缘。

**为什么素材全部在代码里生成、不落地图片文件**
    打包成 exe 后，程序运行在临时解压目录里，外部图片资源要靠
    `sys._MEIPASS` 特殊定位，还要在打包命令里加 `--add-data`，很容易漏。
    代码生成则完全没有这个问题 —— 冻结前后行为一致。

坐标约定：所有图标都按 **24×24 的网格**设计，绘制时按实际尺寸等比放大。
"""

import math

from PIL import Image, ImageChops, ImageDraw

# 超采样倍数。圆角、弧线、斜线都靠它拿到平滑边缘。
# 4 倍已看不出锯齿，再高只是浪费启动时间。
SS = 4


# ---------------------------------------------------------------- 基础工具

def to_rgba(color):
    """把 '#RRGGBB' / (r,g,b) / (r,g,b,a) 统一成 RGBA 四元组。"""
    if isinstance(color, str):
        c = color.lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)
    if len(color) == 3:
        return (int(color[0]), int(color[1]), int(color[2]), 255)
    return tuple(int(v) for v in color)


def _hi(size):
    """目标尺寸 → 超采样后的工作尺寸。"""
    return (max(1, int(round(size[0])) * SS), max(1, int(round(size[1])) * SS))


def _rounded_mask(size, radius, corners):
    """画一张 L 模式的圆角矩形遮罩（用于填色和描边）。"""
    w, h = size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    x0, y0, x1, y1 = 0, 0, w - 1, h - 1
    r = int(max(0, min(radius, (x1 - x0) // 2, (y1 - y0) // 2)))
    tl, tr, br, bl = corners
    if r <= 0:
        d.rectangle([x0, y0, x1, y1], fill=255)
        return mask
    # 中间十字（把四个角留出来）
    d.rectangle([x0 + (r if tl else 0), y0, x1 - (r if tr else 0), y1], fill=255)
    d.rectangle([x0, y0 + (r if tl else 0), x1, y1 - (r if br else 0)], fill=255)
    # 四个圆角
    if tl:
        d.pieslice([x0, y0, x0 + 2 * r, y0 + 2 * r], 180, 270, fill=255)
    if tr:
        d.pieslice([x1 - 2 * r, y0, x1, y0 + 2 * r], 270, 360, fill=255)
    if br:
        d.pieslice([x1 - 2 * r, y1 - 2 * r, x1, y1], 0, 90, fill=255)
    if bl:
        d.pieslice([x0, y1 - 2 * r, x0 + 2 * r, y1], 90, 180, fill=255)
    return mask


# ---------------------------------------------------------------- 形状

def rounded_rect(size, radius=0, fill=None, outline=None, width=1,
                 corners=(True, True, True, True)):
    """抗锯齿的圆角矩形。fill / outline 都可以为 None。

    corners 依次是 (左上, 右上, 右下, 左下)，可以只圆一部分角。
    """
    W, H = _hi(size)
    r = radius * SS
    wd = max(1, int(round(width * SS)))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    if fill is not None:
        img.paste(to_rgba(fill), (0, 0), _rounded_mask((W, H), r, corners))

    if outline is not None:
        # 描边的做法：外圈遮罩减去「向内收缩 width」的内圈遮罩，得到一圈环
        outer = _rounded_mask((W, H), r, corners)
        inner = Image.new("L", (W, H), 0)
        ix0, iy0 = wd, wd
        ix1, iy1 = W - 1 - wd, H - 1 - wd
        if ix1 > ix0 and iy1 > iy0:
            box = Image.new("L", (ix1 - ix0 + 1, iy1 - iy0 + 1), 0)
            box.paste(_rounded_mask((ix1 - ix0 + 1, iy1 - iy0 + 1),
                                    max(0, r - wd), corners), (0, 0))
            inner.paste(box, (ix0, iy0))
        img.paste(to_rgba(outline), (0, 0), ImageChops.subtract(outer, inner))

    return img.resize((int(size[0]), int(size[1])), Image.LANCZOS)


def vgradient(size, top, bottom, radius=0, corners=(True, True, True, True)):
    """竖直线性渐变，可裁剪成圆角。

    只为高度做循环 —— 竖直渐变的每一行颜色都一样，
    不需要逐像素算，也没必要超采样（渐变本身没有锯齿）。
    圆角部分单独用超采样遮罩裁出来，边缘依然是平滑的。
    """
    W, H = int(size[0]), int(size[1])
    if W < 1 or H < 1:
        return Image.new("RGBA", (max(1, W), max(1, H)), (0, 0, 0, 0))
    t, b = to_rgba(top), to_rgba(bottom)
    strip = Image.new("RGBA", (1, H))
    px = strip.load()
    for y in range(H):
        f = y / (H - 1) if H > 1 else 0.0
        px[0, y] = tuple(int(round(t[i] + (b[i] - t[i]) * f)) for i in range(4))
    img = strip.resize((W, H), Image.NEAREST)
    if radius <= 0:
        return img
    mask = _rounded_mask(_hi(size), radius * SS, corners).resize((W, H), Image.LANCZOS)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def hline(size, color, thickness=1):
    """一条横向分隔线。"""
    W = max(1, int(size[0]))
    t = max(1, int(round(thickness)))
    img = Image.new("RGBA", (W, t), to_rgba(color))
    return img


def hgradient(size, left, right, radius=0, corners=(True, True, True, True)):
    """横向线性渐变（保存按钮用）。"""
    W, H = int(size[0]), int(size[1])
    l, r = to_rgba(left), to_rgba(right)
    strip = Image.new("RGBA", (W, 1))
    px = strip.load()
    for x in range(W):
        f = x / (W - 1) if W > 1 else 0.0
        px[x, 0] = tuple(int(round(l[i] + (r[i] - l[i]) * f)) for i in range(4))
    img = strip.resize((W, H), Image.NEAREST)
    if radius <= 0:
        return img
    mask = _rounded_mask(_hi(size), radius * SS, corners).resize((W, H), Image.LANCZOS)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


# ---------------------------------------------------------------- 图标

def _p_file(d, u, w, c):
    """文件（文件名）"""
    d.line([(6 * u, 3 * u), (14 * u, 3 * u), (18 * u, 7.5 * u), (18 * u, 21 * u),
            (6 * u, 21 * u), (6 * u, 3 * u)], fill=c, width=w, joint="curve")
    d.line([(14 * u, 3 * u), (14 * u, 7.5 * u), (18 * u, 7.5 * u)], fill=c, width=w, joint="curve")


def _p_tag(d, u, w, c):
    """标签（格式）"""
    pts = [(3.5 * u, 11 * u), (11 * u, 3.5 * u), (20.5 * u, 3.5 * u),
           (20.5 * u, 13 * u), (13 * u, 20.5 * u), (3.5 * u, 11 * u)]
    d.line(pts, fill=c, width=w, joint="curve")
    d.ellipse([16 * u - 1.6 * u, 7.5 * u - 1.6 * u,
               16 * u + 1.6 * u, 7.5 * u + 1.6 * u], fill=c)


def _p_ruler(d, u, w, c):
    """尺子（尺寸）"""
    d.line([(3 * u, 8 * u), (21 * u, 8 * u), (21 * u, 16 * u), (3 * u, 16 * u),
            (3 * u, 8 * u)], fill=c, width=w, joint="curve")
    for i, x in enumerate((7.5, 11, 14.5, 18)):
        d.line([(x * u, 8 * u), (x * u, (12 if i % 2 == 0 else 10.5) * u)],
               fill=c, width=w)


def _p_disk(d, u, w, c):
    """圆柱（文件大小）"""
    d.ellipse([4 * u, 2.5 * u, 20 * u, 7.5 * u], outline=c, width=w)
    d.line([(4 * u, 5 * u), (4 * u, 18.5 * u)], fill=c, width=w)
    d.line([(20 * u, 5 * u), (20 * u, 18.5 * u)], fill=c, width=w)
    d.arc([4 * u, 15.5 * u, 20 * u, 21.5 * u], start=0, end=180, fill=c, width=w)


def _p_image(d, u, w, c):
    """图片（区块标题用）"""
    d.line([(3.5 * u, 5 * u), (20.5 * u, 5 * u), (20.5 * u, 19 * u), (3.5 * u, 19 * u),
            (3.5 * u, 5 * u)], fill=c, width=w, joint="curve")
    d.ellipse([7 * u, 8.5 * u, 10.2 * u, 11.7 * u], outline=c, width=w)
    d.line([(4.5 * u, 17 * u), (10 * u, 12.5 * u), (14 * u, 16 * u),
            (16.5 * u, 13.8 * u), (19.5 * u, 16.5 * u)], fill=c, width=w, joint="curve")


def _p_sliders(d, u, w, c):
    """滑块（压缩档位标题）"""
    for y, kx in ((7.5, 15.5), (12, 9), (16.5, 16.5)):
        d.line([(3.5 * u, y * u), (20.5 * u, y * u)], fill=c, width=w)
        d.ellipse([(kx - 2.3) * u, (y - 2.3) * u, (kx + 2.3) * u, (y + 2.3) * u],
                  fill=c)


def _p_expand(d, u, w, c):
    """向外箭头（输出尺寸标题）"""
    d.line([(4 * u, 9.5 * u), (4 * u, 4 * u), (9.5 * u, 4 * u)], fill=c, width=w, joint="curve")
    d.line([(14.5 * u, 20 * u), (20 * u, 20 * u), (20 * u, 14.5 * u)],
           fill=c, width=w, joint="curve")
    d.line([(4.8 * u, 4.8 * u), (10.6 * u, 10.6 * u)], fill=c, width=w)
    d.line([(13.4 * u, 13.4 * u), (19.2 * u, 19.2 * u)], fill=c, width=w)


def _p_bars(d, u, w, c):
    """柱状图（压缩结果标题）"""
    d.line([(3.5 * u, 20.5 * u), (20.5 * u, 20.5 * u)], fill=c, width=w)
    for x, top in ((6.5, 12), (11, 7), (15.5, 14.5)):
        d.line([(x * u, 20.5 * u), (x * u, top * u)], fill=c, width=int(w * 1.8))


def _p_folder(d, u, w, c):
    """文件夹（选择图片按钮）"""
    d.line([(3 * u, 18.5 * u), (3 * u, 6 * u), (9.5 * u, 6 * u), (11.5 * u, 8.8 * u),
            (21 * u, 8.8 * u), (21 * u, 18.5 * u), (3 * u, 18.5 * u)],
           fill=c, width=w, joint="curve")


def _p_download(d, u, w, c):
    """下载（保存按钮）"""
    d.line([(12 * u, 3.5 * u), (12 * u, 14.5 * u)], fill=c, width=w)
    d.line([(7 * u, 9.8 * u), (12 * u, 15 * u), (17 * u, 9.8 * u)],
           fill=c, width=w, joint="curve")
    d.line([(5 * u, 16.5 * u), (5 * u, 20.5 * u), (19 * u, 20.5 * u), (19 * u, 16.5 * u)],
           fill=c, width=w, joint="curve")


def _p_sparkle(d, u, w, c):
    """四角星（压缩按钮 / 高画质）"""
    pts = []
    for i in range(8):
        ang = math.pi / 4 * i - math.pi / 2
        rad = 9.5 * u if i % 2 == 0 else 3.4 * u
        pts.append((12 * u + rad * math.cos(ang), 12 * u + rad * math.sin(ang)))
    d.polygon(pts, fill=c)


def _p_contrast(d, u, w, c):
    """半明半暗（均衡档）"""
    d.ellipse([3.5 * u, 3.5 * u, 20.5 * u, 20.5 * u], outline=c, width=w)
    d.pieslice([3.5 * u, 3.5 * u, 20.5 * u, 20.5 * u], start=90, end=270, fill=c)


def _p_compress(d, u, w, c):
    """向内箭头（小体积档）"""
    d.line([(12 * u, 3.5 * u), (12 * u, 10 * u)], fill=c, width=w)
    d.line([(8.8 * u, 7 * u), (12 * u, 10.3 * u), (15.2 * u, 7 * u)],
           fill=c, width=w, joint="curve")
    d.line([(12 * u, 20.5 * u), (12 * u, 14 * u)], fill=c, width=w)
    d.line([(8.8 * u, 17 * u), (12 * u, 13.7 * u), (15.2 * u, 17 * u)],
           fill=c, width=w, joint="curve")


def _p_info(d, u, w, c):
    """信息（提示行）"""
    d.ellipse([3.5 * u, 3.5 * u, 20.5 * u, 20.5 * u], outline=c, width=w)
    d.line([(12 * u, 11 * u), (12 * u, 16.8 * u)], fill=c, width=w)
    d.ellipse([10.7 * u, 7 * u, 13.3 * u, 9.6 * u], fill=c)


def _p_check(d, u, w, c):
    """对勾（复选框）"""
    d.line([(5 * u, 12.5 * u), (10 * u, 17.5 * u), (19 * u, 6.5 * u)],
           fill=c, width=w, joint="curve")


_PAINTERS = {
    "file": _p_file,
    "tag": _p_tag,
    "ruler": _p_ruler,
    "disk": _p_disk,
    "image": _p_image,
    "sliders": _p_sliders,
    "expand": _p_expand,
    "bars": _p_bars,
    "folder": _p_folder,
    "download": _p_download,
    "sparkle": _p_sparkle,
    "contrast": _p_contrast,
    "compress": _p_compress,
    "info": _p_info,
    "check": _p_check,
}

ICON_NAMES = tuple(sorted(_PAINTERS))


def icon(name, size=16, color="#333A42", stroke=1.7):
    """画一个图标，返回抗锯齿后的 RGBA 图。

    name 见 ICON_NAMES；size 是边长（像素）；stroke 是线宽（按 24 网格计）。
    """
    if name not in _PAINTERS:
        raise ValueError("没有这个图标：%r，可用：%s" % (name, "、".join(ICON_NAMES)))
    S = max(1, int(size)) * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _PAINTERS[name](d, S / 24.0, max(1, int(round(stroke * SS))), to_rgba(color))
    return img.resize((int(size), int(size)), Image.LANCZOS)


def app_icon(size=64, color="#3B5C83", accent="#43658B"):
    """窗口/任务栏图标：一个圆角方块 + 向内压缩的双箭头。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pad = max(1, size // 16)
    body = vgradient((size - 2 * pad, size - 2 * pad), accent, color,
                     radius=size * 0.22)
    img.paste(body, (pad, pad), body)
    mark = icon("compress", size=int(size * 0.56), color="#FFFFFF", stroke=2.2)
    off = (size - mark.width) // 2
    img.paste(mark, (off, off), mark)
    return img


def compose(layers):
    """按顺序把多张同尺寸的 RGBA 图叠起来，返回一张。"""
    if not layers:
        raise ValueError("layers 不能为空")
    w, h = layers[0].size
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for layer in layers:
        out.alpha_composite(layer.convert("RGBA"), (0, 0))
    return out
