# -*- coding: utf-8 -*-
"""
身份证照片自动处理工具
================================================================
手机拍摄的身份证照片 -> 自动找卡片四边 -> 透视校正 -> 裁成标准尺寸
(85.6 x 54 mm, 300 DPI) -> 排版到 A4 -> 输出可直接打印的 PDF / PNG

命令行：
    python idcard2a4.py 照片目录            # 整个目录批量
    python idcard2a4.py a.jpg b.jpg         # 指定文件
    python idcard2a4.py 照片目录 -l pair     # 正反面放同一页
    python idcard2a4.py                    # 不带参数 -> 打开图形界面

常用选项：
    -o  输出目录
    -l  auto|single|pair|grid   排版方式
    -r  自动|不旋转|顺时针90|180|逆时针90
    --no-clean   关闭自动增强
    --marks      打印裁切框线
    --gap        卡片间距(mm)
"""
import os
import sys
import math
import argparse
import traceback
from datetime import datetime

try:
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
except ImportError as _err:          # 依赖没装时给个能看懂的提示，而不是一堆报错
    _msg = ('缺少依赖库：%s\n\n请在命令行窗口执行下面这一行安装：\n\n'
            'python -m pip install numpy opencv-python-headless pillow '
            '-i https://pypi.tuna.tsinghua.edu.cn/simple\n' % _err)
    print(_msg)
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, _msg, '身份证打印工具 - 缺少依赖', 0x10)
    except Exception:
        pass
    sys.exit(1)

# ---------------------------------------------------------------- 基本参数
DPI = 300
MM2PX = DPI / 25.4

CARD_W_MM, CARD_H_MM = 85.6, 54.0
CARD_W = int(round(CARD_W_MM * MM2PX))      # 1011 px
CARD_H = int(round(CARD_H_MM * MM2PX))      # 638 px

A4_W = int(round(210 * MM2PX))              # 2480 px
A4_H = int(round(297 * MM2PX))              # 3508 px

CARD_ASPECT = CARD_W_MM / CARD_H_MM         # 1.585
CORNER_R_MM = 3.18          # 二代身份证圆角半径（ISO/IEC 7810 ID-1：3.18mm）
IMG_EXT = ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff', '.heic', '.heif')

# ---------------------------------------------------------------- 版本信息
APP_VERSION = '1.0.0'
APP_AUTHOR = '一只小白'
APP_REPO = 'https://github.com/xiaobyes/idcard-a4-print'

FROZEN = getattr(sys, 'frozen', False)      # 是否打包成了 exe


def _msgbox(title, text, flags=0x40):
    """打包成 exe 后没有控制台，用弹窗告诉用户结果"""
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(text), str(title), flags)
            return True
    except Exception:
        pass
    print(text)
    return False

# iPhone 的 HEIC 支持（装了 pillow-heif 才能读）
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass


# ---------------------------------------------------------------- 圆角
_ALPHA_CACHE = {}


