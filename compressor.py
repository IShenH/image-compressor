# -*- coding: utf-8 -*-
"""图片压缩核心逻辑（M1 阶段）。

这个模块只负责图片处理，**不导入任何界面库**，
因此可以脱离 GUI 独立运行和测试。

对外提供两个函数：
    read_info(path)             读取一张图片的基本信息
    compress(src, dst, level)   压缩并输出到指定位置
"""

import os
from dataclasses import dataclass

from PIL import Image


class CompressError(Exception):
    """压缩过程中「可以解释给用户听」的错误。

    把底层异常统一包成这一种类型，界面层就只需要处理一种错误，
    不用去猜 Pillow 会抛出什么。
    """


# 三档预设 → JPEG 质量参数（0~100，越小体积越小、画质越低）。
# 键名供代码使用；界面上的中文名称（小体积 / 均衡 / 高画质）由 M2 的界面层负责映射。
#
# 这组数值已用真实照片复核（1920x1200，原图本身约 q=90），实测占原图比例：
#     高画质 q=85 → 85.5%     均衡 q=75 → 71.6%     小体积 q=50 → 40.2%
# 高画质**没有**取 q=90：真实照片大多本来就压在 q≈90，再按 q=90 重存会得到
# 「节省 0.0%」，等于按钮按下去没反应。而 q=85 的像素偏差与 q=90 几乎相同
# （1.25 对 1.19，肉眼不可分），却能实打实省下 14.5%。
# 完整数据见 docs/research/image-compression-experiments.md。
LEVELS = {
    "small": 50,
    "balanced": 75,
    "high": 85,
}


@dataclass
class ImageInfo:
    """一张图片在压缩前的基本信息。"""

    path: str
    file_name: str
    format: str
    size_bytes: int
    width: int
    height: int


@dataclass
class CompressResult:
    """一次压缩的结果。"""

    source: ImageInfo
    output_path: str
    output_size_bytes: int

    @property
    def saved_bytes(self) -> int:
        """省下了多少字节。负数表示压缩后反而更大。"""
        return self.source.size_bytes - self.output_size_bytes

    @property
    def ratio(self) -> float:
        """压缩后体积占原图的比例。0.12 表示压到了原来的 12%。"""
        if self.source.size_bytes == 0:
            return 0.0
        return self.output_size_bytes / self.source.size_bytes


def read_info(path: str) -> ImageInfo:
    """读取一张图片的基本信息。

    有一个地方容易误解：`Image.open()` 是**惰性**的 —— 它只读文件头，
    拿到格式、尺寸这些信息就返回，并不真正解码像素。
    所以这里读尺寸很快，而且「文件大小」必须从磁盘上取（os.path.getsize），
    不能从图像对象上取 —— 磁盘上是压缩过的字节，内存里是解码后的像素，两者不是一回事。
    """
    if not os.path.isfile(path):
        raise CompressError(f"文件不存在：{path}")

    try:
        with Image.open(path) as img:
            fmt = img.format or "未知"
            width, height = img.size
    except Exception as exc:
        raise CompressError(f"无法识别这个文件为图片：{exc}") from exc

    return ImageInfo(
        path=path,
        file_name=os.path.basename(path),
        format=fmt,
        size_bytes=os.path.getsize(path),
        width=width,
        height=height,
    )


def compress(src: str, dst: str, level: str = "balanced") -> CompressResult:
    """把 src 压缩后保存到 dst，返回压缩结果。

    M1 阶段只支持 JPEG 输入。其他格式需要按图片类型分流处理，
    计划在 M3 完成（见 ADR-002）。这里先明确报错，不做假装能压的兜底。
    """
    # 1. 档位必须是已知的
    if level not in LEVELS:
        raise CompressError(
            f"未知的压缩档位：{level!r}，可选：{'、'.join(LEVELS)}"
        )

    # 2. 读取原图信息（同时校验了文件是否存在、是否为图片）
    info = read_info(src)

    # 3. M1 只处理 JPEG
    if info.format != "JPEG":
        raise CompressError(
            f"M1 目前只支持 JPEG，而这张是 {info.format}。"
            "其他格式要按图片类型分流处理，计划在 M3 完成。"
        )

    # 4. 原图安全：拒绝写到原图路径上。
    #    用 normcase 是为了在 Windows 上把大小写差异也算作同一个文件。
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
        raise CompressError("输出路径和原图是同一个文件，这会覆盖原图。请另选保存位置。")

    # 5. 保存位置必须存在。这里不自动创建目录 —— 保存位置由用户决定，
    #    程序不擅自替他造目录，只如实报告。
    dst_dir = os.path.dirname(os.path.abspath(dst))
    if not os.path.isdir(dst_dir):
        raise CompressError(f"保存位置不存在：{dst_dir}")

    quality = LEVELS[level]
    try:
        with Image.open(src) as img:
            save_kwargs = {
                "format": "JPEG",
                "quality": quality,
                "optimize": True,
                "progressive": True,
            }

            # 手机竖拍的照片靠 EXIF 里的方向标记来正立显示。
            # 保存时不把它带过去，压缩后的照片就会躺倒。
            # 但注意：**只有确实存在时才传**。传 exif=None 不会「表示没有 EXIF」，
            # Pillow 的 JPEG 插件会直接对 None 取长度而报错。
            exif = img.info.get("exif")
            if exif:
                save_kwargs["exif"] = exif

            img.save(dst, **save_kwargs)
    except Exception as exc:
        raise CompressError(f"压缩失败：{exc}") from exc

    return CompressResult(
        source=info,
        output_path=dst,
        output_size_bytes=os.path.getsize(dst),
    )
