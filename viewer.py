# -*- coding: utf-8 -*-
"""查看器窗口 —— 浏览、缩放、平移、旋转、一键送入压缩。

独立于压缩主窗口：**不 import compressor，也不 import main**，
与外界的全部联系只有一个注入的回调 on_compress(path)。
复用 ui_draw / ui_theme / ui_widgets，所以外观与主窗口一致。

**渲染核心是「按视口裁剪」**：无论图片多大、放大多少倍，
都只把**当前能看到的那一块源区域**重采样成视口大小的位图。
放大 8 倍看一张 4000×3000 的图，整张渲染约要 3 GB 内存；
裁剪渲染始终只需要一张视口大小的图。几何计算集中在
模块级的 render_plan / clamp_offset / fit_zoom 三个纯函数里，
不碰 tkinter，可以直接单测。

**线程模型与压缩界面同一套三规则**：工作线程只解码、只往队列放结果，
绝不碰控件；主线程用 after 轮询队列；结果带 token/scan_id，
过期的直接丢弃（快速连点「下一张」时，慢的那次解码作废）。

**缩略图条不用子控件**：几百张图的文件夹若每张建一个 tk 控件，
就是几百个 Windows 原生窗口。缩略图直接画在一整条 Canvas 上，
缓存超限时按「离当前图远优先」淘汰。
"""

import math
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageOps, ImageTk

import ui_draw
import ui_theme as T
import ui_widgets as W

# 查看器能打开的扩展名。与主窗口拖放区（main.DROP_EXTENSIONS）是同一组格式；
# 刻意不 import main 来取 —— 本模块保持独立，两处各写一行是值得的代价。
VIEWER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp",
                     ".bmp", ".gif", ".tif", ".tiff"}

WHEEL_STEP = 1.25          # 滚轮每格缩放倍率
ZOOM_MIN, ZOOM_MAX = 0.02, 16.0
THUMB_LIMIT = 200          # 缩略图缓存上限（张），超出按离当前图远近淘汰
THUMB_KEEP_NEAR = 20       # 当前图前后这么多张的缩略图不参与淘汰
POLL_MS = 50               # 主线程轮询解码结果的间隔


def format_size(num_bytes):
    """把字节数变成人看得懂的大小。与 main.format_size 同一份逻辑 ——
    查看器不 import main，所以各自持有一份（3 行，不值得为它建公共模块）。"""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / 1024 / 1024:.2f} MB"


# ---------------------------------------------------------------------------
# 纯几何函数：渲染状态机的全部数学，不碰 tkinter，可直接单测。
# 约定：offset 是「显示图左上角」在视口坐标系里的位置，可为负（图被拖出一部分）。
# ---------------------------------------------------------------------------

def fit_zoom(size, viewport):
    """「适应窗口」的倍率：长边塞进视口。小图也放大（看图软件惯例）。"""
    sw, sh = size
    vw, vh = viewport
    if sw <= 0 or sh <= 0 or vw <= 0 or vh <= 0:
        return 1.0
    return min(vw / sw, vh / sh)


def clamp_zoom(z):
    return max(ZOOM_MIN, min(ZOOM_MAX, z))


def clamp_offset(offset, disp_size, viewport):
    """把平移位置夹回合理范围。

    图片某一维 ≤ 视口时该维**居中**（不允许把小图拖到角落）；
    大于视口时夹住两端 —— 图片边缘不许离开视口，用户不会把图「拖丢」。
    """
    ox, oy = offset
    dw, dh = disp_size
    vw, vh = viewport
    if dw <= vw:
        ox = (vw - dw) / 2
    else:
        ox = max(vw - dw, min(0, ox))
    if dh <= vh:
        oy = (vh - dh) / 2
    else:
        oy = max(vh - dh, min(0, oy))
    return (int(round(ox)), int(round(oy)))


