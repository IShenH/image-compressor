# -*- coding: utf-8 -*-
"""图片压缩工具 —— 界面层。

这一层只负责「给用户看什么」和「用户点了什么」，
真正的压缩算法全部在 compressor 模块里，这里一行都不碰。

界面流程：
    选择图片 → 查看信息 → 设置压缩目标 → 压缩 → 查看结果 → 保存

**关于布局**：窗口是固定尺寸（由屏幕工作区反推，保持设计稿 1.777 的比例），
所以整页用一张**代码生成的背景图**打底（页面底色 + 卡片圆角 + 描边 + 分隔线 +
所有**静态**文字），控件再按算好的坐标 `place()` 上去。这样做的理由：

- 窗口尺寸固定 → 不需要动态布局，绝对定位反而最简单可靠
- 静态文字交给背景图，能精确对齐图标与基线，也少二十来个控件
- **但一行文字不会两边各画一半** —— tkinter 与 PIL 是两套文字渲染，
  同一行里混用会看出差异。静态的整行归背景图，动态的整行归控件。
"""

import os
import queue
import shutil
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageDraw

import compressor
import ui_draw
import ui_theme as T
import ui_widgets as W


# 版本号。改这里的同时要更新 CHANGELOG.md
__version__ = "1.1.0"


# 档位：压缩逻辑使用的键名 → 界面显示名称、说明、图标。
# 键名属于逻辑层（见 compressor.LEVELS），中文文案与图标属于界面层，所以映射放在这里。
LEVEL_CHOICES = [
    ("balanced", "均衡", "体积与画质兼顾", "contrast"),
    ("small", "小体积", "压缩最狠，画质损失明显", "compress"),
    ("high", "高画质", "尽量保住画质", "sparkle"),
]
DEFAULT_LEVEL = "balanced"

# 格式在界面上的写法
FORMAT_LABELS = {"JPEG": "JPEG", "PNG": "PNG", "WEBP": "WebP"}

# 压缩收益低于这个比例时，如实提示用户「省得不多」
LOW_GAIN_THRESHOLD = 0.10

# 后台线程跑起来之后，主线程每隔这么久去看一眼有没有结果。
# 关键：**不能用 join() 等它** —— join 会把主线程一起堵住，
# 界面照样卡死，那就等于白开了线程。
POLL_INTERVAL_MS = 60

# 拖放区接受的扩展名。拖进来的是不是图片，先按扩展名筛一道，
# 免得把 .exe 之类的东西丢给 Pillow 去试。
DROP_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

# 「原图信息」和「压缩结果」的行定义（图标 + 静态标签）
INFO_ROWS = [("file", "文件名："), ("tag", "格式："),
             ("ruler", "尺寸："), ("disk", "文件大小：")]
RESULT_LEFT_ROWS = [("image", "压缩前："), ("image", "压缩后："), ("bars", "减少：")]
RESULT_RIGHT_ROWS = [("tag", "输出格式："), ("ruler", "输出尺寸：")]


def enable_dpi_awareness():
    """让 Windows 不要对窗口做位图拉伸。

    不做这一步，在系统缩放 125% / 150% 的屏幕上，整个界面会被拉伸放大而发虚。
    必须在创建 Tk 窗口**之前**调用；非 Windows 系统会抛异常，忽略即可。
    """
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def apply_tk_scaling(root):
    """把 tkinter 的缩放系数对齐到屏幕真实 DPI。

    开启 DPI 感知之后，tkinter 仍然按 96 DPI 计算字号，在高缩放屏幕上字会偏小。
    换算关系：tk 的 scaling = 每一「点」占多少像素 = 屏幕 DPI ÷ 72。

    注意：本项目的字号**全部用负值（像素）**，不受这个换算影响 ——
    这里设置它是为了让 ttk 自带的部件（比如消息框）也跟着放大。
    """
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass


def format_size(num_bytes):
    """把字节数变成人看得懂的大小。"""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / 1024 / 1024:.2f} MB"


