# -*- coding: utf-8 -*-
"""MapleUtil 화면용 커스텀 위젯 — 둥근 버튼, 슬라이드 토글, 그라데이션 헤더, 진행 표시줄"""
import tkinter as tk


def hex_to_rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in rgb)


def lerp(c1, c2, t):
    """두 색 사이를 t(0~1) 비율로 섞는다 — 부드러운 색 전환용"""
    a, b = hex_to_rgb(c1), hex_to_rgb(c2)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    """캔버스에 둥근 사각형"""
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    """둥근 모서리 + 마우스 올릴 때 색이 서서히 변하는 버튼"""

    def __init__(self, parent, text, command, bg, hover, fg, width=112, height=40,
                 radius=12, bold=True, parent_bg=None,
                 dis_bg="#2a2d3a", dis_fg="#6b7085"):
        super().__init__(parent, width=width, height=height, bd=0, highlightthickness=0,
                         bg=parent_bg or parent["bg"])
        self.command = command
        self.base, self.hover_c, self.fg = bg, hover, fg
        self.dis_bg, self.dis_fg = dis_bg, dis_fg
        self._t = 0.0
        self._target = 0.0
        self._anim = None
        self._enabled = True
        self.shape = round_rect(self, 1, 1, width - 1, height - 1, radius,
                                fill=bg, outline="")
        self.label = self.create_text(width / 2, height / 2, text=text, fill=fg,
                                      font=("Malgun Gothic", 10, "bold" if bold else "normal"))
        self.bind("<Enter>", lambda e: self._to(1.0))
        self.bind("<Leave>", lambda e: self._to(0.0))
        self.bind("<Button-1>", self._click)
        self.configure(cursor="hand2")

    def _to(self, target):
        if not self._enabled:
            return
        self._target = target
        if self._anim is None:
            self._step()

    def _step(self):
        d = self._target - self._t
        if abs(d) < 0.06:
            self._t = self._target
            self._anim = None
        else:
            self._t += d * 0.35
            self._anim = self.after(16, self._step)
        self.itemconfigure(self.shape, fill=lerp(self.base, self.hover_c, self._t))

    def _click(self, _):
        if not self._enabled or not self.command:
            return
        self.itemconfigure(self.shape, fill=lerp(self.base, self.hover_c, 1.0))
        self.after(90, lambda: self.itemconfigure(
            self.shape, fill=lerp(self.base, self.hover_c, self._t)))
        self.command()

    def set_enabled(self, on, disabled_bg=None, disabled_fg=None):
        disabled_bg = disabled_bg or self.dis_bg
        disabled_fg = disabled_fg or self.dis_fg
        # 진행 중이던 호버 애니메이션을 멈춘다 (안 그러면 비활성 색을 덮어쓴다)
        if self._anim is not None:
            try:
                self.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None
        self._t = self._target = 0.0
        self._enabled = on
        self.configure(cursor="hand2" if on else "arrow")
        self.itemconfigure(self.shape, fill=self.base if on else disabled_bg)
        self.itemconfigure(self.label, fill=self.fg if on else disabled_fg)


