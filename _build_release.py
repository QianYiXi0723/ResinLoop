# -*- coding: utf-8 -*-
"""构建 exe 发行包（C# 壳 + engine/ 便携 Python）。

产物：
  release/ResinLoop_v<版本>/                     可直接发给小白的目录
  dist/ResinLoop_v<版本>_Windows_x64.zip

版本号唯一来源是 core/__init__.py 的 __version__（OUT / ZIP_NAME / 说明标题都从它派生）。

包内容（面向最终用户，越少越好）：
  ResinLoop.exe                          C# 壳（net48，多文件发布，内嵌图标）
  Microsoft.Web.WebView2.Core.dll        WebView2 托管库（不再 ILRepack 合并）
  Microsoft.Web.WebView2.WinForms.dll    WebView2 WinForms 控件
  WebView2Loader.dll                     WebView2 原生加载器
  engine/                                便携 Python 3.11 + 项目代码（零凭据，首启自建配置）
  小白使用说明.txt

前置：壳已构建（仓库根上述四件齐全，跑 shell/build.ps1），
      engine/python/ 已就位（Embeddable + 预装依赖）。

为什么不再单文件：net48 没有官方 PublishSingleFile，只能手搓 ILRepack，而它换来的
只有 10,888 字节和两个文件，代价是 2026-10-30 那次 TypeLoadException。详见 shell/build.ps1 头部。

注意：本文件会提交到仓库，**不得硬编码任何真实 key**；
自检敏感值全部在运行时从本地 config.json / logs 动态提取。
"""
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent


def _read_version() -> str:
    """版本号唯一来源：core/__init__.py 的 __version__（界面左下角也读同一个值）。"""
    text = (ROOT / "core" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("[FATAL] core/__init__.py 里找不到 __version__ 定义")
    return match.group(1)


VERSION = _read_version()
OUT = ROOT / "release" / f"ResinLoop_v{VERSION}"
ZIP_DIR = ROOT / "dist"
ZIP_NAME = f"ResinLoop_v{VERSION}_Windows_x64"

#: 壳的产物（构建流程见 shell/build.ps1；本脚本不负责编译，只负责组装）
SHELL_EXE = ROOT / "ResinLoop.exe"
#: 壳必须随包的伴生件 —— WebView2Loader.dll 是原生件，另外两个是托管库（不再合并进 exe）
SHELL_COMPANIONS = (
    "Microsoft.Web.WebView2.Core.dll",
    "Microsoft.Web.WebView2.WinForms.dll",
    "WebView2Loader.dll",
)
#: C# 工程目录（只用于"壳是否比源码新"守卫）
SHELL_DIR = ROOT / "shell"
#: 便携解释器来源（Embeddable + 预装 site-packages，构建方法见 tests/rl_test_engine_standalone.py）
ENGINE_PY = ROOT / "engine" / "python"
#: engine 入口模板（exe 发行版专用：纯 HTTP、无窗口；与源码仓 app.py 是两份）
ENGINE_APP_TEMPLATE = ROOT / "shell" / "engine_app.py"

# ---------------- diet 阶段（2026-10-31）：只动打包副本，母本不动 ----------------
# 实测依据（engine/python 40MB，site-packages 19MB 里 PIL 占 15MB）：
#   _avif.pyd 7.6MB / _imagingft.pyd 2.2MB / _webp 408K / _imagingcms 272K
#   对「只渲染 PNG 二维码 + 画托盘图标」的引擎全是死重——PNG 编解码在核心
#   _imaging.pyd 里（zlib 内置），PIL 插件发现是静态 _plugins 名单且
#   except ImportError 吞缺失，删扩展+包装模块对 PNG 路径零影响。
#   sqlite3.dll+_sqlite3.pyd 1.6MB：全项目（代码+依赖链）零引用；
#   python.cat 是 DLL 验签目录，运行时不需要。
#   site-packages .py→.pyc 单态（-b 同目录旧式布局）：省一半源码体积，
#   stdlib python311.zip 本就 637 pyc 零源码，无需处理。
PIL_CODEC_EXT_PRUNE = ("_avif", "_imagingft", "_webp", "_imagingcms", "_imagingtk")
#: 随编解码扩展一起删的包装模块：不删的话 PIL init 会注册出"打不开"的格式。
#: 注意 ImageFont.py 必须保留——pillow 13 的 ImageDraw→ImageText→ImageFont
#: 是顶层导入链，且 _imagingft 在 ImageFont 里是函数内惰性导入（真画字才碰）。
PIL_CODEC_MODULE_PRUNE = (
    "ImageCms.py",
    "AvifImagePlugin.py",
    "WebPImagePlugin.py",
)
ROOT_RUNTIME_PRUNE = ("sqlite3.dll", "_sqlite3.pyd", "python.cat")

#: 瘦身冒烟：engine 自己的解释器验证「依赖可导入 + PIL 画图 + 二维码出 PNG」
SMOKE_SNIPPET = (
    "import io;"
    "import requests, qrcode;"
    "from PIL import Image, ImageDraw;"
    "img = Image.new('RGBA', (8, 8), (0, 0, 0, 0));"
    "ImageDraw.Draw(img).line([(0, 0), (7, 7)]);"
    "buf = io.BytesIO(); img.save(buf, format='PNG');"
    "qr = qrcode.QRCode(border=2, box_size=8, "
    "error_correction=qrcode.constants.ERROR_CORRECT_M);"
    "qr.add_data('smoke'); qr.make(fit=True);"
    "im = qr.make_image(fill_color='#111111', back_color='#ffffff');"
    "im.save(buf, format='PNG');"
    "assert buf.getvalue()[:8] == b'\\x89PNG\\r\\n\\x1a\\n';"
    "print('SMOKE_OK')"
)


def collect_secrets() -> List[str]:
    """从本地真实配置与缓存里提取敏感值，供打包自检使用。

    刻意不在源码里写死任何 key —— 否则这个脚本本身就成了泄漏源。
    config.json 不存在时（别人 clone 下来）返回空列表，自检自动跳过。
    """
    found: set[str] = set()

    def add(value: object) -> None:
        text = str(value or "").strip()
        if len(text) >= 8:
            found.add(text)

    cfg_path = ROOT / "config.json"
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for node in (data.get("push") or {}).values():
            if isinstance(node, dict):
                for field in ("key", "sendkey", "device_key", "webhook", "secret"):
                    add(node.get(field))
        for account in data.get("accounts") or []:
            cred = account.get("credential") or {}
            add(cred.get("uid"))
            add(cred.get("token"))
            for part in str(cred.get("cookie") or "").replace(";", " ").split():
                if "=" in part:
                    add(part.split("=", 1)[1])

    def pick_field(path: Path, *fields: str) -> None:
        if not path.exists():
            return
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        stack = [blob]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in fields:
                        add(value)
                    stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)

    for cache in (ROOT / "logs").glob("skland_cred_*.json"):
        pick_field(cache, "cred", "token")
    # 只查 device_fp / device_id：这俩必须确保 logs 没被打包。
    # oaid 刻意不查 —— 它是 core/mihoyo_api.py 里的真机档案常量。
    pick_field(ROOT / "logs" / "device_fp.json", "device_fp", "device_id")

    binding = ROOT / "logs" / "mihoyo_binding.json"
    if binding.exists():
        try:
            for key in json.loads(binding.read_text(encoding="utf-8")):
                add(str(key).split(":", 1)[0])
        except (OSError, ValueError):
            pass

    return sorted(found)


