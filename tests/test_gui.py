# -*- coding: utf-8 -*-
"""界面层（main.py）的自动化测试。

tkinter 没法用鼠标点，这里的做法是：
把弹窗换成假函数（否则一律会阻塞等点击），然后直接调界面的方法，
并**泵事件循环**让后台线程的结果回到主线程。

运行方式（在项目根目录）：
    python -m unittest discover -s tests
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import tkinter as tk

import main as gui

import helpers


class GuiTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="imgcomp_gui_test_")
        cls.photo = helpers.make_photo(os.path.join(cls.work, "photo.jpg"), (700, 440))
        cls.graphic = helpers.make_graphic(os.path.join(cls.work, "shot.png"), (700, 440))
        cls.broken = helpers.make_broken_jpeg(
            os.path.join(cls.work, "broken.jpg"), cls.photo)
        cls.tight = os.path.join(cls.work, "tight.jpg")
        from PIL import Image
        with Image.open(cls.photo) as src:
            src.convert("RGB").save(cls.tight, "JPEG", quality=25,
                                    optimize=True, progressive=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    # ---------------- 环境 ----------------

    def setUp(self):
        self.popups = []
        gui.messagebox.showinfo = self._popup("info")
        gui.messagebox.showwarning = self._popup("warning")
        gui.messagebox.showerror = self._popup("error")
        gui.messagebox.askyesno = lambda *args, **kwargs: True
        self.saved_kwargs = {}
        self.save_target = None
        gui.filedialog.asksaveasfilename = self._fake_save_dialog
        gui.filedialog.askopenfilename = lambda **kwargs: self.open_target
        self.open_target = None

        gui.enable_dpi_awareness()
        self.root = tk.Tk()
        gui.apply_tk_scaling(self.root)
        self.app = gui.App(self.root)
        self.pump()

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _popup(self, kind):
        def record(title="", message="", **kwargs):
            self.popups.append((kind, title, message))
        return record

    def _fake_save_dialog(self, **kwargs):
        self.saved_kwargs.clear()
        self.saved_kwargs.update(kwargs)
        return self.save_target

    def pump(self, times=2):
        """跑几轮事件循环。后台线程的结果靠 after 回调回到主线程，必须真的转起来。"""
        for _ in range(times):
            self.root.update_idletasks()
            self.root.update()

    def wait_idle(self, timeout=60):
        ok = helpers.wait_until(lambda: not self.app.busy, timeout=timeout, pump=self.pump)
        self.assertTrue(ok, "压缩在 %.0f 秒内没有结束" % timeout)
        self.pump()

    def press_compress(self):
        """点「压缩」并等它跑完。"""
        self.app.do_compress()
        self.wait_idle()

    # ---------------- 初始状态 ----------------

    def test_initial_state(self):
        self.assertIn("disabled", self.app.compress_btn.state())
        self.assertIn("disabled", self.app.save_btn.state())
        self.assertEqual("—", self.app.info_vars["file_name"].get())
        self.assertFalse(self.app.limit_size.get())

    # ---------------- 后台线程 ----------------

    def test_compress_does_not_block_main_thread(self):
        """最关键的一条：点压缩后必须立刻返回，活儿交给后台线程。

        如果是同步执行，这个调用会一直卡到压缩结束 —— 窗口也就跟着冻住了。
        """
        self.app.load_file(self.graphic)
        self.pump()
        fired = []
        self.root.after(20, lambda: fired.append(self.app.busy))

        start = time.perf_counter()
        self.app.do_compress()
        elapsed = (time.perf_counter() - start) * 1000

        self.assertLess(elapsed, 150, "do_compress 阻塞了 %.0f 毫秒" % elapsed)
        self.assertTrue(self.app.busy, "没有进入忙碌状态")
        self.wait_idle()
        # 定时回调能在压缩结束前执行，才说明事件循环一直活着
        self.assertTrue(fired, "定时回调根本没机会执行")
        self.assertIs(True, fired[0], "回调执行时压缩已经结束了，说明主线程被堵住")

    def test_busy_state_locks_the_ui(self):
        self.app.load_file(self.graphic)
        self.pump()
        self.app.do_compress()
        self.root.update_idletasks()

        self.assertTrue(self.app.busy)
        self.assertEqual("正在压缩…", self.app.compress_btn.cget("text"))
        self.assertTrue(self.app.progress.winfo_ismapped(), "进度条应当显示")
        for widget in self.app.input_widgets:
            self.assertIn("disabled", widget.state())
        self.assertIn("disabled", self.app.save_btn.state())

        self.wait_idle()
        self.assertEqual("压缩", self.app.compress_btn.cget("text"))
        self.assertFalse(self.app.progress.winfo_ismapped(), "进度条应当收起")
        for widget in self.app.input_widgets:
            self.assertNotIn("disabled", widget.state())

    def test_compressing_twice_does_not_start_a_second_job(self):
        """压缩期间重复点，不应该再起一个线程（两个任务会抢同一个临时文件）。"""
        self.app.load_file(self.graphic)
        self.pump()
        self.app.do_compress()
        first_result = self.app.result
        self.app.do_compress()  # 应当在 busy 检查处直接返回
        self.wait_idle()
        self.assertIsNot(first_result, self.app.result)
        self.assertIsNotNone(self.app.result)

    # ---------------- 主流程 ----------------

    def test_load_then_compress(self):
        self.app.load_file(self.photo)
        self.pump()
        self.assertEqual(self.app.info.file_name, os.path.basename(self.photo))
        self.assertNotIn("disabled", self.app.compress_btn.state())
        self.assertIn("disabled", self.app.save_btn.state())

        self.press_compress()
        self.assertIsNotNone(self.app.result)
        self.assertNotIn("disabled", self.app.save_btn.state())
        self.assertNotEqual("—", self.app.result_vars["after"].get())
        self.assertNotEqual("—", self.app.result_vars["format"].get())

    def test_png_reports_lossy_quantization(self):
        """减色是有损的，界面上必须看得见。"""
        self.app.load_file(self.graphic)
        self.pump()
        self.press_compress()
        self.assertIn("有损", self.app.result_vars["format"].get())

    def test_changing_level_invalidates_result(self):
        self.app.load_file(self.photo)
        self.pump()
        self.press_compress()
        self.assertNotEqual("—", self.app.result_vars["after"].get())

        self.app.level.set("small")
        self.app._on_level_change()
        self.pump()
        self.assertIsNone(self.app.result, "换了档位，旧结果必须作废")
        self.assertEqual("—", self.app.result_vars["after"].get())
        self.assertIn("disabled", self.app.save_btn.state())

    def test_changing_size_invalidates_result(self):
        self.app.load_file(self.photo)
        self.pump()
        self.press_compress()
        self.app.limit_size.set(True)
        self.app._on_size_change()
        self.pump()
        self.assertIsNone(self.app.result)

    def test_size_limit_applies(self):
        self.app.load_file(self.photo)
        self.app.limit_size.set(True)
        self.app.max_dimension.set("200")
        self.pump()
        self.press_compress()
        self.assertTrue(self.app.result.resized)
        self.assertIn("缩小", self.app.result_vars["dimensions"].get())

    # ---------------- 建议 ----------------

    def test_suggestion_visibility_follows_result(self):
        """有建议就出按钮，没有就不出 —— 不要占着位置。"""
        self.app.load_file(self.photo)
        self.pump()
        self.press_compress()
        has_suggestion = self.app.result.suggestion is not None
        self.assertEqual(has_suggestion, bool(self.app.suggest_btn.winfo_ismapped()))

    def test_adopting_and_reverting_format(self):
        """采纳建议会换格式，并且必须留一条回到原格式的路。

        这里直接构造「有建议」的状态：真实的建议要看图片本身能不能省到 60% 以下，
        靠某张测试图恰好触发太脆弱。
        """
        self.app.load_file(self.photo)
        self.pump()
        self.press_compress()
        original_format = self.app.result.output_format

        self.app.pending_suggestion = gui.compressor.Suggestion(
            format="WEBP", size_bytes=1, saved_ratio=0.5)
        self.app._update_suggestion_buttons()
        self.pump()
        self.assertTrue(self.app.suggest_btn.winfo_ismapped())
        self.assertFalse(self.app.restore_btn.winfo_ismapped(),
                         "还没转换之前不该出现「恢复原格式」")

        self.app.apply_suggestion()
        self.wait_idle()
        self.assertEqual("WEBP", self.app.result.output_format)
        self.assertTrue(self.app.restore_btn.winfo_ismapped(),
                        "转换过格式后必须给一个回到原格式的出口")

        self.app.restore_format()
        self.wait_idle()
        self.assertEqual(original_format, self.app.result.output_format)
        self.assertFalse(self.app.restore_btn.winfo_ismapped())

    def test_adopting_level_advice_switches_level(self):
        """「换个档位」的建议被采纳后，界面上选中的档位也要跟着变。"""
        self.app.load_file(self.photo)
        self.pump()
        self.app.pending_level = "small"
        self.app.pending_suggestion = None
        self.app._update_suggestion_buttons()
        self.pump()
        self.assertTrue(self.app.suggest_btn.winfo_ismapped())

        self.app.apply_suggestion()
        self.wait_idle()
        self.assertEqual("small", self.app.level.get())

    # ---------------- 错误处理 ----------------

    def test_broken_file_is_caught_at_selection(self):
        """坏文件应当在「选完图」就被拦下，而不是等到压缩。"""
        self.app.load_file(self.broken)
        self.pump()
        self.assertTrue(self.popups, "没有弹出任何提示")
        self.assertEqual("error", self.popups[-1][0])
        self.assertIsNone(self.app.info, "坏文件不该被选中")

    def test_missing_file_is_reported(self):
        self.app.load_file(os.path.join(self.work, "does_not_exist.jpg"))
        self.pump()
        self.assertTrue(self.popups)
        self.assertEqual("error", self.popups[-1][0])

    def test_invalid_size_input_is_reported(self):
        self.app.load_file(self.photo)
        self.app.limit_size.set(True)
        self.app.max_dimension.set("abc")
        self.pump()
        self.press_compress()
        self.assertTrue(self.popups, "非法尺寸应当报错")
        self.assertEqual("error", self.popups[-1][0])
        self.assertIsNone(self.app.result)

    def test_no_gain_is_reported_without_writing(self):
        """压不动时如实报错，并且不产出文件。"""
        self.app.load_file(self.tight)
        self.app.level.set("high")
        self.app._on_level_change()
        self.pump()
        self.press_compress()
        if self.app.result is not None:
            self.skipTest("这张测试图在 high 档下仍能压小")
        self.assertTrue(any(kind == "warning" for kind, _, _ in self.popups),
                        "「压不小」应当用提示弹窗告知")
        self.assertIn("disabled", self.app.save_btn.state())

    # ---------------- 保存 ----------------

    def test_save_dialog_uses_matching_extension(self):
        self.app.load_file(self.photo)
        self.app.level.set("small")
        self.app._on_level_change()
        self.pump()
        self.press_compress()
        self.app.do_save()
        self.pump()

        expected_ext = gui.compressor.EXTENSIONS[self.app.result.output_format]
        self.assertEqual(expected_ext, self.saved_kwargs.get("defaultextension"))
        self.assertIn("small", self.saved_kwargs.get("initialfile", ""),
                      "默认文件名应当带上档位")

    def test_save_refuses_to_overwrite_source(self):
        self.app.load_file(self.photo)
        self.pump()
        self.press_compress()
        self.save_target = self.photo          # 用户选了原图本身
        self.assertFalse(self.app.save_to(self.photo))
        self.assertTrue(os.path.isfile(self.photo), "原图必须完好")
        self.assertTrue(any(kind == "warning" for kind, _, _ in self.popups))

    def test_save_writes_a_valid_file(self):
        self.app.load_file(self.photo)
        self.pump()
        self.press_compress()
        target = os.path.join(self.work, "saved.jpg")
        self.assertTrue(self.app.save_to(target))
        self.assertTrue(os.path.isfile(target))
        from PIL import Image
        with Image.open(target) as reopened:
            reopened.verify()

    def test_cancel_dialogs_do_not_crash(self):
        self.open_target = None                # 用户取消了选择
        self.app.choose_file()
        self.pump()
        self.assertIsNone(self.app.info)
        self.save_target = None                # 用户取消了保存
        self.app.do_save()
        self.pump()

    # ---------------- EXIF 方向 ----------------

    def test_preview_respects_exif_orientation(self):
        """手机竖拍靠 EXIF 方向标记正立显示：预览必须跟着转，不能躺着。"""
        p = helpers.make_exif_jpeg(os.path.join(self.work, "rot.jpg"),
                                   (400, 160), orientation=6)
        self.app.load_file(p)
        self.pump()
        self.assertIsNotNone(self.app.thumb_pil)
        self.assertLess(self.app.thumb_pil.width, self.app.thumb_pil.height,
                        "EXIF 方向 6 的横图，预览应当显示为竖图")
        # 信息行显示的是文件里的存储尺寸（这是「文件实际内容」的事实）
        self.assertEqual("400 × 160", self.app.info_vars["dimensions"].get())

    # ---------------- 布局不变量 ----------------

    def test_layout_never_overflows_or_overlaps(self):
        """任何「可选元素显隐」组合下：可见控件都不能超出窗口，互相也不能重叠。

        这条是**补出来的**。之前只断言 winfo_ismapped()，看不出位置问题 ——
        结果「提示行隐藏、但建议按钮显示」时，下面两个按钮的位置仍按
        「三个都显示」往后排，偏出卡片高度，把保存按钮压住了。
        """
        cases = [
            {"note": False, "suggest": False, "restore": False},
            {"note": True, "suggest": False, "restore": False},
            {"note": False, "suggest": True, "restore": False},
            {"note": False, "suggest": False, "restore": True},
            {"note": False, "suggest": True, "restore": True},
            {"note": True, "suggest": True, "restore": True},
        ]
        widgets = [
            ("压缩按钮", "compress_btn"), ("提示行", "note_label"),
            ("建议按钮", "suggest_btn"), ("恢复按钮", "restore_btn"),
            ("保存按钮", "save_btn"),
        ]
        for case in cases:
            with self.subTest(extra=case):
                self.app._extra = dict(case)
                self.app._relayout()
                self.pump()

                boxes = []
                for title, attr in widgets:
                    widget = getattr(self.app, attr)
                    if widget.winfo_ismapped():
                        top = widget.winfo_y()
                        boxes.append((title, top, top + widget.winfo_height()))

                for title, _, bottom in boxes:
                    self.assertLessEqual(
                        bottom, self.app.win_h,
                        "%s 底部 %d 超出窗口高度 %d" % (title, bottom, self.app.win_h))

                boxes.sort(key=lambda box: box[1])
                for (n1, _, b1), (n2, t2, _) in zip(boxes, boxes[1:]):
                    self.assertLessEqual(
                        b1, t2 + 1,
                        "「%s」底部 %d 与「%s」顶部 %d 重叠" % (n1, b1, n2, t2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
