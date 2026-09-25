# 图片压缩工具

一个**完全本地运行**的 Windows 图片压缩工具。图片不上传任何服务器，压缩全程在你自己的机器上完成。

同时它也是一个编程学习项目 —— 项目文档记录了每个技术选择背后的原因，以及踩过的坑。

## 它能做什么

- 把一张图片压小，并**如实告诉你压了多少**
- 三个档位：**小体积 / 均衡 / 高画质**
- 可选限制输出尺寸（**只缩小，不放大**）
- 压不小就明确报错，**绝不产出比原图更大的文件**
- 保持原格式时可以大幅压缩（PNG 走调色板减色）

可读取 JPEG / PNG / WebP / BMP / GIF / TIFF。

## 直接使用

双击 `dist/图片压缩工具.exe`，无需安装 Python。

> 首次启动会慢一两秒 —— 单文件打包的程序需要先把自己解压到临时目录。

## 从源码运行

需要 Python 3.13 和 Pillow：

```bash
pip install pillow
python main.py
```

## 自己打包

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --clean --paths . \
           --name 图片压缩工具 --distpath dist \
           --workpath build/gui_work --specpath build main.py
```

产物在 `dist/图片压缩工具.exe`（约 17 MB）。

## 运行测试

```bash
python -m unittest discover -s tests
```

测试样本图片全部**现场合成**，不依赖任何外部文件。

## 验证打包产物

用 `--windowed` 打的界面程序**没有任何控制台输出** —— 万一 Pillow 的 WebP 支持
（依赖额外 DLL）没被打包进去，界面照样能开，只是用到时候才失败，在开发机上完全看不出来。

所以另有一个冒烟测试程序，单独以控制台方式打包：

```bash
pyinstaller --onefile --console --clean --paths . \
           --name smoke --distpath build/smoke \
           --workpath build/smoke_work --specpath build tests/frozen_smoke.py

build/smoke/smoke.exe
```

它会打印编解码能力，并把每条编码路径实跑一遍。

## 一个容易误解的地方

**「压缩」不是一种统一的算法，而是两种完全不同的机制：**

- **有损**（JPEG / WebP）：丢掉人眼不敏感的高频细节，`quality` 就是这个「丢多少」的旋钮。
- **无损**（PNG / WebP 无损）：只寻找数据里的重复模式。压缩率完全取决于图片本身有多可预测。

由此推出两条容易被忽略的结论：

1. **对照片而言，PNG 几乎压不动**。照片存成 PNG 时重新优化只能省 2~5%，甚至可能变大 —— 因为 DEFLATE 已经对它生效过一遍了。
2. **对截图 / 插画而言，JPEG 会让文件变大**。实测把一张界面截图转成 JPEG，体积涨到原图的 938%。因为 JPEG 的块效应在大片纯色边缘制造噪点，反而增加了信息量。

本工具的做法是**不猜图片类型，直接比较各方案实际产出的体积**，取最小的那个。
详见 `docs/decisions/004-strategy-selection-mechanism.md`。

## 已知限制

- 只处理单张图片，没有批量
- 大图（4000×3000 以上）可能要等几秒，目前只有不确定进度的进度条
- 窗口约 840 px 高，1366×768 的小屏可能放不下

## 文档导航

| 文件 | 内容 |
| --- | --- |
| `docs/decisions/` | 重大选择的决策记录（ADR） |
| `docs/engineering/` | 系统当前如何组织 |
| `docs/research/` | 压缩实验的实测数据 |
| `docs/product/` | 产品需求 |
| `docs/learning/` | 开发过程中真正接触到的概念 |

## 版本

当前版本 **1.0.0**，变更记录见 `CHANGELOG.md`。