class App:
    """主窗口。

    界面上的按钮能不能点，完全由几个变量决定：
        self.info       —— 当前选中的原图信息，没有它就不能压缩
        self.result     —— 最近一次压缩的结果，没有它就不能保存
        self.pending_suggestion —— 有没有「换个格式/档位能更好」的建议
        self.force_format       —— 用户是否主动要求了某个输出格式
    这样就不用到处手动开关按钮，状态只有一个来源。
    """

    def __init__(self, root: tk.Tk):
        self.root = root

        self.info = None
        self.result = None
        self.pending_suggestion = None
        self.pending_level = None
        self.force_format = None

        # 压缩结果先落到临时目录，用户点「保存」时才复制到他选定的位置。
        # 这样「压缩 → 看结果 → 决定存到哪」这个顺序才成立，
        # 也避免在用户还没决定之前就往他的磁盘上写东西。
        self.tmpdir = tempfile.mkdtemp(prefix="imgcomp_")
        self.tmp_base = os.path.join(self.tmpdir, "compressed")

        self.level = tk.StringVar(value=DEFAULT_LEVEL)
        self.limit_size = tk.BooleanVar(value=False)
        self.max_dimension = tk.StringVar(value="1920")

        # 后台线程跑完只把结果放进这个队列，绝不直接碰界面 ——
        # tkinter 不是线程安全的，跨线程操作控件会随机崩溃。
        self.queue = queue.Queue()
        self.busy = False
        self.input_widgets = []   # 压缩期间需要一并禁用的控件

        self.thumb = None         # 预览缩略图的 PhotoImage，必须留引用
        self._show_pos = {}       # 控件 → 摆放参数（显隐时要用）
        # 结果区里三个可选元素的显隐状态。它们影响卡片高度，所以要参与布局计算。
        self._extra = {"note": False, "suggest": False, "restore": False}
        self._layout_ver = 0      # 布局版本号：背景图缓存键要带上它
        self._drop_ready = False  # 拖放只能挂一次窗口过程，不能重复挂
        self.header_h = {}

        self.win_w, self.max_h = T.init(root)
        self.win_h = self.max_h      # 初值；_relayout 会按内容实际需要改
        self.rects = {}

        self._build_ui()
        self._refresh_buttons()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 布局计算 ----------------

    def _extra_height(self):
        """结果卡片里三个可选元素占多高 —— 只算**当前显示的**。

        如果无条件预留，「提示 / 建议按钮 / 恢复按钮」不显示时卡片底部会空一大块。
        ⚠️ 这里的算法必须和 `_draw_result_labels` 里推进 y 的方式**完全一致**，
        否则算出来的卡片高度和实际摆放的位置对不上，下面的按钮会被压住。
        """
        m = T.M
        gap = m.s(6)
        total = 0
        if self._extra["note"]:
            total += m.s(26) + gap
        if self._extra["suggest"]:
            total += m.s(40) + gap
        if self._extra["restore"]:
            total += m.s(34) + gap
        return total

    def _compute_layout(self):
        """算出每个区块的位置，并返回 (坐标表, 内容总高)。

        窗口高度**由内容决定**，不再套设计稿的 1.777 比例 ——
        间距按最小给，没有再分配剩余空间，所以内容多高窗口就多高。
        只有一种例外：内容超过屏幕可用高度时按比例压缩，宁可挤也不能被裁。
        """
        m = T.M
        pad = m.page_pad
        bottom = m.bottom_pad
        inner = self.win_w - 2 * pad

        header = m.s(52)          # 区块标题行（图标 + 标题 + 分隔线）
        # ⚠️ 必须与 _draw_info_card / _draw_result_labels 里的行高一致。
        # 曾经这里写 s(40) 而绘制用 s(46)，卡片高度就比实际内容少算 16px，
        # 信息区最后一行被挤到卡片边缘。
        row = m.s(46)
        hint = m.s(22)            # 灰提示行

        heights = {
            "drop": m.s(130),
            "info": header + max(m.preview_h, len(INFO_ROWS) * row) + 2 * m.card_pad_y,
            "level": header + m.card_h + hint + 2 * m.card_pad_y,
            "size": header + m.btn_h_small + hint + 2 * m.card_pad_y,
            # 压缩按钮下方给进度条留一条缝（平时藏着，但位置要占住，
            # 否则一压缩下面的东西就整体往下跳）
            "compress": m.btn_h + m.s(20),
            # 高度随三个可选元素的显隐变化，不无条件预留（理由见 _extra_height）
            "result": header + 3 * row + self._extra_height() + 2 * m.card_pad_y,
            "save": m.btn_h,
        }
        order = ["drop", "info", "level", "size", "compress", "result", "save"]

        # 区块间距：默认压到最小，个别相邻对单独调。
        # 键是「上面那一块」的 key，值是与它下面那一块的间距。
        # 为什么这几对要不同 —— 间距在视觉上表达的是「这两块有多亲」：
        #   全站统一 3px 时，所有卡片等距排列，看不出内容分组。
        gap_after = {
            "info": m.s(9),      # 档位卡与原图信息分开一点：都是卡片，贴太近像一张被切开
            "compress": m.s(3),  # 结果紧跟按钮 —— 它们是一组「操作 → 结果」，要最紧
            "result": m.s(16),   # 保存按钮与结果拉开：它是对整页结果的收尾，不是结果的一行
        }
        gaps = [gap_after.get(k, m.gap) for k in order[:-1]]

        total = pad + bottom + sum(heights.values()) + sum(gaps)
        if total > self.max_h:
            room = self.max_h - pad - bottom - sum(gaps)
            scale = room / float(sum(heights.values()))
            heights = {k: max(m.s(30), int(v * scale)) for k, v in heights.items()}
            total = pad + bottom + sum(heights.values()) + sum(gaps)

        rects, y = {}, pad
        for key, g in zip(order, gaps + [0]):
            rects[key] = (pad, y, inner, heights[key])
            y += heights[key] + g
        return rects, total

    # ---------------- 摆放与显隐 ----------------

    def _reg(self, widget, **kw):
        """把控件摆上去，并**记下位置**。

        为什么要记：`place()` 不带参数**不能**恢复摆放 —— 位置记着，但控件仍处于
        「未放置」状态（这一点和 `grid()` 不一样，grid 无参可以恢复）。
        所以显隐必须自己带着参数重新 place 一次。
        """
        self._show_pos[widget] = kw
        widget.place(**kw)
        return widget

    def _show(self, widget):
        widget.place(**self._show_pos[widget])

    @staticmethod
    def _hide(widget):
        widget.place_forget()

    # ---------------- 界面搭建 ----------------

    def _build_ui(self):
        m = T.M
        self.root.title(f"图片压缩工具 {__version__}")
        self.root.configure(bg=T.PAGE_BG)
        self.root.resizable(False, False)
        self.root.geometry(T.geometry_for(self.root, self.win_w, self.win_h))
        try:
            self.root.iconphoto(True, T.photo(ui_draw.app_icon(64), key="appicon"))
        except Exception:
            pass

        # 整页背景（含所有静态文字）画在一张 Canvas 上，控件再叠上去
        self.bg = tk.Canvas(self.root, width=self.win_w, height=self.win_h,
                            highlightthickness=0, bd=0, bg=T.PAGE_BG)
        self.bg.place(x=0, y=0)

        self._create_widgets()
        self._relayout()
        self._enable_drop()

    def _draw_background(self):
        """把所有静态内容画进一张图：底色、卡片、圆角、描边、分隔线、静态文字。"""
        m = T.M
        bg = ui_draw.rounded_rect((self.win_w, self.win_h), 0, fill=T.PAGE_BG)

        # ---- 卡片底 ----
        for key in ("info", "level", "size", "result"):
            x, y, w, h = self.rects[key]
            bg.alpha_composite(
                ui_draw.rounded_rect((w, h), m.radius, fill=T.CARD_BG,
                                     outline=T.BORDER, width=1), (x, y))

        # ---- 拖放区（虚线框，表示可拖入）----
        # 文案在左、按钮在右，压成一行 —— 最省高度，也最接近设计稿的横向排布
        x, y, w, h = self.rects["drop"]
        bg.alpha_composite(
            ui_draw.dashed_round_rect((w, h), m.radius, color="#CFC8BE",
                                      width=1, dash=6, gap=5), (x, y))
        d = ImageDraw.Draw(bg)
        left = x + m.s(44)
        d.text((left, y + h // 2 - m.s(15)), "选择图片或拖拽到这里",
               font=T.pil_font(24, bold=True), fill=T.TEXT_STRONG, anchor="lm")
        d.text((left, y + h // 2 + m.s(16)), "支持常见的图片格式（JPG、PNG、WEBP 等）",
               font=T.pil_font(18), fill=T.TEXT_MUTED, anchor="lm")
        btn_w, btn_h = m.s(250), m.s(66)
        # 按钮位置记下来，_apply_positions 要用
        self.drop_btn = (x + w - m.s(44) - btn_w, y + (h - btn_h) // 2,
                         btn_w, btn_h)

        # ---- 各卡片内的静态内容 ----
        self.header_h["info"] = self._draw_header(bg, "info", "image", "原图信息")
        self.header_h["level"] = self._draw_header(bg, "level", "sliders", "压缩档位")
        self.header_h["size"] = self._draw_header(bg, "size", "expand", "输出尺寸")
        self.header_h["result"] = self._draw_header(bg, "result", "bars", "压缩结果")

        d = ImageDraw.Draw(bg)
        self._draw_info_card(bg, d)
        self._draw_level_hint(bg, d)
        self._draw_size_card(bg, d)
        self._draw_result_labels(bg, d)

        # 整页一次性贴上去。重排时卡片高度会变，所以缓存键要带上布局版本号，
        # 否则会拿到上一版尺寸的旧图。
        self.bg.delete("all")
        self.bg.create_image(0, 0, anchor="nw",
                             image=T.photo(bg, key=("pagebg", self.win_w,
                                                    self.win_h, self._layout_ver)))

    def _draw_header(self, bg, key, icon, title):
        """画区块标题（图标 + 标题 + 分隔线），返回正文起始 y。"""
        m = T.M
        x, y, w, h = self.rects[key]
        ix = x + m.card_pad_x
        top = y + m.card_pad_y
        ico = m.s(30)
        bg.alpha_composite(ui_draw.icon(icon, ico, T.TEXT, stroke=1.7), (ix, top))
        ImageDraw.Draw(bg).text((ix + ico + m.s(10), top + ico // 2), title,
                                font=T.pil_font(21, bold=True),
                                fill=T.TEXT_STRONG, anchor="lm")
        line_y = top + ico + m.s(12)
        bg.alpha_composite(
            ui_draw.hline((w - 2 * m.card_pad_x, 1), T.BORDER_SOFT), (ix, line_y))
        return line_y + m.s(12)

    def _draw_info_card(self, bg, d):
        m = T.M
        x, y, w, h = self.rects["info"]
        ix = x + m.card_pad_x
        top = self.header_h["info"]

        # 预览框由 preview 控件自己画，不画在背景图上 ——
        # 否则贴在上面的 Label 是方形的，会盖不住圆角、露出直角
        self.preview_box = (ix, top, m.preview_w, m.preview_h)

        # 右侧四行：图标 + 静态标签
        row = m.s(46)
        label_x = ix + m.preview_w + m.s(26)
        label_w = 0
        for i, (icon, text) in enumerate(INFO_ROWS):
            ry = top + i * row
            ico = m.s(26)
            bg.alpha_composite(ui_draw.icon(icon, ico, T.TEXT_MUTED, stroke=1.7),
                               (label_x, ry + (row - ico) // 2))
            d.text((label_x + ico + m.s(10), ry + row // 2), text,
                   font=T.pil_font(19), fill=T.TEXT_MUTED, anchor="lm")
            label_w = max(label_w,
                          T.pil_font(19).getlength(text))
        # 数值统一左对齐到同一列，行与行之间看起来才整齐
        self.info_value_x = int(label_x + m.s(26) + m.s(10) + label_w + m.s(12))
        self.info_row_y = top
        self.info_row_h = row

    def _draw_level_hint(self, bg, d):
        m = T.M
        x, y, w, h = self.rects["level"]
        ix = x + m.card_pad_x
        hy = self.header_h["level"] + m.card_h + m.s(12)
        ico = m.s(24)
        bg.alpha_composite(ui_draw.icon("info", ico, T.TEXT_MUTED, stroke=1.7),
                           (ix, hy))
        d.text((ix + ico + m.s(10), hy + ico // 2),
               "PNG 没有质量参数，只能靠减色变小（有损）",
               font=T.pil_font(18), fill=T.TEXT_MUTED, anchor="lm")

    def _draw_size_card(self, bg, d):
        m = T.M
        x, y, w, h = self.rects["size"]
        ix = x + m.card_pad_x
        top = self.header_h["size"]
        self.size_row_y = top
        self.size_check_w = m.s(150)
        self.size_field_x = ix + self.size_check_w + m.s(16)
        self.size_field_w = m.s(110)
        d.text((self.size_field_x + self.size_field_w + m.s(12),
                top + m.btn_h_small // 2), "像素",
               font=T.pil_font(19), fill=T.TEXT, anchor="lm")
        d.text((ix, top + m.btn_h_small + m.s(12)),
               "只缩小，不放大",
               font=T.pil_font(18), fill=T.TEXT_MUTED, anchor="lt")

    def _draw_result_labels(self, bg, d):
        """结果区两列网格。左边三行、右边两行，静态标签画进背景，数值由控件填。"""
        m = T.M
        x, y, w, h = self.rects["result"]
        ix = x + m.card_pad_x
        top = self.header_h["result"]
        row = m.s(46)
        col_w = (w - 2 * m.card_pad_x - m.s(24)) // 2
        self.result_row_y = top
        self.result_row_h = row

        self.result_value_x = {}
        for i, (icon, text) in enumerate(RESULT_LEFT_ROWS):
            ry = top + i * row
            ico = m.s(26)
            bg.alpha_composite(ui_draw.icon(icon, ico, T.TEXT_MUTED, stroke=1.7),
                               (ix, ry + (row - ico) // 2))
            d.text((ix + ico + m.s(10), ry + row // 2), text,
                   font=T.pil_font(19), fill=T.TEXT_MUTED, anchor="lm")
            key = ("before", "after", "saved")[i]
            self.result_value_x[key] = int(ix + m.s(26) + m.s(10)
                                           + T.pil_font(19).getlength(text) + m.s(12))

        right_x = ix + col_w + m.s(24)
        for i, (icon, text) in enumerate(RESULT_RIGHT_ROWS):
            ry = top + i * row
            ico = m.s(26)
            bg.alpha_composite(ui_draw.icon(icon, ico, T.TEXT_MUTED, stroke=1.7),
                               (right_x, ry + (row - ico) // 2))
            d.text((right_x + ico + m.s(10), ry + row // 2), text,
                   font=T.pil_font(19), fill=T.TEXT_MUTED, anchor="lm")
            key = ("format", "dimensions")[i]
            self.result_value_x[key] = int(right_x + m.s(26) + m.s(10)
                                           + T.pil_font(19).getlength(text) + m.s(12))

        # 三个可选元素的位置**按实际显示的依次往下排** ——
        # 不能无条件按「三个都显示」算，否则中间某个不显示时，
        # 下面的元素位置就会偏出卡片，把保存按钮压住。
        gap = m.s(6)
        y = top + 3 * row + gap
        self.result_note_y = y
        if self._extra["note"]:
            y += m.s(26) + gap
        self.result_suggest_y = y
        if self._extra["suggest"]:
            y += m.s(40) + gap
        self.result_restore_y = y
        self.card_inner_x = ix
        self.card_inner_w = w - 2 * m.card_pad_x

    def _relayout(self):
        """重新算布局、重画背景、重新摆放控件，必要时调整窗口高度。

        什么时候需要：结果区里「提示行 / 建议按钮 / 恢复原格式按钮」的显隐变了。
        它们不显示时整页必须收上去，窗口也随之变矮 —— 不留任何空白。
        """
        rects, total_h = self._compute_layout()
        self.rects = rects
        if total_h != self.win_h:
            self.win_h = total_h
            self.root.geometry(T.geometry_for(self.root, self.win_w, self.win_h))
            self.bg.configure(width=self.win_w, height=self.win_h)
        # 布局版本号变了，背景图缓存键跟着变，否则会拿到上一版的旧图
        self._layout_ver += 1
        self._draw_background()
        self._apply_positions()

    def _create_widgets(self):
        """建好全部控件。只建一次，位置交给 _apply_positions。"""
        self.info_vars = {k: tk.StringVar(value="—") for k in
                          ("file_name", "format", "dimensions", "size_bytes")}
        self.result_vars = {k: tk.StringVar(value="—") for k in
                            ("before", "after", "saved", "format", "dimensions")}

        self.choose_btn = W.FlatButton(self.bg, text="选择图片…", icon="folder",
                                       kind="primary", command=self.choose_file,
                                       bg=T.PAGE_BG)
        # 预览框用 Canvas 自己画（圆角底 + 图片或占位图标）。
        # 不用 Label 贴图：Label 的背景是方的，盖不住背景图上的圆角框，会露出直角。
        self.preview = tk.Canvas(self.bg, bg=T.CARD_BG, bd=0,
                                 highlightthickness=0)
        self.thumb_pil = None      # 原始缩略图（PIL）；重排时重新合成
        self.thumb = None          # 最终贴上去的 PhotoImage
        self._thumb_ver = 0
        self.info_labels = {}
        for key in self.info_vars:
            lbl = W.label(self.bg, size=19, color=T.TEXT)
            lbl.configure(textvariable=self.info_vars[key], anchor="w")
            self.info_labels[key] = lbl

        self.level_cards = []
        for key, name, desc, icon in LEVEL_CHOICES:
            card = W.ChoiceCard(self.bg, name, desc, icon, key, self.level,
                                command=self._on_level_change, bg=T.CARD_BG)
            self.level_cards.append(card)
            self.input_widgets.append(card)

        self.size_check = W.FlatCheck(self.bg, text="限制最长边",
                                      variable=self.limit_size,
                                      command=self._on_size_change,
                                      bg=T.CARD_BG)
        self.input_widgets.append(self.size_check)

        self.size_field = W.NumberField(self.bg, self.max_dimension, step=100,
                                        minimum=64, maximum=20000,
                                        command=self._on_size_change,
                                        bg=T.CARD_BG)
        # 手动输入不会触发 command，得单独监听按键，否则结果会停留在旧尺寸上
        self.size_field.entry.bind("<KeyRelease>", lambda _e: self._on_size_change())
        # 测试与旧代码都按 size_spin 这个名字找它
        self.size_spin = self.size_field
        self.input_widgets.append(self.size_field)

        self.compress_btn = W.FlatButton(self.bg, text="压缩", icon="sparkle",
                                         kind="primary", command=self.do_compress)
        self.progress = W.ThinProgress(self.bg, bg=T.PAGE_BG)

        self.result_labels = {}
        for key in self.result_vars:
            lbl = W.label(self.bg, size=19, color=T.TEXT)
            lbl.configure(textvariable=self.result_vars[key], anchor="w")
            self.result_labels[key] = lbl

        self.note_label = W.label(self.bg, size=18, color=T.WARN)
        self.suggest_btn = W.FlatButton(self.bg, text="", icon=None,
                                        kind="secondary",
                                        command=self.apply_suggestion, bg=T.CARD_BG)
        self.restore_btn = W.FlatButton(self.bg, text="恢复原格式", icon=None,
                                        kind="secondary",
                                        command=self.restore_format, bg=T.CARD_BG)
        self.save_btn = W.FlatButton(self.bg, text="保存压缩后的图片…",
                                     icon="download", kind="secondary",
                                     command=self.do_save)

    def _apply_positions(self):
        """按最新算出的坐标摆放全部控件，并同步与布局相关的尺寸。"""
        m = T.M

        # ---- 拖放区：文案在左、按钮在右 ----
        bx, by, bw, bh = self.drop_btn
        self.choose_btn.configure(width=bw, height=bh)
        self._reg(self.choose_btn, x=bx, y=by)

        # ---- 原图信息：预览框 + 四个数值 ----
        bx, by, pw, ph = self.preview_box
        self._reg(self.preview, x=bx, y=by, width=pw, height=ph)
        self._draw_preview()
        for i, key in enumerate(self.info_vars):
            self._reg(self.info_labels[key], x=self.info_value_x,
                      y=self.info_row_y + i * self.info_row_h,
                      width=m.s(400), height=self.info_row_h)

        # ---- 压缩档位：三张卡片 ----
        x, y, w, h = self.rects["level"]
        gap = m.s(18)
        card_w = (w - 2 * m.card_pad_x - 2 * gap) // 3
        for i, card in enumerate(self.level_cards):
            card.configure(width=card_w, height=m.card_h)
            self._reg(card, x=x + m.card_pad_x + i * (card_w + gap),
                      y=self.header_h["level"])

        # ---- 输出尺寸 ----
        x, y, w, h = self.rects["size"]
        self.size_check.configure(width=self.size_check_w, height=m.btn_h_small)
        self._reg(self.size_check, x=x + m.card_pad_x, y=self.size_row_y)
        self.size_field.configure(width=self.size_field_w, height=m.btn_h_small)
        self._reg(self.size_field, x=self.size_field_x, y=self.size_row_y)

        # ---- 压缩按钮 + 进度条 ----
        x, y, w, h = self.rects["compress"]
        self.compress_btn.configure(width=w, height=m.btn_h)
        self._reg(self.compress_btn, x=x, y=y)
        pw = int(w * 0.55)
        self.progress.configure(width=pw, height=m.s(12))
        self._reg(self.progress, x=x + (w - pw) // 2, y=y + m.btn_h + m.s(5))

        # ---- 压缩结果 ----
        row_index = {"before": 0, "after": 1, "saved": 2,
                     "format": 0, "dimensions": 1}
        for key, lbl in self.result_labels.items():
            self._reg(lbl, x=self.result_value_x[key],
                      y=self.result_row_y + row_index[key] * self.result_row_h,
                      width=m.s(300), height=self.result_row_h)

        self._reg(self.note_label, x=self.card_inner_x, y=self.result_note_y,
                  width=self.card_inner_w, height=m.s(26))
        self.suggest_btn.configure(width=self.card_inner_w, height=m.s(40))
        self._reg(self.suggest_btn, x=self.card_inner_x, y=self.result_suggest_y)
        self.restore_btn.configure(width=self.card_inner_w, height=m.s(34))
        self._reg(self.restore_btn, x=self.card_inner_x, y=self.result_restore_y)

        # ---- 保存按钮 ----
        x, y, w, h = self.rects["save"]
        self.save_btn.configure(width=w, height=m.btn_h)
        self._reg(self.save_btn, x=x, y=y)

        # ---- 按当前状态决定谁可见 ----
        self._visibility()

    def _visibility(self):
        """按「有没有图 / 是否在压缩 / 三个可选元素的状态」统一刷一遍显隐。"""
        pairs = [
            (self.progress, self.busy),
            (self.note_label, self._extra["note"]),
            (self.suggest_btn, self._extra["suggest"]),
            (self.restore_btn, self._extra["restore"]),
        ]
        for widget, visible in pairs:
            if visible:
                self._show(widget)
            else:
                self._hide(widget)

    def _sync_extra(self, note=None, suggest=None, restore=None):
        """更新三个可选元素的显隐；只要变了就整页重排一次。

        不这么做的话，卡片要么空一大块、要么一显示按钮下面的东西就整体往下跳。
        """
        want = dict(self._extra)
        if note is not None:
            want["note"] = bool(note)
        if suggest is not None:
            want["suggest"] = bool(suggest)
        if restore is not None:
            want["restore"] = bool(restore)
        if want != self._extra:
            self._extra = want
            self._relayout()

    def _enable_drop(self):
        """打开窗口的「接受文件拖放」标志。

        tkinter 本身不支持文件拖放，但 Windows 只要给窗口加上 WS_EX_ACCEPTFILES
        样式、再调一次 DragAcceptFiles，系统就会把拖进来的文件以
        WM_DROPFILES 消息的形式告诉窗口。tkinter 收不到这个消息，
        所以这里用 ctypes 自己挂一个窗口过程来处理它 —— 好处是**零新依赖**。
        """
        if self._drop_ready:
            return
        try:
            import ctypes
            from ctypes import wintypes

            GWL_EXSTYLE, WS_EX_ACCEPTFILES, WM_DROPFILES = -20, 0x00000100, 0x0233
            u32, shell = ctypes.windll.user32, ctypes.windll.shell32
            self.root.update_idletasks()
            hwnd = u32.GetAncestor(self.root.winfo_id(), 2)      # GA_ROOT

            ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_ACCEPTFILES)
            shell.DragAcceptFiles(hwnd, True)

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND,
                                         ctypes.c_uint, wintypes.WPARAM,
                                         wintypes.LPARAM)
            self._drag_files = []

            def handler(h, msg, wp, lp):
                if msg == WM_DROPFILES:
                    count = shell.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                    for i in range(count):
                        need = shell.DragQueryFileW(wp, i, None, 0) + 1
                        buf = ctypes.create_unicode_buffer(need)
                        shell.DragQueryFileW(wp, i, buf, need)
                        self._drag_files.append(buf.value)
                    shell.DragFinish(wp)
                    # 不能在窗口过程里直接动界面（会重入），交给事件循环下一轮
                    self.root.after(0, self._on_drop)
                    return 0
                return u32.CallWindowProcW(old_proc.value, h, msg, wp, lp)

            self._wndproc = WNDPROC(handler)
            old_proc = wintypes.WPARAM(u32.SetWindowLongPtrW(
                hwnd, -4, ctypes.cast(self._wndproc, ctypes.c_void_p).value))
            self._old_proc = old_proc
            self._drop_hwnd = hwnd
        except Exception:
            # 拖放是附加能力，拿不到也不该影响主流程
            self._wndproc = None
        self._drop_ready = True

    def _on_drop(self):
        """处理拖进来的文件。只收单张图片，其余情况明确告知。"""
        files, self._drag_files = self._drag_files, []
        if not files:
            return

        images = [f for f in files if os.path.splitext(f)[1].lower() in DROP_EXTENSIONS]
        folders = [f for f in files if os.path.isdir(f)]

        if len(files) > 1 and not folders:
            if not images:
                messagebox.showwarning(
                    "这些文件不是图片",
                    "拖进来的文件里没有能处理的图片。\n"
                    "支持 JPG、PNG、WebP、BMP、GIF、TIFF。")
                return
            if len(images) > 1:
                messagebox.showinfo(
                    "一次只能处理一张",
                    f"拖进来了 {len(images)} 张图片，这里先处理第一张：\n"
                    f"{os.path.basename(images[0])}")
            self.load_file(images[0])
            return

        if folders:
            messagebox.showwarning(
                "不支持拖入文件夹",
                "请把具体的图片文件拖进来，暂不支持整个文件夹。")
            return

        if not images:
            messagebox.showwarning(
                "这个文件不是图片",
                f"「{os.path.basename(files[0])}」不是能处理的图片格式。\n"
                "支持 JPG、PNG、WebP、BMP、GIF、TIFF。")
            return

        self.load_file(images[0])

    # ---------------- 预览 ----------------

    def _show_preview(self, path):
        """读出缩略图并重画预览框。

        必须先 thumbnail 再存 —— 4000×3000 的图直接塞进控件会吃掉几百兆内存。
        """
        m = T.M
        try:
            with Image.open(path) as img:
                img = img.convert("RGBA")
                # 存三倍大小，重排时重新裁切不会糊
                img.thumbnail((m.preview_w * 3, m.preview_h * 3), Image.LANCZOS)
                self.thumb_pil = img
        except Exception:
            self.thumb_pil = None
        self._thumb_ver += 1
        self._draw_preview()

    def _draw_preview(self):
        """画预览框：圆角底 + 图片（或占位图标）+ 细边框。

        图片按「铺满并居中裁切」放置再套上圆角遮罩，这样不会变形、
        四角也能和卡片底自然衔接。
        """
        m = T.M
        w, h = m.preview_w, m.preview_h
        r = m.radius                  # 框大了，小圆角会显得局促
        img = ui_draw.rounded_rect((w, h), r, fill="#EDEAE4")

        if self.thumb_pil is not None:
            photo = self.thumb_pil
            scale = max(w / float(photo.width), h / float(photo.height))
            photo = photo.resize((max(1, int(photo.width * scale)),
                                  max(1, int(photo.height * scale))),
                                 Image.LANCZOS)
            left, top = (photo.width - w) // 2, (photo.height - h) // 2
            photo = photo.crop((left, top, left + w, top + h))
            img.paste(photo, (0, 0), ui_draw.aa_mask((w, h), r))
        else:
            glyph = ui_draw.icon("image", m.s(96), "#C6C0B7", stroke=1.4)
            img.alpha_composite(glyph, ((w - glyph.width) // 2,
                                        (h - glyph.height) // 2))

        img.alpha_composite(ui_draw.rounded_rect((w, h), r,
                                                 outline=T.BORDER_SOFT, width=1))
        self.thumb = T.photo(img, key=("preview", w, h, self._thumb_ver))
        self.preview.delete("all")
        self.preview.create_image(0, 0, anchor="nw", image=self.thumb)

    # ---------------- 事件处理 ----------------

    def choose_file(self):
        """弹出文件选择框。真正的加载逻辑在 load_file 里，方便单独测试。"""
        path = filedialog.askopenfilename(
            title="选择要压缩的图片",
            filetypes=[("所有支持的图片", "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff"),
                       ("所有文件", "*.*")],
        )
        if path:
            self.load_file(path)

    def load_file(self, path):
        """读入一张图片并刷新界面。对话框之外的逻辑都集中在这里。"""
        try:
            self.info = compressor.read_info(path)
        except compressor.CompressError as exc:
            messagebox.showerror("无法读取", str(exc))
            return

        self.info_vars["file_name"].set(self.info.file_name)
        self.info_vars["format"].set(self.info.format)
        self.info_vars["dimensions"].set(f"{self.info.width} × {self.info.height}")
        self.info_vars["size_bytes"].set(format_size(self.info.size_bytes))
        self._show_preview(path)

        # 换了图片，上一次的结果和用户之前要求的格式都作废
        self._reset_format_choice()
        self._clear_result()
        self._refresh_buttons()

    def _on_level_change(self):
        """档位一变，上一次的压缩结果立刻作废。

        否则界面显示着「小体积」，用户点保存拿到的却是「均衡」的结果 —— 界面在骗人。
        """
        self._clear_result()
        self._refresh_buttons()

    def _on_size_change(self):
        """尺寸选项一变，同理作废上次结果。"""
        self._clear_result()
        self._refresh_buttons()

    def _reset_format_choice(self):
        self.force_format = None

    def apply_suggestion(self):
        """采纳建议：按建议换格式或换档位重新压一次。"""
        if self.pending_level:
            self.level.set(self.pending_level)
        if self.pending_suggestion:
            self.force_format = self.pending_suggestion.format
        self.do_compress()

    def restore_format(self):
        """放弃格式转换，回到保持源格式。"""
        self._reset_format_choice()
        self.do_compress()

    def do_compress(self):
        if self.info is None or self.busy:
            return

        # 只有勾了「限制最长边」才传这个参数；没勾时传 None，表示完全不动尺寸
        max_dim = self.max_dimension.get().strip() if self.limit_size.get() else None
        self._set_busy(True)

        # 压缩交给后台线程。编码大图要好几秒，放在主线程会把窗口卡死；
        # 而 tkinter 的界面刷新全靠主线程 —— 主线程一忙，连「正在压缩」都画不出来。
        threading.Thread(
            target=self._compress_worker,
            args=(self.info.path, self.tmp_base, self.level.get(),
                  self.force_format, max_dim),
            daemon=True,  # 窗口关掉时线程跟着结束，不会把进程吊住
        ).start()
        self.root.after(POLL_INTERVAL_MS, self._poll_result)

    def _compress_worker(self, src, dst, level, fmt, max_dim):
        """后台线程：只干活，只往队列里放结果，**绝不碰任何控件**。

        tkinter 不是线程安全的，在工作线程里操作控件会随机崩溃 ——
        而且这种崩溃往往在测试时看不出来，到了用户手里才偶发。
        """
        try:
            result = compressor.compress(src, dst, level, output_format=fmt,
                                         max_dimension=max_dim)
            self.queue.put(("ok", result))
        except compressor.CompressError as exc:
            self.queue.put(("error", exc))
        except Exception as exc:
            # 兜底：不能让线程悄无声息地死掉，否则界面会永远停在「正在压缩」
            self.queue.put(("crash", exc))

    def _poll_result(self):
        """主线程：隔一会儿看一次队列。

        用轮询而不是 join()，是因为 join 会把主线程一起堵住，界面照样卡死。
        """
        try:
            kind, payload = self.queue.get_nowait()
        except queue.Empty:
            self.root.after(POLL_INTERVAL_MS, self._poll_result)
            return

        self._set_busy(False)

        if kind == "ok":
            self.result = payload
            self.pending_suggestion = payload.suggestion
            self.pending_level = None
        else:
            self.result = None
            self.pending_suggestion = getattr(payload, "suggestion", None)
            self.pending_level = getattr(payload, "suggested_level", None)
            if isinstance(payload, compressor.NoGainError):
                # 压不小：不产出比原图更大的文件，改为如实告知，并把出路摆出来
                messagebox.showwarning("压不小", str(payload))
            elif kind == "crash":
                messagebox.showerror("出错了", f"压缩时发生意外错误：{payload}")
            else:
                messagebox.showerror("压缩失败", str(payload))

        self._show_result()
        self._refresh_buttons()

    def _set_busy(self, busy):
        """切换「正在压缩」状态：禁用输入、显示进度条、换忙碌光标。"""
        self.busy = busy
        if busy:
            self.root.config(cursor="watch")
            self.compress_btn.configure(text="正在压缩…")
            self.progress.start()
        else:
            self.root.config(cursor="")
            self.compress_btn.configure(text="压缩")
            self.progress.stop()
        self._visibility()
        self._refresh_buttons()

    def do_save(self):
        """弹出保存对话框。真正的写入逻辑在 save_to 里，方便单独测试。"""
        # 正常情况下「保存」按钮是禁用的，点不到这里；
        # 但这个前提不该只靠按钮状态来保证 —— 直接在代码里也挡一道。
        if self.result is None:
            return
        ext = compressor.EXTENSIONS[self.result.output_format]
        stem = os.path.splitext(self.info.file_name)[0]
        path = filedialog.asksaveasfilename(
            title="保存压缩后的图片",
            defaultextension=ext,
            # 默认名带上档位：同张图试不同档位时，不至于全叫 xxx_compressed
            initialfile=f"{stem}_{self.level.get()}{ext}",
            filetypes=[(f"{FORMAT_LABELS[self.result.output_format]} 图片", f"*{ext}")],
        )
        if path:
            self.save_to(path)

    def save_to(self, path):
        """把临时目录里的压缩结果复制到 path。"""
        if self.result is None:
            return False

        # 不得无提示覆盖原始文件。
        # 用 normcase 是为了在 Windows 上把大小写差异也算作同一个文件。
        if os.path.normcase(os.path.abspath(path)) == \
                os.path.normcase(os.path.abspath(self.info.path)):
            messagebox.showwarning(
                "不能覆盖原图",
                "你选择的保存位置就是原图本身。\n"
                "请换一个文件名或位置，原图不会被改动。")
            return False

        try:
            shutil.copy2(self.result.output_path, path)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return False

        messagebox.showinfo("保存成功", "压缩后的图片已保存到：\n" + path)
        return True

    # ---------------- 界面状态刷新 ----------------

    def _show_result(self):
        # 没有成功结果时，只保留「建议」按钮 —— 用户仍然可以从这里一键脱困
        if self.result is None:
            self._clear_result(keep_suggestion=True)
            return

        r = self.result
        self.result_vars["before"].set(format_size(r.source.size_bytes))
        self.result_vars["after"].set(format_size(r.output_size_bytes))

        fmt_text = FORMAT_LABELS.get(r.output_format, r.output_format)
        if r.quantized_colors:
            # 减色是**有损**的，必须让用户看见，不能假装它还和原来一样无损
            fmt_text += f"（{r.quantized_colors} 色，减色有损）"
        self.result_vars["format"].set(fmt_text)

        dims = f"{r.output_width} × {r.output_height}"
        self.result_vars["dimensions"].set(dims + "（已缩小）" if r.resized else dims)

        gain = 1 - r.ratio
        self.result_vars["saved"].set(f"{gain * 100:.1f}%")

        # 如实反馈：不把「省得不多」甚至「反而更大」包装成成功优化
        if r.saved_bytes <= 0:
            note = "注意：压缩后反而变大了。这张图本来就压得很紧，或者不适合这个格式。"
            color = T.BAD
        elif gain < LOW_GAIN_THRESHOLD:
            note = "注意：节省不到 10%。这张图很可能已经压缩过了。"
            color = T.WARN
        else:
            note = ""
            color = T.TEXT

        self.note_label.configure(text=note, fg=color)
        self._sync_extra(note=bool(note))
        self._update_suggestion_buttons()

    def _update_suggestion_buttons(self):
        show = False
        if self.pending_suggestion:
            name = FORMAT_LABELS.get(self.pending_suggestion.format,
                                     self.pending_suggestion.format)
            self.suggest_btn.configure(
                text=f"改用 {name}（还能再小 {self.pending_suggestion.saved_ratio * 100:.0f}%）")
            show = True
        elif self.pending_level:
            names = dict((k, n) for k, n, _, _ in LEVEL_CHOICES)
            self.suggest_btn.configure(
                text=f"改用「{names.get(self.pending_level, self.pending_level)}」档重压")
            show = True

        # 已经转过格式了，给一个回到原格式的出口，免得用户走进死胡同
        self._sync_extra(suggest=show, restore=bool(self.force_format))

    def _clear_result(self, keep_suggestion=False):
        self.result = None
        for var in self.result_vars.values():
            var.set("—")
        self.note_label.configure(text="", fg=T.WARN)
        if not keep_suggestion:
            self.pending_suggestion = None
            self.pending_level = None
        self._sync_extra(note=False)
        self._update_suggestion_buttons()

    def _refresh_buttons(self):
        self.compress_btn.state(
            ["!disabled"] if (self.info and not self.busy) else ["disabled"])
        self.save_btn.state(
            ["!disabled"] if (self.result and not self.busy) else ["disabled"])
        self.choose_btn.state(["disabled"] if self.busy else ["!disabled"])
        # 压缩期间不许改设置 —— 否则界面显示的选择，和正在后台跑的那个任务会对不上
        for widget in self.input_widgets:
            widget.state(["disabled"] if self.busy else ["!disabled"])
        for widget in (self.suggest_btn, self.restore_btn):
            widget.state(["disabled"] if self.busy else ["!disabled"])

    def _on_close(self):
        # 后台还在跑时先问一句 —— 直接退出等于把用户刚等的几十秒丢进垃圾桶
        if self.busy and not messagebox.askyesno(
                "仍在压缩", "压缩还没完成，现在退出会丢弃已经算出的结果。\n确定要退出吗？"):
            return
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.root.destroy()


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    apply_tk_scaling(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
