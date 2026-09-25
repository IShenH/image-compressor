# -*- coding: utf-8 -*-
"""自绘控件。

**为什么不用 ttk 控件**：设计稿要求圆角、渐变、描边图标 ——
`ttk.Style` 没有圆角选项，ttk 按钮也不支持渐变，一个都给不了。

**为什么仍然维持一套与 ttk 兼容的接口**：界面改版不该让既有测试失效。
45 项测试真正用到的是 `state()` / `cget("text")` / `configure(text=)` /
`winfo_ismapped()` 这几个方法，所以自绘控件把它们都实现一遍，
测试断言基本不用改 —— 这是刻意为之，不是巧合。
"""

import tkinter as tk

import ui_draw
import ui_theme as T


class Control(tk.Canvas):
    """自绘控件的公共部分：状态标记、悬停、与 ttk 兼容的 `state()`。"""

    def __init__(self, master, width, height, bg=None, **kw):
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        super().__init__(master, width=width, height=height,
                         bg=bg if bg is not None else T.CARD_BG, **kw)
        self._states = set()
        self.enabled = True
        self.hover = False
        self._pressed = False
        self._last_size = (0, 0)
        # 布局事件可能在子类属性就绪之前就触发，所以先立个闸
        self._ready = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", self._on_configure)

    def finish_init(self):
        """子类 __init__ 的最后一步：放开闸门并画第一次。"""
        self._ready = True
        self.redraw()

    # ---- 与 ttk 兼容 ----
    def state(self, spec=None):
        """无参数返回状态元组；传 ["disabled"] / ["!disabled"] 设置状态。"""
        if spec is None:
            return tuple(sorted(self._states))
        for item in spec:
            if item.startswith("!"):
                self._states.discard(item[1:])
            else:
                self._states.add(item)
        self.enabled = "disabled" not in self._states
        self.redraw()
        return tuple(sorted(self._states))

    # ---- 事件 ----
    def _on_enter(self, _e):
        if self._ready and self.enabled and not self.hover:
            self.hover = True
            self.redraw()

    def _on_leave(self, _e):
        if self._ready and self.hover:
            self.hover = False
            self.redraw()

    def _on_configure(self, _e):
        if not self._ready:
            return
        size = (self.winfo_width(), self.winfo_height())
        if size != self._last_size:
            self._last_size = size
            self.redraw()

    def size(self):
        """当前绘制尺寸。控件还没被布局时退回「请求尺寸」。"""
        w, h = self.winfo_width(), self.winfo_height()
        return (w if w > 1 else self.winfo_reqwidth(),
                h if h > 1 else self.winfo_reqheight())

    def redraw(self):
        raise NotImplementedError


