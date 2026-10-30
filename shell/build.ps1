# ResinLoop 壳构建：dotnet publish + ILRepack 单文件化
#
# 用法（仓库根或任意目录）：
#   powershell -ExecutionPolicy Bypass -File shell\build.ps1
#
# 产物：<仓库根>\ResinLoop.exe（net48 单文件，内嵌图标）
#
# ⚠️ ILRepack 绝对不要加 /internalize
#    它会把合并进来的 WebView2 类型全部降为 internal，MainForm 构造时
#    第一次触碰 WebView2（MainForm.cs 里的 DefaultBackgroundColor）就会抛
#    System.TypeLoadException（0xe0434352），exe 一启动即崩、窗口都不出现。
#    2026-10-30 实测：加 /internalize 的 653,824B 版本无法启动；去掉后同一份
#    publish 产物合并出来一模一样大，但能正常起窗口、起引擎。
#
# 只改 Python/页面不需要跑本脚本（那是 engine\ 的事）；只有 shell\*.cs 变了才需要。

$ErrorActionPreference = "Stop"

$shellDir = $PSScriptRoot
$root = Split-Path -Parent $shellDir
$publishDir = Join-Path $shellDir "publish"
$outExe = Join-Path $root "ResinLoop.exe"

Write-Host "==> dotnet publish"
& dotnet publish $shellDir -c Release -o $publishDir
if ($LASTEXITCODE -ne 0) { throw "dotnet publish 失败（exit $LASTEXITCODE）" }

$il = Join-Path $root "build_ilrepack\tools\ILRepack.exe"
if (-not (Test-Path $il)) { throw "缺少 ILRepack：$il" }

$main = Join-Path $publishDir "ResinLoop.exe"
$deps = @(
    (Join-Path $publishDir "Microsoft.Web.WebView2.Core.dll"),
    (Join-Path $publishDir "Microsoft.Web.WebView2.WinForms.dll")
)
foreach ($d in @($main) + $deps) {
    if (-not (Test-Path $d)) { throw "缺少合并输入：$d" }
}

Write-Host "==> ILRepack（不带 /internalize）"
& $il "/out:$outExe" "/ndebug" $main $deps
if ($LASTEXITCODE -ne 0) { throw "ILRepack 失败（exit $LASTEXITCODE）" }

if (-not (Test-Path $outExe)) { throw "合并产物未生成：$outExe" }
$size = (Get-Item $outExe).Length
Write-Host ("==> 完成：{0}  ({1:N0} 字节)" -f $outExe, $size)

# 冒烟：单文件里必须能找到托盘「立即巡检」的文案，否则多半合并漏了资源
$bytes = [System.IO.File]::ReadAllBytes($outExe)
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
if (-not $hit) { throw "冒烟失败：exe 内找不到「立即巡检」（UTF-16LE），可能合并了旧产物" }
Write-Host "==> 冒烟通过：exe 内含「立即巡检」"