class Toggle(tk.Frame):
    """슬라이드 애니메이션이 있는 on/off 스위치"""

    def __init__(self, parent, text, variable, command=None, bg="#171a23",
                 on_color="#4ade80", off_color="#3a3f4f", fg="#e6e8ef", muted="#8b90a3"):
        super().__init__(parent, bg=bg)
        self.var = variable
        self.command = command
        self.on_color, self.off_color = on_color, off_color
        self.fg, self.muted = fg, muted
        w, h, pad = 38, 21, 2      # 얇고 낮은 비율이 더 정돈돼 보인다
        self.w, self.h, self.pad = w, h, pad
        self.cv = tk.Canvas(self, width=w, height=h, bd=0, highlightthickness=0,
                            bg=bg, cursor="hand2")
        self.cv.pack(side="left")
        self.track = round_rect(self.cv, 0, 0, w, h, h / 2, fill=off_color, outline="")
        d = h - pad * 2
        self.knob = self.cv.create_oval(pad, pad, pad + d, pad + d,
                                        fill="#ffffff", outline="")
        self.lbl = tk.Label(self, text=text, bg=bg, fg=fg, cursor="hand2",
                            font=("Malgun Gothic", 9))
        self.lbl.pack(side="left", padx=(10, 0))
        for w_ in (self.cv, self.lbl):
            w_.bind("<Button-1>", self.toggle)
        self._pos = 1.0 if self.var.get() else 0.0
        self._target = self._pos
        self._anim = None
        self._render()

    def toggle(self, *_):
        self.var.set(not self.var.get())
        self._target = 1.0 if self.var.get() else 0.0
        if self._anim is None:
            self._step()
        if self.command:
            self.command()

    def _step(self):
        d = self._target - self._pos
        if abs(d) < 0.03:
            self._pos = self._target
            self._anim = None
        else:
            self._pos += d * 0.3
            self._anim = self.after(16, self._step)
        self._render()

    def _render(self):
        h, w, pad = self.h, self.w, self.pad
        d = h - pad * 2
        x = pad + self._pos * (w - h)      # 손잡이 이동 거리
        self.cv.coords(self.knob, x, pad, x + d, pad + d)
        self.cv.itemconfigure(self.track, fill=lerp(self.off_color, self.on_color, self._pos))
        self.lbl.configure(fg=self.fg if self._pos > 0.5 else self.muted)


class Dropdown(tk.Canvas):
    """둥근 모서리 + 직접 그린 목록 팝업을 쓰는 선택 상자.
    기본 콤보박스보다 다크 테마에 잘 맞고 항목 위에 마우스를 올리면 강조된다."""

    def __init__(self, parent, variable, values, width=150, height=32, radius=9,
                 bg="#212533", hover="#2a2f40", fg="#e6e8ef", muted="#8b90a3",
                 accent="#ff8a3d", parent_bg=None, editable=False, max_rows=12,
                 dis_bg="#1a1d27", dis_fg="#5f6478"):
        super().__init__(parent, width=width, height=height, bd=0, highlightthickness=0,
                         bg=parent_bg or parent["bg"])
        self.var, self.values = variable, list(values)
        self.bg, self.hover_c, self.fg, self.muted = bg, hover, fg, muted
        self.accent, self.max_rows = accent, max_rows
        self.dis_bg, self.dis_fg = dis_bg, dis_fg
        self.w, self.h = width, height
        self._enabled = True
        self._popup = None
        self.shape = round_rect(self, 1, 1, width - 1, height - 1, radius, fill=bg, outline="")
        self.text = self.create_text(12, height / 2, anchor="w", fill=fg,
                                     text=str(variable.get()), font=("Malgun Gothic", 9))
        self.arrow = self.create_text(width - 14, height / 2, text="▾", fill=muted,
                                      font=("Malgun Gothic", 9))
        self.configure(cursor="hand2")
        self.bind("<Enter>", lambda e: self._enabled and self.itemconfigure(self.shape, fill=hover))
        self.bind("<Leave>", lambda e: self._enabled and self.itemconfigure(self.shape, fill=bg))
        self.bind("<Button-1>", self._open)
        variable.trace_add("write", lambda *a: self.itemconfigure(self.text, text=str(self.var.get())))

    def set_enabled(self, on, disabled_bg=None, disabled_fg=None):
        disabled_bg = disabled_bg or self.dis_bg
        disabled_fg = disabled_fg or self.dis_fg
        self._enabled = on
        self.configure(cursor="hand2" if on else "arrow")
        self.itemconfigure(self.shape, fill=self.bg if on else disabled_bg)
        self.itemconfigure(self.text, fill=self.fg if on else disabled_fg)
        self.itemconfigure(self.arrow, fill=self.muted if on else disabled_fg)

    def _close(self, *_):
        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:
                pass
            self._popup = None

    def _open(self, _=None):
        if not self._enabled:
            return
        if self._popup is not None:
            self._close()
            return
        top = tk.Toplevel(self)
        self._popup = top
        top.overrideredirect(True)
        top.configure(bg=self.accent)
        rows = min(len(self.values), self.max_rows)
        row_h = 26
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.h + 4
        top.geometry(f"{self.w}x{rows * row_h + 2}+{x}+{y}")

        wrap = tk.Frame(top, bg=self.bg)
        wrap.pack(fill="both", expand=True, padx=1, pady=1)
        canvas = tk.Canvas(wrap, bg=self.bg, bd=0, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=self.bg)
        canvas.create_window(0, 0, anchor="nw", window=inner, width=self.w - 2)
        if len(self.values) > rows:
            sb = tk.Scrollbar(wrap, command=canvas.yview, width=8)
            sb.pack(side="right", fill="y")
            canvas.configure(yscrollcommand=sb.set)
            canvas.bind_all("<MouseWheel>",
                            lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        for v in self.values:
            lb = tk.Label(inner, text=v, bg=self.bg, fg=self.fg, anchor="w", padx=12,
                          font=("Malgun Gothic", 9), cursor="hand2")
            lb.pack(fill="x", ipady=3)
            lb.bind("<Enter>", lambda e, w=lb: w.configure(bg=self.hover_c, fg=self.accent))
            lb.bind("<Leave>", lambda e, w=lb: w.configure(bg=self.bg, fg=self.fg))
            lb.bind("<Button-1>", lambda e, val=v: (self.var.set(val), self._close()))
        inner.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))
        top.bind("<FocusOut>", self._close)
        top.bind("<Escape>", self._close)
        top.focus_set()