def render_plan(viewport, offset, zoom, src_size):
    """算出这一次渲染的全部几何量。

    返回 (源区域 box, 摆放位置 px, 目标位图尺寸)，或 None（什么都看不到）。
    源区域起点向下取整、终点向上取整 —— 宁可多采一个像素，不能漏出黑缝。
    """
    vw, vh = viewport
    ox, oy = offset
    sw, sh = src_size
    if sw <= 0 or sh <= 0:
        return None
    # 显示坐标下的可见范围
    vx0 = max(0.0, -ox)
    vy0 = max(0.0, -oy)
    vx1 = min(sw * zoom, vw - ox)
    vy1 = min(sh * zoom, vh - oy)
    if vx1 - vx0 < 1 or vy1 - vy0 < 1:
        return None
    # 对应的源区域（整数化）
    sx0 = max(0, int(math.floor(vx0 / zoom)))
    sy0 = max(0, int(math.floor(vy0 / zoom)))
    sx1 = min(sw, int(math.ceil(vx1 / zoom)))
    sy1 = min(sh, int(math.ceil(vy1 / zoom)))
    if sx1 <= sx0 or sy1 <= sy0:
        return None
    px = ox + int(round(sx0 * zoom))
    py = oy + int(round(sy0 * zoom))
    tw = max(1, int(round((sx1 - sx0) * zoom)))
    th = max(1, int(round((sy1 - sy0) * zoom)))
    return (sx0, sy0, sx1, sy1), (px, py), (tw, th)


def _normalize_mode(img):
    """统一成 RGB / RGBA，后面所有渲染只面对这两种模式。"""
    if img.mode == "RGBA":
        return img
    if img.mode == "P" and "transparency" in img.info:
        return img.convert("RGBA")
    if img.mode in ("LA", "PA"):
        return img.convert("RGBA")
    return img.convert("RGB")


def scan_folder(folder):
    """列出文件夹里能查看的图片，按文件名排序（不区分大小写）。

    只看这一层，不递归子目录 —— 查看器第一版保持简单。
    """
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    files = [os.path.join(folder, n) for n in names
             if os.path.isfile(os.path.join(folder, n))
             and os.path.splitext(n)[1].lower() in VIEWER_EXTENSIONS]
    files.sort(key=lambda p: os.path.basename(p).lower())
    return files


