# 辅助脚本：判断某个小浣熊客户端实例是否正在运行。
#
# 为什么需要它：客户端 exe 名是中文，PowerShell 5.1 读 -Command 里的中文字面量会按
# ANSI 解码成乱码，所以脚本里不出现任何中文字面量，路径由参数传入。
# 同时避免在 cmd 里写很长的 -Command 字符串（cmd 的引号/转义会把 PowerShell 语法弄坏，
# 表现为 "Unexpected token ... in expression or statement"）。
#
# 用法: instance_running.ps1 <profile 目录完整路径>
# 退出码: 0=正在运行, 1=未运行

param(
    [Parameter(Mandatory = $true)][string]$ProfileDir
)

$ErrorActionPreference = 'SilentlyContinue'

$running = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like ('*--client-profile*' + $ProfileDir + '*') } |
    Select-Object -First 1

if ($null -ne $running) { exit 0 } else { exit 1 }
