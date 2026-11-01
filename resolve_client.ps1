# 辅助脚本：解析出小浣熊客户端主程序的完整路径。
#
# 为什么需要它：客户端 exe 名是中文（"商汤小浣熊.exe"），Windows PowerShell 5.1
# 读 .ps1 / -Command 里的中文字面量时按 ANSI(GBK) 解码，会得到乱码，导致
# Get-ChildItem -Filter / Test-Path / Get-CimInstance -Filter "Name='...'" 全部失配。
# 解决办法：所有中文都在运行时从文件系统取，脚本里不出现任何中文字面量。
#
# 另一个坑：cmd 的 for /f 捕获 PowerShell 输出时，非 ASCII 路径会被编码转换弄坏
# （实测 "商汤小浣熊.exe" 变成 "商汤小浣熊?exe"）。所以本脚本默认输出 **8.3 短路径**
# （纯 ASCII，如 C:\PROGRAM FILES\@RACC~1\S6B0D~1.EXE），供 for /f 安全捕获；
# 短路径同样能正常启动程序。
#
# 用法: resolve_client.ps1 <安装目录> [-Long]
#   -Long  输出完整长路径（仅用于人工查看，别喂给 for /f）
# 退出码: 0=成功, 1=失败

param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [switch]$Long
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $AppDir)) {
    Write-Error ("install dir not found: " + $AppDir)
    exit 1
}

$exe = Get-ChildItem -LiteralPath $AppDir -Filter *.exe |
    Where-Object { $_.Length -gt 100MB -and $_.Name -notlike 'Uninstall*' } |
    Sort-Object Length -Descending |
    Select-Object -First 1

if ($null -eq $exe) {
    Write-Error ("no main executable found under: " + $AppDir)
    exit 1
}

if ($Long) {
    Write-Output $exe.FullName
    exit 0
}

# 取 8.3 短路径。FileSystemInfo 上没有直接的 ShortName 属性，
# 用 Scripting.FileSystemObject 的 COM 接口拿，失败则退回长路径。
try {
    $fso = New-Object -ComObject Scripting.FileSystemObject
    $short = $fso.GetFile($exe.FullName).ShortPath
    if ($short -and $short -notmatch '[^\x20-\x7E]') {
        Write-Output $short
        exit 0
    }
} catch {
}

Write-Output $exe.FullName
exit 0
