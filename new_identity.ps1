# 辅助脚本：生成一份新的客户端设备身份文件（desktop-device-identity.json）。
#
# 为什么需要它：
# 客户端的设备身份路径是 app.getPath('appData') + '\office-raccoon'，
# 而 appData 取 process.env.APPDATA，且不受 --client-profile 影响。
# 如果多个多开实例共用同一个 %APPDATA%，它们会共享同一个 clientDeviceId，
# 服务端按设备维度绑定账号（POST /api/web/auth/v1/devices_current_bind），
# 导致登一个号就把另一个实例的号踢掉。
#
# 解决办法：给每个实例一个独立的 APPDATA，并在里面预置一份唯一的
# clientDeviceId，客户端 loadOrCreateIdentity 会直接沿用，不会去动共享的那份。
#
# 注意：脚本里不能出现中文字面量（PowerShell 5.1 按 ANSI 解码会变乱码），
# 所以所有文本都用英文，路径由参数传入。
#
# 用法: new_identity.ps1 <identity 文件完整路径>
# 退出码: 0=成功, 1=失败

param(
    [Parameter(Mandatory = $true)][string]$Path
)

$ErrorActionPreference = 'Stop'

$dir = Split-Path -Parent $Path
if (-not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}

if (Test-Path -LiteralPath $Path) {
    # already exists, keep it
    exit 0
}

$id = [System.Guid]::NewGuid().ToString()
$created = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')

$json = '{"schemaVersion":1,"clientDeviceId":"' + $id + '","createdAt":"' + $created + '"}'

# UTF8Encoding($false) = no BOM, LF line ending handled by WriteAllText on Windows
[IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding $false))

if (-not (Test-Path -LiteralPath $Path)) {
    Write-Error ('identity not written: ' + $Path)
    exit 1
}

exit 0
