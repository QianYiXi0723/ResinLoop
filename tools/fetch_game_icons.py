"""下载四款游戏的**官方 App 图标**，存到 static/icons/。

来源：Apple App Store 的官方商品信息接口（`itunes.apple.com/lookup`），
返回 `artworkUrl512`，把尺寸段换成 1024 就能拿到 1024×1024 的原图。
不用自己画，也不用第三方图站。

米哈游三款的新版图标底部有一条出版方名称条（miHoYo / HOYOVERSE，约占高度 11%），
这里会自动裁掉再居中取正方形，所以最终是干净的 910×910 → 512 / 72。

    py -3 tools/fetch_game_icons.py

想换图：把同名的 png 丢进 static/icons/ 覆盖即可，前端只认文件名。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import requests
from PIL import Image, ImageStat

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"

#: 游戏 id → App Store bundle id（国区）
BUNDLES = {
    "genshin": "com.miHoYo.Yuanshen",
    "starrail": "com.miHoYo.hkrpg",
    "zzz": "com.miHoYo.Nap",
    "arknights": "com.hypergryph.arknights",
}

#: 底部出版方名称条占整图高度的比例（1024 里约 114px）
NAMEPLATE_RATIO = 114 / 1024

#: 成品尺寸：主图 512（36px 显示够清晰），@2x 给高分屏
MASTER_SIZE = 512
RETINA_SIZE = 72

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 本机系统代理会拦部分外网，直连更稳
    return s


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


def bottom_is_uniform(im: Image.Image, rows: int = 12, limit: float = 26.0) -> bool:
    """底部这些行是不是一整条同色（判断有没有出版方名称条）。"""
    w, h = im.size
    for y in range(h - rows, h):
        stat = ImageStat.Stat(im.crop((0, y, w, y + 1)))
        if sum(stat.stddev) / 3 > limit:
            return False
    return True


def strip_nameplate(im: Image.Image) -> tuple[Image.Image, str]:
    """裁掉底部名称条并居中取正方形；没有名称条就原样返回。"""
    w, h = im.size
    if not bottom_is_uniform(im):
        return im, "无名称条，保持原图"
    cut = int(round(h * NAMEPLATE_RATIO))
    keep = h - cut
    if keep <= w * 0.8:
        return im, "名称条比例异常，保持原图"
    side = min(w, keep)
    left = (w - side) // 2
    top = (keep - side) // 2
    return im.crop((left, top, left + side, top + side)), f"裁掉底部 {cut}px 名称条"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = session()
    failed = []
    for game_id, bundle in BUNDLES.items():
        try:
            url = lookup_icon_url(s, bundle)
            raw = s.get(url, headers=UA, timeout=30).content
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            if im.width != im.height:
                im = im.crop((0, 0, min(im.size), min(im.size)))
            im, note = strip_nameplate(im)
            master = im.resize((MASTER_SIZE, MASTER_SIZE), Image.LANCZOS)
            retina = im.resize((RETINA_SIZE, RETINA_SIZE), Image.LANCZOS)
            master.save(OUT_DIR / f"{game_id}.png", optimize=True)
            retina.save(OUT_DIR / f"{game_id}@2x.png", optimize=True)
            print(f"  {game_id:10s} {im.size[0]}px → {MASTER_SIZE}/{RETINA_SIZE}  {note}")
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
