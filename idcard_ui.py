# -*- coding: utf-8 -*-
"""
身份证 A4 打印工具 —— 现代化界面（CustomTkinter + 拖拽）

- 文件列表支持从资源管理器直接拖入照片 / 文件夹
- 右侧大预览：处理完成后显示 A4 排版首页（自适应窗口大小）
- 手动四角微调窗口同样是现代样式
"""

import os
import re
import sys
import queue
import threading
import tempfile
import traceback
import webbrowser
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox

import numpy as np
from PIL import Image, ImageDraw, ImageTk

import customtkinter as ctk
from tkinterdnd2 import TkinterDnD, DND_FILES

import idcard2a4 as core


def default_out_dir():
    """默认输出目录：始终是程序自己所在目录下的「输出」，不受“从哪里打开”影响。"""
    try:
        base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    except Exception:
        base = os.getcwd()
    return os.path.join(base, "输出")


# ------------------------------------------------------------------ 视觉规范
BG = "#F2F4F8"
CARD = "#FFFFFF"
ACCENT = "#2F6BF5"
ACCENT_HOVER = "#1F55D6"
ACCENT_SOFT = "#EAF0FF"
TEXT = "#1B1F26"
MUTED = "#79818E"
LINE = "#E5E9F0"
FONT = "Microsoft YaHei UI"

STATUS_STYLE = {
    "待处理": ("#EFF2F7", "#6B7280"),
    "处理中": ("#EAF0FF", "#2F6BF5"),
    "成功": ("#E7F7EE", "#0E8A4C"),
    "补齐": ("#E8F3FF", "#1F6FB2"),
    "粗略": ("#FFF4E0", "#A9660A"),
    "未识别": ("#FFF1E6", "#B2541A"),
    "手动": ("#EAF0FF", "#2F6BF5"),
    "失败": ("#FDECEC", "#C23B32"),
}


def res_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, name)
    return p if os.path.exists(p) else None


def ui_font(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


def blank_page(w, h):
    """空白 A4 纸（带两个卡位提示）"""
    W, H = max(100, int(w)), max(100, int(h))
    im = Image.new("RGB", (W, H), "#FFFFFF")
    d = ImageDraw.Draw(im)
    cw = W * 0.62
    ch = cw / 1.5852
    cx = (W - cw) / 2
    for k in (0, 1):
        cy = H * (0.17 + 0.33 * k)
        d.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=5, fill="#F2F4F8",
                            outline="#E3E8F0", width=1)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=3, outline="#EEF1F6", width=1)
    return im


def chip(parent, text, kind="待处理"):
    fg, tc = STATUS_STYLE.get(kind, STATUS_STYLE["待处理"])
    return ctk.CTkLabel(parent, text=text, font=ui_font(11), text_color=tc,
                        fg_color=fg, corner_radius=9, height=22, width=64)