_ROTATE = {
    0: None,
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


def _recycle(path):
    """把文件送进回收站，而不是直接抹掉 —— 可恢复才配叫「删除」。

    用 shell 的 SHFileOperationW：FOF_ALLOWUNDO 进回收站、
    NOCONFIRMATION 跳过系统确认（应用已自己确认过）、
    SILENT / NOERRORUI 让进度和报错由我们自己管。
    pFrom 要求**双 null** 结尾，所以用 create_unicode_buffer 多补一个。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND),
                        ("wFunc", wintypes.UINT),
                        ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR),
                        ("fFlags", ctypes.c_short),
                        ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", wintypes.LPCWSTR)]

        op = SHFILEOPSTRUCTW()
        op.wFunc = 3                                    # FO_DELETE
        buf = ctypes.create_unicode_buffer(path + "\x00")
        op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
        op.fFlags = 0x40 | 0x10 | 0x4 | 0x400
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return res == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


class ViewerWindow:
    """一个独立窗口。复用方式：open_path() 换一批图片，窗口不重建。"""

    def __init__(self, master, on_compress=None):
        self.master = master
        self.on_compress = on_compress

        # ---- 浏览状态 ----
        self.images = []          # 当前文件列表
        self.index = -1
        self.scan_id = 0          # 第几次扫描；过期线程结果靠它丢弃
        self._load_token = 0      # 第几次请求解码；快速翻页时旧的作废
        self._fail_streak = 0     # 连续打不开的张数（自动跳图的总闸）
        self.pil = None           # 当前图的解码结果（未旋转）
        self.display = None       # 旋转后的显示图
        self.fmt = ""
        self._current_bytes = 0
        self._placeholder_text = "打开图片或文件夹开始浏览"

        # ---- 视图状态 ----
        self.zoom = 1.0
        self.fit_mode = True      # True=适应窗口（跟随窗口尺寸重算）
        self.rotation = 0
        self.offset = (0, 0)
        self._fullscreen = False

        # ---- 线程通信 ----
        self._closed = False
        self.queue = queue.Queue()          # 主图解码结果
        self._thumb_queue = queue.Queue()   # 缩略图结果
        self._thumb_photos = {}             # index -> PhotoImage（必须持引用）
        self._thumb_items = {}              # index -> canvas 条目 id
        self._thumb_cell = (0, 0, 0, 0)
        self._thumb_pitch = 0
        self._view_photo = None             # 当前视口位图，同理必须持引用
        self._resize_job = None
        self._drag_start = None

        # ---- 动图状态 ----
        self._anim = None          # {"frames": [...], "durations": [...]}
        self._anim_i = 0
        self._anim_job = None
        self._anim_paused = False

        # ---- 邻图预读：index -> 解码结果，翻页直接命中 ----
        self._prefetch = {}
        self._prefetch_hits = 0    # 仅供测试观察命中率

        self._build_window()
        self._poll()

    # ---------------- 窗口搭建 ----------------

    def _build_window(self):
        m = T.M
        self.top = tk.Toplevel(self.master)
        self.top.title("图片查看")
        self.top.configure(bg=T.PAGE_BG)
        try:
            self.top.iconphoto(True, T.photo(ui_draw.app_icon(64), key="appicon"))
        except Exception:
            pass

        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        w = min(m.s(760), sw - m.s(24))
        h = min(m.s(460), sh - m.s(80))
        self.top.geometry("%dx%d+%d+%d" % (w, h,
                                           max(0, (sw - w) // 2), max(0, (sh - h) // 2)))
        self.top.minsize(min(m.s(756), w), min(m.s(320), h))

        # ---- 工具栏 ----
        bar = tk.Frame(self.top, bg=T.CARD_BG)
        bar.pack(side="top", fill="x")
        pad = dict(padx=m.s(3), pady=m.s(6))

        def bar_btn(text, cmd, width, icon=None):
            b = W.FlatButton(bar, text=text, icon=icon, kind="secondary",
                             command=cmd, width=m.s(width), height=m.btn_h_small,
                             bg=T.CARD_BG)
            b.pack(side="left", **pad)
            return b

        def sep():
            tk.Frame(bar, width=1, height=m.btn_h_small, bg=T.BORDER).pack(
                side="left", padx=m.s(5), pady=m.s(6))

        # 按钮宽度要按字数给足：FlatButton 装不下会裁字（实测 s(84) 裁掉「打开…」）。
        # 工具栏总宽约 674 逻辑 px，决定窗口最小宽度，见下面的 minsize。
        bar_btn("打开图片", self.open_file_dialog, 100)
        bar_btn("文件夹…", self.open_folder_dialog, 104)
        sep()
        self.prev_btn = bar_btn("", self.prev_image, 44, icon="arrow_left")
        self.next_btn = bar_btn("", self.next_image, 44, icon="arrow_right")
        sep()
        bar_btn("适应窗口", self.fit_view, 96)
        bar_btn("1:1", lambda: self._set_zoom(1.0), 52)
        bar_btn("旋转", self.rotate, 60)
        sep()
        self.delete_btn = bar_btn("", self.delete_current, 44, icon="trash")
        sep()
        self.compress_btn = W.FlatButton(bar, text="压缩这张", kind="primary",
                                         command=self._send_to_compress,
                                         width=m.s(108), height=m.btn_h_small,
                                         bg=T.CARD_BG)
        self.compress_btn.pack(side="left", **pad)

        tk.Frame(self.top, height=1, bg=T.BORDER).pack(side="top", fill="x")

        # ---- 看图画布（深色）----
        self.canvas = tk.Canvas(self.top, bg=T.VIEWER_CANVAS_BG,
                                highlightthickness=0, bd=0)
        # pack 顺序有讲究：先把上下两头的都贴完，画布最后 pack，
        # 中间剩下的空间才全归它。
        self._strip_h = m.s(92)
        self.strip = tk.Canvas(self.top, height=self._strip_h, bg=T.CARD_BG,
                               highlightthickness=0, bd=0)
        self.scrollbar = tk.Scrollbar(self.top, orient="horizontal",
                                      command=self.strip.xview)
        status = tk.Frame(self.top, bg=T.CARD_BG)
        self.status_left = tk.StringVar(value="未打开图片")
        self.status_right = tk.StringVar(value="")
        tk.Label(status, textvariable=self.status_left, bg=T.CARD_BG, fg=T.TEXT,
                 font=T.font_obj(18), anchor="w").pack(
            side="left", padx=m.s(12), pady=m.s(5))
        tk.Label(status, textvariable=self.status_right, bg=T.CARD_BG,
                 fg=T.TEXT_MUTED, font=T.font_obj(18), anchor="e").pack(
            side="right", padx=m.s(12), pady=m.s(5))

        status.pack(side="bottom", fill="x")
        self.scrollbar.pack(side="bottom", fill="x")
        self.strip.pack(side="bottom", fill="x")
        self.canvas.pack(side="top", fill="both", expand=True)
        self.strip.configure(xscrollcommand=self.scrollbar.set)

        # ---- 事件 ----
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Double-Button-1>", lambda _e: self.fit_view())
        self.strip.bind("<Button-1>", self._strip_click)
        self.strip.bind("<MouseWheel>", self._strip_wheel)
        self.top.bind("<Left>", lambda _e: self.prev_image())
        self.top.bind("<Right>", lambda _e: self.next_image())
        self.top.bind("<r>", lambda _e: self.rotate())
        self.top.bind("<R>", lambda _e: self.rotate())
        self.top.bind("<f>", lambda _e: self.fit_view())
        self.top.bind("<F>", lambda _e: self.fit_view())
        self.top.bind("<1>", lambda _e: self._set_zoom(1.0))
        self.top.bind("<space>", lambda _e: self.toggle_anim_pause())
        self.top.bind("<Delete>", lambda _e: self.delete_current())
        self.top.bind("<F11>", lambda _e: self.toggle_fullscreen())
        self.top.bind("<Escape>", self._on_escape)
        self.top.protocol("WM_DELETE_WINDOW", self.close)

        self._render()
        self._update_nav_buttons()

    # ---------------- 打开与扫描 ----------------

    def open_file_dialog(self):
        path = filedialog.askopenfilename(
            parent=self.top, title="选择要查看的图片",
            filetypes=[("所有支持的图片",
                        "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff"),
                       ("所有文件", "*.*")])
        if path:
            self.open_path(path)

    def open_folder_dialog(self):
        folder = filedialog.askdirectory(parent=self.top, title="选择要浏览的文件夹")
        if folder:
            self.open_path(folder)

    def open_path(self, path):
        """打开一个文件或文件夹。

        打开文件时顺带扫描它所在的文件夹 —— 这样「下一张」天然可用，
        而且当前图在列表里的位置保持不变。
        """
        path = os.path.abspath(path)
        if os.path.isdir(path):
            folder, target = path, None
        else:
            folder, target = os.path.dirname(path), path
        files = scan_folder(folder)
        # 目标文件扩展名不在白名单（用户从「所有文件」里挑的）也把它加进来
        if target and target not in files:
            files.append(target)
            files.sort(key=lambda p: os.path.basename(p).lower())
        if not files:
            self.images = []
            self.index = -1
            self.pil = None
            self.display = None
            self._rebuild_strip()
            self._placeholder_text = "这个文件夹里没有可查看的图片"
            self._render()
            self._update_status()
            self._update_nav_buttons()
            self.top.title("图片查看")
            return
        self._placeholder_text = "打开图片或文件夹开始浏览"
        self.images = files
        self._rebuild_strip()
        self.index = -1                      # 强制 _goto 真正加载
        self._goto(files.index(target) if target else 0)

    # ---------------- 翻页 ----------------

    def _goto(self, index, force=False):
        if not self.images:
            return
        index = max(0, min(len(self.images) - 1, index))
        if index == self.index and not force and self.pil is not None:
            self._place_selection()
            return
        self.index = index
        self._load_token += 1
        token = self._load_token
        self.pil = None
        self.display = None
        self.rotation = 0
        self._stop_anim()
        self._fail_streak = 0
        path = self.images[index]
        try:
            self._current_bytes = os.path.getsize(path)
        except OSError:
            self._current_bytes = 0
        self._placeholder_text = "正在加载…"
        self.top.title("图片查看 — %s [%d/%d]"
                       % (os.path.basename(path), index + 1, len(self.images)))
        self.status_left.set("正在加载…")
        self._render()
        self._place_selection()
        self._scroll_thumb_to(index)
        self._update_nav_buttons()
        cached = self._prefetch.pop(index, None)
        if cached is not None:
            # 命中预读：把结果塞回同一条队列，成功处理逻辑只有一份
            self._prefetch_hits += 1
            self.queue.put(("img", token, index) + cached)
        else:
            threading.Thread(target=self._decode_worker,
                             args=(token, index, path), daemon=True).start()

    def _request_prefetch(self, index):
        """把左右邻居各预读一张 —— 翻页时大概率直接命中缓存。"""
        for j in (index - 1, index + 1):
            if 0 <= j < len(self.images) and j not in self._prefetch:
                threading.Thread(target=self._prefetch_worker,
                                 args=(j, self.images[j]), daemon=True).start()

    def next_image(self, *_):
        if self.images:
            self._goto(self.index + 1)

    def prev_image(self, *_):
        if self.images:
            self._goto(self.index - 1)

    # ---------------- 解码线程 ----------------

    def _decode_image(self, path):
        """解码一张图 → (img, fmt, 动画帧|None, 帧时长|None)。

        动图（GIF）在这里把**全部帧**解出来 —— 带硬上限（帧数 × 总像素），
        超限就放弃动画只取第一帧：宁可不动，也不能吃内存吃到死。
        主图解码与邻图预读共用这一条路径。
        """
        with Image.open(path) as im:
            im.load()
            fmt = im.format or ""
            frames = durations = None
            if getattr(im, "is_animated", False):
                n = im.n_frames
                if n <= 200 and im.size[0] * im.size[1] * n <= 60_000_000:
                    frames, durations = [], []
                    for i in range(n):
                        im.seek(i)
                        frames.append(_normalize_mode(im.copy()))
                        durations.append(max(20, int(im.info.get("duration") or 100)))
                    im.seek(0)      # 下面还要取第一帧当静态底图
            img = ImageOps.exif_transpose(im)
        img = _normalize_mode(img)
        return img, fmt, frames, durations

    def _decode_worker(self, token, index, path):
        """后台解码。只往队列放结果，绝不碰控件。"""
        try:
            img, fmt, frames, durations = self._decode_image(path)
            self.queue.put(("img", token, index, img, fmt, frames, durations))
        except Exception as exc:
            self.queue.put(("img_err", token, index, str(exc)))

    def _prefetch_worker(self, index, path):
        """预读邻居。尽力而为：失败就当没预读过，不打扰任何人。"""
        try:
            img, fmt, frames, durations = self._decode_image(path)
            self.queue.put(("prefetch", index, img, fmt, frames, durations))
        except Exception:
            pass

    def _thumb_worker(self, scan_id, paths):
        """后台逐张生成缩略图。draft() 让 JPEG 按缩略图尺寸就近解码，省一大截时间。"""
        box = (T.M.s(96), T.M.s(72))
        for i, p in enumerate(paths):
            if self._closed or scan_id != self.scan_id:
                return
            try:
                with Image.open(p) as im:
                    im.draft("RGB", box)
                    im.load()
                    img = ImageOps.exif_transpose(im)
                    img = _normalize_mode(img)
                    img.thumbnail(box, Image.Resampling.LANCZOS)
            except Exception:
                img = None
            self._thumb_queue.put(("thumb", scan_id, i, img))

    def _poll(self):
        """主线程轮询：把两个队列里攒的结果全部处理掉。"""
        if self._closed:
            return
        try:
            try:
                while True:
                    self._handle(self.queue.get_nowait())
            except queue.Empty:
                pass
            try:
                while True:
                    self._handle_thumb(self._thumb_queue.get_nowait())
            except queue.Empty:
                pass
            self.top.after(POLL_MS, self._poll)
        except tk.TclError:
            # 窗口已经销毁（比如测试拆环境、或主窗口先退出）：安静收摊
            self._closed = True

    def _handle(self, item):
        kind = item[0]
        if kind == "img":
            _, token, index, img, fmt, frames, durations = item
            if token != self._load_token or index != self.index:
                return                       # 过期结果：已经翻页了，丢弃
            self.pil = img
            self.fmt = fmt
            self._fail_streak = 0
            self.rotation = 0
            self._stop_anim()
            self.display = img
            if frames:
                self._anim = {"frames": frames, "durations": durations}
                self._anim_i = 0
                self._anim_paused = False
            self._placeholder_text = "打开图片或文件夹开始浏览"
            self._apply_fit()
            self._render()
            self._update_status()
            if self._anim:
                # 第一帧停够它自己的时长再开始推进
                self._anim_job = self.top.after(
                    self._anim["durations"][0], self._anim_step)
            self._request_prefetch(index)
        elif kind == "prefetch":
            _, index, img, fmt, frames, durations = item
            if 0 <= index < len(self.images):
                self._prefetch[index] = (img, fmt, frames, durations)
                # 只留当前图旁边的预读，远处的丢弃，内存不失控
                for k in [k for k in self._prefetch if abs(k - self.index) > 1]:
                    del self._prefetch[k]
        elif kind == "img_err":
            _, token, index, message = item
            if token != self._load_token or index != self.index:
                return
            self._fail_streak += 1
            self.pil = None
            self.display = None
            self._placeholder_text = "打不开这张图（%s）" % message.splitlines()[0][:60]
            self._render()
            self._update_status()
            # 自动跳到下一张，不让一张坏图中断浏览；连坏一整圈就停下
            if self._fail_streak < len(self.images) and index < len(self.images) - 1:
                self.top.after(120, self.next_image)

    def _handle_thumb(self, item):
        _, scan_id, i, img = item
        if scan_id != self.scan_id or not (0 <= i < len(self.images)):
            return
        c = self.strip
        cw, ch, y0, pad = self._thumb_cell
        x = pad + i * self._thumb_pitch
        old = self._thumb_items.get(i)
        if old:
            c.delete(old)
        if img is None:
            side = min(cw, ch) // 2
            glyph = T.photo(ui_draw.icon("image", side, T.VIEWER_CANVAS_MUTED,
                                         stroke=1.4), key=("vthumbph", side))
            self._thumb_items[i] = c.create_image(x + cw // 2, y0 + ch // 2,
                                                  image=glyph)
            return
        photo = ImageTk.PhotoImage(img)      # PhotoImage 必须在主线程创建
        self._thumb_items[i] = c.create_image(x + cw // 2, y0 + ch // 2,
                                              image=photo)
        self._thumb_photos[i] = photo
        if len(self._thumb_photos) > THUMB_LIMIT:
            for k in list(self._thumb_photos):
                if abs(k - self.index) <= THUMB_KEEP_NEAR:
                    continue
                stale = self._thumb_items.pop(k, None)
                if stale:
                    c.delete(stale)
                self._thumb_photos.pop(k, None)
                if len(self._thumb_photos) <= THUMB_LIMIT:
                    break

    # ---------------- 缩略图条 ----------------

    def _rebuild_strip(self):
        c = self.strip
        c.delete("all")
        self._thumb_photos.clear()
        self._thumb_items.clear()
        self._prefetch.clear()      # 图片列表变了，预读全部作废
        n = len(self.images)
        cw, ch = T.M.s(96), T.M.s(76)
        y0 = (self._strip_h - ch) // 2
        pad = T.M.s(6)
        self._thumb_cell = (cw, ch, y0, pad)
        self._thumb_pitch = cw + T.M.s(6)
        for i in range(n):
            x = pad + i * self._thumb_pitch
            c.create_rectangle(x, y0, x + cw, y0 + ch,
                               fill=T.CARD_BG_SOFT, outline=T.BORDER)
        c.configure(scrollregion=(0, 0, pad * 2 + n * self._thumb_pitch,
                                  self._strip_h))
        self._sel_item = c.create_rectangle(-100, -100, -90, -90,
                                            outline=T.PRIMARY, width=2)
        self.scan_id += 1
        threading.Thread(target=self._thumb_worker,
                         args=(self.scan_id, list(self.images)),
                         daemon=True).start()

    def _place_selection(self):
        if not self.images or not (0 <= self.index < len(self.images)):
            self.strip.coords(self._sel_item, -100, -100, -90, -90)
            return
        cw, ch, y0, pad = self._thumb_cell
        x = pad + self.index * self._thumb_pitch
        self.strip.coords(self._sel_item, x + 1, y0 + 1, x + cw - 1, y0 + ch - 1)

    def _scroll_thumb_to(self, i):
        """让当前图的缩略图滚进可视范围（尽量居中）。"""
        if not self.images:
            return
        pitch = self._thumb_pitch
        total = pitch * len(self.images)
        visible = max(1, self.strip.winfo_width())
        x = i * pitch + pitch / 2 - visible / 2
        self.strip.xview_moveto(max(0.0, min(1.0, x / max(1, total))))

    def _strip_click(self, e):
        if not self.images:
            return
        pad = self._thumb_cell[3]
        i = int((self.strip.canvasx(e.x) - pad) // self._thumb_pitch)
        if 0 <= i < len(self.images) and i != self.index:
            self._goto(i)

    def _strip_wheel(self, e):
        self.strip.xview_scroll(-2 if e.delta > 0 else 2, "units")

    # ---------------- 视图：fit / 缩放 / 平移 / 旋转 ----------------

    def _viewport(self):
        return (max(1, self.canvas.winfo_width()),
                max(1, self.canvas.winfo_height()))

    def _disp_size(self):
        if self.display is None:
            return (0, 0)
        return (self.display.size[0] * self.zoom,
                self.display.size[1] * self.zoom)

    def _apply_fit(self):
        if self.display is None:
            return
        vw, vh = self._viewport()
        self.zoom = clamp_zoom(fit_zoom(self.display.size, (vw, vh)))
        self.fit_mode = True
        self.offset = clamp_offset((0, 0), self._disp_size(), (vw, vh))

    def fit_view(self, *_):
        if self.display is None:
            return
        self._apply_fit()
        self._render()
        self._update_status()

    def _set_zoom(self, target, anchor=None):
        """设置倍率。anchor 是视口上的一个点，缩放前后它下面的图像点不动 ——
        这是「以鼠标为中心缩放」的本质：解一个一元一次方程而已。"""
        if self.display is None:
            return
        new = clamp_zoom(target)
        vw, vh = self._viewport()
        ax, ay = anchor if anchor is not None else (vw // 2, vh // 2)
        ox, oy = self.offset
        ratio = new / self.zoom
        self.zoom = new
        self.fit_mode = False
        self.offset = clamp_offset(
            (ax - (ax - ox) * ratio, ay - (ay - oy) * ratio),
            self._disp_size(), (vw, vh))
        self._render()
        self._update_status()

    def _on_wheel(self, e):
        step = WHEEL_STEP if e.delta > 0 else 1 / WHEEL_STEP
        self._set_zoom(self.zoom * step, anchor=(e.x, e.y))

    def _on_press(self, e):
        self._drag_start = (e.x, e.y, self.offset)

    def _on_drag(self, e):
        if self.display is None or self._drag_start is None:
            return
        sx, sy, (ox, oy) = self._drag_start
        self.fit_mode = False
        self.offset = clamp_offset((ox + e.x - sx, oy + e.y - sy),
                                   self._disp_size(), self._viewport())
        self._render()
        self._update_status()

    def rotate(self, *_):
        """旋转显示。**只改视图，不写回文件** —— 界面上如实标注。"""
        if self.display is None:
            return
        self.rotation = (self.rotation + 90) % 360
        self.display = self.pil.transpose(_ROTATE[self.rotation])
        # 旋转后形状变了，平移位置重新居中最不容易出「图不见了」的怪状态
        self.offset = clamp_offset((0, 0), self._disp_size(), self._viewport())
        self._render()
        self._update_status()

    def toggle_fullscreen(self, *_):
        """全屏开关（F11）。窗口尺寸变化由 <Configure> 事件自动跟上。"""
        self._fullscreen = not self._fullscreen
        try:
            self.top.attributes("-fullscreen", self._fullscreen)
        except tk.TclError:
            self._fullscreen = False

    def _on_escape(self, *_e):
        """Esc：全屏时先退出全屏，否则关窗 —— 与看图软件的直觉一致。"""
        if self._fullscreen:
            self.toggle_fullscreen()
        else:
            self.close()

    # ---------------- 动图 ----------------

    def _anim_step(self):
        """推进到下一帧。在主线程由 after 调度 —— 帧切换本质上也是一次重渲染。"""
        self._anim_job = None
        if self._closed or not self._anim or self._anim_paused:
            return
        frames = self._anim["frames"]
        self._anim_i = (self._anim_i + 1) % len(frames)
        self.display = frames[self._anim_i]
        if self.rotation:
            self.display = self.display.transpose(_ROTATE[self.rotation])
        self._render()
        self._update_status()
        self._anim_job = self.top.after(
            self._anim["durations"][self._anim_i], self._anim_step)

    def toggle_anim_pause(self, *_):
        """空格：播放 / 暂停。没有动图时按了也没反应。"""
        if not self._anim:
            return
        self._anim_paused = not self._anim_paused
        if self._anim_job is not None:
            try:
                self.top.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
        if not self._anim_paused:
            self._anim_job = self.top.after(50, self._anim_step)
        self._update_status()

    def _stop_anim(self):
        if self._anim_job is not None:
            try:
                self.top.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
        self._anim = None
        self._anim_paused = False

    def _on_canvas_configure(self, _e):
        """窗口尺寸变化。防抖：连续拖边框时不必每一像素都重算一遍。"""
        if self._resize_job is not None:
            try:
                self.top.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.top.after(16, self._resize_now)

    def _resize_now(self):
        self._resize_job = None
        try:
            if self.display is None:
                self._render()
                return
            if self.fit_mode:
                self._apply_fit()
            else:
                self.offset = clamp_offset(self.offset, self._disp_size(),
                                           self._viewport())
            self._render()
            self._update_status()
        except tk.TclError:
            pass   # 窗口已销毁

    # ---------------- 渲染 ----------------

    def _render(self):
        c = self.canvas
        c.delete("all")
        vw, vh = self._viewport()
        if self.display is None:
            side = T.M.s(72)
            glyph = T.photo(ui_draw.icon("image", side, T.VIEWER_CANVAS_MUTED,
                                         stroke=1.4), key=("vph", side))
            c.create_image(vw // 2, vh // 2 - T.M.s(26), image=glyph)
            c.create_text(vw // 2, vh // 2 + T.M.s(30),
                          text=self._placeholder_text,
                          font=T.font_obj(19), fill=T.VIEWER_CANVAS_MUTED)
            c.configure(cursor="")
            return

        plan = render_plan((vw, vh), self.offset, self.zoom, self.display.size)
        if plan is None:
            self._view_photo = None
            c.configure(cursor="")
            return
        (sx0, sy0, sx1, sy1), (px, py), (tw, th) = plan
        resample = (Image.Resampling.LANCZOS if self.zoom < 1
                    else Image.Resampling.BILINEAR)
        region = self.display.crop((sx0, sy0, sx1, sy1)).resize((tw, th), resample)
        # 这里刻意**不走** T.photo 的缓存：每次平移缩放的键都不同，
        # 缓存只会把内存吃光。单引用覆盖，旧图随可回收。
        photo = ImageTk.PhotoImage(region)
        self._view_photo = photo
        c.create_image(px, py, anchor="nw", image=photo)

        dw, dh = self._disp_size()
        draggable = dw > vw + 1 or dh > vh + 1
        c.configure(cursor="fleur" if draggable else "")

    # ---------------- 状态与出口 ----------------

    def _update_status(self):
        n = len(self.images)
        if not n or self.index < 0:
            self.status_left.set("未打开图片")
            self.status_right.set("")
            return
        name = os.path.basename(self.images[self.index])
        left = "%s  [%d/%d]" % (name, self.index + 1, n)
        if self.rotation:
            left += "   已旋转 %d°（仅显示，不改文件）" % self.rotation
        self.status_left.set(left)

        dims = ("%d × %d" % self.pil.size) if self.pil is not None else "?"
        if self.fmt == "GIF" and self._anim:
            n = len(self._anim["frames"])
            fmt_txt = "GIF 动图（%d 帧，%s）" % (
                n, "已暂停" if self._anim_paused else "播放中")
        elif self.fmt == "GIF":
            fmt_txt = "GIF（显示第一帧）"
        else:
            fmt_txt = self.fmt or "?"
        self.status_right.set("%s   %s   %s   %d%%"
                              % (fmt_txt, dims, format_size(self._current_bytes),
                                 round(self.zoom * 100)))

    def _update_nav_buttons(self):
        n = len(self.images)
        self.prev_btn.state(
            ["!disabled"] if n and self.index > 0 else ["disabled"])
        self.next_btn.state(
            ["!disabled"] if n and self.index < n - 1 else ["disabled"])
        self.compress_btn.state(
            ["!disabled"] if n and 0 <= self.index < n else ["disabled"])

    def _send_to_compress(self):
        if self.on_compress is None or not (0 <= self.index < len(self.images)):
            return
        self.on_compress(self.images[self.index])

    def delete_current(self, *_):
        """删除当前图：确认后移入回收站（可恢复），停在原位置的下一张。"""
        if not (0 <= self.index < len(self.images)):
            return
        path = self.images[self.index]
        name = os.path.basename(path)
        if not messagebox.askyesno("删除图片", "把这张图片移入回收站？\n%s" % name):
            return
        if not _recycle(path):
            self.status_left.set("删除失败：%s" % name)
            return
        idx = self.index
        self._stop_anim()
        del self.images[idx]
        self._rebuild_strip()           # 列表变了：缩略图与预读全部重建
        if not self.images:
            self.index = -1
            self.pil = None
            self.display = None
            self._placeholder_text = "打开图片或文件夹开始浏览"
            self._render()
            self._update_status()
            self._update_nav_buttons()
            self.top.title("图片查看")
            return
        self.index = -1
        self._goto(min(idx, len(self.images) - 1))

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop_anim()
        try:
            self.top.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    # 开发用独立入口：python viewer.py [图片或文件夹路径]
    # import main 只为复用它的 DPI 两板斧（模块层面仍是 viewer 不依赖 main）；
    # 产品上不提供独立分发，「文件关联」属安装器级决定，见 TASKS 遗留。
    import main as _main

    import ui_theme as _T

    _main.enable_dpi_awareness()
    _root = tk.Tk()
    _main.apply_tk_scaling(_root)
    _T.init(_root)
    _root.withdraw()
    _win = ViewerWindow(_root, on_compress=None)
    if len(sys.argv) > 1:
        _win.open_path(sys.argv[1])
    _root.mainloop()
