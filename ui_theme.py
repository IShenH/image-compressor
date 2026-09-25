# -*- coding: utf-8 -*-
"""界面主题：配色、尺寸、字体。

**所有数值来自设计稿的实测取样**，不是目测估的（取样方法与换算表见
`docs/engineering/ui-spec.md`）。设计稿是 941×1672，实际窗口按比例缩小，
所以每个数值都要乘一个缩放系数 k。

核心约定：**全部用设备像素**。
    窗口尺寸、控件尺寸、字号一律以像素为单位，字号写**负数**
    （Tk 里负数表示「按像素」，不参与 points→像素换算）。
    否则会出现「布局按像素、文字按点」两套坐标系打架，
    在高 DPI 屏幕上彼此错位 —— 这正是 M2 阶段吃过的亏。
"""

import tkinter as tk
from tkinter import font as tkfont

from PIL import ImageTk

import ui_draw

# ---------------------------------------------------------------- 设计稿基准
REF_W = 941
REF_H = 1672

# 窗口宽度（**逻辑像素**）。实际值会乘系统 DPI 缩放 ——
# 之前直接用设备像素定尺寸，等于忽略了用户的 150% 缩放设置，字看着就偏小。
WIN_W_LOGICAL = 467

# 系统 DPI 缩放系数，由 init() 实测（本机 150% → 1.5）
DPI_SCALE = 1.0


def lv(x):
    """逻辑像素 → 设备像素。"""
    return max(1, int(round(x * DPI_SCALE)))

# 设计稿配色（Pillow 取样所得）
PAGE_BG = "#F3EEE7"       # 页面底色
CARD_BG = "#F7F5F0"       # 卡片底色
CARD_BG_SOFT = "#F9F4EE"
BORDER = "#E4DED8"        # 卡片边框 / 分隔线
BORDER_SOFT = "#EAE4DE"
PRIMARY_TOP = "#43658B"   # 主按钮渐变顶
PRIMARY_BOT = "#395B80"   # 主按钮渐变底
PRIMARY = "#3B5C83"       # 主色
PRIMARY_DEEP = "#2E4A6B"  # 按下
PRIMARY_SOFT = "#406185"  # 选择图片按钮
ACCENT = "#B7DFF9"        # 浅蓝辅助
SELECTED_BG = "#EDF1F6"   # 选中卡片底色
SELECTED_BORDER = "#7A9BC0"
DISABLED_BG = "#E9E6E0"
DISABLED_FG = "#A9A49C"

TEXT = "#333A42"          # 正文
TEXT_STRONG = "#22282F"   # 标题
TEXT_MUTED = "#6E6D69"    # 次要说明
TEXT_FAINT = "#9A968E"    # 未填的占位符「—」
ON_PRIMARY = "#FFFFFF"

GOOD = "#3F7A57"
WARN = "#8A6D00"
BAD = "#C0392B"

# 字体族：按可用性挑第一个。注意有的字体只注册了中文名或只注册了 UI 变体
# （本机有 `Microsoft YaHei UI` 但没有 `Microsoft YaHei`），所以两个名字都要试。
FONT_CANDIDATES = ("Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei",
                   "等线", "黑体", "Segoe UI")


class Metrics:
    """按缩放系数算出来的实际尺寸。由 init() 填充。"""

    def __init__(self, k, win_w, max_h):
        self.k = k
        self.window_w = win_w
        self.max_h = max_h          # 可用高度上限；**实际窗口高度由内容决定**

        s = self.s = lambda v: max(1, int(round(v * k)))   # noqa: E731

        # 间距与圆角。间距刻意压到最小（用户要求），不再按设计稿的宽松留白走。
        self.page_pad = s(8)            # 顶部与左右外边距
        self.bottom_pad = s(20)         # 底部留白比顶部大一点，视觉上有收束感
        self.gap = s(4)                 # 区块间距的默认值（个别相邻对在 main 里单独调）
        self.card_pad_x = s(20)
        self.card_pad_y = s(16)
        self.row_gap = s(6)
        self.radius = s(12)
        self.radius_small = s(8)
        self.border_w = 1

        # 控件
        self.btn_h = s(54)            # 通栏按钮高
        self.btn_h_small = s(40)
        self.card_h = s(100)          # 档位卡片高
        # 预览缩略图：高度要与右侧四行信息（4 × s(46)）基本齐平，卡片才不会一边空一边满。
        # 比例取 4:3。
        self.preview_w = s(243)
        self.preview_h = s(182)

        # 字号（负号 = 像素）。数值比设计稿大 —— 布局全部按设备像素走，
        # 字号若照抄设计稿比例，在 150% 缩放的屏幕上会明显偏小。
        self.fs_title_bar = -s(26)
        self.fs_drop_main = -s(24)
        self.fs_drop_sub = -s(18)
        self.fs_section = -s(21)
        self.fs_body = -s(19)
        self.fs_small = -s(18)
        self.fs_btn = -s(22)

    def font(self, size_px, bold=False):
        """构造 tkinter 字体元组。size_px 是**参考图上的字号**，会用 k 换算。"""
        return (FONT_FAMILY, -self.s(size_px), "bold" if bold else "normal")


