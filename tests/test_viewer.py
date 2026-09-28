# -*- coding: utf-8 -*-
"""viewer 模块的自动化测试。

几何纯函数（render_plan / clamp_offset / fit_zoom / scan_folder）直接测；
窗口行为沿用 test_gui 的办法 —— 换掉文件对话框、直接调方法、
泵事件循环等后台线程把结果送回来。

运行方式（在项目根目录）：
    python -m unittest discover -s tests
"""

import gc
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                        # tests/ 自身（helpers）
sys.path.insert(0, os.path.dirname(_HERE))       # 项目根（viewer）

import tkinter as tk

import viewer as V
import ui_theme as T

import helpers

from PIL import Image


def make_img(path, size=(320, 200), color=(120, 140, 180)):
    Image.new("RGB", size, color).save(path)


class PureTest(unittest.TestCase):
    """纯几何函数：不碰 tkinter。"""

    def test_fit_zoom_contains(self):
        self.assertAlmostEqual(2.0, V.fit_zoom((400, 300), (800, 600)))
        self.assertAlmostEqual(0.5, V.fit_zoom((800, 600), (400, 300)))

    def test_fit_zoom_guards_zero(self):
        self.assertEqual(1.0, V.fit_zoom((0, 0), (800, 600)))
        self.assertEqual(1.0, V.fit_zoom((400, 300), (0, 600)))

    def test_clamp_zoom_bounds(self):
        self.assertEqual(V.ZOOM_MAX, V.clamp_zoom(999))
        self.assertEqual(V.ZOOM_MIN, V.clamp_zoom(0.0001))
        self.assertAlmostEqual(1.0, V.clamp_zoom(1.0))

    def test_clamp_offset_centers_small(self):
        self.assertEqual((200, 150),
                         V.clamp_offset((0, 0), (400, 300), (800, 600)))
        # 想把居中的小图拖走？拖不动 —— 这是刻意的行为
        self.assertEqual((200, 150),
                         V.clamp_offset((50, 50), (400, 300), (800, 600)))

    def test_clamp_offset_clamps_large(self):
        # 图 1600x1200 视口 800x600：offset 夹在 [-800, 0]，边缘不许离开视口
        self.assertEqual((-800, -600),
                         V.clamp_offset((-2000, -2000), (1600, 1200), (800, 600)))
        self.assertEqual((0, 0),
                         V.clamp_offset((100, 100), (1600, 1200), (800, 600)))

    def test_render_plan_shows_everything_when_it_fits(self):
        plan = V.render_plan((800, 600), (200, 150), 1.0, (400, 300))
        self.assertEqual((0, 0, 400, 300), plan[0])
        self.assertEqual((200, 150), plan[1])
        self.assertEqual((400, 300), plan[2])

    def test_render_plan_zoomed_still_covers_viewport(self):
        # 400x300 @ zoom 2 → 显示 800x600 恰好铺满视口；
        # 关键是目标位图是 800x600 而**不是**整图放大后的尺寸
        plan = V.render_plan((800, 600), (0, 0), 2.0, (400, 300))
        self.assertEqual((0, 0, 400, 300), plan[0])
        self.assertEqual((800, 600), plan[2])

    def test_render_plan_partial_pan(self):
        # 视口 100x100，offset (-50,-50)：只看得到源图 (50,50)-(150,150) 一块；
        # 它在画布上的摆放位置是 (0,0)（显示坐标 50 加上 offset -50）
        plan = V.render_plan((100, 100), (-50, -50), 1.0, (400, 300))
        self.assertEqual((50, 50, 150, 150), plan[0])
        self.assertEqual((0, 0), plan[1])
        self.assertEqual((100, 100), plan[2])

    def test_render_plan_returns_none_when_offscreen(self):
        self.assertIsNone(V.render_plan((800, 600), (900, 0), 1.0, (400, 300)))
        self.assertIsNone(V.render_plan((800, 600), (0, 0), 1.0, (0, 0)))

    def test_render_plan_never_leaks_black_edges(self):
        """超小倍率下源区域必须仍被 clamp 在图内，且不产生空区域。"""
        plan = V.render_plan((800, 600), (100, 100), V.ZOOM_MIN, (4000, 3000))
        self.assertIsNotNone(plan)
        box = plan[0]
        self.assertGreaterEqual(box[0], 0)
        self.assertGreaterEqual(box[1], 0)
        self.assertLessEqual(box[2], 4000)
        self.assertLessEqual(box[3], 3000)

    def test_scan_folder_sorts_and_filters(self):
        work = tempfile.mkdtemp(prefix="viewer_scan_")
        try:
            make_img(os.path.join(work, "b.png"))
            make_img(os.path.join(work, "a.JPG"))     # 大写扩展名也算
            make_img(os.path.join(work, "c.webp"))
            with open(os.path.join(work, "note.txt"), "w") as fh:
                fh.write("x")
            os.makedirs(os.path.join(work, "sub"))    # 子目录不递归
            files = V.scan_folder(work)
            self.assertEqual(["a.JPG", "b.png", "c.webp"],
                             [os.path.basename(p) for p in files])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_scan_folder_missing_dir(self):
        self.assertEqual([], V.scan_folder(os.path.join("no", "such", "dir")))


