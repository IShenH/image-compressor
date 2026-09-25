# -*- coding: utf-8 -*-
"""图片压缩核心逻辑。

这个模块只负责图片处理，**不导入任何界面库**，
因此可以脱离 GUI 独立运行和测试。

对外提供两个函数：
    read_info(path)                          读取一张图片的基本信息
    compress(src, dst, level, output_format)  压缩并输出到指定位置
"""

import io
import os
from dataclasses import dataclass

from PIL import Image


class CompressError(Exception):
    """压缩过程中「可以解释给用户听」的错误。

    把底层异常统一包成这一种类型，界面层就只需要处理一种错误，
    不用去猜 Pillow 会抛出什么。
    """


class NoGainError(CompressError):
    """这张图在当前档位下压不小。

    与其产出一个「比原图还大」的文件，不如如实报错（Pillow 完全能写出更大的文件，
    但那对用户没有任何价值）。可能附带：
        suggestion       换一种格式能压小
        suggested_level  换成另一个档位能压小
    """

    def __init__(self, message, suggestion=None, suggested_level=None):
        super().__init__(message)
        self.suggestion = suggestion
        self.suggested_level = suggested_level


# ---------------------------------------------------------------------------
# 三档预设 → 有损编码的质量参数（0~100，越小体积越小、画质越低）。
# 键名供代码使用；界面上的中文名称（小体积 / 均衡 / 高画质）由界面层负责映射。
#
# 这组数值已用真实照片复核（1920x1200，原图本身约 q=90），实测占原图比例：
#     高画质 q=85 → 85.5%     均衡 q=75 → 71.6%     小体积 q=50 → 40.2%
# 高画质**没有**取 q=90：真实照片大多本来就压在 q≈90，再按 q=90 重存会得到
# 「节省 0.0%」，等于按钮按下去没反应。而 q=85 的像素偏差与 q=90 几乎相同
# （1.25 对 1.19，肉眼不可分），却能实打实省下 14.5%。
# 完整数据见 docs/research/image-compression-experiments.md。
# ---------------------------------------------------------------------------
LEVELS = {
    "small": 50,
    "balanced": 75,
    "high": 85,
}

# 输出格式 → 文件扩展名
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

# 支持透明通道的输出格式。JPEG 不支持，所以带透明度的图绝不能转成 JPEG。
ALPHA_CAPABLE = {"PNG", "WEBP"}

# 无损类源格式。这类格式「重新编码」几乎没有收益（实测 PNG optimize 通常只省 2~5%），
# 真正的空间在于换成有损格式。
LOSSLESS_FORMATS = {"PNG", "BMP", "GIF", "TIFF"}

# 能够给出合理结果的源格式
SUPPORTED_SOURCES = {"JPEG", "PNG", "WEBP", "BMP", "GIF", "TIFF"}

# 保格式的结果已经省下这么多时，就不再花时间去试别的格式了（WebP 编码很慢）。
SKIP_SUGGESTION_ABOVE = 0.60

# 别的格式至少要比当前结果再小这么多，才值得作为建议提出来。
SUGGESTION_MIN_GAIN = 0.15


@dataclass
class ImageInfo:
    """一张图片在压缩前的基本信息。"""

    path: str
    file_name: str
    format: str
    size_bytes: int
    width: int
    height: int
    mode: str
    has_alpha: bool


@dataclass
class Suggestion:
    """换成另一种格式能压得更小时的建议。由用户决定是否采纳。"""

    format: str
    size_bytes: int
    saved_ratio: float  # 相对当前结果还能再省的比例，0.4 表示还能再小 40%

    def describe(self) -> str:
        return f"改用 {self.format} 还能再小 {self.saved_ratio * 100:.0f}%"


@dataclass
class CompressResult:
    """一次压缩的结果。"""

    source: ImageInfo
    output_path: str
    output_size_bytes: int
    output_format: str
    suggestion: "Suggestion | None" = None

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


def _has_alpha(img) -> bool:
    """判断一张图是否带透明通道。"""
    if img.mode in ("RGBA", "LA", "PA"):
        return True
    # 调色板模式下，透明信息存在 info["transparency"] 里，mode 仍显示为 P
    if img.mode == "P" and "transparency" in img.info:
        return True
    return False


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
            mode = img.mode
            alpha = _has_alpha(img)
    except Exception as exc:
        raise CompressError(f"无法识别这个文件为图片：{exc}") from exc

    return ImageInfo(
        path=path,
        file_name=os.path.basename(path),
        format=fmt,
        size_bytes=os.path.getsize(path),
        width=width,
        height=height,
        mode=mode,
        has_alpha=alpha,
    )


