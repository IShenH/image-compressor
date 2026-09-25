# -*- coding: utf-8 -*-
"""图片压缩工具 —— 界面层。

这一层只负责「给用户看什么」和「用户点了什么」，
真正的压缩算法全部在 compressor 模块里，这里一行都不碰。

界面流程：
    选择图片 → 查看信息 → 设置压缩目标 → 压缩 → 查看结果 → 保存
"""

import os
import queue
import shutil
import tempfile
import threading
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

# 格式在界面上的写法
FORMAT_LABELS = {"JPEG": "JPEG", "PNG": "PNG", "WEBP": "WebP"}

# 压缩收益低于这个比例时，如实提示用户「省得不多」（见 requirement.md §5）
LOW_GAIN_THRESHOLD = 0.10

# 后台线程跑起来之后，主线程每隔这么久去看一眼有没有结果。
# 关键：**不能用 join() 等它** —— join 会把主线程一起堵住，
# 界面照样卡死，那就等于白开了线程。
POLL_INTERVAL_MS = 60


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
        self.input_widgets = []  # 压缩期间需要一并禁用的控件

        self._build_ui()
        self._refresh_buttons()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 界面搭建 ----------------

    def _build_ui(self):
        self.root.title("图片压缩工具")

        style = ttk.Style()
        style.configure("Warn.TLabel", foreground="#8a6d00")
        style.configure("Bad.TLabel", foreground="#c0392b")

        outer = ttk.Frame(self.root, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        row = 0

        # ---- 选择图片 ----
        self.choose_btn = ttk.Button(outer, text="选择图片…", command=self.choose_file)
        self.choose_btn.grid(row=row, column=0, sticky="w")
        self.input_widgets.append(self.choose_btn)
        row += 1

        # ---- 原图信息 ----
        info_box = ttk.LabelFrame(outer, text="原图信息", padding=(10, 6, 10, 6))
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
        level_box = ttk.LabelFrame(outer, text="压缩档位", padding=(10, 6, 10, 6))
        level_box.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        for i, (key, name, desc) in enumerate(LEVEL_CHOICES):
            radio = ttk.Radiobutton(level_box, text=name, value=key, variable=self.level,
                                    command=self._on_level_change)
            radio.grid(row=i, column=0, sticky="w")
            self.input_widgets.append(radio)
            ttk.Label(level_box, text=desc).grid(row=i, column=1, sticky="w", padx=(12, 0))
        # 文案要短：系统缩放 125%/150% 时字体会等比放大，长文案会折成难看的碎片
        ttk.Label(level_box, foreground="#777",
                  text="PNG 只能靠减色变小（有损）"
                  ).grid(row=len(LEVEL_CHOICES), column=0, columnspan=2,
                         sticky="w", pady=(6, 0))
        row += 1

        # ---- 输出尺寸 ----
        size_box = ttk.LabelFrame(outer, text="输出尺寸", padding=(10, 6, 10, 6))
        size_box.grid(row=row, column=0, sticky="ew", pady=(8, 0))
        self.size_check = ttk.Checkbutton(size_box, text="限制最长边",
                                          variable=self.limit_size,
                                          command=self._on_size_change)
        self.size_check.grid(row=0, column=0, sticky="w")
        self.input_widgets.append(self.size_check)
        self.size_spin = ttk.Spinbox(size_box, from_=64, to=20000, increment=100,
                                     width=7, textvariable=self.max_dimension,
                                     command=self._on_size_change)
        self.size_spin.grid(row=0, column=1, sticky="w", padx=(8, 4))
        self.input_widgets.append(self.size_spin)
        # 手动输入不会触发 command，得单独监听按键，否则结果会停留在旧尺寸上
        self.size_spin.bind("<KeyRelease>", lambda _e: self._on_size_change())
        ttk.Label(size_box, text="像素").grid(row=0, column=2, sticky="w")
        ttk.Label(size_box, foreground="#777", text="只缩小，不放大"
                  ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        row += 1

        # ---- 执行压缩 ----
        self.compress_btn = ttk.Button(outer, text="压缩", command=self.do_compress)
        self.compress_btn.grid(row=row, column=0, sticky="ew", pady=(8, 0))
        row += 1

        # 进度条只在压缩期间出现，空闲时收起来，不占地方
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.grid(row=row, column=0, sticky="ew", pady=(6, 0))
        self.progress.grid_remove()
        row += 1

        # ---- 压缩结果 ----
        result_box = ttk.LabelFrame(outer, text="压缩结果", padding=(10, 6, 10, 6))
        result_box.grid(row=row, column=0, sticky="ew", pady=(8, 0))
        # 只有数值列（第 1 列）吸收多余宽度。
        # 不给第 0 列权重，否则标签列会撑开、把数值挤到窗口最右边甚至裁掉。
        result_box.columnconfigure(1, weight=1)
        self.result_vars = {}
        for i, (key, title) in enumerate([
                ("before", "压缩前"), ("after", "压缩后"),
                ("saved", "减少"), ("format", "输出格式"),
                ("dimensions", "输出尺寸")]):
            ttk.Label(result_box, text=f"{title}：").grid(row=i, column=0, sticky="w", pady=1)
            var = tk.StringVar(value="—")
            ttk.Label(result_box, textvariable=var).grid(row=i, column=1, sticky="w", pady=1)
            self.result_vars[key] = var

        last = len(self.result_vars)
        self.note_label = ttk.Label(result_box, text="", wraplength=300, justify="left")
        self.note_label.grid(row=last, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.note_label.grid_remove()

        # 建议按钮：只有在「换个做法能压得更小」时才出现。
        # 是否换格式由用户点它决定 —— 程序不擅自替他改格式（requirement.md §7）。
        self.suggest_btn = ttk.Button(result_box, text="", command=self.apply_suggestion)
        self.suggest_btn.grid(row=last + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.suggest_btn.grid_remove()

        self.restore_btn = ttk.Button(result_box, text="恢复原格式",
                                      command=self.restore_format)
        self.restore_btn.grid(row=last + 2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.restore_btn.grid_remove()
        row += 1

        # ---- 保存 ----
        self.save_btn = ttk.Button(outer, text="保存压缩后的图片…", command=self.do_save)
        self.save_btn.grid(row=row, column=0, sticky="ew", pady=(8, 0))

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
            self.progress.grid()
            self.progress.start(12)
        else:
            self.root.config(cursor="")
            self.compress_btn.configure(text="压缩")
            self.progress.stop()
            self.progress.grid_remove()
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

        # 如实反馈：不把「省得不多」甚至「反而更大」包装成成功优化（requirement.md §5）
        if r.saved_bytes <= 0:
            note = "注意：压缩后反而变大了。这张图本来就压得很紧，或者不适合这个格式。"
            style = "Bad.TLabel"
        elif gain < LOW_GAIN_THRESHOLD:
            note = "注意：节省不到 10%。这张图很可能已经压缩过了。"
            style = "Warn.TLabel"
        else:
            note = ""
            style = "TLabel"

        self.note_label.configure(text=note, style=style)
        if note:
            self.note_label.grid()
        else:
            self.note_label.grid_remove()

        self._update_suggestion_buttons()

    def _update_suggestion_buttons(self):
        if self.pending_suggestion:
            name = FORMAT_LABELS.get(self.pending_suggestion.format,
                                     self.pending_suggestion.format)
            self.suggest_btn.configure(
                text=f"改用 {name}（还能再小 {self.pending_suggestion.saved_ratio * 100:.0f}%）")
            self.suggest_btn.grid()
        elif self.pending_level:
            names = dict((k, n) for k, n, _ in LEVEL_CHOICES)
            self.suggest_btn.configure(
                text=f"改用「{names.get(self.pending_level, self.pending_level)}」档重压")
            self.suggest_btn.grid()
        else:
            self.suggest_btn.grid_remove()

        # 已经转过格式了，给一个回到原格式的出口，免得用户走进死胡同
        self.restore_btn.grid() if self.force_format else self.restore_btn.grid_remove()

    def _clear_result(self, keep_suggestion=False):
        self.result = None
        for var in self.result_vars.values():
            var.set("—")
        self.note_label.configure(style="TLabel", text="")
        self.note_label.grid_remove()
        if not keep_suggestion:
            self.pending_suggestion = None
            self.pending_level = None
        self._update_suggestion_buttons()

    def _refresh_buttons(self):
        self.compress_btn.state(
            ["!disabled"] if (self.info and not self.busy) else ["disabled"])
        self.save_btn.state(
            ["!disabled"] if (self.result and not self.busy) else ["disabled"])
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