class GuiTest(unittest.TestCase):
    """窗口行为。对话框换成假函数，直接调方法 + 泵事件循环。"""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="viewer_gui_")
        cls.a = os.path.join(cls.work, "a.jpg")     # 排最前
        cls.b = os.path.join(cls.work, "b.png")
        cls.c = os.path.join(cls.work, "c.jpg")
        make_img(cls.a, (320, 200))
        make_img(cls.b, (200, 320), (200, 120, 90))
        make_img(cls.c, (300, 300), (90, 160, 90))
        cls.big = os.path.join(cls.work, "big.png")  # 比视口大，测锚点缩放
        make_img(cls.big, (2000, 1500), (60, 70, 90))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def setUp(self):
        self.root = tk.Tk()
        T.init(self.root)      # 新解释器必须换掉字体/图片缓存（tkinter 的坑）
        self.sent = []
        self.v = V.ViewerWindow(self.root, on_compress=self.sent.append)
        self.pump()

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        # 必须在主线程立刻收掉本测试产生的 tk 对象（PhotoImage / StringVar 等）。
        # 不收的话，它们变成循环垃圾，等哪次**解码线程**分配内存触发 GC，
        # 终结器就要从工作线程调 Tcl —— 测试环境没有 mainloop，两个线程
        # 的终结器互相纠缠，偶发把解码线程拖住几十秒（faulthandler 实测）。
        gc.collect()

    def pump(self, times=3):
        for _ in range(times):
            self.root.update_idletasks()
            self.root.update()

    def wait_success(self, timeout=30):
        ok = helpers.wait_until(lambda: self.v.pil is not None,
                                timeout=timeout, pump=self.pump)
        self.assertTrue(ok, "图片在 %.0f 秒内没有加载完成" % timeout)
        self.pump()

    # ---------------- 打开 ----------------

    def test_open_single_file(self):
        # 打开一个文件 = 浏览它所在的文件夹，当前图落在自己的位置上
        self.v.open_path(self.a)
        self.assertEqual(4, len(self.v.images))
        self.assertEqual(0, self.v.index)
        self.wait_success()
        self.assertEqual("图片查看 — a.jpg [1/4]", self.v.top.title())
        self.assertIn("[1/4]", self.v.status_left.get())
        self.assertNotIn("disabled", self.v.compress_btn.state())

    def test_open_file_positions_in_folder(self):
        """打开文件夹里的某一个文件：列表是整个文件夹，落在它的位置上。"""
        self.v.open_path(self.c)          # a.jpg, b.png, big.png, c.jpg
        self.assertEqual(4, len(self.v.images))
        self.assertEqual(3, self.v.index)
        self.wait_success()
        self.assertIn("c.jpg", self.v.status_left.get())

    def test_open_empty_folder(self):
        d = tempfile.mkdtemp(prefix="viewer_empty_")
        try:
            self.v.open_path(d)
            self.assertEqual([], self.v.images)
            self.assertEqual(-1, self.v.index)
            self.assertIn("没有可查看的图片", self.v._placeholder_text)
            self.assertIn("disabled", self.v.compress_btn.state())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # ---------------- 翻页与边界 ----------------

    def test_navigation_order_and_bounds(self):
        self.v.open_path(self.work)
        self.assertEqual(4, len(self.v.images))
        self.wait_success()
        self.assertIn("a.jpg", self.v.status_left.get())

        self.v.next_image()
        self.wait_success()
        self.assertEqual(1, self.v.index)
        self.v.next_image()
        self.wait_success()
        self.assertEqual(2, self.v.index)
        self.v.next_image()
        self.wait_success()
        self.assertEqual(3, self.v.index)
        # 尽头：按钮禁用，再点也不动
        self.assertIn("disabled", self.v.next_btn.state())
        self.v.next_image()
        self.assertEqual(3, self.v.index)

        self.v.prev_image()
        self.wait_success()
        self.assertEqual(2, self.v.index)
        self.assertNotIn("disabled", self.v.next_btn.state())
        self.assertNotIn("disabled", self.v.prev_btn.state())

    def test_keyboard_navigation(self):
        self.v.open_path(self.work)
        self.wait_success()
        # 合成键盘事件要求键盘焦点在这个窗口上
        self.v.top.focus_force()
        self.pump()
        self.v.top.event_generate("<Right>")
        self.wait_success()
        self.assertEqual(1, self.v.index)
        self.v.top.event_generate("<Left>")
        self.wait_success()
        self.assertEqual(0, self.v.index)

    def test_strip_click_navigates(self):
        self.v.open_path(self.work)
        ok = helpers.wait_until(lambda: len(self.v._thumb_photos) >= 4,
                                timeout=30, pump=self.pump)
        self.assertTrue(ok, "缩略图没有生成")
        cw, ch, y0, pad = self.v._thumb_cell
        e = type("FakeEvent", (), {})()
        e.x = pad + 1 * self.v._thumb_pitch + cw // 2   # 第二格中心
        e.y = y0 + ch // 2
        self.v._strip_click(e)
        self.wait_success()
        self.assertEqual(1, self.v.index)
        # 选中框要套在第 2 格上，而不是停在屏幕外
        x0, _, x1, _ = self.v.strip.coords(self.v._sel_item)
        self.assertGreater(x1, 0)

    # ---------------- 坏文件 ----------------

    def test_broken_file_auto_skips_to_next(self):
        d = tempfile.mkdtemp(prefix="viewer_bad_")
        try:
            bad = os.path.join(d, "aaa.jpg")
            good = os.path.join(d, "zzz.jpg")
            with open(bad, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xe0not really a jpeg")
            make_img(good, (100, 80))
            self.v.open_path(d)
            ok = helpers.wait_until(
                lambda: self.v.pil is not None and self.v.index == 1,
                timeout=30, pump=self.pump)
            self.assertTrue(ok, "坏图应当被跳过并加载下一张")
            self.assertIn("zzz.jpg", self.v.status_left.get())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_all_broken_stops_without_crash(self):
        d = tempfile.mkdtemp(prefix="viewer_bad2_")
        try:
            bad = os.path.join(d, "only.jpg")
            with open(bad, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xe0junk")
            self.v.open_path(d)
            ok = helpers.wait_until(
                lambda: self.v._placeholder_text.startswith("打不开"),
                timeout=10, pump=self.pump)
            self.assertTrue(ok, "坏图应当以「打不开」的占位文案告知")
            self.assertEqual(0, self.v.index)
            self.assertIsNone(self.v.pil)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # ---------------- 视图 ----------------

    def test_rotate_only_changes_view(self):
        self.v.open_path(self.a)
        self.wait_success()
        self.assertEqual((320, 200), self.v.display.size)
        self.v.rotate()
        self.assertEqual((200, 320), self.v.display.size)   # 宽高互换
        self.assertEqual(90, self.v.rotation)
        self.assertIn("已旋转 90", self.v.status_left.get())
        self.assertIn("仅显示", self.v.status_left.get())
        # 原图文件本身没被改动
        with Image.open(self.a) as reopened:
            self.assertEqual((320, 200), reopened.size)

    def test_zoom_is_clamped(self):
        self.v.open_path(self.a)
        self.wait_success()
        self.v._set_zoom(9999)
        self.assertEqual(V.ZOOM_MAX, self.v.zoom)
        self.v._set_zoom(0.000001)
        self.assertEqual(V.ZOOM_MIN, self.v.zoom)

    def test_zoom_anchor_keeps_image_point_stable(self):
        """以某点为锚缩放：锚点下面的图像点缩放前后必须是同一个点。"""
        self.v.open_path(self.big)
        self.wait_success()
        self.v.fit_view()
        ax, ay = 150, 100
        z0, (ox0, oy0) = self.v.zoom, self.v.offset
        self.v._set_zoom(3.0, anchor=(ax, ay))
        p0 = ((ax - ox0) / z0, (ay - oy0) / z0)
        p1 = ((ax - self.v.offset[0]) / self.v.zoom,
              (ay - self.v.offset[1]) / self.v.zoom)
        self.assertAlmostEqual(p0[0], p1[0], delta=2)
        self.assertAlmostEqual(p0[1], p1[1], delta=2)
        self.assertFalse(self.v.fit_mode)

    def test_fit_after_zoom_restores_fit_mode(self):
        self.v.open_path(self.big)
        self.wait_success()
        self.v._set_zoom(3.0)
        self.assertFalse(self.v.fit_mode)
        self.v.fit_view()
        self.assertTrue(self.v.fit_mode)
        self.assertAlmostEqual(
            V.fit_zoom(self.v.display.size, self.v._viewport()), self.v.zoom)

    def test_fullscreen_toggle_and_escape_semantics(self):
        """F11 进全屏；Esc 先退出全屏、再按才是关窗。"""
        self.v.open_path(self.a)
        self.wait_success()
        self.v.top.focus_force()
        self.pump()
        self.v.top.event_generate("<F11>")
        self.pump()
        self.assertTrue(self.v.top.attributes("-fullscreen"))
        self.v.top.event_generate("<Escape>")
        self.pump()
        self.assertFalse(self.v.top.attributes("-fullscreen"))
        self.assertTrue(self.v.top.winfo_exists(), "Esc 应当先退出全屏而不是关窗")
        self.v.top.event_generate("<Escape>")
        self.pump()
        self.assertFalse(self.v.top.winfo_exists())

    # ---------------- 缩略图 ----------------

    def test_thumbnails_fill_in_and_selection_follows(self):
        self.v.open_path(self.work)
        ok = helpers.wait_until(lambda: len(self.v._thumb_photos) >= 4,
                                timeout=30, pump=self.pump)
        self.assertTrue(ok, "缩略图没有生成")
        self.v.next_image()
        self.wait_success()
        x0, _, x1, _ = self.v.strip.coords(self.v._sel_item)
        self.assertGreater(x1, 0, "选中框应当套在当前格上")

    # ---------------- 出口 ----------------

    def test_prefetch_serves_next_image_from_cache(self):
        """加载后左右邻居应被预读；翻页命中缓存而不再解码。"""
        self.v.open_path(self.work)
        self.wait_success()
        ok = helpers.wait_until(lambda: 1 in self.v._prefetch,
                                timeout=30, pump=self.pump)
        self.assertTrue(ok, "邻居没有被预读")
        self.v.next_image()
        self.wait_success()
        self.assertEqual(1, self.v.index)
        self.assertEqual(1, self.v._prefetch_hits, "翻页应当命中预读缓存")

    def test_prefetch_cleared_when_list_changes(self):
        self.v.open_path(self.work)
        self.wait_success()
        ok = helpers.wait_until(lambda: 1 in self.v._prefetch,
                                timeout=30, pump=self.pump)
        self.assertTrue(ok)
        self.v.open_path(self.a)          # 换一批图片
        self.assertEqual({}, self.v._prefetch, "换列表后预读必须作废")

    def test_compress_callback_receives_current_path(self):
        self.v.open_path(self.a)
        self.wait_success()
        self.v.compress_btn.invoke()
        self.assertEqual([self.a], self.sent)

    def test_gif_notes_first_frame(self):
        d = tempfile.mkdtemp(prefix="viewer_gif_")
        try:
            g = os.path.join(d, "x.gif")
            Image.new("RGB", (60, 40), (10, 120, 200)).save(g, "GIF")
            self.v.open_path(g)
            self.wait_success()
            self.assertIsNone(self.v._anim, "单帧 GIF 不该进入动画状态")
            self.assertIn("GIF", self.v.status_right.get())
            self.assertIn("第一帧", self.v.status_right.get())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_gif_animates_and_pauses(self):
        d = tempfile.mkdtemp(prefix="viewer_anim_")
        try:
            g = os.path.join(d, "x.gif")
            jpg = os.path.join(d, "y.jpg")
            frames = [Image.new("RGB", (60, 40), c) for c in
                      ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
            frames[0].save(g, save_all=True, append_images=frames[1:],
                           duration=60, loop=0)
            make_img(jpg, (80, 60))
            self.v.open_path(g)
            self.wait_success()
            self.assertIsNotNone(self.v._anim, "多帧 GIF 应当进入动画状态")
            self.assertIn("动图", self.v.status_right.get())
            ok = helpers.wait_until(lambda: self.v._anim_i >= 2,
                                    timeout=10, pump=self.pump)
            self.assertTrue(ok, "动画帧没有推进")
            self.v.toggle_anim_pause()
            self.assertTrue(self.v._anim_paused)
            frozen = self.v._anim_i
            self.pump(10)
            self.assertEqual(frozen, self.v._anim_i, "暂停后帧不应继续推进")
            # 翻到下一张：动画必须停止并清理
            self.v.next_image()
            self.wait_success()
            self.assertIsNone(self.v._anim)
            self.assertIsNone(self.v._anim_job)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_close_releases_window(self):
        self.v.open_path(self.a)
        self.wait_success()
        self.v.close()
        self.pump()
        self.assertFalse(self.v.top.winfo_exists())


if __name__ == "__main__":
    unittest.main()