# ---------------------------------------------------------------------------
# 编码：把图像变成字节，但不落盘
#
# 全程在内存里比较各方案，最后只把胜出者写出去，
# 这样来回比较不会在磁盘上留下一堆中间文件。
# ---------------------------------------------------------------------------

def _encode(img, fmt: str, level: str, lossless: bool = False):
    """按指定格式编码，返回字节；该格式装不下这张图时返回 None。

    不写日志、不抛异常 —— 它是个「尝试」，失败只是意味着这个候选方案不适用。
    """
    buf = io.BytesIO()
    try:
        if fmt == "JPEG":
            if _has_alpha(img):
                return None  # JPEG 不支持透明，硬转会丢掉透明信息
            kwargs = {"quality": LEVELS[level], "optimize": True, "progressive": True}
            # 手机竖拍的照片靠 EXIF 里的方向标记来正立显示。
            # 保存时不带过去，压缩后的照片就会躺倒。
            # 注意：只有确实存在时才传 —— 传 exif=None 并不会「表示没有 EXIF」，
            # Pillow 的 JPEG 插件会直接对 None 取长度而报错。
            exif = img.info.get("exif")
            if exif:
                kwargs["exif"] = exif
            img.convert("RGB").save(buf, format="JPEG", **kwargs)

        elif fmt == "PNG":
            # PNG 没有质量旋钮。optimize=True 让它多试几种压缩策略，
            # 只影响体积和耗时，不影响画质。
            img.save(buf, format="PNG", optimize=True)

        elif fmt == "WEBP":
            kwargs = {"lossless": True} if lossless else {"quality": LEVELS[level]}
            exif = img.info.get("exif")
            if exif:
                kwargs["exif"] = exif
            img.save(buf, format="WEBP", **kwargs)

        else:
            return None
    except Exception:
        return None
    return buf.getvalue()


def _looks_like_flat_graphic(img, info: ImageInfo, level: str) -> bool:
    """判断这张图是不是「扁平图形 / 界面截图」类内容。

    判据来自实测：把图按当前档位压成 JPEG，如果结果**比原文件还大**，
    说明这张图本来就压得极好 —— 大面积纯色、重复图案，JPEG 的块效应
    在色块边缘制造噪点，反而把文件撑大（实测真实 UI 截图达原图的 938%）。

    这个判据的前提是源文件本身是无损格式（PNG/BMP…），
    因为只有无损格式才代表「原图还没被压过」。

    为什么不用「颜色数」判断：实测插画有 26 万种颜色，
    夹在实拍照片的 9 万~55 万之间，根本分不开。
    """
    if info.format not in LOSSLESS_FORMATS:
        return False
    data = _encode(img, "JPEG", level)
    return data is not None and len(data) >= info.size_bytes


def _best_in_format(img, info: ImageInfo, fmt: str, level: str):
    """在**同一个格式内**挑最优参数，返回 (字节, 说明) 或 None。"""
    if fmt == "WEBP":
        lossy = _encode(img, "WEBP", level)
        if _looks_like_flat_graphic(img, info, level):
            lossless = _encode(img, "WEBP", level, lossless=True)
            if lossless is not None and (lossy is None or len(lossless) < len(lossy)):
                return lossless
        return lossy
    return _encode(img, fmt, level)


def _pick_target_format(info: ImageInfo, requested) -> str:
    """决定输出格式。

    requested 为 None 表示「保持源格式」—— 这是默认行为，
    因为用户没有要求转换格式时，程序不该擅自替他决定（见 requirement.md §7）。
    """
    if requested:
        fmt = requested.upper()
        if fmt not in EXTENSIONS:
            raise CompressError(f"不支持的输出格式：{requested}")
        if info.has_alpha and fmt not in ALPHA_CAPABLE:
            raise CompressError(
                f"这张图片带透明通道，不能保存成 {fmt} —— 透明部分会丢失。")
        return fmt

    # 保持源格式。源格式不在我们支持的输出里时，按有无透明通道挑一个安全的：
    # 宁可输出大一点的 PNG（保住透明），也不要默默丢掉透明信息。
    src = info.format
    if src in EXTENSIONS:
        return src
    return "PNG" if info.has_alpha else "JPEG"