class FlatButton(Control):
    """圆角按钮。primary 用竖向渐变，secondary 用浅色横向渐变。"""

    def __init__(self, master, text="", icon=None, kind="primary",
                 command=None, width=200, height=None, bg=None, **kw):
        h = T.M.btn_h if height is None else height
        super().__init__(master, width=width, height=h, bg=bg, **kw)
        self._text = text
        self._icon = icon
        self._kind = kind
        self._command = command
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.finish_init()

    # ---- 与 ttk 兼容 ----
    def cget(self, key):
        if key == "text":
            return self._text
        return super().cget(key)

    def configure(self, **kw):
        if "text" in kw:
            self._text = kw.pop("text")
            self.redraw()
        if "state" in kw:                       # tk 风格：configure(state="disabled")
            value = kw.pop("state")
            self.state([value] if value == "disabled" else ["!" + value])
        if kw:
            super().configure(**kw)

    config = configure

    def invoke(self):
        if self.enabled and self._command:
            self._command()

    def _on_press(self, _e):
        if self.enabled:
            self._pressed = True
            self.redraw()

    def _on_release(self, _e):
        was, self._pressed = self._pressed, False
        self.redraw()
        if was:
            self.invoke()

    def redraw(self):
        self.delete("all")
        w, h = self.size()
        m = T.M
        state = "normal" if self.enabled else "disabled"

        if self._kind == "primary":
            base = T.primary_button_image(w, h, state)
            fg = T.ON_PRIMARY if self.enabled else T.DISABLED_FG
            ico_size = m.s(30)
            font_ref = 22
        else:
            base = T.secondary_button_image(w, h, state)
            fg = T.PRIMARY if self.enabled else T.DISABLED_FG
            ico_size = m.s(30)
            font_ref = 20

        if self.hover and self.enabled:
            base = ui_draw.compose(
                [base, ui_draw.rounded_rect((w, h), m.radius_small,
                                            fill=(255, 255, 255, 28))])
        if self._pressed and self.enabled:
            base = ui_draw.compose(
                [base, ui_draw.rounded_rect((w, h), m.radius_small,
                                            fill=(0, 0, 0, 26))])

        self.create_image(0, 0, anchor="nw",
                          image=T.photo(base, key=("btn", self._kind, w, h, state,
                                                   self.hover, self._pressed)))

        font = T.font_obj(font_ref, bold=(self._kind == "primary"))
        text_w = font.measure(self._text) if self._text else 0
        ico_w = ico_size if self._icon else 0
        gap = m.s(14) if (ico_w and text_w) else 0
        x = (w - (ico_w + gap + text_w)) // 2

        if self._icon:
            glyph = ui_draw.icon(self._icon, ico_size, fg, stroke=1.7)
            self.create_image(x, h // 2, anchor="w",
                              image=T.photo(glyph, key=("btnico", self._icon,
                                                        ico_size, fg)))
            x += ico_w + gap
        if self._text:
            self.create_text(x, h // 2, anchor="w", text=self._text,
                             font=font, fill=fg)


