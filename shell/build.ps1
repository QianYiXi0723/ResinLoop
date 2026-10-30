# ResinLoop 壳构建：dotnet publish（不合并）+ 全新目录首启门禁
#
# 用法：powershell -ExecutionPolicy Bypass -File shell\build.ps1 [-SkipGate]
#
# 产物（仓库根，四件）：
#   ResinLoop.exe
#   Microsoft.Web.WebView2.Core.dll
#   Microsoft.Web.WebView2.WinForms.dll
#   WebView2Loader.dll
#
# 为什么不再用 ILRepack / ILMerge：
#   net48 不支持官方 PublishSingleFile，想"单文件"只能手搓合并。而 2026-10-30 实测，
#   ILRepack 带 /internalize 会把合并进来的 WebView2 类型降为 internal，MainForm..ctor
#   第一次触碰 DefaultBackgroundColor 就抛 System.TypeLoadException（0xe0434352），
#   exe 双击秒退、窗口根本不出现。而它换来的收益只有 10,888 字节和两个文件
#   （WebView2Loader.dll 是原生件，本来就合不进去，"单文件"一直是假象）。
#   成熟项目（BetterGI、绘世启动器）同样发文件夹 + 便携运行时，不手搓合并。
#
# 门禁（-SkipGate 可跳过）：
#   把四件 + engine（剥掉 logs/）复制到 %TEMP% 空目录实跑，断言窗口标题、/api/env、
#   /api/device、首启自动生成 device_identity.json。
#   这道门禁存在的理由：v2.0.0/v2.1 的 TypeLoadException 和 2.1.1 的 NameError，
#   都只在"没有历史产物、没有身份文件"的全新目录才复现；在 shell\publish 里跑冒烟
#   永远是绿的，所以必须复制出去测。

param([switch]$SkipGate)

$ErrorActionPreference = "Stop"

$shellDir = $PSScriptRoot
$root = Split-Path -Parent $shellDir
$publishDir = Join-Path $shellDir "publish"

$shellFiles = @(
    "ResinLoop.exe",
    "Microsoft.Web.WebView2.Core.dll",
    "Microsoft.Web.WebView2.WinForms.dll",
    "WebView2Loader.dll"
)

Write-Host "==> dotnet publish"
& dotnet publish $shellDir -c Release -o $publishDir
if ($LASTEXITCODE -ne 0) { throw "dotnet publish 失败（exit $LASTEXITCODE）" }

# 清掉上一轮遗留：旧的合并版 exe 与 Wpf.dll 绝不能混进发行包
foreach ($name in ($shellFiles + @("Microsoft.Web.WebView2.Wpf.dll", "ResinLoop.exe.config"))) {
    $stale = Join-Path $root $name
    if (Test-Path $stale) { Remove-Item $stale -Force -Confirm:$false }
}

Write-Host "==> 复制四件到仓库根"
foreach ($name in $shellFiles) {
    $src = Join-Path $publishDir $name
    if (-not (Test-Path $src)) { throw "缺少产物：$src" }
    Copy-Item $src (Join-Path $root $name) -Force
    Write-Host ("    {0,-42} {1,10:N0} B" -f $name, (Get-Item (Join-Path $root $name)).Length)
}

# 字节冒烟：托盘「立即巡检」文案必须真的编进去了（防拿旧产物充数）
$bytes = [System.IO.File]::ReadAllBytes((Join-Path $root "ResinLoop.exe"))
$needle = [System.Text.Encoding]::Unicode.GetBytes("立即巡检")
$hit = $false
for ($i = 0; $i -le $bytes.Length - $needle.Length; $i++) {
    if ($bytes[$i] -eq $needle[0]) {
        $ok = $true
        for ($j = 1; $j -lt $needle.Length; $j++) {
            if ($bytes[$i + $j] -ne $needle[$j]) { $ok = $false; break }
        }
        if ($ok) { $hit = $true; break }
    }
}
if (-not $hit) { throw "冒烟失败：exe 内找不到「立即巡检」（UTF-16LE），产物可疑" }
Write-Host "==> 冒烟通过：exe 内含「立即巡检」"