def rounded_mask(w=CARD_W, h=CARD_H, r_mm=None, ss=4):
    """生成带抗锯齿的圆角矩形蒙版（255=保留，0=挖空）"""
    if r_mm is None:
        r_mm = CORNER_R_MM
    key = (int(w), int(h), round(float(r_mm), 2))
    if key in _ALPHA_CACHE:
        return _ALPHA_CACHE[key]

    if r_mm <= 0.2:
        m = np.full((h, w), 255, np.uint8)
    else:
        r = max(2, int(round(float(r_mm) * MM2PX)))
        r = min(r, w // 2 - 1, h // 2 - 1)
        W, H = w * ss, h * ss
        R = r * ss
        m = np.zeros((H, W), np.uint8)
        cv2.rectangle(m, (R, 0), (W - 1 - R, H - 1), 255, -1)
        cv2.rectangle(m, (0, R), (W - 1, H - 1 - R), 255, -1)
        for cx, cy in ((R, R), (W - 1 - R, R), (R, H - 1 - R), (W - 1 - R, H - 1 - R)):
            cv2.circle(m, (cx, cy), R, 255, -1)
        m = cv2.resize(m, (w, h), interpolation=cv2.INTER_AREA)

    _ALPHA_CACHE[key] = m
    return m


# ---------------------------------------------------------------- 读图
def read_image(path):
    """读入图片并自动按 EXIF 方向摆正，返回 BGR ndarray"""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert('RGB')
        arr = np.asarray(im)
    return np.ascontiguousarray(arr[:, :, ::-1])


# ---------------------------------------------------------------- 几何工具
def _norm(v):
    return float(np.linalg.norm(v))


def order_points(pts):
    """把四个角点整理成 左上、右上、右下、左下"""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()          # y - x
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def poly_area(pts):
    return float(abs(cv2.contourArea(np.asarray(pts, dtype=np.float32))))


def _angle_order(quad):
    """把四点按绕质心的角度排成简单四边形（消除交叉/乱序），起点取左上。
    之后再走 order_points 就能得到 左上→右上→右下→左下，绝不会镜像。"""
    q = np.asarray(quad, np.float32).reshape(-1, 2)
    c = q.mean(axis=0)
    ang = np.arctan2(q[:, 1] - c[1], q[:, 0] - c[0])
    q = q[np.argsort(ang)]
    k = int(np.argmin(q.sum(axis=1)))
    q = np.roll(q, -k, axis=0)
    return q.astype(np.float32)


def quad_from_contour(c):
    """尽量从轮廓里取出 4 个角；取不到就用最小外接矩形"""
    peri = cv2.arcLength(c, True)
    if peri <= 0:
        return None
    for eps in (0.015, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08):
        ap = cv2.approxPolyDP(c, eps * peri, True)
        if len(ap) == 4 and cv2.isContourConvex(ap):
            return ap.reshape(4, 2).astype(np.float32)
    rect = cv2.minAreaRect(c)
    if min(rect[1]) < 8:
        return None
    return cv2.boxPoints(rect).astype(np.float32)


def _grad_map(gray_u8):
    """Sobel 梯度幅值图（判断"这条边到底是不是卡边"用）"""
    gx = cv2.Sobel(gray_u8, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray_u8, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def _edge_strength(q, gmag, ns=48, band=3):
    """
    沿四条边各取 ns 个点，在法线方向 ±band 内找最大梯度幅值，
    取每边中位数并归一化。真卡边（卡片与桌面的界线）会连续有强梯度，
    卡内文字块/头像框虽然也是四边形，但边上常常一半在卡内（弱梯度）。
    返回 0~1，越大越像真边缘。
    """
    try:
        q = order_points(q)
        H, W = gmag.shape[:2]
        ss = np.arange(-band, band + 1, dtype=np.float32)
        ts = np.linspace(0.08, 0.92, ns, dtype=np.float32)
        per_side = []
        for i in range(4):
            p0, p1 = q[i], q[(i + 1) % 4]
            d = p1 - p0
            L = float(np.linalg.norm(d))
            if L < 20:
                return 0.0
            n = np.array([-d[1], d[0]], np.float32) / L
            xs = (p0[0] + ts * d[0] + ss[:, None] * n[0]).astype(np.float32)
            ys = (p0[1] + ts * d[1] + ss[:, None] * n[1]).astype(np.float32)
            xs = np.clip(xs.reshape(-1), 0, W - 1)
            ys = np.clip(ys.reshape(-1), 0, H - 1)
            vals = gmag[ys.astype(np.int32), xs.astype(np.int32)].reshape(len(ss), ns)
            peak = vals.max(axis=0)
            per_side.append(float(np.median(peak)))
        med = float(np.median(per_side))
        return float(np.clip(med / 55.0, 0.0, 1.0))
    except Exception:
        return 0.0


def score_quad(q, canvas_area, gray, gmag=None):
    """给候选四边形打分，不像身份证的返回 None"""
    q = order_points(q)
    area = poly_area(q)
    if area < 0.030 * canvas_area or area > 0.95 * canvas_area:
        return None

    w = (_norm(q[1] - q[0]) + _norm(q[2] - q[3])) / 2.0
    h = (_norm(q[3] - q[0]) + _norm(q[2] - q[1])) / 2.0
    if w < 40 or h < 25:
        return None

    ar = max(w, h) / max(min(w, h), 1e-6)
    if ar < 1.15 or ar > 2.45:                     # 透视会让长宽比偏离，放宽
        return None
    ar_score = math.exp(-abs(math.log(ar / CARD_ASPECT)) * 1.3)

    rect = cv2.minAreaRect(q)
    rect_area = rect[1][0] * rect[1][1]
    if rect_area <= 1:
        return None
    rect_score = area / rect_area
    if rect_score < 0.70:                          # 太扁/太圆，不是卡片
        return None

    # 四个内角别太离谱
    for i in range(4):
        v1 = q[(i - 1) % 4] - q[i]
        v2 = q[(i + 1) % 4] - q[i]
        cosv = float(np.dot(v1, v2)) / (max(_norm(v1), 1e-6) * max(_norm(v2), 1e-6))
        ang = math.degrees(math.acos(max(-1.0, min(1.0, cosv))))
        if not (50 <= ang <= 135):
            return None

    # 卡片一般比桌面亮
    mask = np.zeros(gray.shape, np.uint8)
    cv2.fillPoly(mask, [q.astype(np.int32)], 255)
    inside = float(cv2.mean(gray, mask)[0])
    k = max(5, int(min(gray.shape) * 0.012) | 1)
    ring = cv2.subtract(cv2.dilate(mask, np.ones((k, k), np.uint8)), mask)
    outside = float(cv2.mean(gray, ring)[0]) if cv2.countNonZero(ring) else inside
    bright = max(0.0, min(1.0, (inside - outside + 10) / 70.0))

    size_score = min(1.0, area / (0.22 * canvas_area))

    edge_score = _edge_strength(q, gmag) if gmag is not None else 0.5

    score = (0.34 * rect_score + 0.24 * ar_score + 0.15 * bright
             + 0.09 * size_score + 0.18 * edge_score)

    # 边界内侧那一圈里如果夹着明显更暗的东西（桌面阴影、反光边），说明框大了，扣分
    k2 = max(7, int(min(gray.shape) * 0.070) | 1)
    inner_core = cv2.erode(mask, np.ones((k2, k2), np.uint8))
    rim = cv2.subtract(mask, inner_core)
    rim_vals = gray[rim > 0]
    if rim_vals.size:
        rim_lo = float(np.percentile(rim_vals, 10))
        if rim_lo < inside - 45:
            score -= min(0.20, (inside - rim_lo) / 250.0)

    # 明显比周围暗的四边形要扣分：证件本身是亮底，卡内的头像框/文字块则偏暗，
    # 不扣分的话白卡白桌时会误把头像框当成整张卡
    if inside < outside - 25:
        score -= min(0.20, (outside - inside) / 220.0)
    return max(0.0, score)


def _maps(gray_blur):
    """多种二值/边缘图，覆盖不同背景和光照（含低对比度：白卡白桌）"""
    clahe = cv2.createCLAHE(clipLimit=2.6, tileGridSize=(8, 8))
    gcl = clahe.apply(gray_blur)
    out = [
        cv2.Canny(gray_blur, 40, 120, apertureSize=3),
        cv2.Canny(gray_blur, 80, 200, apertureSize=3),
        cv2.Canny(gray_blur, 15, 50, apertureSize=3),
        cv2.Canny(gcl, 30, 90, apertureSize=3),          # 局部增益后的弱边缘
        cv2.Canny(gcl, 60, 170, apertureSize=3),
    ]
    out.append(cv2.adaptiveThreshold(gray_blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY_INV, 31, 9))
    out.append(cv2.adaptiveThreshold(gcl, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY_INV, 45, 6))
    _, otsu = cv2.threshold(gray_blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    out.append(otsu)
    res = []
    for m in out:
        for k in (3, 7):
            # 先膨胀再闭运算：手机照片里卡边常是断续的，膨胀能把断口连起来，
            # 轮廓才能闭合成一个完整的四边形（膨胀带来的外扩由后面的贴边步骤收回）
            d = cv2.dilate(m, np.ones((k, k), np.uint8), iterations=1)
            res.append(cv2.morphologyEx(d, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8)))
    return res


def refine_quad(quad, gray):
    """亚像素级微调四个角，调整过头就丢弃"""
    try:
        pts = quad.reshape(-1, 1, 2).astype(np.float32)
        cv2.cornerSubPix(gray, pts, (7, 7), (-1, -1),
                         (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01))
        newq = pts.reshape(4, 2)
        if float(np.max(np.linalg.norm(newq - quad, axis=1))) < 0.025 * max(gray.shape):
            return newq.astype(np.float32)
    except Exception:
        pass
    return quad


def _line_intersect(a, b):
    """两条直线（各由两点定义）的交点"""
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) < 1e-6:
        return (x1 + x3) / 2.0, (y1 + y3) / 2.0
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d
    return x1 + t * (x2 - x1), y1 + t * (y2 - y1)


def refine_edges(bgr, quad, band_frac=0.035):
    """
    把粗框的四条边"贴"到真实卡边上（这一步决定边缘准不准）。

    做法（比"整边一个中位数偏移"稳得多）：
    1) 四条边各取 80 个采样点，每点沿法线方向 ±band 找灰度跳变峰值，
       并用抛物线插值取亚像素位置（不靠整数像素）；
    2) 每条边得到一堆 (点坐标)，用 cv2.fitLine 稳健拟合出一条直线
       （先 L2 + 去掉离群点，再 Huber 精修），这样即使边缘被手指/反光
       打断一截，直线依然贴合真实边线；
    3) 相邻直线求交 → 四个角；面积/长宽比/角点位移任意一项超限就回退原框。
    """
    if quad is None:
        return quad
    try:
        q = order_points(quad).astype(np.float32)
        H, W = bgr.shape[:2]
        band = max(9.0, band_frac * float(min(H, W)))
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        ns = int(round(band * 2)) + 1
        ss = np.linspace(-band, band, ns).astype(np.float32)
        lines = []

        for i in range(4):
            p0, p1 = q[i], q[(i + 1) % 4]
            d = p1 - p0
            L = float(np.linalg.norm(d))
            if L < 40:
                return quad
            n = np.array([-d[1], d[0]], np.float32) / L
            nt = 80
            ts = np.linspace(0.06, 0.94, nt, dtype=np.float32)
            xs = (p0[0] + ts[:, None] * d[0] + ss[None, :] * n[0]).astype(np.float32)
            ys = (p0[1] + ts[:, None] * d[1] + ss[None, :] * n[1]).astype(np.float32)
            strip = cv2.remap(gray, xs, ys, cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE).astype(np.float32)
            grad = np.abs(np.diff(strip, axis=1))
            grad = cv2.blur(grad, (1, 3))
            # 距离加权：桌面木纹、床单褶皱这些也会产生强梯度，但它们离原框较远。
            # 越靠近原位置的梯度越可信，避免被背景纹理把边"拉"出去。
            mid = 0.5 * (ss[:-1] + ss[1:])
            wgt = np.exp(-(mid / max(band * 0.55, 1.0)) ** 2).astype(np.float32)
            grad = grad * wgt[None, :]
            idx = np.argmax(grad, axis=1)
            peak = grad[np.arange(nt), idx]

            thr = max(2.6, float(np.percentile(peak, 65)))
            ok = (peak >= thr) & (idx >= 2) & (idx <= ns - 3)
            if int(ok.sum()) < 12:
                continue                    # 这条边看不见，用原边

            ii = idx[ok]
            # 抛物线插值到亚像素
            g0 = grad[np.arange(nt), np.clip(ii - 1, 0, ns - 1)][ok]
            g1 = peak[ok]
            g2 = grad[np.arange(nt), np.clip(ii + 1, 0, ns - 1)][ok]
            den = (g0 - 2 * g1 + g2)
            sub = np.where(np.abs(den) > 1e-6, 0.5 * (g0 - g2) / np.maximum(np.abs(den), 1e-6), 0.0)
            sub = np.clip(sub, -0.6, 0.6)
            off = ss[ii] + sub

            tt = ts[ok]
            pts = (p0[None, :] + tt[:, None] * d[None, :]
                   + off[:, None] * n[None, :]).astype(np.float32)

            # 稳健拟合：L2 初拟合 → 去离群 → Huber 精修
            vx, vy, x0, y0 = cv2.fitLine(pts.reshape(-1, 1, 2), cv2.DIST_L2, 0, 0.01, 0.01)
            vx, vy, x0, y0 = float(vx[0]), float(vy[0]), float(x0[0]), float(y0[0])
            resid = np.abs((pts[:, 0] - x0) * vy - (pts[:, 1] - y0) * vx)
            mad = float(np.median(resid)) + 1e-6
            inl = resid <= max(3.0 * mad, 1.6)
            if int(inl.sum()) >= 10:
                pts2 = pts[inl]
                vx, vy, x0, y0 = cv2.fitLine(pts2.reshape(-1, 1, 2), cv2.DIST_HUBER,
                                             0, 0.01, 0.01)
                vx, vy, x0, y0 = float(vx[0]), float(vy[0]), float(x0[0]), float(y0[0])
                resid = np.abs((pts2[:, 0] - x0) * vy - (pts2[:, 1] - y0) * vx)
                if float(np.median(resid)) > 0.45 * band:
                    continue            # 拟合线偏离太多，说明这条边不干净
            lines.append(((x0, y0), (x0 + vx * 1000.0, y0 + vy * 1000.0)))

        if len(lines) < 4:
            return quad

        new = np.array([_line_intersect(lines[(i - 1) % 4], lines[i]) for i in range(4)],
                       np.float32)

        # 验收：面积、长宽比、单个角位移都不能离谱，否则宁可不动
        a0, a1 = poly_area(q), poly_area(new)
        if not (0.82 * a0 <= a1 <= 1.22 * a0):
            return quad
        if not np.all(np.isfinite(new)):
            return quad
        if float(np.max(np.linalg.norm(new - q, axis=1))) > max(band * 0.9, 10.0):
            return quad
        asp0, asp1 = quad_aspect(q), quad_aspect(new)
        if abs(asp1 - asp0) > 0.28:
            return quad
        if not (1.25 <= asp1 <= 2.05):
            return quad
        return new
    except Exception:
        return quad


def detect_card(bgr):
    """返回 (四角坐标 或 None, 说明)"""
    H, W = bgr.shape[:2]
    scale = min(1.0, 1500.0 / max(H, W))
    if scale < 1.0:
        small = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    else:
        small = bgr

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gb = cv2.GaussianBlur(gray, (5, 5), 0)
    canvas_area = float(small.shape[0] * small.shape[1])
    gmag = _grad_map(gb)

    best = None
    cands = []
    for m in _maps(gb):
        cnts, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            if cv2.contourArea(c) < 0.035 * canvas_area:
                continue
            q = quad_from_contour(c)
            if q is None:
                continue
            sc = score_quad(q, canvas_area, gray, gmag)
            if sc is not None:
                cands.append((sc, q, poly_area(q)))
                if best is None or sc > best[0]:
                    best = (sc, q)

    if best is not None and best[0] >= 0.50:
        # 分数接近、且"把当前结果整个包住"的更大四边形优先：卡内的头像框、防伪块
        # 也常常是规整四边形，会抢走整张卡的位置
        if cands:
            top = max(c[0] for c in cands)
            a_best = poly_area(best[1])
            for sc, q2, ar in cands:
                if sc < top - 0.08 or ar < a_best * 1.25:
                    continue
                inner = order_points(best[1])
                outer = order_points(q2).astype(np.float32)
                if all(cv2.pointPolygonTest(outer, (float(p[0]), float(p[1])), False) >= -1.0
                       for p in inner):
                    best, a_best = (sc, q2), ar
        q = refine_quad(order_points(best[1]), gb)
        qf = q / scale
        try:
            qf = refine_edges(bgr, qf)     # 在原始分辨率上把四条边贴准
        except Exception:
            pass
        return qf, '精确'

    # 退路：整张图里最亮的一块（白卡比桌面亮时通常就是它）
    _, th = cv2.threshold(gb, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if cnts:
        c = max(cnts, key=cv2.contourArea)
        if cv2.contourArea(c) > 0.02 * canvas_area:
            r = cv2.minAreaRect(c)
            q = cv2.boxPoints(r).astype(np.float32)
            ar = max(r[1]) / max(min(r[1]), 1e-6)
            if 1.1 <= ar <= 2.6:
                return q / scale, '粗略'

    return None, '未找到边缘'


# ---------------------------------------------------------------- 多卡 / 通用卡片
def quad_aspect(q):
    p = order_points(np.asarray(q, np.float32))
    w = max(_norm(p[1] - p[0]), _norm(p[2] - p[3]))
    h = max(_norm(p[3] - p[0]), _norm(p[2] - p[1]))
    return w / max(h, 1e-6)


def _quad_iou(a, b):
    pa = np.asarray(a, np.float32)
    pb = np.asarray(b, np.float32)
    aa, ab = abs(cv2.contourArea(pa)), abs(cv2.contourArea(pb))
    try:
        inter, _ = cv2.intersectConvexConvex(pa, pb)
    except Exception:
        return 0.0
    inter = max(0.0, float(inter))
    return inter / max(1e-6, aa + ab - inter)


def _quad_inside_ratio(inner, outer):
    pi = order_points(np.asarray(inner, np.float32))
    po = order_points(np.asarray(outer, np.float32)).astype(np.float32)
    ai = abs(cv2.contourArea(pi)) + 1e-6
    try:
        inter, _ = cv2.intersectConvexConvex(pi, po)
    except Exception:
        return 0.0
    return max(0.0, float(inter)) / ai


def _bbox_cover(outer, inner):
    """内框的包围盒有多大比例落在"外框的包围盒"里（用来判"这是同一张卡的内部区块"）"""
    ox, oy, ow, oh = cv2.boundingRect(np.asarray(outer, np.int32))
    ix, iy, iw, ih = cv2.boundingRect(np.asarray(inner, np.int32))
    x1, y1 = max(ox, ix), max(oy, iy)
    x2, y2 = min(ox + ow, ix + iw), min(oy + oh, iy + ih)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return ((x2 - x1) * (y2 - y1)) / float(max(1, iw * ih))


def _candidate_quads(bgr):
    """收集所有"像卡片"的四边形，返回 [(quad, 分数)]（原图坐标）"""
    H, W = bgr.shape[:2]
    scale = min(1.0, 1500.0 / max(H, W))
    small = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) \
        if scale < 1.0 else bgr
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gb = cv2.GaussianBlur(gray, (5, 5), 0)
    area0 = float(small.shape[0] * small.shape[1])
    gmag = _grad_map(gb)

    raw = []
    for m in _maps(gb):
        cnts, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            if cv2.contourArea(c) < 0.02 * area0:
                continue
            q = quad_from_contour(c)
            if q is None:
                continue
            sc = score_quad(q, area0, gray, gmag)
            if sc is not None and sc >= 0.40:
                raw.append((sc, q))

    # 参考成熟项目（jrsdhr/idcard-a4-tool、lettimepassby/idcard-print）的做法：
    # Otsu / 自适应阈值 → 最大连通域 → 凸包 → 最小外接矩形。
    # 卡边被反光、阴影打断时，这条路的闭合性比 Canny 轮廓更好。
    clahe = cv2.createCLAHE(clipLimit=2.6, tileGridSize=(8, 8))
    gcl = clahe.apply(gb)
    _, otsu = cv2.threshold(gb, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    otsu_inv = cv2.bitwise_not(otsu)
    adap = cv2.adaptiveThreshold(gcl, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 51, 5)
    for m in (otsu, otsu_inv, adap):
        m2 = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
        m2 = cv2.morphologyEx(m2, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
        cnts, _ = cv2.findContours(m2, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:3]:
            if cv2.contourArea(c) < 0.03 * area0:
                continue
            hull = cv2.convexHull(c)
            box = cv2.boxPoints(cv2.minAreaRect(hull)).astype(np.float32)
            sc = score_quad(box, area0, gray, gmag)
            if sc is not None and sc >= 0.36:
                raw.append((sc, box))
            qh = quad_from_contour(hull)
            if qh is not None:
                sc = score_quad(qh, area0, gray, gmag)
                if sc is not None and sc >= 0.36:
                    raw.append((sc, qh))

    # 兜底：最大连通块的最小外接矩形（参考项目做法，边缘断续时也能出框）
    edges = cv2.dilate(cv2.Canny(gb, 40, 120), np.ones((5, 5), np.uint8), iterations=2)
    cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:2]:
        if cv2.contourArea(c) < 0.02 * area0:
            continue
        box = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
        sc = score_quad(box, area0, gray, gmag)
        if sc is not None and sc >= 0.35:
            raw.append((sc, box))

    return [(np.asarray(q, np.float32) / scale, sc) for sc, q in raw]


def detect_cards(bgr, max_cards=3):
    """
    一张照片里可能有 1~N 张卡（例如身份证正反面并排拍在一张图上）。
    返回 [(quad, 说明)]，最多 max_cards 张。
    """
    raw = _candidate_quads(bgr)
    if not raw:
        return []
    qa = lambda q: abs(cv2.contourArea(np.asarray(q, np.float32)))

    # 先按"分数"（面积×长宽比吻合度）排序，高分优先；再按包含关系把"卡内区块"剔除
    raw.sort(key=lambda t: -t[1])
    keep = []
    for q, sc in raw:
        asp = quad_aspect(q)
        if not (1.32 <= asp <= 1.98 or 1.32 <= 1.0 / asp <= 1.98):
            continue
        # 卡面一定有内容（文字/图案）：一块几乎均匀的区域不是卡（多半是桌面阴影）
        x, y, w, h = cv2.boundingRect(np.asarray(q, np.int32))
        if w < 20 or h < 20:
            continue
        g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        gx, gy = max(0, x + w // 12), max(0, y + h // 12)
        patch = g[gy:min(g.shape[0], gy + max(8, h * 5 // 6)),
                  gx:min(g.shape[1], gx + max(8, w * 5 // 6))]
        if patch.size > 0 and float(patch.std()) < 12.0:
            continue
        if any(_quad_iou(q, k) > 0.30 for k, _ in keep):
            continue
        if any(_quad_inside_ratio(q, k) > 0.80 for k, _ in keep):
            continue                       # 已经在某张卡内部了（头像框、防伪块）
        # 新框把已有小框整个包住且明显更大 -> 用更大的替换
        for idx, (k, ks) in enumerate(keep):
            if _quad_inside_ratio(k, q) > 0.88 and qa(q) > qa(k) * 1.15 and sc >= ks - 0.10:
                keep[idx] = (q, sc)
                break
        else:
            keep.append((q, sc))
        if len(keep) > max_cards:
            keep.sort(key=lambda t: -qa(t[0]))
            del keep[max_cards:]

    # 同一张卡被拆成"整卡 + 卡内一块"（例如卡右边的文字区、头像框）时，只留整卡：
    # 按面积从大到小，凡是被已保留的更大卡"整块包住"的，一律丢掉
    keep.sort(key=lambda t: -qa(t[0]))
    keep2 = []
    for q, sc in keep:
        if any(_bbox_cover(k, q) >= 0.85 for k, _ in keep2):
            continue
        keep2.append((q, sc))
    keep = keep2

    out = []
    for q, sc in keep:
        q = order_points(np.asarray(q, np.float32))
        q2 = q
        try:
            q2 = refine_edges(bgr, q)
        except Exception:
            q2 = q
        # 精修后如果长宽比反而更不像证件卡，说明修到了卡外的假边缘 -> 用原框
        e1 = abs(quad_aspect(q) - CARD_ASPECT)
        e2 = abs(quad_aspect(q2) - CARD_ASPECT)
        if q2 is not None and e2 <= e1 + 0.02:
            q = q2
        out.append((q, '精确' if sc >= 0.48 else '粗略'))
    return out


# ==================================================================
#  参考 jrsdhr/idcard-a4-tool 的识别与转正：
#   ① 与“边框背景色”的色差图 → Otsu → 腐蚀 → 最大连通域 → 每行左右极值
#      → 凸包 → 最小外接矩形（外切卡片，贴边不切内容）
#   ② 方向：先统一横版，再用内容特征（国徽紧凑红块在左上 / 证件照肤色在右侧）
#      在 4 个角度里挑最像正立的那一个
# ==================================================================
def _bgcolor_quad(bgr, max_side=760, band_frac=0.04, pad=2.5):
    """
    在缩到 max_side 的图上按"背景色差 + Otsu + 最大连通域"找卡片外接矩形。
    返回 (在小图坐标系里的四角, 缩放比, 小图, 标记图)；找不到返回 None。
    """
    H, W = bgr.shape[:2]
    sc = min(1.0, float(max_side) / max(H, W))
    small = (cv2.resize(bgr, (max(2, int(W * sc)), max(2, int(H * sc))),
                        interpolation=cv2.INTER_AREA) if sc < 1.0 else bgr.copy())
    h, w = small.shape[:2]
    if h < 24 or w < 24:
        return None

    band = max(3, int(min(h, w) * band_frac))
    ring = np.zeros((h, w), bool)
    ring[:band, :] = True
    ring[-band:, :] = True
    ring[:, :band] = True
    ring[:, -band:] = True
    bg = np.median(small[ring].reshape(-1, 3), axis=0)          # 背景色（中位数，抗卡片压边）

    diff = np.abs(small.astype(np.float32) - bg[None, None, :]).mean(axis=2)
    dist = np.clip(diff, 0, 255).astype(np.uint8)
    thr, _ = cv2.threshold(dist, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if thr < 12:                                                # 背景和卡片几乎无色差
        return None

    mask = (dist > thr).astype(np.uint8) * 255
    # 3x3 腐蚀一次去掉毛边（后面 minAreaRect 外扩 pad 补回来）
    mask = cv2.erode(mask, np.ones((3, 3), np.uint8), 1)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 4)
    if n <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    best = 1 + int(np.argmax(areas))
    if stats[best, cv2.CC_STAT_AREA] < 0.06 * h * w:            # 太小：不是卡片
        return None

    idx = (labels == best).astype(np.uint8)
    # 每行的左右极值点（比全部像素点快得多，且轮廓更"外切"）
    pts = []
    for y in range(h):
        xs = np.flatnonzero(idx[y])
        if xs.size:
            pts.append((float(xs[0]), float(y)))
            if xs[-1] != xs[0]:
                pts.append((float(xs[-1]), float(y)))
    if len(pts) < 8:
        return None

    hull = cv2.convexHull(np.asarray(pts, np.float32).reshape(-1, 1, 2)).reshape(-1, 2)
    rect = cv2.minAreaRect(hull)                                # ((cx,cy),(w,h),angle)
    (cx, cy), (rw, rh), ang = rect
    rw, rh = rw + pad * 2, rh + pad * 2
    box = cv2.boxPoints(((cx, cy), (rw, rh), ang)).astype(np.float32)
    quad = order_points(box)

    asp = quad_aspect(quad)
    if not (1.20 <= asp <= 2.60 or 1.20 <= 1.0 / asp <= 2.60):
        return None
    # 贴住整幅画面（四条边都顶到画外）多半是"把整张照片当卡片了"
    xs = quad[:, 0]
    ys = quad[:, 1]
    edge_hits = (xs.min() <= 1.5) + (xs.max() >= w - 2.5) + (ys.min() <= 1.5) + (ys.max() >= h - 2.5)
    if edge_hits >= 3:
        return None
    return quad, sc, small, idx


def _refine_bg_quad(bgr, quad):
    """把缩略图上的四角换算回原图，再用亚像素直线拟合精修一次"""
    q = np.asarray(quad, np.float32)
    try:
        q2 = refine_edges(bgr, q)
        if q2 is not None and abs(quad_aspect(q2) - CARD_ASPECT) <= abs(quad_aspect(q) - CARD_ASPECT) + 0.03:
            return order_points(q2)
    except Exception:
        pass
    return order_points(q)


def detect_cards_ref(bgr, max_cards=3):
    """
    参考项目的识别路线：背景色差 + Otsu + 最大连通域 + 凸包最小外接矩形。
    一张图里有正反面（两张卡）时，把先找到的那张从掩膜里抹掉再找下一张。
    返回 [(quad, 说明)]；一张都找不到时返回 []（由调用方退回旧算法）。
    """
    work = bgr.copy()
    out = []
    for k in range(max_cards):
        got = _bgcolor_quad(work)
        if got is None:
            break
        quad_small, sc, small, idx = got
        quad = quad_small / sc
        quad = _refine_bg_quad(bgr, quad)
        quad[:, 0] = np.clip(quad[:, 0], 0, bgr.shape[1] - 1)
        quad[:, 1] = np.clip(quad[:, 1], 0, bgr.shape[0] - 1)
        if any(_quad_iou(quad, q) > 0.55 for q, _ in out):
            break
        if out:
            q1 = out[0][0]
            a1 = abs(cv2.contourArea(q1.astype(np.float32)))
            a2 = abs(cv2.contourArea(quad.astype(np.float32)))
            # 第二张卡必须：够大、且基本不落在第一张卡里面（否则就是卡内的一块内容）
            if a2 < 0.45 * a1 or _quad_inside_ratio(quad, q1) > 0.25:
                break
        out.append((quad, '精确'))

        # 把这张卡从小图上抹成背景色，再找下一张
        bg = np.median(small[idx.astype(bool)].reshape(-1, 3), axis=0)
        bgc = tuple(float(v) for v in bg)
        m = (idx > 0).astype(np.uint8)
        m = cv2.dilate(m, np.ones((5, 5), np.uint8), 1)
        small[m > 0] = bgc
        poly = quad_small.astype(np.float32).reshape(-1, 2)
        cv2.fillPoly(small, [np.round(poly).astype(np.int32)], bgc, cv2.LINE_8)
        big = (poly - poly.mean(axis=0)) * 1.12 + poly.mean(axis=0)
        cv2.fillPoly(small, [np.round(big).astype(np.int32)], bgc, cv2.LINE_8)
        work = small
    return out


# ------------------------------------------------- 卡被画面切掉：按卡面文字块补齐整卡
# 卡面文字块在标准整卡(1011x638)上的像素范围，用来从"文字看得见、卡边看不见"的照片
# 反推整张卡的位置和大小。数字是同一个检测函数在标准整卡上量出来的，口径一致。
_CONTENT_TPL = {
    'front': (78, 85, 912, 520),
    'back': (240, 57, 930, 540),
}
_CONTENT_TPL_ASP = {'front': 1.92, 'back': 1.43}


def _char_centers(bgr):
    """卡面文字：深色字 + 蓝青色导引字（都是小块，布料纹理那种细小噪点会被滤掉）"""
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    med = float(np.median(g))
    dark = g < int(max(30, min(150, med - 55)))
    colored = (hsv[:, :, 1] > 55) & (hsv[:, :, 2] < 205)
    m = (dark | colored).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, _lab, st, _cen = cv2.connectedComponentsWithStats(m, 8)
    H, W = g.shape
    pts = []
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if not (12 <= h <= int(0.13 * H) and 5 <= w <= int(0.13 * W)):
            continue
        if a < 45 or a > 0.004 * W * H:
            continue
        if w / max(1, h) > 5 or h / max(1, w) > 5:
            continue
        if a / float(max(1, w * h)) < 0.15:
            continue
        pts.append((x + w / 2.0, y + h / 2.0))
    return np.array(pts, np.float32)


def _dense_cluster(xy, rounds=2, k=3, factor=1.7):
    """只留成片成行的那些字，散落的噪点剔掉"""
    pts = xy
    for _ in range(rounds):
        if len(pts) < 10:
            break
        d = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=2)
        np.fill_diagonal(d, np.inf)
        kd = np.sort(d, axis=1)[:, :k].mean(axis=1)
        pts = pts[kd <= np.median(kd) * factor]
    return pts


def content_block(bgr, trim=2.0):
    """卡面文字块的最小外接四边形（原图坐标）+ 长宽比"""
    xy = _char_centers(bgr)
    if len(xy) < 12:
        return None, 0.0
    xy = _dense_cluster(xy)
    if len(xy) < 12:
        return None, 0.0
    mu = xy.mean(axis=0)
    best = None
    for ang in np.arange(-25.0, 25.01, 0.5):
        R = cv2.getRotationMatrix2D((float(mu[0]), float(mu[1])), float(ang), 1.0)
        r = cv2.transform(xy.reshape(-1, 1, 2), R).reshape(-1, 2)
        x0, x1 = np.percentile(r[:, 0], [trim, 100.0 - trim])
        y0, y1 = np.percentile(r[:, 1], [trim, 100.0 - trim])
        area = float((x1 - x0) * (y1 - y0))
        if best is None or area < best[0]:
            best = (area, float(ang), float(x0), float(x1), float(y0), float(y1))
    _, ang, x0, x1, y0, y1 = best
    R = cv2.getRotationMatrix2D((float(mu[0]), float(mu[1])), ang, 1.0)
    Rinv = cv2.invertAffineTransform(R)
    q = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float32)
    q = cv2.transform(q.reshape(-1, 1, 2), Rinv).reshape(-1, 2)
    q = order_points(q.astype(np.float32))
    bw = float(np.linalg.norm(q[1] - q[0]))
    bh = float(np.linalg.norm(q[3] - q[0]))
    return q, (bw / bh if bh > 1 else 0.0)


def _card_from_block(block, side):
    """文字块 -> 整卡四角（原图坐标，可能伸到画面外）"""
    t = _CONTENT_TPL[side]
    dst = np.array([[t[0], t[1]], [t[2], t[1]], [t[2], t[3]], [t[0], t[3]]], np.float32)
    hm = cv2.getPerspectiveTransform(block.astype(np.float32), dst)
    corners = np.array([[0, 0], [CARD_W, 0], [CARD_W, CARD_H], [0, CARD_H]], np.float32)
    return cv2.perspectiveTransform(corners.reshape(-1, 1, 2), np.linalg.inv(hm)).reshape(-1, 2)


def _warp_with_pad(bgr, quad):
    """四角伸出画面时，先按边缘像素把画面补大再校正"""
    H, W = bgr.shape[:2]
    left = max(0, int(np.ceil(-quad[:, 0].min())) + 12)
    top = max(0, int(np.ceil(-quad[:, 1].min())) + 12)
    right = max(0, int(np.ceil(quad[:, 0].max() - (W - 1))) + 12)
    bottom = max(0, int(np.ceil(quad[:, 1].max() - (H - 1))) + 12)
    if left or top or right or bottom:
        big = cv2.copyMakeBorder(bgr, top, bottom, left, right, cv2.BORDER_REPLICATE)
    else:
        big, top, left = bgr, 0, 0
    q = order_points((quad + np.array([left, top], np.float32)).astype(np.float32))
    return warp_to_card(big, q)


def _edge_dark_ratio(card, frac=0.06):
    """成品卡四边有多少是"内容"——比例不对时字会被切到边上"""
    g = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY)
    med = float(np.median(g))
    dark = g < med - 60
    H, W = g.shape
    bx, by = max(2, int(W * frac)), max(2, int(H * frac))
    edge = np.zeros_like(dark)
    edge[:by, :] = True
    edge[-by:, :] = True
    edge[:, :bx] = True
    edge[:, -bx:] = True
    return float(dark[edge].mean()) if edge.any() else 0.0


def detect_card_by_content(bgr):
    """
    卡被画面切掉一截（卡边拍不全）但卡面信息完整时：靠文字块推算整卡位置，
    超出画面的那一小截用边缘像素补上。识别不出来时返回 []（交给别的兜底）。
    """
    H, W = bgr.shape[:2]
    block, asp = content_block(bgr)
    if block is None or not (1.10 <= asp <= 2.70):
        return []
    barea = abs(float(cv2.contourArea(block.astype(np.float32))))
    if barea < 0.02 * W * H or barea > 0.80 * W * H:
        return []
    # 正反面先靠整张照片里的"人脸肤色 / 红国徽"猜一下，猜不准就两种都试
    try:
        pf = _layout_features(bgr)
    except Exception:
        pf = {'skinFrac': 0.0, 'redFrac': 0.0}
    guess = None
    if pf.get('redFrac', 0) > 0.012:
        guess = 'back'
    elif pf.get('skinFrac', 0) > 0.012:
        guess = 'front'
    order = [guess, 'back' if guess == 'front' else 'front'] if guess else ['front', 'back']
    cand = []
    for s in order:
        quad = _card_from_block(block, s)
        qarea = abs(float(cv2.contourArea(quad.astype(np.float32))))
        if qarea < barea or qarea > 6.0 * barea:
            continue
        try:
            card, _note = _warp_with_pad(bgr, quad)
        except Exception:
            continue
        if card is None or card.shape[0] < 50:
            continue
        f = _layout_features(card)
        score = layout_score(f) - 60.0 * _edge_dark_ratio(card)
        score -= 0.4 * abs(_CONTENT_TPL_ASP[s] - asp)
        if s == 'front' and f['skinFrac'] > 0.02 and f['skinCx'] > 0.55:
            score += 0.5
        if s == 'back' and f['redFrac'] > 0.02 and f['redCx'] < 0.5 and f['redCy'] < 0.55:
            score += 0.5
        if s == guess:
            score += 0.35
        cand.append((score, s, quad, layout_score(f), classify_layout(f)))
    if not cand:
        return []
    cand.sort(key=lambda c: -c[0])
    # 卡面得认得出是身份证（人脸/国徽位置对得上），否则宁可不用这条路，交给别的兜底
    if cand[0][0] < -12.0 or cand[0][3] < 1.0 or cand[0][4] == 'other':
        return []
    side_name = '正面' if cand[0][1] == 'front' else '背面'
    note = '内容补齐（%s）' % side_name
    quad = cand[0][2].astype(np.float32)
    if quad[:, 0].max() > W - 2 or quad[:, 1].max() > H - 2 or \
            quad[:, 0].min() < 1 or quad[:, 1].min() < 1:
        note += '·补边'
    # 往外推出来的边往往差几个像素，往里收一点，免得把卡外的背景带进来
    ctr = quad.mean(axis=0)
    quad = (quad - ctr) * 0.97 + ctr
    return [(order_points(quad), note)]


def _layout_features(card):
    """
    卡面内容统计（与参考项目 analyzeCard 同口径，全部旋转无关）：
    饱和红块占比/重心/横纵跨度、肤色占比/重心、深色人像块占比/重心。
    """
    H, W = card.shape[:2]
    w, h = 160, max(2, int(round(160.0 * H / max(1, W))))
    small = cv2.resize(card, (w, h), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.int16)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]

    bx, by = int(w * 0.08), int(h * 0.08)
    core = np.zeros((h, w), bool)
    core[by:h - by, bx:w - bx] = True

    # 饱和红：国徽 / 老证件红底照片
    red = core & (r > 115) & (r - g > 75) & (r - b > 55)
    # 肤色（排除偏暖的卡底）
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    skin = core & (r > 95) & (g > 40) & (b > 20) & (b < 190) & (mx - mn > 20) & \
        (r - g >= 22) & (r - g < 75) & (r > b) & (g >= b - 10)
    # 深色块（头发 / 深色衣领）
    dark = core & (r < 80) & (g < 80) & (b < 80)

    n = max(1, int(core.sum()))

    def cen(m):
        if not m.any():
            return 0.5, 0.5
        ys, xs = np.nonzero(m)
        return float(xs.mean() / w), float(ys.mean() / h)

    def ext(m):
        if not m.any():
            return 0.0, 0.0
        cols = np.nonzero(m.sum(axis=0) >= 2)[0]
        rows = np.nonzero(m.sum(axis=1) >= 2)[0]
        if cols.size == 0 or rows.size == 0:
            return 0.0, 0.0
        return (cols[-1] - cols[0] + 1) / float(w), (rows[-1] - rows[0] + 1) / float(h)

    rcx, rcy = cen(red)
    rex, rey = ext(red)
    scx, scy = cen(skin)
    dcx, dcy = cen(dark)
    return {
        'redFrac': float(red.sum()) / n, 'redCx': rcx, 'redCy': rcy,
        'redExtX': rex, 'redExtY': rey,
        'skinFrac': float(skin.sum()) / n, 'skinCx': scx, 'skinCy': scy,
        'darkFrac': float(dark.sum()) / n, 'darkCx': dcx, 'darkCy': dcy,
    }


def classify_layout(f):
    """按参考项目的判据判卡种：紧凑红块=国徽面；明显肤色+深色人像=人像面"""
    if f['redFrac'] > 0.02 and f['redExtX'] < 0.45 and f['redExtY'] < 0.5:
        return 'id_back'
    if f['skinFrac'] > 0.03 and f['darkFrac'] > 0.012:
        return 'id_front'
    if f['darkFrac'] > 0.02 and f['skinFrac'] > 0.008 and f['redFrac'] < 0.03:
        return 'id_front'
    return 'other'


def layout_score(f):
    """
    给"这个角度是不是正立"打分，越大越像正立。
    人像面：证件照在**右侧**、头在上（肤色/头发重心应在 (0.78, 0.30) 附近）
    国徽面：国徽在**左上**（红块重心应在 (0.15, 0.25) 附近）
    """
    s_front = -1e9
    if f['skinFrac'] > 0.012:
        s_front = 2.0 - 2.0 * abs(f['skinCx'] - 0.78) - 1.2 * abs(f['skinCy'] - 0.32)
    if f['darkFrac'] > 0.008:
        s_dark = 1.6 - 2.0 * abs(f['darkCx'] - 0.80) - 0.8 * abs(f['darkCy'] - 0.26)
        s_front = s_dark if s_front < -1e8 else max(s_front, s_dark) * 0.6 + s_front * 0.4
    s_back = -1e9
    if f['redFrac'] > 0.012 and f['redExtX'] < 0.55:
        s_back = 2.0 - 2.2 * abs(f['redCx'] - 0.16) - 1.4 * abs(f['redCy'] - 0.25)
    return max(s_front, s_back)


def orient4(card):
    """
    方向校正（参考 jrsdhr/idcard-a4-tool 的保守做法）：
      ① 竖拍先按"横排文字更像横的"转到横版；
      ② 再按内容特征决定要不要翻 180°：
         国徽面 —— 红块跑到右下（cx+cy > 1.1）才是倒了；
         人像面 —— 人脸明显在左半边（cx < 0.45）才是倒了；
      ③ 翻转后必须"正立分"确实更高才翻，避免把本来正的翻倒。
    返回 (card, kind, note)。
    """
    card2, lnote = to_landscape(card)

    f = _layout_features(card2)
    kind = classify_layout(f)
    flip = False
    if kind == 'id_back':
        flip = (f['redCx'] + f['redCy']) > 1.10
    elif kind == 'id_front':
        if f['skinFrac'] >= 0.03:
            flip = f['skinCx'] < 0.45
        elif f['redFrac'] > 0.02:
            flip = f['redCx'] < 0.42
    elif kind == 'other':
        return card2, kind, lnote or '方向已自动校正'

    r180 = cv2.rotate(card2, cv2.ROTATE_180)
    s0, s1 = layout_score(f), layout_score(_layout_features(r180))
    if flip or (s1 > s0 + 0.45):
        if s1 >= s0 - 0.15:                     # 翻过来确实更对才翻
            return r180, classify_layout(_layout_features(r180)), \
                (lnote + ' / 上下颠倒已翻正' if lnote else '上下颠倒已翻正')
    return card2, kind, lnote or '方向已自动校正'


def expand_quad(quad, frac=0.018):
    """把四角整体向外扩一点，保证卡的四条边都在框内（多出来的由贴边步骤切掉）"""
    p = np.asarray(quad, np.float32).reshape(4, 2)
    c = p.mean(axis=0)
    return c + (p - c) * (1.0 + frac)


def tighten_card(card, max_frac=0.055):
    """
    透视校正后按"真实卡边"再收一圈：
    在四条边附近找最强的那条线（卡边），它外侧若是平坦背景就切掉。
    既不会带进背景，也不会切掉卡内图案。
    """
    H, W = card.shape[:2]
    if H < 120 or W < 120:
        return card
    g = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.GaussianBlur(g, (3, 3), 0)
    gy = np.abs(cv2.Sobel(gb, cv2.CV_32F, 0, 1, ksize=3))
    gx = np.abs(cv2.Sobel(gb, cv2.CV_32F, 1, 0, ksize=3))
    cy0, cy1 = int(H * 0.30), int(H * 0.70)
    cx0, cx1 = int(W * 0.30), int(W * 0.70)
    ref_std = max(2.0, float(np.std(gb[cy0:cy1, cx0:cx1])))
    ref_mu = float(np.mean(gb[cy0:cy1, cx0:cx1]))

    def shift(profile, strip, lim):
        if profile.size < 3:
            return 0
        m = int(np.argmax(profile))
        if m < 2 or m > lim:
            return 0
        if profile[m] < (float(np.median(profile)) + 1e-6) * 2.0:
            return 0
        if float(np.std(strip)) > max(6.0, 0.5 * ref_std):
            return 0
        if abs(float(np.mean(strip)) - ref_mu) < 6.0:
            return 0
        return m - 1

    lim_y = max(3, int(max_frac * H))
    lim_x = max(3, int(max_frac * W))

    top_prof = gy[:lim_y, cx0:cx1].mean(axis=1)
    mt = int(np.argmax(top_prof))
    top = shift(top_prof, g[:max(1, mt), cx0:cx1], lim_y)

    bot_prof = gy[H - lim_y:][::-1, cx0:cx1].mean(axis=1)
    mb = int(np.argmax(bot_prof))
    bottom = shift(bot_prof, g[H - max(1, mb):, cx0:cx1], lim_y)

    lef_prof = gx[cy0:cy1, :lim_x].mean(axis=0)
    ml = int(np.argmax(lef_prof))
    left = shift(lef_prof, g[cy0:cy1, :max(1, ml)], lim_x)

    rig_prof = gx[cy0:cy1, W - lim_x:][:, ::-1].mean(axis=0)
    mr = int(np.argmax(rig_prof))
    right = shift(rig_prof, g[cy0:cy1, W - max(1, mr):], lim_x)

    if top + bottom + left + right == 0:
        return card
    if H - top - bottom < H * 0.75 or W - left - right < W * 0.75:
        return card
    return card[top:H - bottom, left:W - right]


def _text_row_score(card):
    """横排文字越像横的，分越高（判断卡片有没有被转了 90°）"""
    g = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (3, 3), 0)
    gy = np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    gx = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))
    H, W = g.shape
    ey = float(gy[int(H * 0.14):int(H * 0.86), int(W * 0.06):int(W * 0.94)].mean())
    ex = float(gx[int(H * 0.14):int(H * 0.86), int(W * 0.06):int(W * 0.94)].mean())
    return ey / (ex + 1e-6)


def _red_mass(card):
    """最大的红色块（国徽）占比与重心"""
    H, W = card.shape[:2]
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    red = (((h <= 12) | (h >= 168)) & (s >= 95) & (v >= 55)).astype(np.uint8) * 255
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    cnts, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return 0.0, 0.5, 0.5
    c = max(cnts, key=cv2.contourArea)
    a = cv2.contourArea(c) / float(W * H)
    m = cv2.moments(c)
    if m['m00'] <= 0:
        return 0.0, 0.5, 0.5
    return a, m['m10'] / m['m00'] / W, m['m01'] / m['m00'] / H


def _emblem(card):
    """
    找"国徽"：红色、近方形的一块（红色大字是细长条，会被排除）。
    返回 (面积占比, 重心x, 重心y)；找不到返回 (0.0, .5, .5)。
    """
    H, W = card.shape[:2]
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    red = (((h <= 14) | (h >= 165)) & (s >= 70) & (v >= 45)).astype(np.uint8)
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    cnts, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = (0.0, 0.5, 0.5)
    for c in cnts:
        a = cv2.contourArea(c) / float(W * H)
        if a < 0.0035:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        ar = bw / float(max(1, bh))
        fill = a / max(1e-6, (bw * bh) / float(W * H))
        # 国徽：近方形、比较实心（不是一条细字）
        if 0.55 <= ar <= 1.95 and fill >= 0.40 and a > best[0]:
            m = cv2.moments(c)
            if m['m00'] > 0:
                best = (a, m['m10'] / m['m00'] / W, m['m01'] / m['m00'] / H)
    return best


def _dark_blob(card, th=70):
    """
    找"人像暗块"（头发/深色衣领）：卡片上最大的一块深色实心区域。
    返回 (面积占比, 重心x, 重心y)；找不到返回 (0.0, .5, .5)。
    身份证正面靠它判上下颠倒——头发永远在该块的上方，倒了就会跑到下面。
    """
    H, W = card.shape[:2]
    g = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY)
    m = (g < th).astype(np.uint8)
    # 掐掉外圈，避免卡边的深色边被当成"头发"
    m[: int(H * 0.06), :] = 0
    m[int(H * 0.94):, :] = 0
    m[:, : int(W * 0.05)] = 0
    m[:, int(W * 0.95):] = 0
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = (0.0, 0.5, 0.5)
    for c in cnts:
        a = cv2.contourArea(c) / float(W * H)
        if a < 0.006:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        fill = a / max(1e-6, (bw * bh) / float(W * H))
        if fill < 0.35 or not (0.45 <= bw / float(max(1, bh)) <= 2.2):
            continue
        mm = cv2.moments(c)
        if mm['m00'] <= 0:
            continue
        cx = mm['m10'] / mm['m00'] / W
        cy = mm['m01'] / mm['m00'] / H
        # 越靠左越像证件照里的人像（优先面积大的）
        if a > best[0]:
            best = (a, cx, cy)
    return best