class ChoiceCard(Control):
    """压缩档位卡片：整块可点，选中时换底色与边框色，左侧有单选圆点。"""

    def __init__(self, master, name, desc, icon, value, variable,
                 command=None, width=170, height=None, bg=None, **kw):
        h = T.M.card_h if height is None else height
        super().__init__(master, width=width, height=h,
                         bg=bg if bg is not None else T.CARD_BG, **kw)
        self._name = name
        self._desc = desc
        self._icon = icon
        self._value = value
        self._var = variable
        self._command = command
        self.bind("<Button-1>", self._on_click)
        variable.trace_add("write", lambda *_: self.redraw())
        self.finish_init()

    @property
    def selected(self):
        return self._var.get() == self._value

    def _on_click(self, _e):
        if not self.enabled:
            return
        self._var.set(self._value)          # trace 会触发重绘
        if self._command:
            self._command()

    def redraw(self):
        self.delete("all")
        w, h = self.size()
        m = T.M
        sel = self.selected

        base = T.choice_card_image(w, h, sel)
        self.create_image(0, 0, anchor="nw",
                          image=T.photo(base, key=("choice", w, h, sel)))

        pad = m.card_pad_x
        # 按卡片高度比例定位，而不是写死偏移 —— 卡片一改高，内容跟着居中
        top = int(round(h * 0.30))

        # 单选圆点
        rr = m.s(11)
        ring_color = T.PRIMARY if sel else "#BDB7AE"
        ring = ui_draw.rounded_rect((rr * 2, rr * 2), rr, outline=ring_color,
                                    width=2)
        self.create_image(pad, top - rr, anchor="nw",
                          image=T.photo(ring, key=("ring", rr, sel)))
        if sel:
            dot = ui_draw.rounded_rect((rr, rr), rr // 2, fill=T.PRIMARY)
            self.create_image(pad + rr // 2, top - rr // 2, anchor="nw",
                              image=T.photo(dot, key=("dot", rr)))

        # 名称
        name_font = T.font_obj(21, bold=True)
        self.create_text(pad + rr * 2 + m.s(12), top, anchor="w",
                         text=self._name, font=name_font,
                         fill=T.TEXT_STRONG if self.enabled else T.DISABLED_FG)

        # 右侧小图标
        ico_size = m.s(30)
        glyph = ui_draw.icon(self._icon, ico_size,
                             T.PRIMARY if sel else "#9A968E", stroke=1.7)
        self.create_image(w - pad, top, anchor="e",
                          image=T.photo(glyph, key=("cico", self._icon,
                                                    ico_size, sel)))

        # 说明（可能两行，按宽度手动折）
        desc_font = T.font_obj(18)
        for i, line in enumerate(self._wrap(self._desc, desc_font,
                                            w - pad * 2)):
            self.create_text(pad, int(round(h * 0.60)) + i * m.s(26), anchor="nw",
                             text=line, font=desc_font,
                             fill=T.TEXT_MUTED if self.enabled else T.DISABLED_FG)

    @staticmethod
    def _wrap(text, font, max_width):
        lines, cur = [], ""
        for ch in text:
            if font.measure(cur + ch) > max_width and cur:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        return lines[:2]


class FlatCheck(Control):
    """自绘复选框：圆角方框 + 对勾 + 右侧文字。"""

    def __init__(self, master, text="", variable=None, command=None,
                 width=200, height=None, bg=None, **kw):
        h = T.M.s(46) if height is None else height
        super().__init__(master, width=width, height=h,
                         bg=bg if bg is not None else T.CARD_BG, **kw)
        self._text = text
        self._var = variable if variable is not None else tk.BooleanVar(False)
        self._command = command
        self.bind("<Button-1>", self._on_click)
        self._var.trace_add("write", lambda *_: self.redraw())
        self.finish_init()

    def cget(self, key):
        if key == "text":
            return self._text
        return super().cget(key)

    def configure(self, **kw):
        if "text" in kw:
            self._text = kw.pop("text")
            self.redraw()
        if kw:
            super().configure(**kw)

    config = configure

    def toggle(self):
        if self.enabled:
            self._var.set(not self._var.get())
            if self._command:
                self._command()

    def _on_click(self, _e):
        self.toggle()

    def redraw(self):
        self.delete("all")
        w, h = self.size()
        m = T.M
        on = bool(self._var.get())
        box = m.s(34)

        fill = T.PRIMARY if on else "#FFFFFF"
        edge = T.PRIMARY if on else "#C6C0B7"
        if not self.enabled:
            fill, edge = ("#D5D1CA" if on else "#F0EDE8"), T.DISABLED_FG
        square = ui_draw.rounded_rect((box, box), m.s(8), fill=fill,
                                      outline=edge, width=1)
        self.create_image(0, (h - box) // 2, anchor="w",
                          image=T.photo(square, key=("chk", box, on, self.enabled)))
        if on:
            tick = ui_draw.icon("check", int(box * 0.62), T.ON_PRIMARY, stroke=2.2)
            self.create_image(box // 2, h // 2, anchor="center",
                              image=T.photo(tick, key=("tick", box)))
        if self._text:
            self.create_text(box + m.s(16), h // 2, anchor="w", text=self._text,
                             font=T.font_obj(19),
                             fill=T.TEXT if self.enabled else T.DISABLED_FG)


class NumberField(Control):
    """带上下箭头的数字输入框。

    箭头直接画在同一个 Canvas 上、用点击位置区分上下 ——
    比嵌两个子控件简单，也不会出现边框对不齐的问题。
    """

    def __init__(self, master, variable, width=110, height=None, step=100,
                 minimum=64, maximum=20000, command=None, bg=None, **kw):
        h = T.M.btn_h_small if height is None else height
        super().__init__(master, width=width, height=h,
                         bg=bg if bg is not None else T.CARD_BG, **kw)
        self._var = variable
        self.step = step
        self.minimum = minimum
        self.maximum = maximum
        self._command = command
        self.entry = tk.Entry(self, textvariable=variable, bd=0,
                              highlightthickness=0, bg="#FFFFFF",
                              disabledbackground=T.DISABLED_BG,
                              disabledforeground=T.DISABLED_FG,
                              justify="right", font=T.font_obj(19),
                              fg=T.TEXT, insertbackground=T.TEXT)
        self.bind("<Button-1>", self._on_click)
        self.finish_init()

    def _arrow_area(self):
        return T.M.s(30)

    def _on_click(self, event):
        if not self.enabled:
            return
        w, h = self.size()
        if event.x >= w - self._arrow_area():
            self.step_by(-1 if event.y > h // 2 else 1)

    def step_by(self, sign):
        try:
            value = int(float(self._var.get()))
        except (TypeError, ValueError):
            value = self.minimum
        value = max(self.minimum, min(self.maximum, value + sign * self.step))
        self._var.set(str(value))
        if self._command:
            self._command()

    def redraw(self):
        self.delete("all")
        w, h = self.size()
        m = T.M
        base = ui_draw.rounded_rect((w, h), m.radius_small,
                                    fill=T.DISABLED_BG if not self.enabled else "#FFFFFF",
                                    outline=T.BORDER, width=1)
        self.create_image(0, 0, anchor="nw", image=T.photo(base, key=("num", w, h, self.enabled)))

        arrow_w = self._arrow_area()
        field_w = w - arrow_w - m.s(8)
        self.entry.configure(state="disabled" if not self.enabled else "normal")
        self.create_window(m.s(8), h // 2, window=self.entry, anchor="w",
                           width=field_w, height=h - m.s(8))

        # 分隔线 + 上下箭头
        self.create_line(w - arrow_w, 0, w - arrow_w, h, fill=T.BORDER_SOFT)
        color = T.TEXT_MUTED if self.enabled else T.DISABLED_FG
        cxp = w - arrow_w // 2
        aw, ah = m.s(9), m.s(6)
        self.create_polygon(cxp - aw, h // 2 - ah - m.s(3), cxp + aw, h // 2 - ah - m.s(3),
                            cxp, h // 2 - m.s(3), fill=color)
        self.create_polygon(cxp - aw, h // 2 + ah + m.s(3), cxp + aw, h // 2 + ah + m.s(3),
                            cxp, h // 2 + m.s(3), fill=color)


# ---------------------------------------------------------------- 小工具

class ThinProgress(Control):
    """细进度条（不确定进度，来回滑动）。

    设计稿里没有它 —— 这是 M4 为「后台线程正在干活」加的状态反馈，不能因为改版丢掉。
    用自绘而不用 ttk.Progressbar，是为了颜色和圆角跟整页一致。
    """

    def __init__(self, master, width=200, height=None, bg=None, **kw):
        h = T.M.s(12) if height is None else height
        super().__init__(master, width=width, height=h, bg=bg, **kw)
        self._pos = 0.0
        self._job = None
        self.finish_init()

    def start(self, interval=14):
        if self._job is None:
            self._tick(interval)

    def stop(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick(self, interval):
        self._pos = (self._pos + 0.035) % 1.0
        self.redraw()
        self._job = self.after(interval, lambda: self._tick(interval))

    def redraw(self):
        self.delete("all")
        w, h = self.size()
        m = T.M
        track = ui_draw.rounded_rect((w, h), h // 2, fill="#E6E2DB")
        self.create_image(0, 0, anchor="nw", image=T.photo(track, key=("track", w, h)))
        bar_w = max(h * 2, int(w * 0.32))
        span = w + bar_w
        x = int(self._pos * span) - bar_w
        x0 = max(0, x)
        x1 = min(w, x + bar_w)
        if x1 > x0:
            bar = ui_draw.rounded_rect((x1 - x0, h), h // 2,
                                       fill=T.PRIMARY, corners=(
                                           x == x0, x + bar_w == x1,
                                           x + bar_w == x1, x == x0))
            self.create_image(x0, 0, anchor="nw",
                              image=T.photo(bar, key=("bar", x1 - x0, h, x0 == x,
                                                      x + bar_w == x1)))


def label(master, text="", size=19, bold=False, color=None, bg=None, **kw):
    return tk.Label(master, text=text, font=T.font_obj(size, bold),
                    fg=color or T.TEXT, bg=bg or T.CARD_BG,
                    bd=0, highlightthickness=0, **kw)


def icon_label(master, icon, ref_size=22, color=None, bg=None, **kw):
    """只有图标的小标签。"""
    px = T.M.s(ref_size)
    tint = color or T.TEXT
    glyph = ui_draw.icon(icon, px, tint, stroke=1.7)
    return tk.Label(master, image=T.photo(glyph, key=("labico", icon, px, tint)),
                    bg=bg or T.CARD_BG, bd=0, highlightthickness=0, **kw)