# 文案铁律（改这段前先读）：
#   1. 不写「点哪个按钮 → 勾哪几个 → 怎么扫」这类分步扫码教学；
#   2. 绑定关系按游戏写，不写平台之间的扫码次数对比
#      （「米游社 1 次 / 森空岛每款 1 次」），也不写「一次扫码绑三款」「每款必须单独扫」；
#   3. 不替用户判断谁是「小白」，语气与 README / 界面保持一致。
README_TXT = """ResinLoop v{VERSION} · 日常巡查 —— 小白使用说明
================================================

每天定时查一次游戏日常进度，没做完就推送到手机（微信 / QQ）。
支持：原神、崩坏：星穹铁道、绝区零、明日方舟、明日方舟：终末地

本版本是免安装绿色版：不用装 Python、不用装任何运行库，
整个文件夹拷到哪台 64 位 Windows 10/11 电脑都能直接用。


第一步：双击 ResinLoop.exe
------------------------------------------------
会弹出一个独立窗口（右下角同时出现托盘图标）。

第一次打开是【演示模式】，页面上方有蓝色横幅提示 —— 这是正常的！
说明你还没登录任何账号，看到的数据都是空的。

点横幅上的「去扫码登录 →」进入下一步。


第二步：绑定账号
------------------------------------------------
【账号绑定】页按平台分组：米游社（原神 / 崩坏：星穹铁道 / 绝区零）、
森空岛（明日方舟 / 明日方舟：终末地）。每个游戏的登录状态分开显示，
跟着页面上的提示做就行。

状态点：绿 = 已绑定，黄 = 超过 7 天未更新，红 = 未绑定。
全部绑定后，顶部的演示模式横幅会自己消失。

【游戏监控】的开关 = 这款游戏要不要每天提醒，随手拨一下就行。


第三步：通知绑定（让提醒到你手机上）
------------------------------------------------
【通知绑定】页，二选一或都填：
  Server酱（微信推送）：打开 https://sct.ftqq.com 微信扫码，复制 SendKey 填进去
  Qmsg 酱（QQ 推送）  ：打开 https://qmsg.zendee.cn 登录，复制 key 填进去

填完先点「发送测试推送」，手机收到了再点「保存配置」。


第四步：挂机，完事
------------------------------------------------
默认每天 08:30 / 20:30 各检查一次（【定时与状态】页可改）。
到点自动检查、自动推送，你什么都不用做。

  · 点窗口右上角 X = 最小化到托盘，巡查照常运行
  · 想彻底退出：托盘图标右键 → 「彻底退出」
  · 想开机自启：【定时与状态】页 → 「开机自启动」拨到开


常见问题
------------------------------------------------
Q：双击没反应 / 被杀毒软件拦了？
A：本软件完全由本地脚本构成、未购买代码签名证书，部分杀软会误报。
   在杀软里选择「信任/允许运行」即可。不放心可到 GitHub 阅读全部源码。

Q：提示缺 WebView2 运行时？
A：极少数精简系统没有它。到微软官网搜 "WebView2 Runtime" 装一下（免费，几 MB）。

Q：扫码报 1034 / 5003（风控）？
A：换个网络（比如手机热点）重试；或在【账号绑定】页展开「设备伪装」，
   按提示贴上自己手机的设备指纹，通过率更高。

Q：数据存在哪？安全吗？
A：全部数据（登录凭证、巡检历史）只存在本机这个文件夹的 engine 子目录里，
   不上传任何服务器。注意：别把自己配好的文件夹原样发给别人。

Q：只读查询，不会代打游戏、不会自动签到。
A：是的。本项目使用 MIT 许可证，可自由修改分发。
"""