# ------------------------------------------------------------------ 手动四角
class QuadEditor(ctk.CTkToplevel):
    """在照片上按顺序点四个角，实时预览矫正结果"""

    def __init__(self, master, path, on_apply, max_w=900, max_h=560):
        super().__init__(master)
        self.title("手动框选四角 - " + os.path.basename(path))
        self.configure(fg_color=BG)
        self.transient(master)
        self.on_apply = on_apply
        self.path = path

        bgr = core.read_image(path)
        self.bgr = bgr
        H, W = bgr.shape[:2]
        self.scale = min(1.0, max_w / float(W), max_h / float(H))
        disp = core.cv2.resize(bgr, (max(1, int(W * self.scale)), max(1, int(H * self.scale))))
        self.disp = disp

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(head, text="手动框选四角", font=ui_font(16, "bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkLabel(head, text="拖动圆点贴着卡的四条边微调；也可「清空」后按 左上→右上→右下→左下 点四个角",
                     font=ui_font(11), text_color=MUTED).pack(side="left", padx=12)

        body = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 6))

        self.canvas = tk.Canvas(body, width=self.disp.shape[1], height=self.disp.shape[0],
                                bg="#20232A", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(side="left", padx=12, pady=12)
        self.photo = ImageTk.PhotoImage(Image.fromarray(self.disp[:, :, ::-1]))
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        side = ctk.CTkFrame(body, fg_color="transparent")
        side.pack(side="left", fill="both", expand=True, padx=(0, 12), pady=12)
        ctk.CTkLabel(side, text="矫正预览", font=ui_font(12, "bold"),
                     text_color=TEXT).pack(anchor="w")
        # 用 Canvas 画预览：图片怎么换都不会碎，也不受 ctk 图片回收的影响
        self.pv = tk.Canvas(side, bg="#F4F6FB", highlightthickness=0, bd=0)
        self.pv.pack(fill="both", expand=True, pady=(6, 0))
        self._pv_img = None
        self._last_card = None
        self.pv.bind("<Configure>", self._on_pv_resize)
        self._pv_text("点满四个角后\n这里显示矫正结果")
        self._pv_ready = True

        try:
            auto_quad = core.detect_quad_auto(bgr)
        except Exception:
            auto_quad = None
        if auto_quad is None:
            try:
                auto_quad, _ = core.detect_card(bgr)
            except Exception:
                auto_quad = None
        self.auto_quad = auto_quad
        if auto_quad is not None:
            pts = (np.asarray(auto_quad, np.float32) * self.scale).astype(np.int32).ravel().tolist()
            self.canvas.create_polygon(pts, outline="#38D6D6", width=2, dash=(6, 4), fill="")
            # 直接把自动识别的四角放上去，可以拖动微调
            self.points = [(float(p[0]) * self.scale, float(p[1]) * self.scale) for p in auto_quad]
        else:
            self.points = []
        self._drag = -1
        self._pv_job = None
        self._recenter_job = None
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self._redraw()
        self.after(140, self._raise_up)

        self.hint = ctk.CTkLabel(self, text="", font=ui_font(11), text_color=MUTED,
                                 anchor="w", justify="left")
        self.hint.pack(fill="x", padx=18, pady=(0, 2))

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(foot, text="撤消一点", width=96, height=34, corner_radius=9,
                      fg_color="#E9EDF4", hover_color="#DEE3EC", text_color=TEXT,
                      font=ui_font(12), command=self.undo).pack(side="left")
        ctk.CTkButton(foot, text="清空", width=72, height=34, corner_radius=9,
                      fg_color="#E9EDF4", hover_color="#DEE3EC", text_color=TEXT,
                      font=ui_font(12), command=self.clear).pack(side="left", padx=8)
        ctk.CTkButton(foot, text="取消", width=72, height=34, corner_radius=9,
                      fg_color="transparent", border_width=1, border_color=LINE,
                      text_color=MUTED, hover_color="#F2F4F8",
                      font=ui_font(12), command=self.destroy).pack(side="right")
        self.apply_btn = ctk.CTkButton(foot, text="应用并重新生成", width=148, height=36,
                                       corner_radius=9, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                       font=ui_font(12, "bold"), command=self.apply)
        self.apply_btn.pack(side="right", padx=8)

        self._center(master)

    def _center(self, master):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - w) // 2
            y = master.winfo_rooty() + (master.winfo_height() - h) // 2
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

    def _raise_up(self):
        try:
            self.lift()
            self.attributes("-topmost", True)
            self.after(400, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except Exception:
            pass

    def _clamp_pt(self, x, y):
        """把鼠标坐标夹在画布内，角点永远不会被拖出画面看不见。"""
        w = max(3, self.disp.shape[1] - 2)
        h = max(3, self.disp.shape[0] - 2)
        return (float(min(max(x, 1), w)), float(min(max(y, 1), h)))

    def _on_click(self, ev):
        # 点在已有角点附近 -> 拖动它；否则按"点一个加一个"的顺序加点
        for i, (x, y) in enumerate(self.points):
            if abs(ev.x - x) <= 16 and abs(ev.y - y) <= 16:
                self._drag = i
                return
        if len(self.points) >= 4:
            return
        self.points.append(self._clamp_pt(ev.x, ev.y))
        self._redraw()

    def _on_drag(self, ev):
        if 0 <= self._drag < len(self.points):
            self.points[self._drag] = self._clamp_pt(ev.x, ev.y)
            self._redraw()

    def _on_release(self, ev):
        self._drag = -1

    def _redraw(self):
        self.canvas.delete("mark")
        for i, (x, y) in enumerate(self.points):
            self.canvas.create_oval(x - 7, y - 7, x + 7, y + 7, fill=ACCENT,
                                    outline="white", width=2, tags="mark")
            self.canvas.create_text(x + 16, y - 15, text=str(i + 1), fill="white",
                                    font=(FONT, 9, "bold"), tags="mark")
        if len(self.points) == 4:
            self.canvas.create_polygon([c for p in self.points for c in p],
                                       outline="#FF5A5A", width=2, fill="", tags="mark")
        self._update_preview()

    def _pv_text(self, text):
        """预览区显示一句提示文字（居中）。"""
        self._pv_msg = text
        try:
            self.pv.delete("all")
            self._pv_img = None
            self._last_card = None
            w = self.pv.winfo_width()
            h = self.pv.winfo_height()
            if w <= 1:
                w, h = 320, 240
            self.pv.create_text(w // 2, h // 2, text=text, fill=MUTED,
                                font=(FONT, 10), justify="center")
        except Exception:
            pass

    def _on_pv_resize(self, ev=None):
        # 窗口刚开时画布还没定型，延后再重画一次，避免缩在左下角
        try:
            if getattr(self, "_recenter_job", None):
                self.after_cancel(self._recenter_job)
        except Exception:
            pass
        self._recenter_job = self.after(80, self._recenter)

    def _recenter(self):
        self._recenter_job = None
        if self._last_card is not None:
            self._show_card(self._last_card)
        elif getattr(self, "_pv_msg", ""):
            self._pv_text(self._pv_msg)

    def _update_preview(self):
        """右侧预览：拖动时先快速给个矫正图，停手 0.4 秒后换成真正处理结果。"""
        if len(self.points) != 4:
            self._pv_text("点满四个角后\n这里显示矫正结果")
            return
        quad = np.array([[x / self.scale, y / self.scale]
                         for x, y in self.points], np.float32)
        try:
            q = core._sanitize_quad(quad, self.bgr.shape[1], self.bgr.shape[0], manual=True)
        except Exception:
            q = None
        # 校验不过也先按用户点的四角出个图，绝不只留一句"太小"
        used = quad if q is None else q
        ok = False
        try:
            try:
                card, _ = core.warp_card_soft(self.bgr, used)
            except Exception:
                card, _ = core.warp_to_card(self.bgr, used)
            ok = self._show_card(card)
        except Exception:
            ok = False
        if not ok and self._pv_img is None:
            self._pv_text("这个框没法矫正\n请贴着卡的四个角重点")
        # 停手后再算一次完整结果（含切边、摆正），和最终出图一致
        try:
            if self._pv_job:
                self.after_cancel(self._pv_job)
        except Exception:
            pass
        self._pv_job = self.after(420, self._full_preview)

    def _show_card(self, card):
        """把矫正后的卡画到预览区（等比缩放居中）。成功返回 True。"""
        try:
            if not self.winfo_exists():
                return False
        except Exception:
            return False
        self._last_card = card
        self._pv_msg = ""
        try:
            cw = self.pv.winfo_width()
            ch = self.pv.winfo_height()
            if cw <= 1 or ch <= 1:
                cw, ch = 420, 300
            cw, ch = max(cw - 14, 60), max(ch - 14, 40)
            sc = min(cw / float(card.shape[1]), ch / float(card.shape[0]))
            tw = max(1, int(card.shape[1] * sc))
            th = max(1, int(card.shape[0] * sc))
            small = core.cv2.resize(card, (tw, th))
            self._pv_img = ImageTk.PhotoImage(Image.fromarray(small[:, :, ::-1]))
            self.pv.delete("all")
            self.pv.create_image(cw // 2 + 7, ch // 2 + 7, image=self._pv_img,
                                 anchor="center")
            return True
        except Exception:
            traceback.print_exc()
            return False

    def _full_preview(self):
        self._pv_job = None
        if len(self.points) != 4:
            return
        try:
            quad = np.array([[x / self.scale, y / self.scale]
                             for x, y in self.points], np.float32)
            q = core._sanitize_quad(quad, self.bgr.shape[1], self.bgr.shape[0], manual=True)
            if q is None:
                if self._pv_img is None:
                    self._pv_text("四个角几乎重合或围得太小\n把圆点拖到卡片的四个角上")
                return
            items = core.process_image(self.path, quad_override=q)
            if items:
                self._show_card(items[0]['card'])
        except Exception:
            traceback.print_exc()

    def undo(self):
        if self.points:
            self.points.pop()
            self._redraw()

    def clear(self):
        del self.points[:]
        self._hint("")
        self._redraw()

    def apply(self):
        if len(self.points) != 4:
            self._hint("请按 左上→右上→右下→左下 的顺序点满四个角", bad=True)
            return
        raw = np.array([[x / self.scale, y / self.scale] for x, y in self.points], np.float32)
        try:
            quad = core._sanitize_quad(raw, self.bgr.shape[1], self.bgr.shape[0], manual=True)
        except Exception:
            quad = None
        if quad is None:
            self._hint("四个角几乎重合或者围得太小，把这四个圆点拖到卡片的四个角上再点「应用」", bad=True)
            return
        if self._pv_job:
            try:
                self.after_cancel(self._pv_job)
            except Exception:
                pass
            self._pv_job = None
        self.destroy()
        try:
            self.on_apply(quad)
        except Exception:
            traceback.print_exc()

    def _hint(self, text, bad=False):
        """对话框里的即时提示（不弹窗、不会被挡住）。"""
        try:
            self.hint.configure(text=text, text_color="#D64545" if bad else MUTED)
        except Exception:
            pass

    def _toast(self, text):
        t = ctk.CTkToplevel(self)
        t.title("提示")
        t.configure(fg_color=CARD)
        t.resizable(False, False)
        ctk.CTkLabel(t, text=text, font=ui_font(12), text_color=TEXT,
                     wraplength=320).pack(padx=26, pady=22)
        ctk.CTkButton(t, text="知道了", width=96, height=32, corner_radius=9,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, font=ui_font(12),
                      command=t.destroy).pack(pady=(0, 18))
        t.transient(self)
        t.grab_set()

# ------------------------------------------------------------------ 主界面
class App(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self):
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        super().__init__(fg_color=BG)

        self.TkdndVersion = None
        try:
            self.TkdndVersion = TkinterDnD._require(self)
        except Exception:
            traceback.print_exc()
        self.title("身份证 / 银行卡 A4 打印工具 v%s" % core.APP_VERSION)
        # 只固定控件缩放（字体、按钮大小一致），窗口尺寸交给 Win32 最大化，
        # 这样不管是 100% / 125% / 150% 缩放，界面都不会被挤掉。
        try:
            ctk.set_widget_scaling(1.0)
            ctk.set_window_scaling(1.0)
        except Exception:
            pass
        self.geometry("1180x760+60+40")
        self.minsize(1000, 700)
        self.after(150, self._fit)

        ico = res_path("app.ico")
        if ico:
            try:
                self.iconbitmap(ico)
            except Exception:
                pass

        self.files = []
        self.rows = {}
        self.manual_quads = {}
        self.flips = {}
        self.result = None
        self._q = queue.Queue()
        self._preview_img = None
        self._pv_ref = None
        self._pv_job = None
        self._busy = False
        self._pending_start = False
        self._tune_job = None

        self._build_header()
        self._build_body()
        self._build_status()

        self._render_preview()

    # ---------------------------------------------------------------- 顶部
    def _work_area(self):
        """屏幕可用区（物理像素，已排除任务栏）。拿不到就退回 tk 的屏幕尺寸。"""
        try:
            import ctypes
            from ctypes import wintypes
            r = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
                if r.right - r.left > 300 and r.bottom - r.top > 300:
                    return r.left, r.top, r.right - r.left, r.bottom - r.top
        except Exception:
            pass
        return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def _client_physical(self):
        """窗口客户区的物理像素尺寸 (w, h)；失败返回 None。"""
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            hwnd = u.GetAncestor(int(self.winfo_id()), 2)
            cr = wintypes.RECT()
            u.GetClientRect(hwnd, ctypes.byref(cr))
            if cr.right > 50 and cr.bottom > 50:
                return cr.right, cr.bottom
        except Exception:
            pass
        return None

    def _fit(self):
        """直接最大化：由 Windows 自己把窗口铺满屏幕可用区（不进任务栏、不超屏）。
        这样不管 100% / 125% / 150% 缩放，参数区和按钮永远都在屏内。"""
        try:
            self.state("zoomed")
            return
        except Exception:
            pass
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w, h = max(1120, min(sw - 24, 1680)), max(720, min(sh - 96, 1120))
            self.geometry(f"{int(w)}x{int(h)}+{max(0, int((sw - w) / 2))}"
                          f"+{max(0, int((sh - h) / 2) - 8)}")
        except Exception:
            pass
    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=74)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_propagate(False)

        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left", padx=20, pady=12)
        badge = ctk.CTkLabel(left, text="ID", width=38, height=38, corner_radius=10,
                             fg_color=ACCENT, text_color="#FFFFFF", font=ui_font(15, "bold"))
        badge.pack(side="left", padx=(0, 10))
        tbox = ctk.CTkFrame(left, fg_color="transparent")
        tbox.pack(side="left")
        ctk.CTkLabel(tbox, text="身份证 / 银行卡 A4 打印工具", font=ui_font(19, "bold"),
                     text_color=TEXT, anchor="w", height=24).pack(anchor="w")
        ctk.CTkLabel(tbox, text="手机拍的身份证、银行卡 → 自动抠卡摆正 → 真实尺寸一键 A4 打印",
                     font=ui_font(11), text_color=MUTED, anchor="w", height=18).pack(anchor="w")

        ctk.CTkButton(head, text="关于", width=60, height=26, corner_radius=9,
                      font=ui_font(11), fg_color="#F0F3F8", hover_color="#E3E8F0",
                      text_color="#59616D", border_width=0,
                      command=self._open_about).pack(side="right", padx=(0, 16))

        ctk.CTkLabel(head, text="全部本地电脑处理，无需联网", font=ui_font(11),
                     text_color="#3F8F62", fg_color="#E7F7EE", corner_radius=10,
                     height=26, padx=12).pack(side="right", padx=(0, 10))

        line = ctk.CTkFrame(self, fg_color=LINE, height=1, corner_radius=0)
        line.grid(row=0, column=0, sticky="sew")

    # ---------------------------------------------------------------- 关于
    def _open_about(self):
        win = ctk.CTkToplevel(self)
        win.title("关于")
        win.configure(fg_color=CARD)
        win.resizable(False, False)
        ico = res_path("app.ico")
        if ico:
            try:
                win.iconbitmap(ico)
            except Exception:
                pass
        w, h = 470, 266
        try:
            x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - h) // 3)
        except Exception:
            x = y = 0
        win.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

        top = ctk.CTkFrame(win, fg_color="transparent")
        top.pack(fill="x", padx=26, pady=(24, 0))
        ctk.CTkLabel(top, text="ID", width=46, height=46, corner_radius=12,
                     fg_color=ACCENT, text_color="#FFFFFF",
                     font=ui_font(17, "bold")).pack(side="left")
        box = ctk.CTkFrame(top, fg_color="transparent")
        box.pack(side="left", padx=14)
        ctk.CTkLabel(box, text="身份证 / 银行卡 A4 打印工具", font=ui_font(16, "bold"),
                     text_color=TEXT, height=24).pack(anchor="w")
        ctk.CTkLabel(box, text="版本 v%s · 300 DPI · 单文件绿色版" % core.APP_VERSION,
                     font=ui_font(11), text_color=MUTED, height=18).pack(anchor="w")

        ctk.CTkFrame(win, fg_color=LINE, height=1, corner_radius=0).pack(
            fill="x", padx=26, pady=(18, 16))

        info = ctk.CTkFrame(win, fg_color="transparent")
        info.pack(fill="x", padx=26)
        info.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(info, text="开 发 者", font=ui_font(12), text_color=MUTED,
                     width=64, anchor="w", height=26).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(info, text=core.APP_AUTHOR, font=ui_font(12, "bold"),
                     text_color=TEXT, anchor="w", height=26).grid(row=0, column=1, sticky="w")

        ctk.CTkLabel(info, text="GitHub", font=ui_font(12), text_color=MUTED,
                     width=64, anchor="w", height=26).grid(row=1, column=0, sticky="w",
                                                           pady=(6, 0))
        link = tk.Label(info, text=core.APP_REPO, bg=CARD, fg=ACCENT,
                        font=(FONT, 11, "underline"), cursor="hand2", anchor="w")
        link.grid(row=1, column=1, sticky="w", pady=(6, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open(core.APP_REPO))

        ctk.CTkLabel(win, text="程序全部在本机运行，不联网、不上传任何照片。",
                     font=ui_font(11), text_color="#9AA1AC", anchor="w",
                     height=20).pack(fill="x", padx=26, pady=(14, 0))

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=26, pady=(18, 0))

        def copy_link():
            try:
                self.clipboard_clear()
                self.clipboard_append(core.APP_REPO)
            except Exception:
                pass
            b_copy.configure(text="已复制")
            win.after(1400, lambda: b_copy.configure(text="复制链接"))

        b_close = ctk.CTkButton(btns, text="关闭", width=84, height=32, corner_radius=9,
                                font=ui_font(12), fg_color="#F0F3F8", hover_color="#E3E8F0",
                                text_color="#59616D", command=win.destroy)
        b_close.pack(side="right")
        b_copy = ctk.CTkButton(btns, text="复制链接", width=92, height=32, corner_radius=9,
                               font=ui_font(12), fg_color="transparent", hover_color="#EEF2F7",
                               text_color=MUTED, border_width=1, border_color=LINE,
                               command=copy_link)
        b_copy.pack(side="right", padx=(0, 10))
        ctk.CTkButton(btns, text="打开 GitHub", width=112, height=32, corner_radius=9,
                      font=ui_font(12, "bold"), fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      text_color="#FFFFFF",
                      command=lambda: webbrowser.open(core.APP_REPO)).pack(side="right",
                                                                            padx=(0, 10))

        win.transient(self)
        win.lift()
        try:
            win.focus_force()
        except Exception:
            pass

    # ---------------------------------------------------------------- 主体
    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=18, pady=14)
        body.grid_columnconfigure(0, weight=3, minsize=560)
        body.grid_columnconfigure(1, weight=2, minsize=380)
        body.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        lcol = ctk.CTkFrame(body, fg_color="transparent", width=600)
        lcol.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        lcol.grid_propagate(False)
        left = ctk.CTkFrame(lcol, fg_color="transparent", width=600)
        left.pack(fill="both", expand=True)

        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=14)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_left(left)
        self._build_right(right)

    def _build_left(self, parent):
        # 左列改用 grid：列表占中间可伸缩，参数和按钮固定贴底，任何窗口高度都不会被挤掉
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        # 拖拽区
        self.drop = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14,
                                 border_width=2, border_color="#C9D6F0", height=84)
        self.drop.grid(row=0, column=0, sticky="ew")

        inner = ctk.CTkFrame(self.drop, fg_color="transparent")
        inner.pack(expand=True, padx=18, pady=4)
        self.plus = ctk.CTkLabel(inner, text="＋", width=44, height=44, corner_radius=22,
                                 fg_color=ACCENT_SOFT, text_color=ACCENT,
                                 font=ui_font(26, "bold"))
        self.plus.pack(side="left", padx=(0, 14))
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left")
        self.drop_title = ctk.CTkLabel(txt, text="把照片拖到这里，或点击选择文件",
                                       font=ui_font(15, "bold"), text_color=TEXT)
        self.drop_title.pack(anchor="w")
        ctk.CTkLabel(txt, text="支持 JPG/PNG/BMP/WEBP/TIFF，可多次拖入",
                     font=ui_font(11), text_color=MUTED).pack(anchor="w", pady=(2, 0))

        for w in (self.drop, inner, self.plus, txt, self.drop_title):
            w.bind("<Button-1>", lambda e: self.pick_files())

        self._dnd_widgets = [self.drop, inner, self.plus, txt, self.drop_title]
        self._bind_dnd()

        # 列表标题
        bar = ctk.CTkFrame(parent, fg_color="transparent", height=30)
        bar.grid(row=1, column=0, sticky="ew", pady=(8, 2))
        ctk.CTkLabel(bar, text="照片列表", font=ui_font(13, "bold"), text_color=TEXT,
                     height=24).pack(side="left")
        self.count_chip = ctk.CTkLabel(bar, text="0 张", font=ui_font(11),
                                       text_color=MUTED, fg_color="#E9EDF4",
                                       corner_radius=9, height=22, width=54)
        self.count_chip.pack(side="left", padx=8)
        ctk.CTkButton(bar, text="选择文件", width=84, height=28, corner_radius=8,
                      fg_color="transparent", border_width=1, border_color=LINE,
                      text_color=MUTED, hover_color="#F2F4F8", font=ui_font(11),
                      command=self.pick_files).pack(side="right")
        ctk.CTkButton(bar, text="清空", width=56, height=28, corner_radius=8,
                      fg_color="transparent", text_color=MUTED, hover_color="#F2F4F8",
                      font=ui_font(11), command=self.clear_files).pack(side="right", padx=6)

        self.lwrap = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, height=150)
        self.listbox = ctk.CTkScrollableFrame(self.lwrap, fg_color=CARD, corner_radius=14,
                                              scrollbar_button_color="#D6DCE6",
                                              scrollbar_button_hover_color="#BFC7D4")
        self._dnd_widgets.append(self.listbox)
        self._bind_dnd()
        self._render_list()

        # 底部整块（参数 + 开始按钮）先占好位置，列表再吃掉剩下的空间
        btm = ctk.CTkFrame(parent, fg_color="transparent")
        btm.grid(row=3, column=0, sticky="ew", pady=(10, 0))

        # 参数
        opt = ctk.CTkFrame(btm, fg_color=CARD, corner_radius=14)
        opt.pack(fill="x")
        ctk.CTkLabel(opt, text="输出参数", font=ui_font(13, "bold"), text_color=TEXT,
                     height=22).grid(row=0, column=0, sticky="w", padx=16, pady=(8, 2))
        opt.grid_columnconfigure(0, weight=1)
        opt.grid_columnconfigure(1, weight=1)

        grid = ctk.CTkFrame(opt, fg_color="transparent")
        grid.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 10))
        for c in range(5):
            grid.grid_columnconfigure(c, weight=0, minsize=52)
        grid.grid_columnconfigure(1, weight=1)
        grid.grid_columnconfigure(3, weight=1)

        def lab(r, c, text):
            ctk.CTkLabel(grid, text=text, font=ui_font(11), text_color=MUTED,
                         height=20, anchor="w").grid(row=r, column=c, sticky="w",
                                                     padx=(0, 8), pady=2)

        self.fmt_var = tk.StringVar(value="A4 打印页")
        self.layout_var = tk.StringVar(value="自动排版")
        self.gap_var = tk.StringVar(value="10")
        self.corner_var = tk.StringVar(value="3.18")
        self.rot_var = tk.StringVar(value="自动")
        self.clean_var = tk.BooleanVar(value=True)
        self.marks_var = tk.BooleanVar(value=False)

        lab(0, 0, "输出内容"); lab(0, 2, "排版方式")
        ctk.CTkOptionMenu(grid, values=["A4 打印页", "证件图片", "A4+图片"],
                          variable=self.fmt_var,
                          width=136, height=32, corner_radius=9, font=ui_font(12),
                          fg_color="#F4F6FB", button_color="#E4E9F3",
                          button_hover_color="#D6DCE8", text_color=TEXT,
                         dropdown_font=ui_font(12)).grid(row=0, column=1, sticky="w",
                                                         padx=(0, 14), pady=4)
        self.layout_menu = ctk.CTkOptionMenu(
            grid, values=["自动（推荐）", "每张单独一页", "两张一页（正反面）",
                          "八张一页"], variable=self.layout_var,
            width=136, height=32, corner_radius=9, font=ui_font(12),
            fg_color="#F4F6FB", button_color="#E4E9F3",
            button_hover_color="#D6DCE8", text_color=TEXT,
            dropdown_font=ui_font(12))
        self.layout_menu.grid(row=0, column=3, sticky="w", pady=4)

        lab(1, 0, "卡片间距 mm"); lab(1, 2, "圆角半径 mm")
        ctk.CTkEntry(grid, textvariable=self.gap_var, width=136, height=32,
                     corner_radius=9, font=ui_font(12), fg_color="#F4F6FB",
                     border_color=LINE, text_color=TEXT).grid(row=1, column=1, sticky="w",
                                                              padx=(0, 14), pady=4)
        self.corner_entry = ctk.CTkEntry(grid, textvariable=self.corner_var, width=136, height=32,
                     corner_radius=9, font=ui_font(12), fg_color="#F4F6FB",
                     border_color=LINE, text_color=TEXT)
        self.corner_entry.grid(row=1, column=3, sticky="w", pady=4)

        sw = ctk.CTkFrame(grid, fg_color="transparent")
        sw.grid(row=2, column=0, columnspan=5, sticky="ew", pady=4)
        ctk.CTkSwitch(sw, text="自动增强", variable=self.clean_var, font=ui_font(11),
                      text_color=TEXT, progress_color=ACCENT, width=32).pack(side="left")
        ctk.CTkButton(sw, text="亮度/对比度默认", width=118, height=26, corner_radius=8,
                      fg_color="#F4F6FB", hover_color="#E7EBF4", text_color=MUTED,
                      border_width=1, border_color=LINE, font=ui_font(11),
                      command=self.reset_tune).pack(side="right")

        tune = ctk.CTkFrame(grid, fg_color="transparent")
        tune.grid(row=3, column=0, columnspan=5, sticky="ew", pady=4)
        tune.grid_columnconfigure(0, weight=1)
        tune.grid_columnconfigure(1, weight=1)

        self.bright_var = tk.DoubleVar(value=0.0)
        self.contrast_var = tk.DoubleVar(value=1.0)

        fb = ctk.CTkFrame(tune, fg_color="transparent")
        fb.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ctk.CTkLabel(fb, text="亮度", font=ui_font(11), text_color=MUTED,
                     width=34, anchor="w").pack(side="left")
        ctk.CTkSlider(fb, from_=-80, to=80, number_of_steps=32, variable=self.bright_var,
                      width=80, height=18, corner_radius=9, progress_color=ACCENT,
                      button_color=ACCENT, button_hover_color=ACCENT_HOVER,
                      command=lambda v: self._on_tune()).pack(side="left", fill="x",
                                                             expand=True, padx=6)
        self.bright_lbl = ctk.CTkLabel(fb, text="0", font=ui_font(11), text_color=MUTED,
                                       width=30, anchor="e")
        self.bright_lbl.pack(side="left")

        fc = ctk.CTkFrame(tune, fg_color="transparent")
        fc.grid(row=0, column=1, sticky="ew")
        ctk.CTkLabel(fc, text="对比度", font=ui_font(11), text_color=MUTED,
                     width=44, anchor="w").pack(side="left")
        ctk.CTkSlider(fc, from_=0.6, to=1.8, number_of_steps=24, variable=self.contrast_var,
                      width=80, height=18, corner_radius=9, progress_color=ACCENT,
                      button_color=ACCENT, button_hover_color=ACCENT_HOVER,
                      command=lambda v: self._on_tune()).pack(side="left", fill="x",
                                                             expand=True, padx=6)
        self.contrast_lbl = ctk.CTkLabel(fc, text="1.00", font=ui_font(11), text_color=MUTED,
                                         width=36, anchor="e")
        self.contrast_lbl.pack(side="left")


        lab(4, 0, "输出目录")
        self.out_var = tk.StringVar(value=default_out_dir())
        row3 = ctk.CTkFrame(grid, fg_color="transparent")
        row3.grid(row=4, column=1, columnspan=4, sticky="ew", pady=(4, 0))
        row3.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(row3, textvariable=self.out_var, height=32, corner_radius=9,
                     font=ui_font(12), fg_color="#F4F6FB", border_color=LINE,
                     text_color=TEXT).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(row3, text="浏览…", width=76, height=32, corner_radius=9,
                      fg_color="#E9EDF4", hover_color="#DEE3EC", text_color=TEXT,
                      font=ui_font(12), command=self.pick_out).grid(row=0, column=1, padx=(8, 0))

        # 底部按钮
        act = ctk.CTkFrame(btm, fg_color="transparent")
        act.pack(fill="x", pady=(10, 0))
        act.grid_columnconfigure(0, weight=1)
        self.run_btn = ctk.CTkButton(act, text="开始处理并生成 A4", height=46, corner_radius=11,
                                     font=ui_font(14, "bold"), fg_color=ACCENT,
                                     hover_color=ACCENT_HOVER, command=self.start)
        self.run_btn.pack(side="left", fill="x", expand=True)
        self.manual_btn = None
        # 列表最后 pack：让它占满中间剩余空间，参数区和按钮永远贴在底部可见
        self.lwrap.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        self.listbox.pack(fill="both", expand=True, padx=2, pady=2)

    def _build_right(self, parent):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 6))
        ctk.CTkLabel(head, text="预览", font=ui_font(15, "bold"), text_color=TEXT,
                     height=24).pack(side="left")
        ctk.CTkLabel(head, text="A4 · 300 DPI · 真实尺寸 85.6×54mm", font=ui_font(11),
                     text_color="#4A5568", fg_color="#EEF1F6", corner_radius=9,
                     height=24, padx=10).pack(side="left", padx=10)

        self.pv_box = ctk.CTkFrame(parent, fg_color="#EDEFF4", corner_radius=12, height=360)
        self.pv_box.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self.pv_img = ctk.CTkLabel(self.pv_box, text="", fg_color="transparent")
        self.pv_img.place(relx=0.5, rely=0.5, anchor="center")
        self.pv_hint = ctk.CTkLabel(parent, text="添加照片后点「开始处理」，这里会显示 A4 排版预览\n双击图片可直接打开生成的文件",
                                    font=ui_font(11), text_color=MUTED, justify="center")
        self.pv_hint.pack(pady=(0, 6))
        self.pv_img.bind("<Double-Button-1>", lambda e: self.open_result())

        foot = ctk.CTkFrame(parent, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(0, 14))
        # 按钮先占位（靠右），信息文字最后再放，避免长文件名把按钮挤出窗口
        self.print_btn = ctk.CTkButton(foot, text="打印", width=72, height=30,
                                       corner_radius=9, fg_color="#E9EDF4",
                                       hover_color="#DEE3EC", text_color=TEXT,
                                       font=ui_font(11, "bold"),
                                       state="disabled", command=self.print_result)
        self.print_btn.pack(side="right")
        self.open_btn = ctk.CTkButton(foot, text="打开 PDF", width=88, height=30,
                                      corner_radius=9, fg_color=ACCENT,
                                      hover_color=ACCENT_HOVER, font=ui_font(11),
                                      state="disabled", command=self.open_result)
        self.open_btn.pack(side="right", padx=8)
        self.open_dir_btn = ctk.CTkButton(foot, text="打开目录", width=84, height=30,
                                          corner_radius=9, fg_color="#E9EDF4",
                                          hover_color="#DEE3EC", text_color=TEXT,
                                          font=ui_font(11), state="disabled",
                                          command=self.open_dir)
        self.open_dir_btn.pack(side="right", padx=(0, 8))
        self.info_lbl = ctk.CTkLabel(foot, text="尚未生成文件", font=ui_font(11),
                                     text_color=MUTED, anchor="w")
        self.info_lbl.pack(side="left", fill="x", expand=True)
        self.pv_box.bind("<Configure>", self._on_resize)

    def _build_status(self):
        bar = ctk.CTkFrame(self, fg_color="transparent", height=34)
        bar.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 10))
        self.progress = ctk.CTkProgressBar(bar, width=160, height=6, corner_radius=3,
                                           progress_color=ACCENT, mode="determinate")
        self.progress.set(0)
        self.progress.pack(side="left")
        self.status_lbl = ctk.CTkLabel(bar, text="就绪", font=ui_font(11), text_color=MUTED)
        self.status_lbl.pack(side="left", padx=10)
        if not self.TkdndVersion:
            self.status_lbl.configure(text="提示：拖拽组件未加载，可点「选择文件」添加照片",
                                      text_color="#A9660A")

    # ---------------------------------------------------------------- 拖拽
    def _bind_dnd(self):
        """重新注册拖拽目标。每次落盘后都重注册一次，保证可以连续多次拖入。"""
        if not self.TkdndVersion:
            return
        for w in getattr(self, "_dnd_widgets", []):
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
                w.dnd_bind("<<DropEnter>>", self.on_drag_in)
                w.dnd_bind("<<DropLeave>>", self.on_drag_out)
            except Exception:
                pass
        try:
            if not getattr(self, "_root_dnd", False):
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self.on_drop)
                self._root_dnd = True
        except Exception:
            pass

    def on_drag_in(self, event=None):
        self.drop.configure(border_color=ACCENT, fg_color="#F5F8FF")
        return event.action if hasattr(event, "action") else "copy"

    def on_drag_out(self, event=None):
        self.drop.configure(border_color="#C9D6F0", fg_color=CARD)
        return event.action if hasattr(event, "action") else "copy"

    def on_drop(self, event):
        self.on_drag_out()
        raw = event.data or ""
        try:
            paths = list(self.tk.splitlist(raw))
        except Exception:
            paths = [raw.strip("{}")]
        try:
            self.add_paths([p for p in paths if p])
        except Exception as e:
            self.status_lbl.configure(text=f"添加失败：{e}", text_color="#C23B32")
        finally:
            # 关键：某些环境下拖放后注册会失效，落盘后立刻重注册，实现连续拖入
            self.after(30, self._bind_dnd)
        return "copy"

    def pick_files(self):
        files = filedialog.askopenfilenames(
            title="选择证件照片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff"),
                       ("所有文件", "*.*")])
        if files:
            self.add_paths(list(files))

    def pick_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_var.set(d)

    # ---------------------------------------------------------------- 文件
    def add_paths(self, paths):
        found = core.collect_files(paths)
        new = [f for f in found if f not in self.files]
        skipped = len(paths) - len({p for p in paths if os.path.isdir(p)} | set(found))
        if not found:
            self.status_lbl.configure(text="没有识别到图片文件（支持 JPG/PNG/BMP/WEBP/TIFF）",
                                      text_color="#C23B32")
            return
        self.files.extend(new)
        self._render_list()
        msg = f"已添加 {len(new)} 张照片" if new else "这些照片已经在列表里了"
        if skipped > 0:
            msg += f"，忽略 {skipped} 个不支持的文件"
        self.status_lbl.configure(text=msg, text_color=MUTED if new else "#A9660A")

    def clear_files(self):
        self.files = []
        self.rows = {}
        self.manual_quads = {}
        self.flips = {}
        self._render_list()
        self.status_lbl.configure(text="列表已清空", text_color=MUTED)

    def remove_file(self, path):
        self.files = [f for f in self.files if f != path]
        self.manual_quads.pop(path, None)
        self.manual_quads.pop(os.path.normcase(os.path.abspath(path)), None)
        self.flips.pop(path, None)
        self.flips.pop(os.path.normcase(os.path.abspath(path)), None)
        self._render_list()

    def move_file(self, path, delta):
        """上下调序：调整列表顺序（也就是 A4 上的排列顺序），有结果就按新顺序重排。"""
        if self._busy:
            self.status_lbl.configure(text="正在处理中，稍等再调顺序", text_color="#A9660A")
            return
        try:
            i = self.files.index(path)
        except ValueError:
            return
        j = i + int(delta)
        if j < 0 or j >= len(self.files):
            return
        self.files[i], self.files[j] = self.files[j], self.files[i]
        self._render_list()
        name = os.path.basename(path)
        pos = f"第 {j + 1} 位"
        if self.result:
            self.status_lbl.configure(text=f"「{name}」已移到{pos}，按新顺序重新生成中…",
                                      text_color="#2F6BF5")
            self.start()
        else:
            self.status_lbl.configure(text=f"「{name}」已移到{pos}", text_color="#2F6BF5")

    def flip_file(self, path):
        """方向不对：每点一次按 0→90→180→270 循环转，转完立即重新生成。"""
        key = os.path.normcase(os.path.abspath(path))
        cur = self.flips.get(key) or 0
        nxt = (int(cur) + 90) % 360
        self.flips[path] = nxt
        self.flips[key] = nxt
        if nxt == 0:
            self.flips.pop(path, None)
            self.flips.pop(key, None)
        self._render_list()
        if self.result and not self._busy:
            self.status_lbl.configure(
                text=f"「{os.path.basename(path)}」手动转到 {nxt}°，重新生成中…",
                text_color="#2F6BF5")
            self.start()
        else:
            self.status_lbl.configure(
                text=f"「{os.path.basename(path)}」将按 {nxt}° 输出（再点一次继续转 90°）",
                text_color="#2F6BF5")

    def _render_list(self):
        for w in self.listbox.winfo_children():
            w.destroy()
        self.rows = {}

        if not self.files:
            empty = ctk.CTkFrame(self.listbox, fg_color="transparent", height=18)
            empty.pack(fill="x", expand=True, pady=2)
            ctk.CTkLabel(empty, text="还没有照片", font=ui_font(13, "bold"),
                         text_color="#9AA2AF").pack()
            ctk.CTkLabel(empty, text="把照片拖到上面的区域，或点击「选择文件」",
                         font=ui_font(11), text_color="#AEB5C0").pack(pady=(4, 0))
        else:
            for i, f in enumerate(self.files, 1):
                self._make_row(i, f)

        self.count_chip.configure(text=f"{len(self.files)} 张")

    def _make_row(self, idx, path):
        row = ctk.CTkFrame(self.listbox, fg_color="transparent", height=42)
        row.pack(fill="x", pady=2)
        # 右侧按钮先占位，文件名最后放（长了就自动被裁掉，不会把按钮挤没）
        ctk.CTkButton(row, text="✕", width=26, height=26, corner_radius=8, fg_color="transparent",
                      hover_color="#FDECEC", text_color="#B9C0CC", font=ui_font(12),
                      command=lambda p=path: self.remove_file(p)).pack(side="right", padx=(0, 2))
        # 上下调序：↑↓ 两个小箭头（第一位不能上移、最后一位不能下移）
        first = idx <= 1
        last = idx >= len(self.files)
        ctk.CTkButton(row, text="▼", width=22, height=26, corner_radius=7, fg_color="transparent",
                      hover_color=BG if last else "#E9EEF8",
                      text_color="#D5DAE2" if last else "#8C94A3", font=ui_font(11),
                      command=(lambda p=path: None) if last else (lambda p=path: self.move_file(p, 1))
                      ).pack(side="right", padx=(0, 1))
        ctk.CTkButton(row, text="▲", width=22, height=26, corner_radius=7, fg_color="transparent",
                      hover_color=BG if first else "#E9EEF8",
                      text_color="#D5DAE2" if first else "#8C94A3", font=ui_font(11),
                      command=(lambda p=path: None) if first else (lambda p=path: self.move_file(p, -1))
                      ).pack(side="right", padx=(0, 1))
        rev = self.flips.get(path) or self.flips.get(
            os.path.normcase(os.path.abspath(path))) or 0
        st = chip(row, "待处理")
        st.pack(side="right", padx=(4, 6))
        self.rows[path] = st
        ctk.CTkButton(row, text=f"方向 {int(rev)}°" if rev else "方向",
                      width=62, height=26, corner_radius=8,
                      fg_color="#EEF1F6" if not rev else "#E4EDFF",
                      hover_color="#E2E7F0",
                      text_color=TEXT if not rev else "#2F6BF5",
                      font=ui_font(11), command=lambda p=path: self.flip_file(p)).pack(
            side="right", padx=(0, 4))
        ctk.CTkButton(row, text="四角", width=40, height=26, corner_radius=8,
                      fg_color="#EEF1F6", hover_color="#E2E7F0", text_color=TEXT,
                      font=ui_font(11), command=lambda p=path: self.open_manual(p)).pack(
            side="right", padx=(0, 4))
        ctk.CTkLabel(row, text=str(idx), width=26, height=26, corner_radius=8,
                     fg_color="#EEF1F6", text_color=MUTED, font=ui_font(11)).pack(
            side="left", padx=(4, 8))
        name = os.path.basename(path)
        if len(name) > 40:
            name = name[:34] + "…" + os.path.splitext(name)[1]
        ctk.CTkLabel(row, text=name, font=ui_font(12), text_color=TEXT,
                     anchor="w").pack(side="left", fill="x", expand=True)
    # ---------------------------------------------------------------- 参数
    def _on_tune(self):
        """亮度/对比度滑块变化：更新数值显示；已有结果时自动重算预览。"""
        try:
            b = float(self.bright_var.get())
            c = float(self.contrast_var.get())
        except Exception:
            return
        self.bright_lbl.configure(text=f"{b:+.0f}")
        self.contrast_lbl.configure(text=f"{c:.2f}")
        if self.result and not self._busy:
            if self._tune_job:
                try:
                    self.after_cancel(self._tune_job)
                except Exception:
                    pass
            self._tune_job = self.after(320, self._retune)

    def _retune(self):
        self._tune_job = None
        if not self._busy and self.files:
            self.start()

    def reset_tune(self):
        """亮度回到 0、对比度回到 1.00，并重新生成。"""
        self.bright_var.set(0.0)
        self.contrast_var.set(1.0)
        self.bright_lbl.configure(text="0")
        self.contrast_lbl.configure(text="1.00")
        if self._tune_job:
            try:
                self.after_cancel(self._tune_job)
            except Exception:
                pass
            self._tune_job = None
        if self.files and not self._busy:
            self.status_lbl.configure(text="亮度/对比度已恢复默认", text_color="#2F6BF5")
            self.start()

    def _opts(self):
        fmt_map = {"仅 A4 打印页（PDF）": "a4", "每张证件一张图片": "image", "A4 + 证件图片": "both"}
        layout_map = {"自动（推荐）": "auto", "每张单独一页": "single",
                      "两张一页（正反面）": "pair", "八张一页": "grid"}
        try:
            gap = float(self.gap_var.get())
        except Exception:
            gap = 10.0
        try:
            corner = float(self.corner_var.get())
        except Exception:
            corner = core.CORNER_R_MM
        out = self.out_var.get().strip() or default_out_dir()
        return dict(out_dir=out, fmt=fmt_map.get(self.fmt_var.get(), "a4"),
                    layout=layout_map.get(self.layout_var.get(), "auto"),
                    rotate=self.rot_var.get(), gap_mm=gap, corner_mm=corner,
                    clean=bool(self.clean_var.get()), marks=bool(self.marks_var.get()),
                    bright=float(self.bright_var.get()),
                    contrast=float(self.contrast_var.get()),
                    flips=dict(self.flips))

    def start(self):
        if self._busy:
            return
        if not self.files:
            self.status_lbl.configure(text="请先添加照片", text_color="#C23B32")
            return
        opts = self._opts()
        self._busy = True
        self.run_btn.configure(state="disabled", text="正在处理…")
        self.manual_btn = None
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        for p in self.files:
            if p in self.rows:
                self.rows[p].configure(text="处理中", fg_color=STATUS_STYLE["处理中"][0],
                                       text_color=STATUS_STYLE["处理中"][1])
        self.status_lbl.configure(text=f"正在处理 {len(self.files)} 张照片…", text_color=MUTED)

        t = threading.Thread(target=self._worker, args=(list(self.files), dict(opts),
                                                        dict(self.manual_quads)), daemon=True)
        t.start()
        self._poll()

    def _poll(self):
        try:
            while True:
                item = self._q.get_nowait()
                if item[0] == "log":
                    self._on_log(item[1])
                else:
                    self._done(item[1], item[2], item[3])
        except queue.Empty:
            pass
        if self._busy:
            self.after(120, self._poll)

    def _on_log(self, line):
        m = re.search(r"\[(\d+)/(\d+)\]", line)
        if m:
            i, n = int(m.group(1)), int(m.group(2))
            try:
                self.progress.set(max(0.02, min(0.98, i / float(n))))
            except Exception:
                pass
            self.status_lbl.configure(text=f"正在处理第 {i}/{n} 张…", text_color=MUTED)

    def _worker(self, files, opts, quads):
        try:
            pdir = os.path.join(tempfile.gettempdir(), "idcard2a4_print")
            try:
                if os.path.isdir(pdir):
                    for fn in os.listdir(pdir):
                        if fn.lower().endswith(".png"):
                            os.remove(os.path.join(pdir, fn))
            except Exception:
                pass
            res, rep = core.run_batch(files, opts["out_dir"], layout=opts["layout"],
                                      rotate=opts["rotate"], clean=opts["clean"],
                                      gap_mm=opts["gap_mm"], marks=opts["marks"],
                                      fmt=opts["fmt"], corner_mm=opts["corner_mm"],
                                      log=lambda s: self._q.put(("log", s)), quads=quads,
                                      flips=opts.get("flips"),
                                      bright=opts.get("bright", 0.0),
                                      contrast=opts.get("contrast", 1.0),
                                      print_dir=pdir)
            err = None
        except Exception as e:
            res, rep, err = None, [], str(e)
            traceback.print_exc()
        self._q.put(("done", res, rep, err))

    def _done(self, res, rep, err):
        self._busy = False
        self.progress.configure(mode="determinate")
        self.run_btn.configure(state="normal", text="开始处理并生成 A4")

        if err or not res:
            self.progress.set(0)
            reason = (err or "未知错误")
            self.status_lbl.configure(text="处理失败：" + reason[:120], text_color="#C23B32")
            for p in self.files:
                if p in self.rows:
                    self.rows[p].configure(text="失败", fg_color=STATUS_STYLE["失败"][0],
                                           text_color=STATUS_STYLE["失败"][1])
            out = self.out_var.get().strip() or default_out_dir()
            log_path = ""
            try:
                log_path = os.path.join(tempfile.gettempdir(), "身份证A4打印_错误日志.txt")
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 输出目录={out}\n"
                             f"文件={self.files}\n错误={reason}\n{traceback.format_exc()}\n")
            except Exception:
                log_path = ""
            tip = (f"原因：\n{reason[:600]}\n\n"
                   f"输出目录：{out}\n\n"
                   "建议：\n"
                   "1）把「输出目录」换成桌面之类确定可写的文件夹再试；\n"
                   "2）确认照片没被其他程序占用（例如还在微信/QQ 里预览）；\n"
                   "3）若提示找不到模块或图片坏了，换一张照片试。")
            if log_path:
                tip += f"\n\n详细日志：{log_path}"
            try:
                messagebox.showerror("处理失败", tip, parent=self)
            except Exception:
                pass
            self._maybe_pending()
            return

        self.progress.set(1)
        self.result = res

        # 更新每张状态
        bad_edge = 0
        patched = 0
        for name, state, note in rep:
            for path in self.files:
                if os.path.basename(path) == name and path in self.rows:
                    if state != "成功":
                        kind = "失败"
                    elif "内容补齐" in note:
                        kind = "补齐"
                        patched += 1
                    elif ("未检测到卡片边缘" in note or "整图裁切" in note):
                        kind = "未识别"
                        bad_edge += 1
                    elif ("粗略" in note or "整图" in note):
                        kind = "粗略"
                    elif ("手动" in note or path in self.manual_quads):
                        kind = "手动"
                    else:
                        kind = "成功"
                    fg, tc = STATUS_STYLE[kind]
                    txt = {"成功": "精确", "粗略": "粗略", "手动": "手动", "补齐": "已补齐",
                           "未识别": "未识别", "失败": "失败"}[kind]
                    self.rows[path].configure(text=txt, fg_color=fg, text_color=tc)

        ok = res.get("cards") or res["count"]
        bits = [f"{ok} 张卡处理完成"]
        if res["pages"]:
            bits.append(f"A4 {res['pages']} 页")
        if res["images"]:
            bits.append(f"独立图片 {len(res['images'])} 张")
        if self.manual_quads:
            n_manual = len({os.path.normcase(os.path.abspath(k))
                            for k in self.manual_quads})
            bits.append(f"手动框选 {n_manual} 张")
        if patched:
            bits.append(f"自动补齐 {patched} 张")
        self.status_lbl.configure(text="完成！" + " · ".join(bits), text_color="#0E8A4C")
        if bad_edge:
            # 这些卡多半没拍全（一边被画面切掉），整图压缩出来是废的，直接提示重拍或手动框
            tip_dlg = (f"有 {bad_edge} 张没找到卡片边缘，卡面信息也认不全，没法自动补齐。\n\n"
                       "建议：\n"
                       "1）重拍一张：卡要完整入镜，四周留一点空；\n"
                       "2）或点该行的「四角」，手动贴着卡边点四个角。")
            try:
                messagebox.showwarning("有照片没识别到卡边", tip_dlg, parent=self)
            except Exception:
                pass

        # 预览
        prev = res.get("preview")
        if prev and os.path.exists(prev):
            try:
                img = Image.open(prev)
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGBA")
                    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
                    bg.alpha_composite(img)
                    img = bg
                self._preview_img = img.convert("RGB")
                self._render_preview()
                self.pv_hint.configure(text="双击图片打开生成的文件 · 缩放窗口预览会跟着变大变小")
            except Exception:
                traceback.print_exc()

        info = f"{ok} 张 · 300 DPI"
        if res.get("pdf"):
            info += " · " + os.path.basename(res["pdf"])
        self.info_lbl.configure(text=info)
        self.open_btn.configure(state="normal")
        self.print_btn.configure(state="normal") if res.get("pdf") else None
        self.open_dir_btn.configure(state="normal")

        bad = [r for r in rep if r[1] != "成功"]
        if bad:
            why = (bad[0][2] or "未知原因").strip()
            if len(why) > 46:
                why = why[:46] + "…"
            self.status_lbl.configure(
                text=self.status_lbl.cget("text") + f"（{len(bad)} 张失败：{bad[0][0]} → {why}）",
                text_color="#A9660A")
        self._maybe_pending()

    def _maybe_pending(self):
        """处理过程中用户又改了四角：这批结束后自动按新四角再算一次。"""
        if getattr(self, "_pending_start", False) and self.files:
            self._pending_start = False
            self.after(120, self.start)

    # ---------------------------------------------------------------- 预览
    def _on_resize(self, event=None):
        if self._pv_job:
            try:
                self.after_cancel(self._pv_job)
            except Exception:
                pass
        self._pv_job = self.after(140, self._render_preview)

    def _render_preview(self):
        self._pv_job = None
        if getattr(self, "pv_hint", None) is None:
            return
        try:
            s = float(ctk.ScalingTracker.get_widget_scaling(self))
        except Exception:
            s = 1.0
        s = s if s > 0.3 else 1.0
        # 统一换算成屏幕像素（CTkImage 的 size 是逻辑尺寸，会被缩放比放大）
        box_w = max(160, self.pv_box.winfo_width() - 40)
        box_h = max(160, self.pv_box.winfo_height() - 34)

        src = self._preview_img
        if src is None:
            ratio = 1.4142
            if box_h / float(box_w) < ratio:
                h = box_h - 8
                w = int(h / ratio)
            else:
                w = box_w - 8
                h = int(w * ratio)
            page = blank_page(w, h)
        else:
            page = src.copy()
            page.thumbnail((box_w - 34, box_h - 40), Image.LANCZOS)

        pw, ph = page.size
        pad_x, pad_y = 12, 10
        bg = Image.new("RGB", (pw + pad_x * 2 + 6, ph + pad_y * 2 + 6), "#EDEFF4")
        d = ImageDraw.Draw(bg)
        for dx, dy, col in [(6, 8, "#DCE1EA"), (4, 5, "#E4E9F1"), (2, 3, "#EAEEF5")]:
            d.rounded_rectangle([pad_x + dx, pad_y + dy, pad_x + pw + dx, pad_y + ph + dy],
                                radius=4, fill=col)
        bg.paste(page, (pad_x, pad_y))

        bw, bh = bg.size
        cimg = ctk.CTkImage(light_image=bg, dark_image=bg,
                            size=(max(1, int(bw / s)), max(1, int(bh / s))))
        self.pv_img.configure(image=cimg, text="", width=int(bw / s), height=int(bh / s))
        self._pv_ref = cimg

    # ---------------------------------------------------------------- 手动
    def open_manual(self, path=None):
        if not self.files:
            self.status_lbl.configure(text="请先添加照片，再手动框四角", text_color="#A9660A")
            return
        path = path or self.files[0]

        def on_apply(q):
            self.manual_quads[path] = q
            self.manual_quads[os.path.normcase(os.path.abspath(path))] = q
            if path in self.rows:
                fg, tc = STATUS_STYLE["手动"]
                self.rows[path].configure(text="手动", fg_color=fg, text_color=tc)
            if self._busy:
                self._pending_start = True
                self.status_lbl.configure(
                    text=f"已记录「{os.path.basename(path)}」的四角，正在处理的这批完成后自动重算",
                    text_color="#2F6BF5")
                return
            self.status_lbl.configure(
                text=f"已记录「{os.path.basename(path)}」的四角，重新生成中…",
                text_color="#2F6BF5")
            self.start()          # 不管之前有没有生成过结果，都立刻按新四角重算

        QuadEditor(self, path, on_apply)

    # ---------------------------------------------------------------- 打开
    def open_result(self):
        if not self.result:
            return
        target = self.result.get("pdf") or (
            self.result["images"][0] if self.result.get("images") else None)
        if target and os.path.exists(target):
            os.startfile(target)

    def open_dir(self):
        if not self.result:
            return
        target = self.result.get("pdf") or (
            self.result["images"][0] if self.result.get("images") else None)
        if target:
            os.startfile(os.path.dirname(target))

    def print_result(self):
        """「打印」：直接调起 Windows 系统打印（选打印机、份数后走 GDI 真实尺寸打印）。"""
        if self._busy:
            self.status_lbl.configure(text="正在处理，稍等再打印", text_color="#A9660A")
            return
        res = self.result or {}
        if not res.get("page_pngs") and not res.get("pdf"):
            self.status_lbl.configure(text="还没有可打印的结果，请先点「开始处理」",
                                      text_color="#A9660A")
            return
        if not win_print.available():
            self._print_fallback("当前系统不支持直接打印")
            return
        dlg = PrintDialog(self, res)
        dlg.show()

    def _print_fallback(self, why=""):
        """GDI 打印不可用时的兜底：交给系统默认程序打印 / 打开 PDF。"""
        pdf = (self.result or {}).get("pdf")
        if pdf and os.path.exists(pdf):
            try:
                os.startfile(pdf, "print")
                self.status_lbl.configure(text=f"已交给系统默认程序打印{('（' + why + '）') if why else ''}",
                                          text_color="#0E8A4C")
                return
            except Exception:
                pass
            try:
                os.startfile(pdf)
            except Exception:
                pass
            self.status_lbl.configure(text=f"已打开 PDF，请在阅读器里按 Ctrl+P 打印（{why}）",
                                      text_color="#A9660A")
        else:
            self.status_lbl.configure(text=f"无法打印：{why or '没有可打印的内容'}",
                                      text_color="#C23B32")