def _skin_blob(card):
    """最像"证件照"的肤色块：返回 (面积占比, bbox 宽, bbox 高, 填充率, 重心x, 重心y)"""
    H, W = card.shape[:2]
    sm = _skin_mask(card).astype(np.uint8)
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    red = (((h <= 12) | (h >= 168)) & (s >= 95) & (v >= 55)).astype(np.uint8)
    sm = sm & (1 - red)
    sm = cv2.morphologyEx(sm, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    sm = cv2.morphologyEx(sm, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    cnts, _ = cv2.findContours(sm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return 0.0, 0, 0, 0.0, 0.5, 0.5
    c = max(cnts, key=cv2.contourArea)
    a = cv2.contourArea(c) / float(W * H)
    bb = cv2.boundingRect(c)
    bw, bh = bb[2], bb[3]
    fill = a / max(1e-6, (bw * bh) / float(W * H))
    m = cv2.moments(c)
    cx = (m['m10'] / m['m00'] / W) if m['m00'] > 0 else 0.5
    cy = (m['m01'] / m['m00'] / H) if m['m00'] > 0 else 0.5
    return a, bw, bh, fill, cx, cy


def card_kind(card):
    """
    卡种粗判：
      身份证背面 —— 红色面积大（国徽 + 两行红字约占 2% 以上）
      身份证正面 —— 有人像暗块（头发），且红色很少
      其他卡     —— 都不是
    只用旋转无关特征，90°/180° 都不影响。
    """
    red_a, _, _ = _red_mass(card)
    ea, ex, ey = _emblem(card)
    da, dx, dy = _dark_blob(card)
    if ea >= 0.012 and red_a >= 0.018:
        return 'id_back'
    if red_a >= 0.030:
        return 'id_back'
    if da >= 0.008 and dx <= 0.62 and red_a < 0.018:
        return 'id_front'
    a, bw, bh, fill, cx, cy = _skin_blob(card)
    if a >= 0.05 and 1.0 <= bh / max(1.0, float(bw)) <= 2.0 and red_a < 0.012:
        return 'id_front'
    return 'other'


def to_landscape(card):
    """竖着的卡（照片里拍成竖的）转成横版：比较两个 90° 方向谁的横排文字更像"""
    if card.shape[0] <= card.shape[1] * 1.15:
        return card, ''
    r90 = cv2.rotate(card, cv2.ROTATE_90_CLOCKWISE)
    r270 = cv2.rotate(card, cv2.ROTATE_90_COUNTERCLOCKWISE)
    s90 = _text_row_score(r90)
    s270 = _text_row_score(r270)
    pick90 = s90 >= s270
    # 文字分打平时（银行卡这种横条纹理），看"证件照/芯片"该在左边来判断方向
    if abs(s90 - s270) <= 0.15 * max(s90, s270, 1e-6):
        a90, *_ = _skin_blob(r90)
        a270, b270, h270, f270, cx270, cy270 = _skin_blob(r270)
        if a90 >= 0.012 and a270 >= 0.012:
            cx90 = _skin_blob(r90)[4]
            pick90 = cx90 < cx270
    if pick90:
        return r90, '旋转90°摆正'
    return r270, '旋转90°摆正(逆)'


def _tf_apply(img, k):
    """k: 0 原样 / 1 转180 / 2 左右镜像 / 3 镜像+转180"""
    if k == 0:
        return img
    if k == 1:
        return cv2.rotate(img, cv2.ROTATE_180)
    if k == 2:
        return cv2.flip(img, 1)
    return cv2.flip(cv2.rotate(img, cv2.ROTATE_180), 1)


def _tf_point(x, y, k):
    if k == 0:
        return x, y
    if k == 1:
        return 1.0 - x, 1.0 - y
    if k == 2:
        return 1.0 - x, y
    return x, 1.0 - y


def orient_180(card, kind):
    """
    按"身份证该长什么样"硬性规定方向——这是唯一可靠的办法：
      正面：人像（头发暗块）必定在**左上**
      背面：国徽必定在**左上**
    先量出当前位置，再从 4 种变换（原样 / 转180 / 左右镜像 / 镜像+转180）里
    挑一种把特征送回左上，差多少一目了然。这样连"被镜像"的图也能救回来。
    普通卡（银行卡等）没有这种硬约束，不翻。
    """
    if kind == 'id_front':
        # 二代证人像面：文字在左、照片在**右侧**，人像头部在右上
        da, dx, dy = _dark_blob(card)
        if da >= 0.006:
            px, py, tx, ty, tf = dx, dy, 0.78, 0.27, '人像'
        else:
            a, bw, bh, fill, cx, cy = _skin_blob(card)
            if a < 0.05:
                return card, '方向已自动校正'
            px, py, tx, ty, tf = cx, cy, 0.76, 0.30, '人像'
    elif kind == 'id_back':
        ea, ex, ey = _emblem(card)
        if ea >= 0.004:
            px, py, tx, ty, tf = ex, ey, 0.14, 0.24, '国徽'
        else:
            ra, rx, ry = _red_mass(card)
            if ra < 0.004:
                return card, '方向已自动校正'
            px, py, tx, ty, tf = rx, ry, 0.15, 0.25, '红字'
    else:
        return card, '方向已自动校正'

    best = None
    # 手机拍照不会产生左右镜像，镜像会把文字反掉——只在极端情况（误差很大）才允许
    for k in (0, 1, 2, 3):
        qx, qy = _tf_point(px, py, k)
        err = abs(qx - tx) * 1.35 + abs(qy - ty) + (0.55 if k >= 2 else 0.0)
        if best is None or err < best[0] - 1e-9:
            best = (err, k)
    k = best[1]
    if k == 0:
        return card, '方向已自动校正'
    if k == 1:
        return _tf_apply(card, 1), '上下颠倒已翻正'
    if k == 2:
        return _tf_apply(card, 2), '左右镜像已修正'
    return _tf_apply(card, 3), '倒置+镜像已修正'


def auto_orient_full(card, kind='id'):
    """兼容旧接口：先摆正 90°，再按 kind 判 180°"""
    card, n1 = to_landscape(card)
    if kind in (None, 'id'):
        kind = card_kind(card)
    card, n2 = orient_180(card, kind)
    note = ' / '.join(x for x in (n1, n2) if x)
    return card, note


def warp_card_soft(bgr, quad):
    """外扩一点再校正，给 tighten_card 留出可切的背景"""
    card, note = warp_to_card(bgr, expand_quad(quad, 0.018))
    card = tighten_card(card)
    return card, note


# ---------------------------------------------------------------- 校正与美化
def warp_to_card(bgr, quad=None):
    """透视校正到标准尺寸，返回 (卡片图 BGR, 说明)"""
    if quad is None:
        H, W = bgr.shape[:2]
        target_ar = CARD_ASPECT
        if W / float(H) > target_ar:                     # 太宽 -> 左右切
            nw = int(H * target_ar)
            x0 = (W - nw) // 2
            crop = bgr[:, x0:x0 + nw]
        else:                                            # 太高 -> 上下切
            nh = int(W / target_ar)
            y0 = (H - nh) // 2
            crop = bgr[y0:y0 + nh, :]
        card = cv2.resize(crop, (CARD_W, CARD_H), interpolation=cv2.INTER_CUBIC)
        return card, '整图裁切（未检测到卡片边缘）'

    pts = order_points(quad)
    tl, tr, br, bl = pts
    w = max(_norm(tr - tl), _norm(br - bl))
    h = max(_norm(bl - tl), _norm(br - tr))

    if w >= h:
        tw, th = CARD_W, CARD_H
        dst = np.float32([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]])
        M = cv2.getPerspectiveTransform(np.float32([tl, tr, br, bl]), dst)
        card = cv2.warpPerspective(bgr, M, (tw, th), flags=cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_REPLICATE)
        return card, '已校正'
    else:
        tw, th = CARD_H, CARD_W
        dst = np.float32([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]])
        M = cv2.getPerspectiveTransform(np.float32([tl, tr, br, bl]), dst)
        card = cv2.warpPerspective(bgr, M, (tw, th), flags=cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_REPLICATE)
        return card, '已校正（竖版）'


def trim_to_content(bgr, max_frac=0.045, std_ratio=0.50):
    """
    校正后的卡片如果比真卡大了一圈（多带进来桌面/桌面阴影），这里按"背景很平、
    且明显比卡片亮或明显更暗"的判据，把四周多余的窄条切掉。
    卡片本身有防伪底纹和印刷内容，纹理方差大，不会被误切。
    """
    H, W = bgr.shape[:2]
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mu = cv2.boxFilter(g, -1, (5, 5))
    var = cv2.boxFilter(g * g, -1, (5, 5)) - mu * mu
    std = np.sqrt(np.maximum(var, 0.0))

    ci = slice(int(H * 0.25), int(H * 0.75))
    cj = slice(int(W * 0.25), int(W * 0.75))
    ref_std = float(np.median(std[ci, cj]))
    ref_b = float(np.median(g[ci, cj]))
    if ref_std < 3.0:
        ref_std = 3.0

    lim_y, lim_x = int(max_frac * H), int(max_frac * W)

    def is_bg(vals_std, vals_b):
        s = float(np.mean(vals_std))
        b = float(np.mean(vals_b))
        return s < std_ratio * ref_std and (b > ref_b + 9 or b < ref_b - 35)

    top = bottom = left = right = 0
    while top < lim_y and is_bg(std[top], g[top]):
        top += 1
    while bottom < lim_y and is_bg(std[H - 1 - bottom], g[H - 1 - bottom]):
        bottom += 1
    while left < lim_x and is_bg(std[:, left], g[:, left]):
        left += 1
    while right < lim_x and is_bg(std[:, W - 1 - right], g[:, W - 1 - right]):
        right += 1

    if top == 0 and bottom == 0 and left == 0 and right == 0:
        return bgr
    if H - top - bottom < H * 0.5 or W - left - right < W * 0.5:
        return bgr
    return bgr[top:H - bottom, left:W - right]


def trim_dark_border(bgr, max_frac=0.060, gap=22):
    """去掉校正后残留的深色背景窄边"""
    H, W = bgr.shape[:2]
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    ref = float(np.mean(g[int(H * 0.2):int(H * 0.8), int(W * 0.2):int(W * 0.8)]))
    maxpx = max(1, int(max_frac * min(H, W)))
    top = bottom = left = right = 0

    while top < maxpx and top + 20 < H // 2 and float(np.mean(g[top])) < ref - gap:
        top += 1
    while bottom < maxpx and bottom + 20 < H // 2 and float(np.mean(g[H - 1 - bottom])) < ref - gap:
        bottom += 1
    while left < maxpx and left + 20 < W // 2 and float(np.mean(g[:, left])) < ref - gap:
        left += 1
    while right < maxpx and right + 20 < W // 2 and float(np.mean(g[:, W - 1 - right])) < ref - gap:
        right += 1

    if top + bottom + left + right == 0:
        return bgr
    return bgr[top:H - bottom, left:W - right]


def crop_to_card_rect(card, max_frac=0.12):
    """
    校正后卡面上如果还留着一圈桌面（木纹、桌布、阴影），把"整块卡面"重新框一次。
    做法同参考项目：Otsu 二值 → 开闭运算 → 取包含画面中心的最大连通域 → 凸包 → 外接矩形裁切。
    只接受"裁剪量不大、长宽比仍然对"的结果，否则原样返回。
    """
    H, W = card.shape[:2]
    if H < 80 or W < 80:
        return card
    g = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # 卡面可能是浅色（身份证）也可能是深色（部分银行卡）：按中心区域亮度决定极性
    c = float(np.mean(g[int(H * 0.35):int(H * 0.65), int(W * 0.35):int(W * 0.65)]))
    if c < 128:
        th = cv2.bitwise_not(th)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return card
    best = None
    for cc in cnts:
        a = cv2.contourArea(cc)
        if a < 0.55 * H * W:
            continue
        x, y, w, h = cv2.boundingRect(cc)
        inside_center = (x <= W * 0.4 <= x + w) or (x <= W * 0.5 <= x + w)
        if not inside_center:
            continue
        if best is None or a > best[0]:
            best = (a, (x, y, w, h))
    if best is None:
        return card
    x, y, w, h = best[1]
    # 裁太多说明框错了，宁可不动
    if (x > max_frac * W or y > max_frac * H or
            (W - x - w) > max_frac * W or (H - y - h) > max_frac * H):
        return card
    if x == 0 and y == 0 and w == W and h == H:
        return card
    if w < 0.75 * W or h < 0.75 * H:
        return card
    out = card[y:y + h, x:x + w]
    asp = w / float(h)
    if not (1.38 <= asp <= 1.82):
        return card
    return out


def snap_border(card, max_frac=0.055, min_step=9.0):
    """
    拍照→放大会在卡边留下一圈"融合的暗边"（渐变，不是纯色），阈值裁剪切不干净。
    这里改成找"台阶"：从每条边往里看灰度曲线的最大跳变位置，那里就是真实卡边，
    直接切到台阶内侧。只在跳变足够强、位置合理时才动。
    """
    H, W = card.shape[:2]
    if H < 80 or W < 80:
        return card
    g = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lim_y = max(2, int(max_frac * H))
    lim_x = max(2, int(max_frac * W))

    def find_step(prof, lim):
        """返回该边应该切掉多少像素：优先找明显台阶，退而求其次找"暗边渐变"的结束点"""
        seg = prof[:lim + 6]
        if seg.size < 6:
            return 0
        sm = cv2.blur(seg.reshape(-1, 1), (1, 5)).ravel()
        d = np.abs(np.diff(sm))
        if d.size == 0:
            return 0
        i = int(np.argmax(d))
        if d[i] >= min_step:                 # 清晰的台阶（卡边与桌面）
            return i + 1
        # 渐变型暗边：从最陡处一直走到坡度缓下来，位置就是卡边
        if d[i] < 1.5:
            return 0
        j = i
        while j < min(lim, d.size - 1) and d[j] >= 1.0:
            j += 1
        return j if 2 <= j <= lim else 0

    top = find_step(g.mean(axis=1), lim_y)
    bottom = find_step(g[::-1].mean(axis=1), lim_y)
    left = find_step(g.mean(axis=0), lim_x)
    right = find_step(g[:, ::-1].mean(axis=0), lim_x)

    if top in (0, H // 2) or bottom in (0, H // 2) or left in (0, W // 2) or right in (0, W // 2):
        return card
    if H - top - bottom < 0.8 * H or W - left - right < 0.8 * W:
        return card
    return card[top:H - bottom, left:W - right]


def auto_clean(bgr):
    """
    像“复印机”那样处理：
      1) 抹平光照不均（手机闪光灯、手影造成的明暗差）
      2) 把纸面白位拉到接近纯白、黑位压到纯黑
      3) 白平衡微调 + 轻微锐化
    只动亮度和色温，不改变证件本身的颜色和内容。
    """
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    L = l.astype(np.float32)
    H, W = L.shape

    # 1) 均光：用大尺度模糊估计纸面亮度场，再按场归一化
    sig = max(H, W) * 0.09
    bg = cv2.GaussianBlur(L, (0, 0), sig)
    bg = np.maximum(bg, 18.0)
    L = np.clip(L * (float(np.mean(bg)) / bg), 0, 255)

    # 2) 黑白位拉伸
    lo = float(np.percentile(L, 1.5))
    hi = float(np.percentile(L, 95.0))
    hi = max(hi, lo + 60.0)
    gain = 255.0 / (hi - lo)
    gain = max(0.90, min(gain, 2.60))
    L = np.clip((L - lo) * gain, 0, 255)

    # 2b) 黑色还偏灰的话，用 gamma 把白位继续抬起来（不动暗部）
    p90 = float(np.percentile(L, 90))
    if p90 < 232:
        gamma = float(np.log(0.94) / np.log(max(p90, 60) / 255.0))
        gamma = max(0.55, min(gamma, 1.15))
        L = 255.0 * np.power(np.clip(L, 0, 255) / 255.0, gamma)

    if float(np.mean(L)) < 120:              # 明显偏暗再补一点局部对比
        cl = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(
            L.astype(np.uint8)).astype(np.float32)
        L = np.clip(0.55 * L + 0.45 * cl, 0, 255)

    # 3) 轻微白平衡（偏黄/偏蓝的灯光）
    for idx, ch in ((1, a), (2, b)):
        m = float(np.mean(ch))
        if abs(m - 128.0) > 6.0:
            shift = (m - 128.0) * 0.55
            fixed = np.clip(ch.astype(np.float32) - shift, 0, 255).astype(np.uint8)
            if idx == 1:
                a = fixed
            else:
                b = fixed

    out = cv2.cvtColor(cv2.merge([L.astype(np.uint8), a, b]), cv2.COLOR_LAB2BGR)
    blur = cv2.GaussianBlur(out, (0, 0), 1.1)
    out = cv2.addWeighted(out, 1.18, blur, -0.18, 0)
    return out


def _skin_mask(card):
    """肤色区域（身份证头像照的判定用）"""
    ycrcb = cv2.cvtColor(card, cv2.COLOR_BGR2YCrCb)
    y, cr, cb = cv2.split(ycrcb)
    m = ((cr >= 133) & (cr <= 175) & (cb >= 75) & (cb <= 130) & (y >= 50))
    return m.astype(np.uint8)


def orient_score(card):
    """
    给"这张卡片是否正立"打分，越正立分数越高。三条依据（按可靠性排序）：
      1) 背面红色国徽：正立时在左半边；
      2) 正面证件照：正立时照片在左半边，肤色集中在左侧；
      3) 证件照偏暗：正立时左半边整体比右半边暗一点。
    """
    H, W = card.shape[:2]
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    score = 0.0

    # 1) 红色国徽
    red = (((h <= 12) | (h >= 168)) & (s >= 95) & (v >= 55)).astype(np.uint8) * 255
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    cnts, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if cnts:
        c = max(cnts, key=cv2.contourArea)
        a = cv2.contourArea(c) / float(W * H)
        m = cv2.moments(c)
        if 0.004 <= a <= 0.20 and m['m00'] > 0:
            cx = m['m10'] / m['m00'] / float(W)
            score += float(np.clip((0.52 - cx) * 3.0, -1.0, 1.0))

    # 2) 肤色集中在一侧（证件照）
    sm = _skin_mask(card)
    left_skin = float(sm[:, :int(W * 0.42)].mean())
    right_skin = float(sm[:, W - int(W * 0.42):].mean())
    if max(left_skin, right_skin) > 0.02:
        score += float(np.clip((left_skin - right_skin) * 6.0, -1.0, 1.0))

    # 3) 左右明暗（证件照一侧更暗）
    g = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY).astype(np.float32)
    band = int(H * 0.10)
    lw = int(W * 0.35)
    left = float(np.mean(g[band:H - band, int(W * 0.03):int(W * 0.03) + lw]))
    right = float(np.mean(g[band:H - band, W - int(W * 0.03) - lw:W - int(W * 0.03)]))
    score += float(np.clip((right - left) / 34.0, -0.5, 0.5))
    return score


def auto_orient(card, margin=0.35):
    """自动判断是不是上下颠倒了，需要的话转 180°"""
    s0 = orient_score(card)
    r180 = cv2.rotate(card, cv2.ROTATE_180)
    if orient_score(r180) > s0 + margin:
        return r180, '自动翻正180°'
    return card, '方向自动'


def best_landscape(card):
    """竖版校正出来的卡片 -> 在顺/逆两个 90° 方向里挑更像正立的那个"""
    cw = cv2.rotate(card, cv2.ROTATE_90_CLOCKWISE)
    ccw = cv2.rotate(card, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if orient_score(ccw) > orient_score(cw) + 0.10:
        return ccw, '转正90°(逆)'
    return cw, '转正90°(顺)'


def _sanitize_quad(quad, W, H, manual=False):
    """校验/纠正四个角点：必须 4 点、在画面内、面积足够、凸四边形。
    manual=True 是用户手点的四角：会先按角度重排（交叉/乱序也能用），
    碰到角点重合或边太短时自动用最小外接矩形兜住，尽量不拒绝。
    不合格返回 None（调用方退回自动识别，绝不抛异常）。"""
    try:
        q = np.asarray(quad, np.float32).reshape(-1, 2)
    except Exception:
        return None
    if q.shape[0] == 4:
        pass
    elif q.shape[0] > 4:                     # 多点：取凸包近似成 4 点
        try:
            hull = cv2.convexHull(q.reshape(-1, 1, 2))
            q = hull.reshape(-1, 2).astype(np.float32)
            if q.shape[0] != 4:
                return None
        except Exception:
            return None
    else:
        return None
    if not np.all(np.isfinite(q)):
        return None

    def _fix_rect(qq):
        """用最小外接矩形兜住（保留用户指的大致范围），再夹回画面内。"""
        rect = cv2.minAreaRect(qq.reshape(-1, 1, 2))
        if min(rect[1]) < 25:
            return None
        box = order_points(cv2.boxPoints(rect)).astype(np.float32)
        box[:, 0] = np.clip(box[:, 0], inset, max(W - 1 - inset, inset))
        box[:, 1] = np.clip(box[:, 1], inset, max(H - 1 - inset, inset))
        return box

    inset = 0.5
    q[:, 0] = np.clip(q[:, 0], inset, max(W - 1 - inset, inset))
    q[:, 1] = np.clip(q[:, 1], inset, max(H - 1 - inset, inset))
    if manual:
        q = _angle_order(q)                  # 先排成简单四边形，杜绝交叉
    q = order_points(q).astype(np.float32)
    thr = 0.004 if manual else 0.04          # 手点的框允许更小
    try:
        area = float(abs(cv2.contourArea(q.reshape(-1, 1, 2))))
        hull_a = float(abs(cv2.contourArea(
            cv2.convexHull(q.reshape(-1, 1, 2)))))
    except Exception:
        return None
    if area < float(W) * float(H) * thr:      # 太小：多半是误点
        if not manual:
            return None
        q = _fix_rect(q)                      # 手点：聚成一小片就按矩形兜住
        if q is None:
            return None
    elif hull_a > 0 and area / hull_a < 0.85:  # 蝴蝶结/自交：先取凸包
        try:
            q = order_points(cv2.convexHull(q.reshape(-1, 1, 2)).reshape(-1, 2))
        except Exception:
            return None
    edges = [float(np.linalg.norm(q[(i + 1) % 4] - q[i])) for i in range(4)]
    min_edge = 6.0 if manual else max(12.0, 0.01 * min(W, H))
    if min(edges) < min_edge:
        if not manual:
            return None
        # 有两个角点重合/贴在一起：用最小外接矩形兜住
        q = _fix_rect(q)
        if q is None:
            return None
        edges = [float(np.linalg.norm(q[(i + 1) % 4] - q[i])) for i in range(4)]
        if min(edges) < 6.0:
            return None
    return q.astype(np.float32)


def detect_quad_auto(bgr):
    """给「手动框选四角」窗口用的初始四角：把几种识别结果都试一遍，
    选最像银行卡的那个（比例接近 1.585、面积不是整帧）。识别不到返回 None。
    注意：这里不抛出异常。"""
    W, H = bgr.shape[1], bgr.shape[0]
    cands = []
    for fn in ('detect_cards_ref', 'detect_card_by_content'):
        try:
            got = globals()[fn](bgr, max_cards=1) if fn == 'detect_cards_ref' else globals()[fn](bgr)
        except Exception:
            got = None
        if not got:
            continue
        for item in got:
            quad = item[0] if isinstance(item, (list, tuple)) else item
            try:
                q = _sanitize_quad(quad, W, H)
            except Exception:
                q = None
            if q is not None:
                cands.append(q)
    if not cands:
        try:
            q0, _ = detect_card(bgr)
            q = _sanitize_quad(q0, W, H) if q0 is not None else None
            if q is not None:
                cands.append(q)
        except Exception:
            pass

    def _score(q):
        e = [float(np.linalg.norm(q[(i + 1) % 4] - q[i])) for i in range(4)]
        a = abs(cv2.contourArea(q.reshape(-1, 1, 2)))
        frac = a / float(W * H)
        asp = max(e) / max(min(e), 1.0)
        s = -abs(np.log(max(asp, 1e-3) / CARD_ASPECT)) * 4.0
        if frac > 0.9:                       # 整帧：不是卡
            s -= 8.0
        elif frac < 0.06:
            s -= 4.0
        return s

    try:
        return max(cands, key=_score)
    except Exception:
        return None


def process_image(path, rotate='自动', clean=True, quad_override=None, max_cards=3,
                  manual_deg=0, bright=0.0, contrast=1.0):
    """
    处理一张照片，返回 [{'card': BGR, 'note': 说明, 'kind': 卡种}]。
    一张图里有正反面（两张卡）时会返回两组。
    manual_deg: 手动翻转角度（0/90/180/270），非 0 时完全听用户的，不再自动判断方向。
    bright: 手动亮度微调（-80 ~ +80）；contrast: 手动对比度（0.5 ~ 2.0）。
    """
    bgr = read_image(path)
    quads = []
    if quad_override is not None:
        q = _sanitize_quad(quad_override, bgr.shape[1], bgr.shape[0], manual=True)
        if q is not None:
            quads = [(q, '手动框选')]
    if not quads:
        # 先用参考项目的路线（背景色差 + Otsu + 最大连通域），识别不到再退回旧算法
        quads = detect_cards_ref(bgr, max_cards=max_cards)
        if not quads:
            # 卡被画面切掉一截：靠卡面文字块把整卡补出来
            quads = detect_card_by_content(bgr)
        if not quads:
            quads = detect_cards(bgr, max_cards=max_cards)

    cards = []
    if not quads:
        card, note = warp_to_card(bgr, None)
        cards.append({'card': card, 'note': note, 'kind': 'other'})
    else:
        for q, note in quads:
            try:
                card, wnote = warp_card_soft(bgr, q)
            except Exception:
                card, wnote = warp_to_card(bgr, q)
                note = '粗略'
            cards.append({'card': card, 'note': f'{note} / {wnote}', 'kind': None})

    for item in cards:
        card = item['card']

        # 手动翻转优先（用户点了「翻转」，就完全按用户的来）
        if manual_deg:
            deg = int(manual_deg) % 360
            if deg == 90:
                card = cv2.rotate(card, cv2.ROTATE_90_CLOCKWISE)
            elif deg == 180:
                card = cv2.rotate(card, cv2.ROTATE_180)
            elif deg == 270:
                card = cv2.rotate(card, cv2.ROTATE_90_COUNTERCLOCKWISE)
            item['note'] += f' / 手动翻转{deg}°'

        auto = (not manual_deg) and rotate in ('自动', None, 'auto', '')
        if not auto:
            card, lnote = to_landscape(card)
            if lnote:
                item['note'] += ' / ' + lnote

        if manual_deg:
            # 手动已定，不再自动转
            item['kind'] = card_kind(card)
        elif auto:
            card, kind, onote = orient4(card)
            item['kind'] = kind
            item['note'] += ' / ' + onote
            if card.shape[0] > card.shape[1] * 1.05:      # 仍是竖版：老判据兜底
                card, lnote2 = to_landscape(card)
                item['note'] += ' / ' + lnote2
        else:
            kind = card_kind(card)
            item['kind'] = kind
            try:
                deg = int(str(rotate).replace('度', '').strip())
            except Exception:
                deg = 0
            deg = {'顺时针90': 90, '逆时针90': -90, '90': 90, '180': 180, '270': -90,
                   '-90': -90}.get(rotate, deg)
            if deg % 360 == 90:
                card = cv2.rotate(card, cv2.ROTATE_90_CLOCKWISE)
            elif deg % 360 == 180:
                card = cv2.rotate(card, cv2.ROTATE_180)
            elif deg % 360 == 270:
                card = cv2.rotate(card, cv2.ROTATE_90_COUNTERCLOCKWISE)

        try:
            card = trim_to_content(card)
            # 残留的桌面/阴影窄边可能不止一道（先切掉一道，剩下的又露出来），重复几遍
            for _ in range(3):
                before = card
                card = trim_dark_border(card)
                if card.shape == before.shape:
                    break
        except Exception:
            pass
        try:
            card = crop_to_card_rect(card)
        except Exception:
            pass
        try:
            card = snap_border(card)
            card = snap_border(card)
        except Exception:
            pass
        if card.shape[1] != CARD_W or card.shape[0] != CARD_H:
            card = cv2.resize(card, (CARD_W, CARD_H), interpolation=cv2.INTER_AREA)
        try:
            if clean:
                card = auto_clean(card)
        except Exception:
            pass
        if bright or abs(contrast - 1.0) > 1e-3:
            f = card.astype(np.float32) * float(contrast) + float(bright)
            card = np.clip(f, 0, 255).astype(np.uint8)
            item['note'] += f' / 亮度{bright:+.0f} 对比度{contrast:.2f}'
        item['card'] = card

    # 一张图里同时有正反面：正面排前面
    if len(cards) >= 2:
        cards.sort(key=lambda it: {'id_front': 0, 'id_back': 1}.get(it['kind'], 2))
    return cards


def process_one(path, rotate='自动', clean=True, quad_override=None):
    cards = process_image(path, rotate=rotate, clean=clean, quad_override=quad_override)
    if not cards:
        raise RuntimeError('没有识别到卡片')
    return cards[0]['card'], cards[0]['note']


# ---------------------------------------------------------------- A4 排版
def _blank():
    return Image.new('RGB', (A4_W, A4_H), 'white')


def _paste(page, card_bgr, x, y, marks=False, mask=None, corner_mm=CORNER_R_MM):
    img = Image.fromarray(cv2.cvtColor(card_bgr, cv2.COLOR_BGR2RGB))
    pm = Image.fromarray(mask) if mask is not None else None
    x, y = int(x), int(y)
    page.paste(img, (x, y), pm)

    # 卡片的浅灰细描边：让圆角轮廓在白纸上看得出来
    from PIL import ImageDraw
    r = int(round(max(0.0, corner_mm) * MM2PX))
    d = ImageDraw.Draw(page)
    d.rounded_rectangle([x, y, x + CARD_W - 1, y + CARD_H - 1], radius=r,
                        outline=(214, 214, 214), width=2)

    if marks:
        d.rectangle([x, y, x + CARD_W - 1, y + CARD_H - 1],
                    outline=(190, 190, 190), width=1)


def build_pages(cards, layout='auto', gap_mm=10.0, marks=False, mask=None,
                corner_mm=CORNER_R_MM):
    n = len(cards)
    if n == 0:
        return []
    if layout == 'auto':
        layout = 'single' if n == 1 else ('pair' if n == 2 else 'grid')

    gap = int(round(gap_mm * MM2PX))
    pages = []

    if layout == 'single':
        for c in cards:
            p = _blank()
            _paste(p, c, (A4_W - CARD_W) // 2, (A4_H - CARD_H) // 2, marks,
                   mask, corner_mm)
            pages.append(p)

    elif layout == 'pair':                       # 正反面上下排在同一页
        for i in range(0, n, 2):
            p = _blank()
            x = (A4_W - CARD_W) // 2
            if i + 1 < n:
                block = 2 * CARD_H + gap
                y0 = (A4_H - block) // 2
                _paste(p, cards[i], x, y0, marks, mask, corner_mm)
                _paste(p, cards[i + 1], x, y0 + CARD_H + gap, marks, mask, corner_mm)
            else:
                _paste(p, cards[i], x, (A4_H - CARD_H) // 2, marks, mask, corner_mm)
            pages.append(p)

    else:                                        # 2 x 4 网格，一页最多 8 张
        cols, rows = 2, 4
        per = cols * rows
        for s in range(0, n, per):
            chunk = cards[s:s + per]
            used = max(1, int(math.ceil(len(chunk) / float(cols))))   # 按实际张数垂直居中
            block_w = cols * CARD_W + (cols - 1) * gap
            block_h = used * CARD_H + (used - 1) * gap
            x0 = (A4_W - block_w) // 2
            y0 = (A4_H - block_h) // 2
            p = _blank()
            for k, c in enumerate(chunk):
                r, col = divmod(k, cols)
                _paste(p, c, x0 + col * (CARD_W + gap),
                       y0 + r * (CARD_H + gap), marks, mask, corner_mm)
            pages.append(p)

    return pages


def save_output(pages, out_dir, prefix='身份证A4打印'):
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    pdf_path = os.path.join(out_dir, f'{prefix}_{ts}.pdf')
    pages[0].save(pdf_path, 'PDF', resolution=float(DPI),
                  save_all=True, append_images=pages[1:])
    png_path = os.path.join(out_dir, f'{prefix}_预览_第1页_{ts}.png')
    pages[0].save(png_path, dpi=(DPI, DPI))
    return pdf_path, png_path


def save_card_images(cards, out_dir, mask=None, names=None, prefix='证件卡片',
                     corner_mm=CORNER_R_MM):
    """把每张证件单独存成一张图片（带透明圆角，300 DPI，85.6×54mm）"""
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime('%H%M%S')
    paths = []
    for i, c in enumerate(cards, 1):
        rgb = cv2.cvtColor(c, cv2.COLOR_BGR2RGB)
        if mask is not None:
            im = Image.fromarray(np.dstack([rgb, mask]), 'RGBA')
        else:
            im = Image.fromarray(rgb)

        from PIL import ImageDraw
        d = ImageDraw.Draw(im)
        r = int(round(max(0.0, corner_mm) * MM2PX))
        d.rounded_rectangle([0, 0, CARD_W - 1, CARD_H - 1], radius=r,
                            outline=(214, 214, 214, 255), width=2)

        stem = ''
        if names and i - 1 < len(names):
            stem = os.path.splitext(os.path.basename(names[i - 1]))[0]
            stem = ''.join(ch for ch in stem if ch not in '\\/:*?"<>|').strip()[:32]

        fn = f'{prefix}_{i:02d}_{stem}.png' if stem else f'{prefix}_{i:02d}.png'
        p = os.path.join(out_dir, fn)
        if os.path.exists(p):
            p = os.path.join(out_dir, f'{prefix}_{i:02d}_{stem}_{ts}.png')
        im.save(p, dpi=(DPI, DPI))
        paths.append(p)
    return paths


# ---------------------------------------------------------------- 批处理入口
def collect_files(inputs):
    files = []
    for it in inputs:
        if os.path.isdir(it):
            for name in sorted(os.listdir(it)):
                if name.lower().endswith(IMG_EXT):
                    files.append(os.path.join(it, name))
        elif os.path.isfile(it) and it.lower().endswith(IMG_EXT):
            files.append(it)
    # 去重且保持顺序
    seen, res = set(), []
    for f in files:
        k = os.path.abspath(f).lower()
        if k not in seen:
            seen.add(k)
            res.append(f)
    return res


def run_batch(files, out_dir, layout='auto', rotate='自动', clean=True,
              gap_mm=10.0, marks=False, fmt='a4', corner_mm=CORNER_R_MM, log=print,
              quads=None, flips=None, bright=0.0, contrast=1.0, print_dir=None, **kwargs):
    """
    fmt: 'a4'   只输出 A4 打印文件（PDF + 预览 PNG）—— 默认
         'image' 只把每张证件输出成独立图片（PNG，带透明圆角）
         'both'  两种都出
    corner_mm: 圆角半径，0 表示直角
    quads: {文件路径: 四角坐标}，手动框选的结果，优先于自动检测
    flips: {文件路径: 角度}，手动翻转（0/90/180/270），优先于自动方向判断
    说明：只做身份证 / 银行卡等 85.6×54mm 卡片，作业模式已移除。
    """
    quads = quads or {}
    flips = flips or {}

    def pick(d, f):
        """按 绝对路径(小写) / 原路径 两种键取手动设置；不用 `or`，避免 ndarray 布尔判断报错。"""
        v = d.get(os.path.normcase(os.path.abspath(f)))
        if v is None:
            v = d.get(f)
        return v

    cards, report, ok_names = [], [], []
    for i, f in enumerate(files, 1):
        try:
            items = process_image(f, rotate=rotate, clean=clean,
                                  quad_override=pick(quads, f),
                                  manual_deg=pick(flips, f) or 0,
                                  bright=bright, contrast=contrast)
            for it in items:
                cards.append(it['card'])
                ok_names.append(f)
            n = len(items)
            head = f'{n} 张卡，' if n > 1 else ''
            report.append((os.path.basename(f), '成功', head + items[0]['note']))
            log(f'[{i}/{len(files)}] {os.path.basename(f)}  ->  识别到 {n} 张卡')
        except Exception as e:
            report.append((os.path.basename(f), '失败', str(e)))
            log(f'[{i}/{len(files)}] {os.path.basename(f)}  处理失败：{e}')

    if not cards:
        raise RuntimeError('没有一张图片处理成功')

    mask = rounded_mask(CARD_W, CARD_H, corner_mm) if corner_mm and corner_mm > 0.2 else None

    res = {'pdf': None, 'preview': None, 'images': [], 'pages': 0,
           'count': len(report), 'cards': len(cards), 'page_pngs': []}

    pages = None
    if fmt in ('a4', 'both') or print_dir:
        pages = build_pages(cards, layout=layout, gap_mm=gap_mm,
                            marks=marks, mask=mask, corner_mm=corner_mm)

    if fmt in ('a4', 'both'):
        pdf_path, png_path = save_output(pages, out_dir)
        res.update(pdf=pdf_path, preview=png_path, pages=len(pages))

    if print_dir and pages:
        # 给「打印」用的整页位图：每页一张，真实 A4 尺寸，不进用户输出目录
        try:
            os.makedirs(print_dir, exist_ok=True)
            pngs = []
            for i, p in enumerate(pages, 1):
                q = os.path.join(print_dir, 'page_%03d.png' % i)
                p.save(q, dpi=(DPI, DPI))
                pngs.append(q)
            res['page_pngs'] = pngs
            res['pages'] = res['pages'] or len(pngs)
        except Exception as e:
            log(f'生成打印用页面图失败：{e}')

    if fmt in ('image', 'both'):
        imgs = save_card_images(cards, out_dir, mask=mask, names=ok_names,
                                corner_mm=corner_mm)
        res['images'] = imgs
        if res['preview'] is None and imgs:
            res['preview'] = imgs[0]

    return res, report


# ---------------------------------------------------------------- 手动框选
def make_quad_editor(parent, path, on_apply, max_w=980, max_h=640):
    """
    打开一个窗口，在照片上按顺序点四个角（左上→右上→右下→左下），
    点满四个就调 on_apply(quad)。绿色虚线是程序自动找到的框，可作参考。
    """
    import tkinter as tk
    from tkinter import messagebox
    from PIL import ImageTk

    bgr = read_image(path)
    H, W = bgr.shape[:2]
    scale = min(1.0, max_w / float(W), max_h / float(H))
    disp = cv2.resize(bgr, (max(1, int(W * scale)), max(1, int(H * scale))))
    tw, th = disp.shape[1], disp.shape[0]

    win = tk.Toplevel(parent)
    win.title('手动框选四角 - ' + os.path.basename(path))
    win.transient(parent)

    tk.Label(win, text='按顺序点四个角：左上 → 右上 → 右下 → 左下（绿色虚线是自动识别结果，可参考）',
             font=('Microsoft YaHei UI', 9)).pack(pady=6)

    canvas = tk.Canvas(win, width=tw, height=th, bg='#222', highlightthickness=0,
                       cursor='crosshair')
    canvas.pack(padx=8)
    photo = ImageTk.PhotoImage(Image.fromarray(disp[:, :, ::-1]))
    canvas.create_image(0, 0, anchor='nw', image=photo)
    canvas.image = photo                     # 防回收

    try:
        auto_quad, _ = detect_card(bgr)
    except Exception:
        auto_quad = None
    if auto_quad is not None:
        pts = (np.asarray(auto_quad, np.float32) * scale).astype(np.int32).ravel().tolist()
        canvas.create_polygon(pts, outline='#38d17a', width=2, dash=(6, 4), fill='')

    pts_c = []

    def redraw():
        canvas.delete('mark')
        for i, (x, y) in enumerate(pts_c):
            canvas.create_oval(x - 6, y - 6, x + 6, y + 6, fill='#ff9f43',
                               outline='white', width=2, tags='mark')
            canvas.create_text(x + 15, y - 14, text=str(i + 1), fill='white',
                               font=('Microsoft YaHei UI', 9, 'bold'), tags='mark')
        if len(pts_c) == 4:
            canvas.create_polygon([c for p in pts_c for c in p], outline='#ff4d4f',
                                  width=2, fill='', tags='mark')

    def on_click(ev):
        if len(pts_c) >= 4:
            return
        pts_c.append((ev.x, ev.y))
        redraw()

    canvas.bind('<Button-1>', on_click)

    bt = tk.Frame(win)
    bt.pack(pady=8)

    def undo():
        if pts_c:
            pts_c.pop()
            redraw()

    def clear_all():
        del pts_c[:]
        redraw()

    def do_apply():
        if len(pts_c) != 4:
            messagebox.showwarning('还差几个点', '请按 左上→右上→右下→左下 的顺序点满四个角')
            return
        quad = np.array([[x / scale, y / scale] for x, y in pts_c], np.float32)
        win.destroy()
        on_apply(order_points(quad))

    ttk.Button(bt, text='撤消一点', command=undo).pack(side='left')
    ttk.Button(bt, text='清空', command=clear_all).pack(side='left', padx=6)
    ttk.Button(bt, text='取消', command=win.destroy).pack(side='left')
    apply_btn = ttk.Button(bt, text='应用并重新生成', command=do_apply)
    apply_btn.pack(side='left', padx=6)

    # 便于自动化测试
    win._canvas = canvas
    win._points = pts_c
    win._apply = do_apply
    win._apply_btn = apply_btn
    return win


# ---------------------------------------------------------------- 图形界面
def launch_gui():
    # 优先使用现代化界面（CustomTkinter + 拖拽大预览）；不可用时回退到基础界面
    try:
        import idcard_ui
    except Exception:
        idcard_ui = None
    if idcard_ui is not None:
        idcard_ui.run()
        return

    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import threading

    root = tk.Tk()
    root.title('身份证照片 → A4 打印工具')
    root.geometry('940x648')
    root.minsize(900, 610)

    title = tk.Label(root, text='身份证自动校正 · A4 排版打印',
                     font=('Microsoft YaHei UI', 16, 'bold'))
    title.pack(pady=(12, 2))
    tk.Label(root, text='手机拍的照片 → 自动抠出身份证 → 标准尺寸 → 一页 A4 直接打印',
             fg='#666', font=('Microsoft YaHei UI', 9)).pack()

    # 文件列表 + 结果预览
    lf = tk.Frame(root)
    lf.pack(fill='both', expand=True, padx=14, pady=8)

    prev_lbl = tk.Label(lf, text='处理结果预览\n（生成后显示第一页）', fg='#999',
                        font=('Microsoft YaHei UI', 9), width=30,
                        relief='sunken', bd=1, anchor='center', justify='center')
    prev_lbl.pack(side='right', fill='both', padx=(10, 0))

    lb = tk.Listbox(lf, selectmode='extended', font=('Microsoft YaHei UI', 9),
                    height=9, activestyle='none')
    sb = ttk.Scrollbar(lf, orient='vertical', command=lb.yview)
    lb.configure(yscrollcommand=sb.set)
    sb.pack(side='right', fill='y')
    lb.pack(side='left', fill='both', expand=True)

    def add_files():
        fs = filedialog.askopenfilenames(
            title='选择身份证照片',
            filetypes=[('图片', '*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff *.heic *.heif'),
                       ('所有文件', '*.*')])
        for f in fs:
            lb.insert('end', f)

    def add_dir():
        d = filedialog.askdirectory(title='选择照片文件夹')
        if not d:
            return
        for name in sorted(os.listdir(d)):
            if name.lower().endswith(IMG_EXT):
                lb.insert('end', os.path.join(d, name))

    def remove_sel():
        for i in reversed(lb.curselection()):
            lb.delete(i)

    bf = tk.Frame(root)
    bf.pack(fill='x', padx=14)
    ttk.Button(bf, text='添加照片…', command=add_files).pack(side='left')
    ttk.Button(bf, text='添加文件夹…', command=add_dir).pack(side='left', padx=6)
    ttk.Button(bf, text='移除选中', command=remove_sel).pack(side='left')
    ttk.Button(bf, text='清空', command=lambda: lb.delete(0, 'end')).pack(side='left', padx=6)

    manual_quads = {}

    def open_manual():
        idx = lb.curselection()
        if not idx and lb.size() > 0:
            idx = (0,)
        if not idx:
            messagebox.showinfo('提示', '请先添加照片，再手动框选四角')
            return
        path = lb.get(idx[0])

        def on_apply(q):
            manual_quads[path] = q
            manual_quads[os.path.normcase(os.path.abspath(path))] = q
            status.config(text='已记录手动框选：' + os.path.basename(path) + '（已重新生成）',
                          fg='#0a7')
            work()

        make_quad_editor(root, path, on_apply)

    ttk.Button(bf, text='手动框四角…', command=open_manual).pack(side='left', padx=6)

    # 选项
    opt = tk.LabelFrame(root, text='排版设置', font=('Microsoft YaHei UI', 9))
    opt.pack(fill='x', padx=14, pady=8)

    row1 = tk.Frame(opt)
    row1.pack(fill='x', pady=4, padx=8)
    tk.Label(row1, text='输出方式：', font=('Microsoft YaHei UI', 9)).pack(side='left')
    fmt_var = tk.StringVar(value='A4 打印文件（PDF，可直接打印）')
    fmt_map = {
        'A4 打印文件（PDF，可直接打印）': 'a4',
        '两张图片（每张证件一个 PNG）': 'image',
        '两者都要（A4 + 图片）': 'both',
    }
    ttk.Combobox(row1, textvariable=fmt_var, values=list(fmt_map.keys()),
                 state='readonly', width=34,
                 font=('Microsoft YaHei UI', 9)).pack(side='left')
    tk.Label(row1, text='　方向微调：', font=('Microsoft YaHei UI', 9)).pack(side='left')
    rot_var = tk.StringVar(value='自动')
    ttk.Combobox(row1, textvariable=rot_var,
                 values=['自动', '不旋转', '顺时针90', '180', '逆时针90'],
                 state='readonly', width=11).pack(side='left')

    row2 = tk.Frame(opt)
    row2.pack(fill='x', pady=4, padx=8)
    tk.Label(row2, text='排版方式：', font=('Microsoft YaHei UI', 9)).pack(side='left')
    layout_var = tk.StringVar(value='自动（1张单页 / 2张正反面同页 / 更多张排网格）')
    layout_map = {
        '自动（1张单页 / 2张正反面同页 / 更多张排网格）': 'auto',
        '每页一张': 'single',
        '正反面放同一页': 'pair',
        '2×4 网格（一页8张）': 'grid',
    }
    ttk.Combobox(row2, textvariable=layout_var, values=list(layout_map.keys()),
                 state='readonly', width=40,
                 font=('Microsoft YaHei UI', 9)).pack(side='left')
    tk.Label(row2, text='　间距(mm)：', font=('Microsoft YaHei UI', 9)).pack(side='left')
    gap_var = tk.StringVar(value='10')
    ttk.Entry(row2, textvariable=gap_var, width=5).pack(side='left')
    tk.Label(row2, text='　圆角(mm)：', font=('Microsoft YaHei UI', 9)).pack(side='left')
    corner_var = tk.StringVar(value='3.18')
    ttk.Entry(row2, textvariable=corner_var, width=5).pack(side='left')

    row3 = tk.Frame(opt)
    row3.pack(fill='x', pady=(0, 6), padx=8)
    clean_var = tk.BooleanVar(value=True)
    tk.Checkbutton(row3, text='自动增强（提亮/对比度/轻锐化）',
                   variable=clean_var, font=('Microsoft YaHei UI', 9)).pack(side='left')
    marks_var = tk.BooleanVar(value=False)
    tk.Checkbutton(row3, text='打印裁切框线', variable=marks_var,
                   font=('Microsoft YaHei UI', 9)).pack(side='left', padx=10)

    # 输出目录 + 开始
    row4 = tk.Frame(root)
    row4.pack(fill='x', padx=14, pady=4)
    tk.Label(row4, text='输出目录：', font=('Microsoft YaHei UI', 9)).pack(side='left')
    out_var = tk.StringVar(value=os.path.join(os.path.expanduser('~'), 'Desktop', '身份证打印输出'))
    ttk.Entry(row4, textvariable=out_var, width=52).pack(side='left', padx=4)
    ttk.Button(row4, text='选择…', command=lambda: out_var.set(
        filedialog.askdirectory(initialdir=out_var.get()) or out_var.get())).pack(side='left')

    status = tk.Label(root, text='', fg='#0a7', font=('Microsoft YaHei UI', 9),
                      anchor='w', justify='left', wraplength=720)
    status.pack(fill='x', padx=14)

    run_btn = ttk.Button(root, text='开始处理 → 生成文件')

    def work():
        files = list(lb.get(0, 'end'))
        if not files:
            messagebox.showwarning('提示', '请先添加照片')
            run_btn.config(state='normal')
            return
        try:
            gap = float(gap_var.get())
        except Exception:
            gap = 10.0
        try:
            corner = float(corner_var.get())
        except Exception:
            corner = CORNER_R_MM
        fmt = fmt_map[fmt_var.get()]

        try:
            status.config(text='正在处理，请稍等…', fg='#333')
            root.update_idletasks()
            res, rep = run_batch(
                files, out_var.get(), layout=layout_map[layout_var.get()],
                rotate=rot_var.get(), clean=clean_var.get(),
                gap_mm=gap, marks=marks_var.get(), fmt=fmt, corner_mm=corner,
                log=lambda s: None, quads=manual_quads)
            bad = [r for r in rep if r[1] != '成功']
            rough = [r for r in rep if r[1] == '成功' and
                     ('粗略' in r[2] or '整图' in r[2])]

            if res['pdf'] and res['images']:
                msg = (f'完成！共 {res["count"]} 张照片，A4 共 {res["pages"]} 页，'
                       f'另出 {len(res["images"])} 张证件图片。\n'
                       f'PDF：{res["pdf"]}\n图片：{os.path.dirname(res["images"][0])}')
                status.config(text=f'完成：A4 {res["pages"]} 页 + {len(res["images"])} 张图片',
                              fg='#0a7')
            elif res['pdf']:
                msg = (f'完成！共 {res["count"]} 张照片，{res["pages"]} 页 A4。\n'
                       f'PDF：{res["pdf"]}\n预览图：{res["preview"]}')
                status.config(text=f'完成：{os.path.basename(res["pdf"])}（{res["pages"]} 页）',
                              fg='#0a7')
            else:
                msg = (f'完成！共输出 {len(res["images"])} 张证件图片（85.6×54mm，300DPI）。\n'
                       f'目录：{os.path.dirname(res["images"][0]) if res["images"] else out_var.get()}')
                status.config(text=f'完成：{len(res["images"])} 张证件图片', fg='#0a7')

            if bad:
                msg += f'\n\n有 {len(bad)} 张处理异常：\n' + \
                       '\n'.join(f'· {b[0]}：{b[2]}' for b in bad[:6])
            if rough:
                msg += f'\n\n有 {len(rough)} 张没能精确找到卡片边缘，按近似区域处理，\n' \
                       '建议换一张背景对比更明显的照片重拍。'

            # 结果预览
            try:
                from PIL import ImageTk
                im = Image.open(res['preview'])
                im.thumbnail((300, 460), Image.LANCZOS)
                ph = ImageTk.PhotoImage(im)
                prev_lbl.configure(image=ph, text='')
                prev_lbl.image = ph
            except Exception:
                pass

            if messagebox.askyesno('处理完成', msg + '\n\n是否打开输出文件夹？'):
                try:
                    os.startfile(out_var.get())
                except Exception:
                    pass
        except Exception as e:
            status.config(text=f'出错：{e}', fg='#c00')
            messagebox.showerror('出错', f'{e}\n\n{traceback.format_exc(limit=3)}')
        finally:
            run_btn.config(state='normal')

    def start():
        run_btn.config(state='disabled')
        threading.Thread(target=work, daemon=True).start()

    run_btn.configure(command=start)
    run_btn.pack(pady=(6, 12), ipadx=20, ipady=6)

    tk.Label(root, text='打印提示：打印时选“实际大小 / 100%”，不要选“适应页面”，否则尺寸会缩水。',
             fg='#a60', font=('Microsoft YaHei UI', 9)).pack(pady=(0, 10))

    root.mainloop()


# ---------------------------------------------------------------- CLI
def parse_rotate(s):
    if s in ('自动', 'auto'):
        return '自动'
    if s in ('不旋转', 'none', '0'):
        return '不旋转'
    return s


def main():
    ap = argparse.ArgumentParser(
        description='身份证照片自动校正 + A4 排版打印',
        formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument('inputs', nargs='*', help='照片文件或文件夹（可多个）')
    ap.add_argument('-o', '--out', default=None, help='输出目录（默认：桌面/身份证打印输出）')
    ap.add_argument('-f', '--fmt', default='a4', choices=['a4', 'image', 'both'],
                    help='输出方式：a4=只出A4打印文件(默认) / image=每张证件一张图片 / both=都要')
    ap.add_argument('-l', '--layout', default='auto',
                    choices=['auto', 'single', 'pair', 'grid'], help='排版方式')
    ap.add_argument('-r', '--rotate', default='自动', help='方向微调：自动/不旋转/顺时针90/180/逆时针90')
    ap.add_argument('--corner', type=float, default=CORNER_R_MM,
                    help='证件圆角半径，毫米（默认 3.18，填 0 为直角）')
    ap.add_argument('--no-clean', action='store_true', help='关闭自动增强')
    ap.add_argument('--marks', action='store_true', help='打印裁切框线')
    ap.add_argument('--gap', type=float, default=10.0, help='卡片间距，毫米')
    ap.add_argument('--gui', action='store_true', help='打开图形界面')
    ap.add_argument('-V', '--version', action='store_true',
                    help='显示版本与作者信息')
    args = ap.parse_args()

    if args.version:
        txt = ('身份证 / 银行卡 A4 打印工具  v%s\n开发者：%s\n%s'
               % (APP_VERSION, APP_AUTHOR, APP_REPO))
        print(txt)
        if FROZEN:
            _msgbox('关于', txt, 0x40)
        return

    if args.gui or not args.inputs:
        launch_gui()
        return

    files = collect_files(args.inputs)
    if not files:
        print('没有找到可处理的图片。')
        return 2

    out_dir = args.out or os.path.join(os.path.expanduser('~'), 'Desktop', '身份证打印输出')
    print(f'共 {len(files)} 张照片，输出目录：{out_dir}')
    try:
        res, report = run_batch(
            files, out_dir, layout=args.layout, rotate=parse_rotate(args.rotate),
            clean=not args.no_clean, gap_mm=args.gap, marks=args.marks,
            fmt=args.fmt, corner_mm=args.corner)
    except Exception as e:
        print(f'处理失败：{e}')
        if FROZEN:
            _msgbox('处理失败', f'{e}', 0x10)
        return 1

    print('\n完成：')
    lines = []
    if res['pdf']:
        print(f'  PDF  : {res["pdf"]}  ({res["pages"]} 页)')
        print(f'  预览 : {res["preview"]}')
        print('打印时请选择“实际大小 / 100%”。')
        lines.append(f'A4 打印文件：{res["pages"]} 页')
        lines.append(res['pdf'])
    if res['images']:
        print(f'  图片 : {len(res["images"])} 张（85.6×54mm / 300DPI / 透明圆角）')
        for p in res['images']:
            print(f'         {os.path.basename(p)}')
        lines.append(f'证件图片：{len(res["images"])} 张')
    lines.append(f'输出目录：{out_dir}')

    if FROZEN:                      # 拖到 exe 上处理：没有控制台，弹窗 + 打开文件夹
        _msgbox('处理完成', '\n'.join(lines) +
                '\n\n打印时请选择“实际大小 / 100%”，不要选“适应页面”。')
        try:
            os.startfile(out_dir)
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())