def _alternatives_for(info: ImageInfo, exclude: str):
    """列出值得一试的替代输出格式（用于给出建议）。"""
    alts = []
    # 带透明的图一旦转成 JPEG 就会丢透明信息，所以这类图永远不把 JPEG 当建议
    if not info.has_alpha:
        alts.append("JPEG")
    alts.append("WEBP")
    return [a for a in alts if a != exclude]


def compress(src: str, dst: str, level: str = "balanced", output_format=None) -> CompressResult:
    """把 src 压缩后保存到 dst，返回压缩结果。

    level          三个档位之一，见 LEVELS
    output_format  目标格式；None 表示保持源格式（默认）

    如果无论如何都压不小，会抛 NoGainError —— 宁可不产出，也不给用户一个更大的文件。
    """
    if level not in LEVELS:
        raise CompressError(f"未知的压缩档位：{level!r}，可选：{'、'.join(LEVELS)}")

    info = read_info(src)

    if info.format not in SUPPORTED_SOURCES:
        raise CompressError(f"暂不支持 {info.format} 格式的图片。")

    # 原图安全：拒绝写到原图路径上。
    # 用 normcase 是为了在 Windows 上把大小写差异也算作同一个文件。
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
        raise CompressError("输出路径和原图是同一个文件，这会覆盖原图。请另选保存位置。")

    target = _pick_target_format(info, output_format)

    with Image.open(src) as img:
        img.load()  # 先解码一次，后面要反复编码，避免每次都重新解

        data = _best_in_format(img, info, target, level)
        if data is None:
            raise CompressError(f"这张图片无法保存成 {target} 格式。")

        # ---- 尝试替代格式：既用于「压不动时的兜底」，也用于生成建议 ----
        suggestion = None
        alts = _alternatives_for(info, target)
        if alts and (1 - len(data) / info.size_bytes) < SKIP_SUGGESTION_ABOVE:
            for alt in alts:
                ad = _best_in_format(img, info, alt, level)
                if ad is None:
                    continue
                if suggestion is None or len(ad) < suggestion.size_bytes:
                    suggestion = Suggestion(
                        format=alt,
                        size_bytes=len(ad),
                        saved_ratio=1 - len(ad) / len(data),
                    )

            # 只有「明显更小」才值得打扰用户
            if suggestion is not None and suggestion.saved_ratio < SUGGESTION_MIN_GAIN:
                suggestion = None

        # ---- 压不小就不产出（避免给用户一个更大的文件）----
        if len(data) >= info.size_bytes:
            if suggestion is not None and suggestion.size_bytes < info.size_bytes:
                raise NoGainError(
                    f"保持 {target} 格式压不小（原图 {info.size_bytes} 字节）。"
                    f"换成 {suggestion.format} 可以压到 {suggestion.size_bytes} 字节。",
                    suggestion=suggestion,
                )

            # 换格式也没用，那看看是不是档位选得太保守 ——
            # 有损编码很便宜（JPEG 约 6~130ms），多试两档的成本可以忽略。
            # 不实测就说「试试更激进的档位」是不负责任的猜测，所以这里真的去试。
            if target in ("JPEG", "WEBP"):
                for other in LEVELS:
                    if other == level:
                        continue
                    d2 = _best_in_format(img, info, target, other)
                    if d2 is not None and len(d2) < info.size_bytes:
                        raise NoGainError(
                            f"这张图在「{level}」档位下压不小，"
                            f"但换成「{other}」档可以压到 {len(d2)} 字节。",
                            suggested_level=other,
                        )

            raise NoGainError(
                f"这张图在「{level}」档位下压不小 —— 它很可能已经被压得很紧了，"
                "继续压缩不会让文件更小。"
            )

        # ---- 落盘 ----
        # 输出格式可能和 dst 的扩展名对不上（例如转成 WebP 却给了 .jpg），
        # 这里把扩展名修正过来，并把真正写出的路径回传给调用方。
        dst = os.path.splitext(dst)[0] + EXTENSIONS[target]
        dst_dir = os.path.dirname(os.path.abspath(dst))
        if not os.path.isdir(dst_dir):
            raise CompressError(f"保存位置不存在：{dst_dir}")

        try:
            with open(dst, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            raise CompressError(f"写入失败：{exc}") from exc

    return CompressResult(
        source=info,
        output_path=dst,
        output_size_bytes=len(data),
        output_format=target,
        suggestion=suggestion,
    )