# ---------------------------------------------------------------- Windows 打印（无需任何外部程序）
import ctypes
from ctypes import wintypes


class _PRINTER_INFO_4W(ctypes.Structure):
    _fields_ = [("pPrinterName", ctypes.c_wchar_p),
                ("pServerName", ctypes.c_wchar_p),
                ("Attributes", wintypes.DWORD)]


class _DOCINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_int),
                ("lpszDocName", ctypes.c_wchar_p),
                ("lpszOutput", ctypes.c_wchar_p),
                ("lpszDatatype", ctypes.c_wchar_p),
                ("fwType", ctypes.c_ulong)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BMIH), ("bmiColors", ctypes.c_uint32 * 3)]


class WinPrint(object):
    """用 Windows 自带 GDI 把 A4 整页位图直接送到打印机：真实 210×297mm，1:1 不缩放。"""

    LOGPIXELSX, LOGPIXELSY = 88, 90
    HORZRES, VERTRES = 8, 10
    PHYSICALOFFSETX, PHYSICALOFFSETY = 112, 113
    SRCCOPY = 0x00CC0020
    DIB_RGB_COLORS = 0

    def __init__(self):
        self._ok = False
        self._err = ""
        try:
            self.spool = ctypes.WinDLL("winspool.drv", use_last_error=True)
            self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
            self.user = ctypes.WinDLL("user32", use_last_error=True)

            self.spool.EnumPrintersW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD,
                                                 ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD),
                                                 ctypes.POINTER(wintypes.DWORD)]
            self.spool.EnumPrintersW.restype = wintypes.BOOL
            self.spool.GetDefaultPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            self.spool.GetDefaultPrinterW.restype = wintypes.BOOL

            self.gdi.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           wintypes.LPCWSTR, ctypes.c_void_p]
            self.gdi.CreateDCW.restype = wintypes.HDC
            self.gdi.DeleteDC.argtypes = [wintypes.HDC]
            self.gdi.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
            self.gdi.GetDeviceCaps.restype = ctypes.c_int
            self.gdi.StartDocW.argtypes = [wintypes.HDC, ctypes.POINTER(_DOCINFO)]
            self.gdi.StartDocW.restype = ctypes.c_int
            self.gdi.StartPage.argtypes = [wintypes.HDC]
            self.gdi.StartPage.restype = ctypes.c_int
            self.gdi.EndPage.argtypes = [wintypes.HDC]
            self.gdi.EndPage.restype = ctypes.c_int
            self.gdi.EndDoc.argtypes = [wintypes.HDC]
            self.gdi.EndDoc.restype = ctypes.c_int
            self.gdi.AbortDoc.argtypes = [wintypes.HDC]
            self.gdi.StretchDIBits.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int,
                                               ctypes.c_void_p, ctypes.POINTER(_BMI),
                                               wintypes.UINT, wintypes.DWORD]
            self.gdi.StretchDIBits.restype = ctypes.c_int
            self._ok = True
        except Exception as e:                       # 非 Windows / 环境异常
            self._err = str(e)

    def available(self):
        return self._ok

    # ------------------------------------------------------------ 打印机列表
    def list_printers(self):
        if not self._ok:
            return []
        flags = 0x2 | 0x4                            # LOCAL | CONNECTIONS
        needed, returned = wintypes.DWORD(0), wintypes.DWORD(0)
        try:
            self.spool.EnumPrintersW(flags, None, 4, None, 0,
                                     ctypes.byref(needed), ctypes.byref(returned))
            if not needed.value:
                return []
            buf = ctypes.create_string_buffer(needed.value)
            if not self.spool.EnumPrintersW(flags, None, 4, buf, needed.value,
                                            ctypes.byref(needed), ctypes.byref(returned)):
                return []
            arr = ctypes.cast(buf, ctypes.POINTER(_PRINTER_INFO_4W))
            out = []
            for i in range(returned.value):
                n = arr[i].pPrinterName
                if n and n not in out:
                    out.append(n)
            return out
        except Exception:
            return []

    def default_printer(self):
        if not self._ok:
            return ""
        try:
            size = wintypes.DWORD(0)
            self.spool.GetDefaultPrinterW(None, ctypes.byref(size))
            if not size.value:
                return ""
            b = ctypes.create_unicode_buffer(size.value)
            if self.spool.GetDefaultPrinterW(b, ctypes.byref(size)):
                return b.value
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------ 打印
    def print_pages(self, pngs, printer, copies=1, on_page=None):
        """把每张 A4 页面图按真实尺寸打印。返回 (成功页数, 错误信息)。"""
        if not self._ok:
            return 0, self._err or "系统打印不可用"
        if not printer:
            return 0, "没有选择打印机"
        if not pngs:
            return 0, "没有可打印的页面"

        from PIL import Image

        hdc = self.gdi.CreateDCW("WINSPOOL", printer, None, None)
        if not hdc:
            return 0, f"无法连接打印机「{printer}」（错误码 {ctypes.get_last_error()}）"
        done = 0
        try:
            dpix = self.gdi.GetDeviceCaps(hdc, self.LOGPIXELSX) or 300
            dpiy = self.gdi.GetDeviceCaps(hdc, self.LOGPIXELSY) or 300
            offx = self.gdi.GetDeviceCaps(hdc, self.PHYSICALOFFSETX)
            offy = self.gdi.GetDeviceCaps(hdc, self.PHYSICALOFFSETY)

            for _ in range(max(1, int(copies))):
                di = _DOCINFO()
                di.cbSize = ctypes.sizeof(_DOCINFO)
                di.lpszDocName = "身份证A4打印"
                if self.gdi.StartDocW(hdc, ctypes.byref(di)) <= 0:
                    return done, f"打印任务被拒绝（错误码 {ctypes.get_last_error()}）"
                try:
                    for k, p in enumerate(pngs, 1):
                        im = Image.open(p)
                        if im.mode != "RGB":
                            im = im.convert("RGB")
                        w, h = im.size
                        bits = im.tobytes("raw", "BGR")
                        bmi = _BMI()
                        bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
                        bmi.bmiHeader.biWidth = w
                        bmi.bmiHeader.biHeight = -h          # 负数=自上而下
                        bmi.bmiHeader.biPlanes = 1
                        bmi.bmiHeader.biBitCount = 24
                        bmi.bmiHeader.biCompression = 0      # BI_RGB
                        bmi.bmiHeader.biSizeImage = len(bits)
                        buf = ctypes.create_string_buffer(bits, len(bits))
                        if self.gdi.StartPage(hdc) <= 0:
                            raise RuntimeError(f"第 {k} 页发送失败（错误码 {ctypes.get_last_error()}）")
                        tw = int(round(w * dpix / float(core.DPI)))
                        th = int(round(h * dpiy / float(core.DPI)))
                        r = self.gdi.StretchDIBits(hdc, -offx, -offy, tw, th,
                                                   0, 0, w, h, buf, ctypes.byref(bmi),
                                                   self.DIB_RGB_COLORS, self.SRCCOPY)
                        self.gdi.EndPage(hdc)
                        if r <= 0:
                            raise RuntimeError(f"第 {k} 页绘制失败（错误码 {ctypes.get_last_error()}）")
                        done += 1
                        if on_page:
                            on_page(k, len(pngs))
                    self.gdi.EndDoc(hdc)
                except Exception as e:
                    self.gdi.AbortDoc(hdc)
                    return done, str(e)
            return done, ""
        finally:
            self.gdi.DeleteDC(hdc)


