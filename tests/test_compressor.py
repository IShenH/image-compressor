# -*- coding: utf-8 -*-
"""compressor 模块的自动化测试。

运行方式（在项目根目录）：
    python -m unittest discover -s tests -t .

样本图片全部现场合成，不依赖任何个人文件。
"""

import os
import shutil
import sys
import tempfile
import unittest

# 让「直接运行本文件」和「unittest discover」两种方式都能找到依赖
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                        # tests/ 自身（helpers）
sys.path.insert(0, os.path.dirname(_HERE))       # 项目根（compressor）

import compressor
from PIL import Image

import helpers


class CompressorTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="imgcomp_test_")
        cls.photo = helpers.make_photo(os.path.join(cls.work, "photo.jpg"))
        cls.photo_png = helpers.make_photo_png(os.path.join(cls.work, "photo.png"))
        cls.graphic = helpers.make_graphic(os.path.join(cls.work, "shot.png"))
        cls.icon = helpers.make_icon(os.path.join(cls.work, "icon.png"))
        cls.soft = helpers.make_soft_alpha(os.path.join(cls.work, "glow.png"))
        cls.broken = helpers.make_broken_jpeg(
            os.path.join(cls.work, "broken.jpg"), cls.photo)
        # 已经用「和我们相同的参数」压过一遍的图 —— 再压几乎没有空间
        cls.tight = os.path.join(cls.work, "tight.jpg")
        with Image.open(cls.photo) as src:
            src.convert("RGB").save(cls.tight, "JPEG", quality=30,
                                    optimize=True, progressive=True)
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def out(self, name):
        """给每次调用一个全新的输出路径，避免用例之间互相干扰。"""
        CompressorTest.counter += 1
        return os.path.join(self.work, "out_%03d_%s" % (self.counter, name))

    # ---------------- 读取信息 ----------------

    def test_read_info_reports_basics(self):
        info = compressor.read_info(self.photo)
        self.assertEqual("JPEG", info.format)
        self.assertEqual(info.size_bytes, os.path.getsize(self.photo))
        self.assertGreater(info.width, 0)
        self.assertGreater(info.height, 0)
        self.assertFalse(info.has_alpha)

    def test_read_info_detects_broken_files(self):
        """坏文件必须在「选完图」这一步就被发现，而不是等到压缩。

        这里是最容易漏的一类：Image.open() 是惰性的，只读文件头 ——
        「文件头正常但数据被截断」的图在 open() 时不会报错。
        """
        empty = os.path.join(self.work, "empty.jpg")
        open(empty, "wb").close()

        text = os.path.join(self.work, "text.jpg")
        with open(text, "w", encoding="utf-8") as handle:
            handle.write("这不是图片" * 100)

        for path in (empty, text, self.broken):
            with self.subTest(path=os.path.basename(path)):
                with self.assertRaises(compressor.CompressError):
                    compressor.read_info(path)

    def test_broken_file_message_is_readable(self):
        """报错文案要是人话，不能把英文原始异常直接丢给用户。"""
        with self.assertRaises(compressor.CompressError) as ctx:
            compressor.read_info(self.broken)
        message = str(ctx.exception)
        self.assertIn("截断", message)
        self.assertNotIn("Traceback", message)

    # ---------------- 核心不变量 ----------------

    def test_never_produces_larger_file(self):
        """最关键的一条：任何输入、任何档位，都不许产出比原图更大的文件。"""
        for name in ("photo", "photo_png", "graphic", "icon", "soft", "tight"):
            source = getattr(self, name)
            size = os.path.getsize(source)
            for level in compressor.LEVELS:
                with self.subTest(source=name, level=level):
                    try:
                        result = compressor.compress(
                            source, self.out("%s_%s" % (name, level)), level)
                    except compressor.NoGainError:
                        continue  # 压不动就明确报错，这是允许的
                    self.assertLess(result.output_size_bytes, size)

    def test_output_is_a_readable_image(self):
        for name in ("photo", "photo_png", "graphic", "icon"):
            source = getattr(self, name)
            with self.subTest(source=name):
                result = compressor.compress(source, self.out(name), "balanced")
                with Image.open(result.output_path) as reopened:
                    reopened.verify()
                with Image.open(result.output_path) as reopened:
                    self.assertEqual(result.output_format, reopened.format)

    def test_extension_matches_output_format(self):
        for name in ("photo", "photo_png", "graphic"):
            source = getattr(self, name)
            with self.subTest(source=name):
                result = compressor.compress(source, self.out(name + ".dat"), "balanced")
                expected = compressor.EXTENSIONS[result.output_format]
                self.assertTrue(result.output_path.endswith(expected),
                                "%s 应以 %s 结尾" % (result.output_path, expected))

    # ---------------- 档位 ----------------

    def test_levels_are_monotonic_on_photos(self):
        """档位越激进，体积越小；否则档位就失去意义了。"""
        sizes = {}
        for level in ("high", "balanced", "small"):
            result = compressor.compress(self.photo, self.out("lv_" + level), level)
            sizes[level] = result.output_size_bytes
        self.assertLess(sizes["small"], sizes["balanced"])
        self.assertLess(sizes["balanced"], sizes["high"])

    def test_unknown_level_is_rejected(self):
        with self.assertRaises(compressor.CompressError):
            compressor.compress(self.photo, self.out("x.jpg"), "bogus")

    # ---------------- 格式 ----------------

    def test_jpeg_keeps_format_by_default(self):
        result = compressor.compress(self.photo, self.out("keep.jpg"), "balanced")
        self.assertEqual("JPEG", result.output_format)
        self.assertIsNone(result.quantized_colors)

    def test_png_is_quantized_and_reported(self):
        """PNG 靠减色变小，而且必须把「减色有损」这件事报告出来。"""
        result = compressor.compress(self.photo_png, self.out("q.png"), "balanced")
        self.assertEqual("PNG", result.output_format)
        self.assertIsNotNone(result.quantized_colors)
        self.assertLess(result.ratio, 0.5, "照片存成 PNG 时减色应能大幅缩小")

    def test_transparent_image_refuses_jpeg(self):
        """JPEG 不支持透明通道，硬转会丢掉透明信息。"""
        with self.assertRaises(compressor.CompressError) as ctx:
            compressor.compress(self.icon, self.out("icon.jpg"), "balanced",
                                output_format="JPEG")
        self.assertIn("透明", str(ctx.exception))

    def test_transparent_image_never_suggests_jpeg(self):
        result = compressor.compress(self.icon, self.out("icon.png"), "balanced")
        if result.suggestion is not None:
            self.assertNotEqual("JPEG", result.suggestion.format)

    def test_binary_alpha_can_be_quantized(self):
        """图标 / logo 的透明只有两态，减色对它没有影响。"""
        result = compressor.compress(self.icon, self.out("icon2.png"), "balanced")
        self.assertIsNotNone(result.quantized_colors)

    def test_gradient_alpha_skips_quantization(self):
        """半透明渐变遇到减色会被压成台阶，必须自动跳过。"""
        result = compressor.compress(self.soft, self.out("glow.png"), "balanced")
        self.assertIsNone(result.quantized_colors,
                          "alpha 复杂的图不应被减色")

    def test_explicit_format_conversion(self):
        result = compressor.compress(self.photo, self.out("c.webp"), "balanced",
                                     output_format="WEBP")
        self.assertEqual("WEBP", result.output_format)
        self.assertTrue(result.output_path.endswith(".webp"))

    def test_unknown_output_format_is_rejected(self):
        with self.assertRaises(compressor.CompressError):
            compressor.compress(self.photo, self.out("z.jpg"), "balanced",
                                output_format="TIFF2")

    # ---------------- 尺寸 ----------------

    def test_downscale_shrinks_and_keeps_ratio(self):
        info = compressor.read_info(self.photo)
        result = compressor.compress(self.photo, self.out("small.jpg"), "balanced",
                                     max_dimension=300)
        self.assertTrue(result.resized)
        self.assertEqual(300, max(result.output_width, result.output_height))
        expected = info.width / info.height
        actual = result.output_width / result.output_height
        self.assertAlmostEqual(expected, actual, places=2)

    def test_downscale_never_enlarges(self):
        info = compressor.read_info(self.photo)
        result = compressor.compress(self.photo, self.out("big.jpg"), "balanced",
                                     max_dimension=max(info.width, info.height) * 4)
        self.assertFalse(result.resized)
        self.assertEqual((info.width, info.height),
                         (result.output_width, result.output_height))

    def test_downscale_rejects_bad_values(self):
        for bad in ("abc", 0, -5):
            with self.subTest(value=bad):
                with self.assertRaises(compressor.CompressError):
                    compressor.compress(self.photo, self.out("bad.jpg"), "balanced",
                                        max_dimension=bad)

    def test_downscale_beats_encoding_alone(self):
        """缩小尺寸的收益应当远大于单纯调编码参数 —— 这是它的主要价值。"""
        plain = compressor.compress(self.photo, self.out("plain.jpg"), "balanced")
        shrunk = compressor.compress(self.photo, self.out("shrunk.jpg"), "balanced",
                                     max_dimension=300)
        self.assertLess(shrunk.output_size_bytes, plain.output_size_bytes / 2)

    # ---------------- 安全与边界 ----------------

    def test_refuses_to_overwrite_source(self):
        with self.assertRaises(compressor.CompressError) as ctx:
            compressor.compress(self.photo, self.photo, "balanced")
        self.assertIn("覆盖原图", str(ctx.exception))
        self.assertTrue(os.path.isfile(self.photo), "原图必须完好无损")

    def test_missing_source_is_reported(self):
        with self.assertRaises(compressor.CompressError):
            compressor.compress(os.path.join(self.work, "nope.jpg"),
                                self.out("nope.jpg"), "balanced")

    def test_missing_destination_directory_is_reported(self):
        with self.assertRaises(compressor.CompressError) as ctx:
            compressor.compress(self.photo,
                                os.path.join(self.work, "no", "such", "dir", "x.jpg"),
                                "balanced")
        self.assertIn("不存在", str(ctx.exception))

    def test_no_gain_error_advice_is_verified(self):
        """压不动时给的「换档位」建议，必须真的管用 —— 不能是猜的。"""
        try:
            compressor.compress(self.tight, self.out("tight.jpg"), "high")
            self.skipTest("这张测试图在 high 档下仍能压小，无需给建议")
        except compressor.NoGainError as exc:
            if exc.suggested_level is None:
                return  # 没给建议，也算合格
            result = compressor.compress(self.tight, self.out("tight2.jpg"),
                                         exc.suggested_level)
            self.assertLess(result.output_size_bytes,
                            os.path.getsize(self.tight))

    def test_exif_orientation_survives(self):
        """手机竖拍的照片靠 EXIF 方向标记正立，压完不能躺倒。"""
        rotated = os.path.join(self.work, "rotated.jpg")
        exif = Image.Exif()
        exif[274] = 6
        with Image.open(self.photo) as src:
            src.convert("RGB").save(rotated, "JPEG", quality=90, exif=exif)
        result = compressor.compress(rotated, self.out("rot.jpg"), "balanced")
        with Image.open(result.output_path) as reopened:
            self.assertEqual(6, reopened.getexif().get(274))


if __name__ == "__main__":
    unittest.main(verbosity=2)