if ($SkipGate) {
    Write-Host "==> 已跳过首启门禁（-SkipGate）"
    exit 0
}

# ---------- 全新目录首启门禁 ----------
$engineSrc = Join-Path $root "engine"
if (-not (Test-Path (Join-Path $engineSrc "python\pythonw.exe"))) {
    throw "engine\ 未就位（缺 python\pythonw.exe），先按 tests\rl_test_engine_standalone.py 重建"
}

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()

# 固定目录，不要随机后缀：Windows 防火墙按可执行文件完整路径记规则，每换一个路径就要重新弹窗
# 确认一次（每个路径还会落 TCP/UDP 两条规则）。以前用 rl_gate_<随机8位> 跑了几轮构建，系统里
# 就积了几十条指向 %TEMP% 的僵尸规则。固定目录 + 每次先清空，仍然满足「全新目录首启」的语义。
# 两个会话可能同时跑构建，用锁文件把门禁串行化，否则会互相删掉对方的 gate 目录
$gate = Join-Path $env:TEMP "rl_gate_fixed"
$gateLockPath = Join-Path $env:TEMP "rl_gate_fixed.lock"
$gateLock = $null
for ($i = 0; $i -lt 150; $i++) {
    try {
        $gateLock = [System.IO.File]::Open($gateLockPath, [System.IO.FileMode]::CreateNew,
                                           [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        break
    } catch {
        # 锁文件超过 10 分钟 = 上一次异常退出留下的，强删后重试
        if ((Test-Path $gateLockPath) -and
            ((Get-Date) - (Get-Item $gateLockPath).LastWriteTime).TotalMinutes -gt 10) {
            Remove-Item $gateLockPath -Force -ErrorAction SilentlyContinue
            continue
        }
        Start-Sleep -Seconds 2
    }
}
if ($gateLock -eq $null) { throw "等待门禁锁超时（5 分钟）：$gateLockPath 被另一个构建占用" }

if (Test-Path $gate) { Remove-Item $gate -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue }
if (Test-Path $gate) {
    # 这里在 try 之外，必须先把锁放掉，否则 .lock 会卡住后面所有构建
    if ($gateLock -ne $null) { $gateLock.Dispose(); Remove-Item $gateLockPath -Force -ErrorAction SilentlyContinue }
    throw ("旧门禁目录删不掉：" + $gate + "（多半是 msedgewebview2 还占着 logs\webview2，先结束再重试）")
}
New-Item -ItemType Directory -Path $gate -Force | Out-Null
$proc = $null

try {
    Write-Host ("==> 门禁：全新目录 " + $gate + "  端口 " + $port)
    foreach ($name in $shellFiles) { Copy-Item (Join-Path $root $name) (Join-Path $gate $name) -Force }

    # engine 必须剥掉 logs/ 与 __pycache__：留着身份文件就测不到"首次运行"那条路径
    & robocopy $engineSrc (Join-Path $gate "engine") /E /XD logs __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy 复制 engine 失败（exit $LASTEXITCODE）" }

    $proc = Start-Process -FilePath (Join-Path $gate "ResinLoop.exe") `
                          -ArgumentList "--port", $port, "--allow-multi" -WorkingDirectory $gate -PassThru

    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 400
        $proc.Refresh()
        if ($proc.HasExited) { throw ("壳在就绪前退出，ExitCode=0x{0:X8}" -f $proc.ExitCode) }
        $code = & curl.exe -s -o NUL -w "%{http_code}" --noproxy "*" --max-time 2 ("http://127.0.0.1:$port/api/env")
        if ($code -eq "200") { $ready = $true; break }
    }
    if (-not $ready) { throw "引擎 24 秒内未就绪" }

    $title = ""
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 400
        $proc.Refresh()
        if ($proc.HasExited) { throw ("壳在就绪后崩溃，ExitCode=0x{0:X8}" -f $proc.ExitCode) }
        $title = $proc.MainWindowTitle
        if ($title -match "ResinLoop") { break }
    }
    if ($title -notmatch "ResinLoop") { throw ("窗口标题异常：'" + $title + "'") }

    foreach ($path in @("/api/env", "/api/device", "/api/live", "/api/schedule")) {
        $c = & curl.exe -s -o NUL -w "%{http_code}" --noproxy "*" --max-time 6 ("http://127.0.0.1:$port" + $path)
        if ($c -ne "200") { throw ("门禁失败：{0} -> {1}" -f $path, $c) }
    }

    $dev = (& curl.exe -s --noproxy "*" --max-time 6 ("http://127.0.0.1:$port/api/device")) -join ""
    if ($dev -notmatch "model") { throw "/api/device 返回体缺少 model 字段（设备档案没建起来）" }

    $idf = Join-Path $gate "engine\logs\device_identity.json"
    if (-not (Test-Path $idf)) { throw "首启未生成 engine\logs\device_identity.json（全新安装路径没走通）" }

    # 页面真的渲染出来了吗？壳在 NavigationCompleted 后会跑一次 DOM 自检 + fetch 探针写 shell.log。
    # 只看 /api/* 是 200 不够 —— 页面白屏、资源 404、页面接管失效都不会影响接口。
    # 注意这里是「等信号」不是「读一次」：导航完成比 curl 那几个接口慢，固定顺序读会拿到半截日志。
    $slog = Join-Path $gate "logs\shell.log"
    if (-not (Test-Path $slog)) { throw "壳没写 logs\shell.log" }
    $probe = $null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 400
        $probe = Select-String -Path $slog -Pattern 'API 探针 200/LEN\d+' | Select-Object -Last 1
        if ($probe) { break }
    }
    if (-not $probe) {
        Write-Host "--- shell.log 尾部 ---"
        Get-Content $slog -Encoding UTF8 -ErrorAction SilentlyContinue |
            Select-Object -Last 16 | ForEach-Object { Write-Host ("    " + $_) }
        throw "页面 fetch 没走通（/api/* 没有被壳拦下代转到引擎）"
    }
    if (-not (Select-String -Path $slog -Pattern "页面接管" -Quiet)) { throw "页面没走壳内接管（仍在使用本地 HTTP）" }
    if (-not (Select-String -Path $slog -Pattern "页面加载 成功" -Quiet)) { throw "页面加载失败（见上面的 shell.log 尾部）" }
    $dom = Select-String -Path $slog -Pattern "DOM 自检 tabpanel=(\d+)" | Select-Object -Last 1
    if (-not $dom) { throw "页面没有完成 DOM 自检（白屏或 JS 未执行）" }
    $tabs = [int]$dom.Matches[0].Groups[1].Value
    if ($tabs -lt 1) { throw ("页面 DOM 异常：tabpanel=" + $tabs) }

    Write-Host ("    窗口：'" + $title + "'")
    Write-Host ("    /api/device " + $dev.Length + " 字节；身份文件 " + (Get-Item $idf).Length + " B；页面 tabpanel=" + $tabs + "；fetch 探针 200")
    Write-Host "==> 门禁通过"
}
finally {
    if ($proc -ne $null -and -not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
    Get-Process -Name "pythonw" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$gate*" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    # WebView2 的子进程不跟着父进程退，user-data-dir 又落在 gate 里，不清理就会锁住删除
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -like ("*" + $gate + "*") } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 800
    if (Test-Path $gate) { Remove-Item $gate -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue }
    if ($gateLock -ne $null) {
        $gateLock.Dispose()
        Remove-Item $gateLockPath -Force -ErrorAction SilentlyContinue
    }
}