# 版本号只写在上面的 {VERSION} 占位里，别在文案中再抄一遍数字
if "{VERSION}" not in README_TXT:
    raise SystemExit("[FATAL] README_TXT 缺少 {VERSION} 占位，版本号会印错")
README_TXT = README_TXT.replace("{VERSION}", VERSION)


def write_crlf_bom(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8-sig"))


def _require(path: Path, what: str) -> Path:
    if not path.exists():
        print(f"[FATAL] 缺少 {what}：{path}")
        print("        壳请先构建（powershell -ExecutionPolicy Bypass -File shell\\build.ps1），"
              "引擎见 tests/rl_test_engine_standalone.py 前置说明")
        sys.exit(1)
    return path


def verify_shell_fresh() -> None:
    """壳必须比 C# 源码新 —— 否则包里塞的是上一版 exe。

    这种「旧壳新包」能通过条数、md5、zip 逐项校验，只有 mtime 拦得住，
    所以校验放在复制之前；CSV/JS 改动不受影响，只有 .cs / .csproj 会触发。
    """
    if not SHELL_EXE.exists() or not SHELL_DIR.is_dir():
        return  # 缺件交给 _require 报错，此处只判新旧

    sources = [
        p
        for pattern in ("*.cs", "*.csproj")
        for p in SHELL_DIR.rglob(pattern)
        if p.is_file() and not ({"obj", "bin"} & set(p.relative_to(SHELL_DIR).parts))
    ]
    if not sources:
        return

    def fmt(ts: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    newest = max(sources, key=lambda p: p.stat().st_mtime)
    exe_mtime = SHELL_EXE.stat().st_mtime
    if newest.stat().st_mtime > exe_mtime:
        print("[FATAL] 壳比 C# 源码旧 —— 包里会塞进旧 exe（md5 / 条目数校验都查不出来）")
        print(f"        最新源码 : {newest.relative_to(ROOT)}  {fmt(newest.stat().st_mtime)}")
        print(f"        现有壳   : {SHELL_EXE.name}  {fmt(exe_mtime)}")
        print("        先重编译再打包：")
        print("          powershell -ExecutionPolicy Bypass -File shell\\build.ps1")
        sys.exit(1)

    # publish 过但没拷回根层：根 exe 还是上一次的
    published = SHELL_DIR / "publish" / "ResinLoop.exe"
    if published.exists() and published.stat().st_mtime > exe_mtime:
        print("[FATAL] shell/publish/ResinLoop.exe 比仓库根的壳新 —— 没跑 shell\\build.ps1 的复制步骤")
        print(f"        publish  : {fmt(published.stat().st_mtime)}")
        print(f"        根 exe   : {fmt(exe_mtime)}")
        sys.exit(1)


def diet_engine_python(py_dir: Path) -> None:
    """瘦身打包副本里的 Python 运行时（母本 engine/python 不动，可重复可回滚）。

    每一步都带存在性守卫：母本将来换了运行时（比如 python-build-standalone）
    布局变化时，这里最多少省几 MB，绝不会把构建炸掉。
    """
    saved = 0

    def rm(path: Path) -> None:
        nonlocal saved
        if path.exists():
            saved += path.stat().st_size
            path.unlink()

    def rm_glob(directory: Path, pattern: str) -> None:
        for item in directory.glob(pattern):
            rm(item)

    sp = py_dir / "Lib" / "site-packages"
    pil = sp / "PIL"

    # 1) PIL 用不到的编解码扩展（glob 版本后缀，不写死 cp311）
    for stem in PIL_CODEC_EXT_PRUNE:
        rm_glob(pil, f"{stem}.*.pyd")
    # 2) 对应的纯 Python 包装（连注册都省掉）
    for name in PIL_CODEC_MODULE_PRUNE:
        rm(pil / name)
    # 3) pip --target 留下的控制台脚本（qr.exe / normalizer.exe 等，运行时无用）
    bin_dir = sp / "bin"
    if bin_dir.is_dir():
        shutil.rmtree(bin_dir)
    # 4) 解释器根目录死重
    for name in ROOT_RUNTIME_PRUNE:
        rm(py_dir / name)

    # 5) site-packages .py → .pyc 单态：engine 自己的解释器编译（版本必然匹配），
    #    -b = 旧式同目录布局，删源后即 sourceless import
    result = subprocess.run(
        [str(py_dir / "python.exe"), "-m", "compileall", "-b", "-q", str(sp)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if result.returncode != 0:
        print("[FATAL] compileall 失败，中止（不敢在没编译完的情况下删源码）")
        print((result.stdout or "")[-2000:])
        print((result.stderr or "")[-2000:])
        sys.exit(1)
    for py in sp.rglob("*.py"):
        saved += py.stat().st_size
        py.unlink()

    print(f"[DIET] 瘦身完成：省 {saved/1024/1024:.1f} MB（副本）")


def smoke_engine_runtime(engine_dir: Path) -> None:
    """瘦身后用打包副本的解释器跑一遍真依赖链，失败即中止打包。

    冒烟链 = requests 可导入 + PIL 画图 + 二维码出 PNG（与 login_ui.qr_png_base64
    同参数同调用姿势）。 diet 删的是"确定不用"的东西，这一步是最后一道闸。
    """
    py = engine_dir / "python" / "python.exe"
    result = subprocess.run(
        [str(py), "-c", SMOKE_SNIPPET],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(engine_dir),
    )
    if result.returncode != 0 or "SMOKE_OK" not in (result.stdout or ""):
        print("[FATAL] 瘦身后运行时冒烟失败 —— 中止打包")
        print((result.stdout or "")[-2000:])
        print((result.stderr or "")[-2000:])
        sys.exit(1)
    print("[OK] 瘦身运行时冒烟通过（requests / PIL 画图 / 二维码 PNG）")


def build_engine(out_engine: Path) -> None:
    """engine/ = 便携解释器 + 当前工作树代码（零凭据零缓存）。"""
    # 1) 便携 Python（解释器 + 预装依赖，纯库无隐私；剥掉 __pycache__ 减肥）
    shutil.copytree(
        _require(ENGINE_PY, "便携 Python（engine/python）"),
        out_engine / "python",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyd.bak"),
    )
    diet_engine_python(out_engine / "python")

    # 2) 项目代码：当前工作树（v2.1 未入库前的最新改动也要带上）
    for py in ("login_ui.py", "main.py", "daemon.py"):
        shutil.copy2(ROOT / py, out_engine / py)
    (out_engine / "core").mkdir()
    for py in sorted((ROOT / "core").glob("*.py")):
        shutil.copy2(py, out_engine / "core" / py.name)

    # 3) 前端三件套（只要正身，.bak 备份不进包）
    (out_engine / "ui").mkdir()
    for name in ("index.html", "style.css", "app.js"):
        shutil.copy2(ROOT / "ui" / name, out_engine / "ui" / name)

    # 4) 图标 + APK（体积大不入库，但必须随包）
    if (ROOT / "static" / "icons").is_dir():
        shutil.copytree(ROOT / "static" / "icons", out_engine / "static" / "icons")
    tools_src = ROOT / "tools"
    if tools_src.is_dir():
        (out_engine / "tools").mkdir()
        for item in sorted(tools_src.iterdir()):
            if item.is_file() and not item.name.startswith("."):
                shutil.copy2(item, out_engine / "tools" / item.name)

    # 5) 入口：exe 专用引擎模板（纯 HTTP 无窗口），不是源码仓的 app.py
    shutil.copy2(_require(ENGINE_APP_TEMPLATE, "引擎入口模板（shell/engine_app.py）"),
                 out_engine / "app.py")

    # 6) daemon 去托盘：发行版托盘归 C# 壳（与 engine/daemon.py 同一处方）
    daemon_path = out_engine / "daemon.py"
    src = daemon_path.read_text(encoding="utf-8")
    needle = "return run_daemon(use_tray=not args.no_tray)"
    if src.count(needle) != 1:
        print(f"[FATAL] daemon.py 托盘开关锚点异常（出现 {src.count(needle)} 次），中止")
        sys.exit(1)
    daemon_path.write_text(
        src.replace(
            needle,
            "# exe 发行版：托盘由 C# 壳负责（ResinLoop.exe --tray），daemon 恒为 headless\n"
            "    return run_daemon(use_tray=False)",
        ),
        encoding="utf-8",
    )

    # 7) 刻意不带 config.json：首启 ensure_config 自动生成出厂默认
    #    （provider=mock → 欢迎横幅引导扫码，绝不显示假数据）


def verify_version_consistency() -> None:
    """csproj 的 <Version> 必须与 core/__init__.py 一致。

    两处不同步的后果很隐蔽：zip 名和界面写的是新版本，而 exe 属性页、资源管理器
    里显示的是旧版本 —— v2.1.2 构建时就差点这样发出去。
    """
    csproj = ROOT / "shell" / "ResinLoop.csproj"
    text = csproj.read_text(encoding="utf-8")
    match = re.search(r"<Version>\s*([^<\s]+)\s*</Version>", text)
    if not match:
        raise SystemExit("[FATAL] shell/ResinLoop.csproj 里找不到 <Version>")
    if match.group(1) != VERSION:
        raise SystemExit(
            f"[FATAL] 版本号不一致：ResinLoop.csproj={match.group(1)} / core/__init__.py={VERSION}"
        )
    print(f"[OK] 版本号一致：{VERSION}")


def main() -> None:
    verify_shell_fresh()
    verify_version_consistency()

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # 1) 壳四件（exe + 2 个托管库 + 原生 loader）
    shutil.copy2(_require(SHELL_EXE, "C# 壳 ResinLoop.exe（先跑 shell\\build.ps1）"), OUT / "ResinLoop.exe")
    for name in SHELL_COMPANIONS:
        shutil.copy2(_require(ROOT / name, name), OUT / name)

    # 2) engine（含 diet 瘦身 + 瘦身后运行时冒烟，失败即中止）
    build_engine(OUT / "engine")
    smoke_engine_runtime(OUT / "engine")

    # 3) 使用说明（记事本友好：UTF-8 BOM + CRLF）
    write_crlf_bom(OUT / "小白使用说明.txt", README_TXT)

    # 4) 体检：敏感值来自本地真实配置/缓存，源码零硬编码
    banned = collect_secrets()
    leaked = []
    for f in OUT.rglob("*"):
        if f.is_file():
            body = f.read_bytes().decode("utf-8", errors="ignore")
            for token in banned:
                if token in body:
                    leaked.append(f"{f.relative_to(OUT)} <- {token[:6]}…")

    # 泄漏自检是门禁不是告示：带密 zip 一个都不许出（曾经只打印一行就继续打包）
    if leaked:
        print("[FATAL] 泄漏自检命中敏感值，中止打包：")
        for item in leaked:
            print("  -", item)
        sys.exit(1)

    # 5) 打包
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = ZIP_DIR / ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()
    archive = shutil.make_archive(str(zip_path), "zip", root_dir=OUT.parent, base_dir=OUT.name)

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"OUT_DIR   : {OUT}")
    print(f"FILES     : {sum(1 for f in OUT.rglob('*') if f.is_file())} 个, {total/1024/1024:.1f} MB")
    print(f"ZIP       : {archive}  ({Path(archive).stat().st_size/1024/1024:.1f} MB)")
    print(f"SECRETS   : {'干净 ✓' if not leaked else '发现泄漏 -> ' + str(leaked)}")
    top = sorted(p.relative_to(OUT) for p in OUT.iterdir())
    print(f"顶层清单  : {[str(p) for p in top]}")


if __name__ == "__main__":
    main()