FONT_FAMILY = "Microsoft YaHei UI"

# 默认值来自本机实测（1920×1080 @150%）。init() 会按实际屏幕重算。
M = Metrics(700.0 / REF_W, 700, 1016)


# ---------------------------------------------------------------- 屏幕测算

def _rect_type():
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]
    return RECT


def work_area():
    """工作区大小（设备像素，已扣掉任务栏）。取不到就返回 None。"""
    try:
        import ctypes
        RECT = _rect_type()
        r = RECT()
        # SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            w, h = r.right - r.left, r.bottom - r.top
            if w > 200 and h > 200:
                return w, h
    except Exception:
        pass
    return None


_frame_cache = (0, 0)


def frame_overhead(root):
    """窗口外框比客户区多出来的部分 (横向, 纵向)。

    必须量真实窗口 —— 用 GetSystemMetrics 的公式算不准：
    本机 150% 缩放下公式给 45px，实测是 56px（还差一层窗口阴影）。
    做法是把窗口先摆到屏幕外，量完再挪回来，避免开机闪一下。
    """
    global _frame_cache
    try:
        import ctypes
        root.geometry("1x1+-3000+-3000")
        root.update_idletasks()
        root.update()
        hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)   # GA_ROOT
        RECT = _rect_type()
        r = RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return 0, 0
        _frame_cache = ((r.right - r.left) - root.winfo_width(),
                        (r.bottom - r.top) - root.winfo_height())
    except Exception:
        _frame_cache = (0, 0)
    return _frame_cache


_anchor_y = 4