class GradientHeader(tk.Canvas):
    """좌우 그라데이션 헤더 (아바타 + 제목)"""

    def __init__(self, parent, title, subtitle, c1, c2, avatar=None, height=78):
        super().__init__(parent, height=height, bd=0, highlightthickness=0, bg=c1)
        self.c1, self.c2 = c1, c2
        self.title, self.subtitle, self.avatar = title, subtitle, avatar
        self.height_ = height
        self.bind("<Configure>", self._draw)

    def _draw(self, _=None):
        self.delete("all")
        w = max(self.winfo_width(), 1)
        steps = 64
        for i in range(steps):
            x1 = w * i / steps
            x2 = w * (i + 1) / steps + 1
            self.create_rectangle(x1, 0, x2, self.height_,
                                  fill=lerp(self.c1, self.c2, i / (steps - 1)), outline="")
        tx = 18
        if self.avatar is not None:
            self.create_image(20, self.height_ / 2, image=self.avatar, anchor="w")
            tx = 20 + self.avatar.width() + 14
        self.create_text(tx, self.height_ / 2 - 11, text=self.title, anchor="w",
                         fill="#ffffff", font=("Malgun Gothic", 17, "bold"))
        self.create_text(tx, self.height_ / 2 + 13, text=self.subtitle, anchor="w",
                         fill="#ffe6d2", font=("Malgun Gothic", 9))


class ActivityBar(tk.Canvas):
    """작업 중일 때 흐르는 진행 표시줄"""

    def __init__(self, parent, bg, color, height=3):
        super().__init__(parent, height=height, bd=0, highlightthickness=0, bg=bg)
        self.color = color
        self.h = height
        self.bar = None
        self.pos = 0.0
        self.running = False

    def start(self):
        if self.running:
            return
        self.running = True
        self.pos = -0.25
        if self.bar is None:
            self.bar = self.create_rectangle(0, 0, 0, self.h, fill=self.color, outline="")
        self._step()

    def stop(self):
        self.running = False
        if self.bar is not None:
            self.coords(self.bar, 0, 0, 0, self.h)

    def _step(self):
        if not self.running:
            return
        w = max(self.winfo_width(), 1)
        seg = w * 0.28
        x = self.pos * w
        self.coords(self.bar, x, 0, x + seg, self.h)
        self.pos += 0.012
        if self.pos > 1.05:
            self.pos = -0.28
        self.after(16, self._step)
