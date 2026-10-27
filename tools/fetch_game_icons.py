"""获取四款游戏的**官方 App 图标**，存到 static/icons/。

两个来源，按优先级：

1. **本机米哈游启动器自带的 .ico**（`C:\\Program Files\\miHoYo Launcher\\<版本>\\ico\\`）
   —— 原神 / 星铁 / 绝区零 三款都有，**是经典版，不带周年角标**，最高 256×256。
2. **Apple App Store 官方商品接口**（`itunes.apple.com/lookup`）—— 返回 `artworkUrl512`，
   把尺寸段换成 1024 就是 1024×1024 原图，用于启动器里没有的游戏（明日方舟）。

⚠️ 2026-10 起 App Store / TapTap / 官网 favicon 上的原神图标**全是「6 周年」版**
（左上角带 6th 角标），只有启动器里的本地 ico 还是经典版，所以原神必须走第 1 条。
启动器没装的话脚本会自动退回 App Store，并在输出里提示「带角标」。

米哈游的图标底部有一条出版方名称条（miHoYo / HOYOVERSE），这里会自动裁掉再居中取正方形。

    py -3 tools/fetch_game_icons.py

想换图：把同名的 png 丢进 static/icons/ 覆盖即可，前端只认文件名（改了 mtime 会自动破缓存）。
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import requests
from PIL import Image, ImageStat

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"

#: 游戏 id → App Store bundle id（国区），启动器里没有时的兜底
BUNDLES = {
    "genshin": "com.miHoYo.Yuanshen",
    "starrail": "com.miHoYo.hkrpg",
    "zzz": "com.miHoYo.Nap",
    "arknights": "com.hypergryph.arknights",
}

#: 游戏 id → 米哈游启动器里的 ico 文件名（不带后缀）。
#: 只放原神：星铁 / 绝区零 的商店图是 1024，分辨率更高且本来就没有角标，没必要降级到 256 的 ico。
LAUNCHER_ICOS = {
    "genshin": "hk4e_cn",
}

#: 米哈游启动器安装位置（版本号目录每次更新都会变，用通配符找）
LAUNCHER_ROOT = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "miHoYo Launcher"

#: 底部出版方名称条占整图高度的比例（1024 里约 114px，256 的 ico 里约 28px）
NAMEPLATE_RATIO = 114 / 1024

#: 成品尺寸：主图最多 512，@2x 给高分屏（ico 只有 256 时不放大，免得糊）
MASTER_SIZE = 512
RETINA_SIZE = 72

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 本机系统代理会拦部分外网，直连更稳
    return s


# --------------------------------------------------------------------------- #
# 来源一：本机启动器里的官方 ico
# --------------------------------------------------------------------------- #


def find_launcher_ico(stem: str) -> Path | None:
    """在启动器安装目录里找最新的 <stem>.ico；没装启动器就返回 None。"""
    if not LAUNCHER_ROOT.is_dir():
        return None
    found = sorted(LAUNCHER_ROOT.glob(f"*/ico/{stem}.ico"), key=lambda p: p.parent.parent.name)
    return found[-1] if found else None


def load_ico(path: Path) -> Image.Image:
    """取 ico 里最大的那一帧，转成白底 RGB（ico 可能带透明通道）。"""
    probe = Image.open(path)
    size = max(probe.info.get("sizes") or {probe.size}, key=lambda s: s[0] * s[1])
    image = Image.open(path)
    image.size = size
    if image.mode != "RGB":
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, (0, 0), rgba.split()[-1])
        image = flat
    return image


# --------------------------------------------------------------------------- #
# 来源二：App Store
# --------------------------------------------------------------------------- #


def lookup_icon_url(s: requests.Session, bundle_id: str) -> str:
    resp = s.get(
        "https://itunes.apple.com/lookup",
        params={"bundleId": bundle_id, "country": "cn"},
        headers=UA,
        timeout=15,
    )
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    if not results:
        raise RuntimeError(f"App Store 查不到 {bundle_id}")
    return str(results[0]["artworkUrl512"]).replace("/512x512bb.", "/1024x1024bb.")


# --------------------------------------------------------------------------- #
# 公共处理
# --------------------------------------------------------------------------- #


def looks_like_nameplate(im: Image.Image, rows: int = 8) -> bool:
    """底部这几行是不是一条横贯整图的深色名称条。

    条上有白色字，所以不能只看「是否同色」，改成看「像素是否集中在同一个深色上」。
    """
    w, h = im.size
    strip = im.crop((0, h - rows, w, h))
    stat = ImageStat.Stat(strip)
    if sum(stat.mean) / 3 > 100:  # 名称条是深色，亮底肯定不是
        return False
    pixels = list(strip.convert("RGB").getdata())
    mid = tuple(sorted(channel)[len(channel) // 2] for channel in zip(*pixels))
    near = sum(
        1 for r, g, b in pixels
        if abs(r - mid[0]) < 32 and abs(g - mid[1]) < 32 and abs(b - mid[2]) < 32
    )
    return near / len(pixels) > 0.45


def strip_nameplate(im: Image.Image) -> tuple[Image.Image, str]:
    """裁掉底部名称条并居中取正方形；没有名称条就原样返回。"""
    w, h = im.size
    if not looks_like_nameplate(im):
        return im, "无名称条，保持原图"
    cut = int(round(h * NAMEPLATE_RATIO))
    keep = h - cut
    if keep <= w * 0.8:
        return im, "名称条比例异常，保持原图"
    side = min(w, keep)
    left = (w - side) // 2
    top = (keep - side) // 2
    return im.crop((left, top, left + side, top + side)), f"裁掉底部 {cut}px 名称条"


def export(im: Image.Image, game_id: str) -> None:
    """原图够大就缩到 512，本来就是小图（启动器 ico 只有 256）就不放大，免得糊。"""
    master = im if im.width <= MASTER_SIZE else im.resize((MASTER_SIZE, MASTER_SIZE), Image.LANCZOS)
    retina = im.resize((RETINA_SIZE, RETINA_SIZE), Image.LANCZOS)
    master.save(OUT_DIR / f"{game_id}.png", optimize=True)
    retina.save(OUT_DIR / f"{game_id}@2x.png", optimize=True)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = session()
    failed = []
    for game_id, bundle in BUNDLES.items():
        try:
            ico = find_launcher_ico(LAUNCHER_ICOS.get(game_id, ""))
            if ico is not None:
                im = load_ico(ico)
                source = f"启动器 {ico.name}"
                warning = ""
            else:
                url = lookup_icon_url(s, bundle)
                raw = s.get(url, headers=UA, timeout=30).content
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                source = "App Store"
                warning = "（该图带周年角标，启动器没装）" if game_id == "genshin" else ""
            if im.width != im.height:
                im = im.crop((0, 0, min(im.size), min(im.size)))
            im, note = strip_nameplate(im)
            export(im, game_id)
            print(f"  {game_id:10s} {im.size[0]:>4}px  ← {source:22s} {note}{warning}")
        except Exception as exc:  # noqa: BLE001 - 单个失败不影响其它
            failed.append(game_id)
            print(f"  {game_id:10s} 失败：{exc}")
    if failed:
        print("\n以下游戏没抓到，请手动把官方图标放进 static/icons/：")
        for game_id in failed:
            print(f"  {game_id}.png / {game_id}@2x.png")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