def geometry_for(root, w, h):
    """窗口 geometry 字符串。

    水平居中；垂直固定在同一锚点上 —— 窗口高度会随内容变化，
    如果每次重新居中，高度一变整窗就会往上跳一下。
    ⚠️ 水平居中要算上边框开销（`geometry()` 设的是客户区）。
    """
    area = work_area() or (root.winfo_screenwidth(), root.winfo_screenheight())
    fw, _ = _frame_cache
    x = max(0, (area[0] - (w + fw)) // 2)
    return "%dx%d+%d+%d" % (w, h, x, _anchor_y)


def init(root, margin=8):
    """按实际屏幕算出窗口宽度与尺寸参数，返回 (宽, 可用高度上限)。

    **高度不再固定按设计稿比例**：窗口高度由内容决定（见 App._compute_layout）。
    这里只负责给出宽度和「最高能有多高」，剩下交给界面层。

    宽度用**逻辑像素 × DPI 缩放** —— 直接拿设备像素当尺寸等于忽略用户的缩放设置，
    字会明显偏小。
    """
    global M, FONT_FAMILY, DPI_SCALE, _anchor_y

    try:
        families = set(tkfont.families(root))
        for name in FONT_CANDIDATES:
            if name in families:
                FONT_FAMILY = name
                break
    except Exception:
        pass

    # 真实的系统缩放：DPI 感知打开后，1 英寸等于多少像素 ÷ 96
    try:
        DPI_SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    except Exception:
        DPI_SCALE = 1.0

    area = work_area()
    if area is None:
        area = (root.winfo_screenwidth(), root.winfo_screenheight())
    avail_w, avail_h = area

    _, frame_h = frame_overhead(root)
    max_h = avail_h - frame_h - margin

    win_w = min(lv(WIN_W_LOGICAL), avail_w - margin)
    win_w = max(420, win_w)

    M = Metrics(win_w / float(REF_W), win_w, max_h)
    # 尺寸变了，之前按旧尺寸缓存的字体与图片都不能再用
    clear_cache()
    _font_cache.clear()
    _pil_font_cache.clear()

    # 垂直位置固定在这个锚点上。高度一变只向下长，不整体跳一下。
    _anchor_y = max(4, (avail_h - max_h) // 2)
    return win_w, max_h


# ---------------------------------------------------------------- 图片缓存

_photo_cache = {}
# PhotoImage 必须被强引用着，否则会被垃圾回收，界面上显示成一片空白 ——
# 这是 tkinter 最经典的坑之一，所以缓存不只是为了提速，也是为了「活着」。
_keep_alive = []


def photo(image, key=None):
    """PIL 图 → tkinter 可用的 PhotoImage（按 key 缓存并保持强引用）。"""
    if key is not None and key in _photo_cache:
        return _photo_cache[key]
    p = ImageTk.PhotoImage(image)
    _keep_alive.append(p)
    if key is not None:
        _photo_cache[key] = p
    return p


def clear_cache():
    _photo_cache.clear()
    del _keep_alive[:]


_font_cache = {}
_pil_font_cache = {}

# tkinter 字体族名 → 对应的字体文件（正体, 粗体）
_FONT_FILES = {
    "Microsoft YaHei UI": ("msyh.ttc", "msyhbd.ttc"),
    "微软雅黑": ("msyh.ttc", "msyhbd.ttc"),
    "Microsoft YaHei": ("msyh.ttc", "msyhbd.ttc"),
    "等线": ("Deng.ttf", "Dengb.ttf"),
    "黑体": ("simhei.ttf", "simhei.ttf"),
    "Segoe UI": ("segoeui.ttf", "segoeuib.ttf"),
}


def pil_font(ref_size, bold=False):
    """PIL 用的字体对象（把静态文字画进背景图时用）。

    ⚠️ tkinter 与 PIL 是两套文字渲染，同一族、同一像素尺寸下字形仍有细微差别。
    所以约定：**一行文字不要在两边各画一半** ——
    静态的整行交给背景图，动态的整行交给 tkinter 控件。
    """
    key = (int(ref_size), bool(bold))
    if key in _pil_font_cache:
        return _pil_font_cache[key]

    import os

    from PIL import ImageFont

    px = M.s(ref_size)
    files = _FONT_FILES.get(FONT_FAMILY, ("msyh.ttc", "msyhbd.ttc"))
    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in (files[1 if bold else 0], files[0], "simhei.ttf", "segoeui.ttf"):
        path = os.path.join(fonts_dir, name)
        if os.path.isfile(path):
            try:
                font = ImageFont.truetype(path, px)
                _pil_font_cache[key] = font
                return font
            except Exception:
                continue
    font = ImageFont.load_default()
    _pil_font_cache[key] = font
    return font


def font_obj(ref_size, bold=False):
    """取一个缓存的 tkfont.Font（量文字宽度要用它）。

    ref_size 是**设计稿上的字号**，内部会按 k 换算成像素。
    """
    key = (int(ref_size), bool(bold))
    if key not in _font_cache:
        _font_cache[key] = tkfont.Font(family=FONT_FAMILY,
                                       size=-M.s(ref_size),
                                       weight="bold" if bold else "normal")
    return _font_cache[key]


# ---------------------------------------------------------------- 常用素材

def card_image(w, h, radius=None, fill=CARD_BG, border=BORDER, corners=None):
    """一张卡片底：圆角 + 填充 + 细边框。"""
    radius = M.radius if radius is None else radius
    if corners is None:
        corners = (True, True, True, True)
    img = ui_draw.rounded_rect((w, h), radius, fill=fill, corners=corners)
    if border:
        img.alpha_composite(
            ui_draw.rounded_rect((w, h), radius, outline=border, width=1, corners=corners))
    return img


def primary_button_image(w, h, state="normal"):
    """主按钮底：竖向渐变 + 圆角。"""
    if state == "disabled":
        return ui_draw.vgradient((w, h), DISABLED_BG, DISABLED_BG,
                                 radius=M.radius_small)
    return ui_draw.vgradient((w, h), PRIMARY_TOP, PRIMARY_BOT,
                             radius=M.radius_small)


def secondary_button_image(w, h, state="normal"):
    """次要按钮底：浅色横向渐变 + 细边框。"""
    if state == "disabled":
        base = ui_draw.hgradient((w, h), DISABLED_BG, DISABLED_BG,
                                 radius=M.radius_small)
    else:
        base = ui_draw.hgradient((w, h), "#FBFAF7", "#EEF2F7",
                                 radius=M.radius_small)
    base.alpha_composite(
        ui_draw.rounded_rect((w, h), M.radius_small,
                             outline="#DCE3EC" if state != "disabled" else "#DCD8D1",
                             width=1))
    return base


def choice_card_image(w, h, selected):
    """档位卡片底。选中时换底色与边框色。"""
    fill = SELECTED_BG if selected else CARD_BG
    border = SELECTED_BORDER if selected else BORDER
    img = ui_draw.rounded_rect((w, h), M.radius, fill=fill)
    img.alpha_composite(
        ui_draw.rounded_rect((w, h), M.radius, outline=border,
                             width=2 if selected else 1))
    return img


def divider(w, color=BORDER_SOFT):
    return ui_draw.hline((w, 1), color)