win_print = WinPrint()


class PrintDialog(ctk.CTkToplevel):
    """打印设置：选打印机、选份数，然后直接交给系统打印（真实尺寸，不缩放）。"""

    def __init__(self, app, res):
        super().__init__(app)
        self.app = app
        self.res = res
        self.title("打印")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(app)
        self._busy = False

        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=18, pady=16)

        ctk.CTkLabel(f, text="打印 A4", font=ui_font(17, "bold"), text_color=TEXT).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        ctk.CTkLabel(f, text="打印机", font=ui_font(12), text_color=MUTED, width=56,
                     anchor="w").grid(row=1, column=0, sticky="w", pady=4)
        printers = win_print.list_printers()
        dflt = win_print.default_printer()
        self.pv = ctk.StringVar(value=dflt or (printers[0] if printers else ""))
        cb = ctk.CTkComboBox(f, values=printers or ["（没有检测到打印机）"], variable=self.pv,
                             width=300, height=32, corner_radius=9, font=ui_font(12),
                             dropdown_font=ui_font(12), state="normal" if printers else "disabled")
        cb.grid(row=1, column=1, sticky="w", pady=4)

        ctk.CTkLabel(f, text="份数", font=ui_font(12), text_color=MUTED, width=56,
                     anchor="w").grid(row=2, column=0, sticky="w", pady=4)
        self.cv = ctk.StringVar(value="1")
        ctk.CTkOptionMenu(f, values=[str(i) for i in range(1, 10)], variable=self.cv,
                          width=96, height=32, corner_radius=9, font=ui_font(12),
                          fg_color="#EEF1F6", button_color="#DDE3EE", button_hover_color="#CFD7E4",
                          text_color=TEXT, dropdown_fg_color="#FFFFFF", dropdown_hover_color="#EEF1F6",
                          dropdown_text_color=TEXT,
                          command=lambda *_: None).grid(row=2, column=1, sticky="w", pady=4)

        pages = len(res.get("page_pngs") or []) or res.get("pages") or 1
        ctk.CTkLabel(f, text=f"{pages} 页 A4 · 真实尺寸 210×297mm（纸张请选 A4，按 1:1 发送，打印机里不要再缩放）",
                     font=ui_font(11), text_color=MUTED, wraplength=360, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 2))
        if not printers:
            ctk.CTkLabel(f, text="没检测到打印机：请先装好打印机驱动，或在系统「设置-打印机」里添加。",
                         font=ui_font(11), text_color="#C23B32", wraplength=360,
                         justify="left").grid(row=4, column=0, columnspan=2, sticky="w")

        self.tip = ctk.CTkLabel(f, text="", font=ui_font(11), text_color="#2F6BF5",
                                wraplength=360, justify="left")
        self.tip.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        bar = ctk.CTkFrame(f, fg_color="transparent")
        bar.grid(row=6, column=0, columnspan=2, sticky="e", pady=(18, 4))
        ctk.CTkButton(bar, text="取消", width=86, height=34, corner_radius=9,
                      fg_color="#EEF1F6", hover_color="#E2E7F0", text_color=TEXT,
                      font=ui_font(12), command=self.destroy).pack(side="right", padx=(8, 0))
        self.ok = ctk.CTkButton(bar, text="打印", width=110, height=34, corner_radius=9,
                                fg_color=ACCENT, hover_color="#2159D8", text_color="#FFFFFF",
                                font=ui_font(12, "bold"),
                                state="normal" if printers else "disabled",
                                command=self.do_print)
        self.ok.pack(side="right")

        self.geometry("486x312")
        self.after(70, self._front)

    def _front(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

    def do_print(self):
        if self._busy:
            return
        printer = (self.pv.get() or "").strip()
        try:
            copies = max(1, int(self.cv.get()))
        except Exception:
            copies = 1
        pngs = list(self.res.get("page_pngs") or [])
        if not pngs:
            self.app._print_fallback("没有可打印的页面图")
            self.destroy()
            return
        self._busy = True
        self.ok.configure(state="disabled", text="正在发送…")
        self.tip.configure(text=f"正在发送到「{printer}」…", text_color="#2F6BF5")

        def work():
            n, err = win_print.print_pages(pngs, printer, copies,
                                           on_page=lambda k, total: self.after(
                                               0, lambda: self._page(k, total)))
            self.after(0, lambda: self._finish(n, err, printer, copies))

        threading.Thread(target=work, daemon=True).start()

    def _page(self, k, total):
        try:
            self.tip.configure(text=f"正在发送第 {k}/{total} 页…")
        except Exception:
            pass

    def _finish(self, n, err, printer, copies):
        self._busy = False
        try:
            if err and not n:
                self.ok.configure(state="normal", text="重试")
                self.tip.configure(text=f"打印失败：{err}", text_color="#C23B32")
                self.app.status_lbl.configure(text=f"打印失败：{err}", text_color="#C23B32")
                return
            self.destroy()
            msg = f"已发送到打印机「{printer}」：{n} 页 × {copies} 份"
            if err:
                msg += f"（有提示：{err}）"
            self.app.status_lbl.configure(text=msg, text_color="#0E8A4C")
        except Exception:
            pass

    def show(self):
        return self


def run():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run()























