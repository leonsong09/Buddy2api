# 辅助脚本：关闭指定小浣熊客户端实例（按 profile 目录匹配）。
#
# 为什么需要它：同上，cmd 里写长 PowerShell 命令会被引号/转义弄坏。
# 绝不用 taskkill /T（会连带杀掉同一进程树里的其他实例）。
#
# 用法: stop_instance.ps1 <profile 目录完整路径>

param(
    [Parameter(Mandatory = $true)][string]$ProfileDir
)

$ErrorActionPreference = 'SilentlyContinue'

Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like ('*--client-profile*' + $ProfileDir + '*') } |
    ForEach-Object {
        Write-Output ("stopping pid " + $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force
    }

exit 0
