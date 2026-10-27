"""生成四款游戏的「App 图标风」头像，输出到 static/icons/。

派蒙 / 三月七 / 邦布 / 阿米娅 —— 统一构图：圆角方形底 + 人物剪影 + 游戏主色。
画布 288px 超采样，再缩到 72px（@2x）和 36px，边缘自然抗锯齿。

    py -3 tools/gen_game_icons.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"
S = 288                     # 超采样画布边长
R = 64                      # 圆角半径（36px 下约等于 8px）
RATIO = 8                   # 半径比例，用于 36px 版本


def gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    """竖向线性渐变。"""
    img = Image.new("RGB", (1, size))
    px = img.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((size, size), Image.BILINEAR)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def glow(img: Image.Image, center: tuple, radius: int, color: tuple, alpha: int = 90) -> None:
    """一团柔光，让人物从背景里浮出来。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    steps = 18
    for i in range(steps, 0, -1):
        r = int(radius * i / steps)
        a = int(alpha * (1 - i / steps) ** 1.6)
        d.ellipse([center[0] - r, center[1] - r, center[0] + r, center[1] + r], fill=color + (a,))
    img.alpha_composite(layer)


def star4(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill: tuple) -> None:
    """四角星（原石 / 星琼那种闪光点）。"""
    k = r * 0.30
    d.polygon(
        [(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
         (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)],
        fill=fill,
    )


def hair_cap(d: ImageDraw.ImageDraw, box, color: tuple, tips: int = 0) -> None:
    """上半个椭圆当刘海，可选几缕尖角。"""
    x0, y0, x1, y1 = box
    d.pieslice([x0, y0, x1, y1], 180, 360, fill=color)
    if tips:
        w = (x1 - x0) / (tips * 2 - 1)
        base = y0 + (y1 - y0) * 0.55
        for i in range(tips):
            cx = x0 + w * (i * 2 + 0.5)
            d.polygon([(cx - w * 0.5, base - 8), (cx + w * 0.5, base - 8), (cx, base + 26)],
                      fill=color)


def eyes(d: ImageDraw.ImageDraw, box_l, box_r, color: tuple, sparkle=(255, 255, 255, 235)) -> None:
    for box in (box_l, box_r):
        d.ellipse(box, fill=color)
        x0, y0, x1, y1 = box
        w = x1 - x0
        h = y1 - y0
        d.ellipse([int(x0 + w * 0.18), int(y0 + h * 0.12),
                   int(x0 + w * 0.38), int(y0 + h * 0.38)], fill=sparkle)
        d.ellipse([int(x0 + w * 0.58), int(y1 - h * 0.36),
                   int(x0 + w * 0.74), int(y1 - h * 0.18)], fill=sparkle)


# --------------------------------------------------------------------------- #
# 原神 · 派蒙
# --------------------------------------------------------------------------- #

def draw_genshin() -> Image.Image:
    base = gradient(S, (146, 184, 242), (228, 242, 255)).convert("RGBA")
    glow(base, (144, 150), 138, (255, 224, 150), 130)

    d = ImageDraw.Draw(base)
    # 深蓝披风：白发＋白背景本来糊在一起，用深色肩线撑出剪影
    d.rounded_rectangle([60, 200, 228, 300], radius=46, fill=(43, 58, 105, 255))
    d.arc([70, 208, 218, 286], 200, 340, fill=(226, 172, 62, 255), width=7)
    # 后发（只比脸大一圈，别做成"白蛋"）
    d.ellipse([74, 68, 214, 238], fill=(226, 234, 248, 255))
    # 侧发
    for x0 in (76, 184):
        d.rounded_rectangle([x0, 128, x0 + 26, 212], radius=13, fill=(242, 247, 255, 255))
    # 脸
    d.ellipse([98, 106, 190, 200], fill=(253, 228, 208, 255))
    # 刘海
    hair_cap(d, [82, 74, 206, 152], (246, 250, 255, 255), tips=3)
    # 金色发冠（只在额头上方）
    d.arc([100, 66, 188, 138], 205, 335, fill=(226, 172, 62, 255), width=9)
    d.polygon([(144, 58), (155, 76), (144, 94), (133, 76)], fill=(244, 200, 100, 255))
    # 眼睛
    eyes(d, [112, 130, 134, 168], [154, 130, 176, 168], (58, 108, 166, 255))
    # 嘴
    d.arc([133, 170, 155, 190], 20, 160, fill=(196, 122, 108, 255), width=5)
    # 腮红
    d.ellipse([104, 160, 122, 172], fill=(250, 196, 186, 150))
    d.ellipse([166, 160, 184, 172], fill=(250, 196, 186, 150))
    # 漂浮的原石
    star4(d, 234, 56, 30, (238, 190, 74, 255))
    star4(d, 234, 56, 15, (255, 240, 190, 255))
    return base


# --------------------------------------------------------------------------- #
# 星铁 · 三月七
# --------------------------------------------------------------------------- #

def draw_starrail() -> Image.Image:
    base = gradient(S, (255, 206, 232), (255, 160, 205)).convert("RGBA")
    glow(base, (150, 140), 130, (255, 255, 255), 120)

    d = ImageDraw.Draw(base)
    # 浅蓝外套肩线
    d.rounded_rectangle([64, 208, 224, 300], radius=44, fill=(230, 240, 252, 255))
    d.rounded_rectangle([64, 208, 224, 300], radius=44, outline=(180, 214, 242, 255), width=5)
    # 后发
    d.ellipse([72, 68, 216, 240], fill=(246, 150, 198, 255))
    # 双侧发：粉发 + 蓝色发梢
    for x0 in (76, 186):
        d.rounded_rectangle([x0, 124, x0 + 28, 240], radius=14, fill=(248, 164, 206, 255))
        d.rounded_rectangle([x0, 190, x0 + 28, 240], radius=14, fill=(148, 204, 240, 255))
    # 脸
    d.ellipse([96, 108, 192, 206], fill=(253, 231, 218, 255))
    # 刘海
    hair_cap(d, [82, 72, 206, 158], (249, 172, 210, 255), tips=3)
    # 眼睛
    eyes(d, [110, 132, 134, 170], [154, 132, 178, 170], (228, 100, 168, 255))
    # 嘴
    d.arc([134, 172, 154, 190], 20, 160, fill=(206, 118, 128, 255), width=5)
    # 蓝色发饰
    d.ellipse([180, 76, 224, 120], fill=(126, 190, 236, 255))
    d.ellipse([192, 88, 212, 108], fill=(255, 255, 255, 230))
    # 雪花点缀（左下角，别抢脸）
    cx, cy, r = 62, 216, 18
    for k in range(3):
        ang = math.radians(60 * k)
        dx, dy = math.cos(ang) * r, math.sin(ang) * r
        d.line([(cx - dx, cy - dy), (cx + dx, cy + dy)], fill=(255, 255, 255, 200), width=5)
    return base


# --------------------------------------------------------------------------- #
# 绝区零 · 邦布
# --------------------------------------------------------------------------- #

def draw_zzz() -> Image.Image:
    base = gradient(S, (44, 54, 100), (14, 18, 34)).convert("RGBA")
    glow(base, (144, 150), 130, (86, 170, 255), 130)

    d = ImageDraw.Draw(base)
    # 天线耳朵
    for x0 in (74, 174):
        d.rounded_rectangle([x0, 26, x0 + 40, 132], radius=20, fill=(238, 240, 246, 255))
        d.rounded_rectangle([x0 + 12, 26, x0 + 28, 96], radius=10, fill=(255, 178, 64, 255))
    # 头骨
    d.rounded_rectangle([48, 92, 240, 244], radius=52, fill=(244, 246, 250, 255))
    d.rounded_rectangle([48, 92, 240, 244], radius=52, outline=(196, 202, 214, 255), width=4)
    # 屏幕脸
    d.rounded_rectangle([78, 120, 210, 214], radius=30, fill=(13, 16, 24, 255))
    # 眼睛（带辉光）
    for cx in (114, 174):
        d.ellipse([cx - 24, 148, cx + 24, 186], fill=(255, 178, 64, 60))
        d.ellipse([cx - 16, 152, cx + 16, 182], fill=(255, 186, 74, 255))
        d.ellipse([cx - 9, 158, cx + 1, 168], fill=(255, 240, 205, 255))
    # 小嘴
    d.arc([130, 178, 158, 200], 20, 160, fill=(255, 178, 64, 255), width=5)
    return base


# --------------------------------------------------------------------------- #
# 明日方舟 · 阿米娅
# --------------------------------------------------------------------------- #

def draw_arknights() -> Image.Image:
    base = gradient(S, (42, 54, 86), (17, 22, 38)).convert("RGBA")
    glow(base, (144, 150), 130, (92, 132, 210), 120)

    d = ImageDraw.Draw(base)
    # 兔耳
    d.polygon([(84, 112), (100, 16), (130, 22), (116, 118)], fill=(106, 74, 60, 255))
    d.polygon([(204, 112), (188, 16), (158, 22), (172, 118)], fill=(106, 74, 60, 255))
    d.polygon([(97, 100), (106, 34), (122, 38), (113, 104)], fill=(198, 148, 130, 255))
    d.polygon([(191, 100), (182, 34), (166, 38), (175, 104)], fill=(198, 148, 130, 255))
    # 后发
    d.ellipse([76, 88, 212, 252], fill=(102, 71, 56, 255))
    # 脸
    d.ellipse([98, 114, 190, 208], fill=(251, 227, 207, 255))
    # 刘海（中分）
    hair_cap(d, [84, 80, 204, 164], (108, 75, 59, 255), tips=2)
    d.polygon([(144, 94), (156, 154), (132, 154)], fill=(251, 227, 207, 255))
    # 眼睛
    eyes(d, [110, 142, 132, 178], [156, 142, 178, 178], (196, 128, 58, 255))
    # 嘴
    d.arc([134, 182, 154, 200], 20, 160, fill=(190, 128, 116, 255), width=5)
    # 蓝色衣领
    d.rounded_rectangle([102, 232, 186, 288], radius=22, fill=(46, 95, 166, 255))
    d.rounded_rectangle([124, 232, 164, 288], radius=16, fill=(232, 238, 248, 255))
    return base


PAINTERS = {
    "genshin": draw_genshin,
    "starrail": draw_starrail,
    "zzz": draw_zzz,
    "arknights": draw_arknights,
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for game_id, painter in PAINTERS.items():
        canvas = painter()
        canvas.putalpha(rounded_mask(S, R))
        big = canvas.resize((72, 72), Image.LANCZOS)
        small = canvas.resize((36, 36), Image.LANCZOS)
        big.save(OUT_DIR / f"{game_id}@2x.png")
        small.save(OUT_DIR / f"{game_id}.png")
        print(f"  {game_id:10s} 36x36 + 72x72")


if __name__ == "__main__":
    main()
