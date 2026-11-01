# 辅助脚本：关闭所有由"小浣熊多开.bat"启动的客户端实例。
#
# 只杀命令行里同时含 @raccoon 与 --client-profile 的进程，
# 不动用户手动双击启动的默认实例（那个没有 --client-profile）。
#
# 用法: stop_all_instances.ps1

$ErrorActionPreference = 'SilentlyContinue'

Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*@raccoon*--client-profile*' } |
    ForEach-Object {
        Write-Output ("stopping pid " + $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force
    }

exit 0
