# -*- coding: utf-8 -*-
"""图片压缩工具 —— 界面层（M2）。

这一层只负责「给用户看什么」和「用户点了什么」，
真正的压缩算法全部在 compressor 模块里，这里一行都不碰。

界面流程（对应 ROADMAP 的 M2）：
    选择图片 → 查看信息 → 设置压缩目标 → 压缩 → 查看结果 → 保存
"""

import os
import shutil
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import compressor


# 档位：压缩逻辑使用的键名 → 界面显示名称与说明。
# 键名属于逻辑层（见 compressor.LEVELS），中文文案属于界面层，所以映射放在这里。
LEVEL_CHOICES = [
    ("balanced", "均衡", "体积与画质兼顾"),
    ("small", "小体积", "压得最狠，画质损失明显"),
    ("high", "高画质", "尽量保住画质"),
]
DEFAULT_LEVEL = "balanced"

# 压缩收益低于这个比例时，如实提示用户「省得不多」（见 requirement.md §5）
LOW_GAIN_THRESHOLD = 0.10


def enable_dpi_awareness():
    """让 Windows 不要对窗口做位图拉伸。

    不做这一步，在系统缩放 125% / 150% 的屏幕上，整个界面会被拉伸放大而发虚。
    必须在创建 Tk 窗口**之前**调用；非 Windows 系统会抛异常，忽略即可。
    """
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def apply_tk_scaling(root):
    """把 tkinter 的缩放系数对齐到屏幕真实 DPI。

    开启 DPI 感知之后，tkinter 仍然按 96 DPI 计算字号，在高缩放屏幕上字会偏小。
    换算关系：tk 的 scaling = 每一「点」占多少像素 = 屏幕 DPI ÷ 72。
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

    界面上的按钮能不能点，完全由两个变量决定：
        self.info    —— 当前选中的原图信息，没有它就不能压缩
        self.result  —— 最近一次压缩的结果，没有它就不能保存
    这样就不用到处手动开关按钮，状态只有一个来源。
    """

    def __init__(self, root: tk.Tk):
        self.root = root

        self.info = None
        self.result = None

        # 压缩结果先落到临时目录，用户点「保存」时才复制到他选定的位置。
        # 这样「压缩 → 看结果 → 决定存到哪」这个顺序才成立，
        # 也避免在用户还没决定之前就往他的磁盘上写东西。
        self.tmpdir = tempfile.mkdtemp(prefix="imgcomp_")
        self.tmp_output = os.path.join(self.tmpdir, "compressed.jpg")

        self.level = tk.StringVar(value=DEFAULT_LEVEL)

        self._build_ui()
        self._refresh_buttons()

        # 关窗口时顺手清掉临时目录
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 界面搭建 ----------------

    def _build_ui(self):
        self.root.title("图片压缩工具")

        style = ttk.Style()
        style.configure("Warn.TLabel", foreground="#8a6d00")
        style.configure("Bad.TLabel", foreground="#c0392b")

        outer = ttk.Frame(self.root, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        row = 0

        # ---- 选择图片 ----
        ttk.Button(outer, text="选择图片…", command=self.choose_file).grid(
            row=row, column=0, sticky="w")
        row += 1

        # ---- 原图信息 ----
        info_box = ttk.LabelFrame(outer, text="原图信息", padding=(12, 8, 12, 10))
        info_box.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        self.info_vars = {}
        for i, (key, title) in enumerate([
                ("file_name", "文件名"), ("format", "格式"),
                ("dimensions", "尺寸"), ("size_bytes", "文件大小")]):
            ttk.Label(info_box, text=f"{title}：").grid(row=i, column=0, sticky="w", pady=1)
            var = tk.StringVar(value="—")
            ttk.Label(info_box, textvariable=var).grid(row=i, column=1, sticky="w", pady=1)
            self.info_vars[key] = var
        row += 1

        # ---- 压缩档位 ----
        level_box = ttk.LabelFrame(outer, text="压缩档位", padding=(12, 8, 12, 10))
        level_box.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        for i, (key, name, desc) in enumerate(LEVEL_CHOICES):
            ttk.Radiobutton(level_box, text=name, value=key, variable=self.level,
                            command=self._on_level_change).grid(row=i, column=0, sticky="w")
            ttk.Label(level_box, text=desc).grid(row=i, column=1, sticky="w", padx=(12, 0))
        row += 1

        # ---- 执行压缩 ----
        self.compress_btn = ttk.Button(outer, text="压缩", command=self.do_compress)
        self.compress_btn.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        row += 1

        # ---- 压缩结果 ----
        result_box = ttk.LabelFrame(outer, text="压缩结果", padding=(12, 8, 12, 10))
        result_box.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        self.result_vars = {}
        for i, (key, title) in enumerate([
                ("before", "压缩前"), ("after", "压缩后"), ("saved", "减少")]):
            ttk.Label(result_box, text=f"{title}：").grid(row=i, column=0, sticky="w", pady=1)
            var = tk.StringVar(value="—")
            ttk.Label(result_box, textvariable=var).grid(row=i, column=1, sticky="w", pady=1)
            self.result_vars[key] = var
        self.note_label = ttk.Label(result_box, text="", wraplength=340, justify="left")
        self.note_label.grid(row=len(self.result_vars), column=0, columnspan=2,
                             sticky="w", pady=(6, 0))
        # 没有提示要显示时，让它彻底退出布局，否则结果框底部会留一块空白。
        # 用 grid_remove 而不是 grid_forget：前者会记住这里的行号列号，之后再 grid() 就能原样回来。
        self.note_label.grid_remove()
        row += 1

        # ---- 保存 ----
        self.save_btn = ttk.Button(outer, text="保存压缩后的图片…", command=self.do_save)
        self.save_btn.grid(row=row, column=0, sticky="ew", pady=(12, 0))

    # ---------------- 事件处理 ----------------

    def choose_file(self):
        """弹出文件选择框。真正的加载逻辑在 load_file 里，方便单独测试。"""
        path = filedialog.askopenfilename(
            title="选择要压缩的图片",
            filetypes=[("JPEG 图片", "*.jpg *.jpeg *.JPG *.JPEG"), ("所有文件", "*.*")],
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

        # 换了图片，上一次的压缩结果就作废了
        self._clear_result()
        self._refresh_buttons()

    def _on_level_change(self):
        """档位一变，上一次的压缩结果立刻作废。

        否则界面显示着「小体积」，用户点保存拿到的却是「均衡」的结果 —— 界面在骗人。
        """
        self._clear_result()
        self._refresh_buttons()

    def do_compress(self):
        if self.info is None:
            return

        self.root.config(cursor="watch")
        # 先让界面重绘一次。不调用的话，鼠标忙碌光标要等压缩跑完才出现，
        # 用户会觉得程序卡死了 —— 因为 tkinter 只在「空闲」时才刷新界面。
        self.root.update_idletasks()
        try:
            self.result = compressor.compress(
                self.info.path, self.tmp_output, self.level.get())
        except compressor.CompressError as exc:
            messagebox.showerror("压缩失败", str(exc))
            self.result = None
        finally:
            self.root.config(cursor="")

        self._show_result()
        self._refresh_buttons()

    def do_save(self):
        """弹出保存对话框。真正的写入逻辑在 save_to 里，方便单独测试。"""
        stem = os.path.splitext(self.info.file_name)[0]
        path = filedialog.asksaveasfilename(
            title="保存压缩后的图片",
            defaultextension=".jpg",
            initialfile=f"{stem}_compressed.jpg",
            filetypes=[("JPEG 图片", "*.jpg")],
        )
        if path:
            self.save_to(path)

    def save_to(self, path):
        """把临时目录里的压缩结果复制到 path。"""
        if self.result is None:
            return False

        # requirement.md §6：不得无提示覆盖原始文件。
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
        if self.result is None:
            self._clear_result()
            return

        r = self.result
        self.result_vars["before"].set(format_size(r.source.size_bytes))
        self.result_vars["after"].set(format_size(r.output_size_bytes))

        gain = 1 - r.ratio
        self.result_vars["saved"].set(f"{gain * 100:.1f}%")

        # 如实反馈：不把「省得不多」甚至「反而更大」包装成成功优化（requirement.md §5）
        if r.saved_bytes <= 0:
            note = ("注意：压缩后反而变大了。这张图可能本来就压得很紧，"
                    "或者内容不适合用 JPEG（例如截图、插画、带文字的图片）。")
            style = "Bad.TLabel"
        elif gain < LOW_GAIN_THRESHOLD:
            note = "注意：节省很少（不到 10%）。这张图很可能已经压缩过了。"
            style = "Warn.TLabel"
        else:
            note = ""
            style = "TLabel"

        self.note_label.configure(text=note, style=style)
        if note:
            self.note_label.grid()
        else:
            self.note_label.grid_remove()

    def _clear_result(self):
        self.result = None
        for var in self.result_vars.values():
            var.set("—")
        self.note_label.configure(style="TLabel", text="")
        self.note_label.grid_remove()

    def _refresh_buttons(self):
        self.compress_btn.state(["!disabled"] if self.info else ["disabled"])
        self.save_btn.state(["!disabled"] if self.result else ["disabled"])

    def _on_close(self):
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
