# 辅助脚本：写入小浣熊客户端的 profile 标记文件。
#
# 为什么需要它：marker 内容必须逐字节等于 {"application":"@raccoon/office-desktop","version":1} + LF，
# cmd 的 echo 重定向会写成 CRLF（多一个 \r 就校验失败）。
# 同时脚本里不能出现任何中文字面量（PowerShell 5.1 按 ANSI 解码会变乱码），
# 所以路径由参数传入。
#
# 用法: write_marker.ps1 <marker 文件完整路径>

param(
    [Parameter(Mandatory = $true)][string]$Path
)

$ErrorActionPreference = 'Stop'

$content = '{"application":"@raccoon/office-desktop","version":1}' + [char]10

# UTF8Encoding($false) = 无 BOM，否则客户端按 JSON 解析会失败
[IO.File]::WriteAllText($Path, $content, (New-Object System.Text.UTF8Encoding $false))

if (-not (Test-Path -LiteralPath $Path)) {
    Write-Error ("marker not written: " + $Path)
    exit 1
}
exit 0
