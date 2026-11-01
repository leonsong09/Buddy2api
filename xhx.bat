@echo off
setlocal EnableDelayedExpansion
rem ============================================================
rem  这是"小浣熊多开.bat"的 ASCII 名副本，内容完全一致。
rem
rem  为什么要它：bat 文件名是中文，在 GBK 代码页的 cmd 里
rem  用 call "完整中文路径" 会失败（不是内部或外部命令），
rem  8.3 短路径对中文名也解不出来。所以提供这个 ASCII 名入口。
rem
rem  用法（在本文件所在目录执行）:
rem    xhx list         列出当前所有实例
rem    xhx new 一号     创建并启动一个新实例
rem    xhx new 二号     再开一个
rem    xhx start 二号   启动已存在的实例
rem    xhx stop 二号    只关这一个（保留数据）
rem    xhx stopall      关掉所有多开实例
rem    xhx auth 二号    打印 auth.json 路径，用于网关导入
rem    xhx rm 二号      删除实例
rem
rem  每个实例对应一个可独立登录的小浣熊账号。
rem ============================================================

rem ============================================================
rem  商汤小浣熊多开管理器
rem
rem  用法:
rem    小浣熊多开.bat             列出当前所有实例
rem    小浣熊多开.bat new 二号    新建并启动一个实例
rem    小浣熊多开.bat start 二号  启动已存在的实例
rem    小浣熊多开.bat stop 二号   关闭实例（保留 profile 数据）
rem    小浣熊多开.bat stopall     关闭所有实例
rem    小浣熊多开.bat auth 二号   打印该实例的 auth.json 路径（用于网关导入）
rem    小浣熊多开.bat rm 二号     删除实例（删除 profile 数据！不可恢复）
rem
rem  每个实例 = 一个独立 profile 目录 = 一个可独立登录的小浣熊账号。
rem ============================================================

set "APPDIR=C:\Program Files\@raccoonoffice-desktop"
set "ROOT=%USERPROFILE%\RaccoonInstances"
set "MARKER=.raccoon-lab-client.json"
set "PS_DIR=%~dp0"
set "MARKER_CONTENT={"application":"@raccoon/office-desktop","version":1}"

rem 客户端 exe 名是中文（商汤小浣熊.exe），PowerShell 5.1 读 .ps1 按 ANSI 解码会把它弄成乱码，
rem 所以这里不硬编码 exe 名，改成运行时用一个 UTF-8 的辅助 ps1 解析出来（取体积最大且非 Uninstall 的那个）。
rem 辅助脚本路径固定，内容由本文件旁边的 resolve_client.ps1 提供。
set "PS_RESOLVER=%~dp0resolve_client.ps1"
if not exist "%PS_RESOLVER%" (
    echo [错误] 缺少辅助脚本: %PS_RESOLVER%
    pause
    exit /b 1
)
set "CLIENT="
for /f "usebackq delims=" %%F in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_RESOLVER%" "%APPDIR%"`) do set "CLIENT=%%F"

if not exist "%APPDIR%" (
    echo [错误] 找不到客户端安装目录: %APPDIR%
    echo        请确认安装路径。
    pause
    exit /b 1
)
if not defined CLIENT (
    echo [错误] 在 %APPDIR% 里解析不到客户端主程序。
    pause
    exit /b 1
)
if not exist "%CLIENT%" (
    echo [错误] 客户端主程序不存在: %CLIENT%
    pause
    exit /b 1
)

if not exist "%ROOT%" mkdir "%ROOT%"

rem ---------- 命令分发 ----------
if /i "%~1"=="list"    goto do_list
if /i "%~1"=="new"     goto do_new
if /i "%~1"=="start"   goto do_start
if /i "%~1"=="stop"    goto do_stop
if /i "%~1"=="stopall" goto do_stopall
if /i "%~1"=="auth"    goto do_auth
if /i "%~1"=="rm"      goto do_rm
if "%~1"==""           goto do_list

echo [错误] 未知命令: %~1
echo.
echo 用法: %~nx0 ^| new 名称 ^| start 名称 ^| stop 名称 ^| stopall ^| rm 名称 ^| auth 名称 ^| list
pause
exit /b 1

rem ============================================================
rem  命令实现
rem ============================================================

:do_list
echo ============================================================
echo   小浣熊实例列表
echo ============================================================
echo 实例根目录: %ROOT%
echo.
set "FOUND="
for /d %%D in ("%ROOT%\*") do (
    set "FOUND=1"
    call :instance_status "%%~nxD"
)
if not defined FOUND (
    echo （还没有实例）
    echo.
    echo 创建第一个实例:  %~nx0 new 一号
    echo 创建第二个实例:  %~nx0 new 二号
)
echo.
echo 提示: 每个实例对应一个可独立登录的账号。
echo       在网关导入前，先在该实例里登录，然后用 %~nx0 auth 名称 查看 auth.json 路径。
pause
exit /b 0

rem ---- 子程序: instance_status <实例名> ----
:instance_status
set "P=%ROOT%\%~1"
set "STATE=未登录"
if exist "%P%\agent\config\auth.json" set "STATE=已登录"
set "RUN=停止"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_DIR%instance_running.ps1" "%P%"
if not errorlevel 1 set "RUN=运行中"
echo   [%RUN%] %~1   %STATE%
echo            %P%
exit /b 0

:do_new
if not exist "%ROOT%\%~2" goto create_profile
echo [提示] 实例 %~2 已存在，直接启动。
goto launch

:create_profile
echo [1/3] 创建实例目录 %ROOT%\%~2 ...
mkdir "%ROOT%\%~2%"
if not exist "%ROOT%\%~2%" (
    echo [错误] 目录创建失败
    pause
    exit /b 1
)
rem 客户端要求 marker 内容逐字节等于 {"application":"@raccoon/office-desktop","version":1} + LF，
rem 且 nlink 必须为 1、不能是符号链接。
rem 注意：cmd 的 echo 会写成 CRLF（多一个 \r 就逐字节校验失败），所以改用 ps1 写。
rem 目录名可能含中文，用 %~2 原样传参（不经过 shell 二次解析）。
call :write_marker "%ROOT%\%~2%"
echo [2/3] 写入 profile 标记 %MARKER%
if not exist "%ROOT%\%~2\%MARKER%" (
    echo [错误] profile 标记写入失败
    pause
    exit /b 1
)
echo [3/3] 启动客户端...

:launch
rem ============================================================
rem 关键：给每个实例一份独立的 APPDATA。
rem
rem 客户端的设备身份文件（desktop-device-identity.json，含 clientDeviceId）路径是
rem   app.getPath('appData') + '\office-raccoon'
rem 而 appData 在 Windows 上取 process.env.APPDATA，且**不受 --client-profile 影响**。
rem 如果所有实例共用同一个 %APPDATA%，它们上报的就是同一个 clientDeviceId，
rem 服务端按设备维度绑定账号（POST /api/web/auth/v1/devices_current_bind），
rem 结果就是：登一个号，另一个实例的号被踢掉。
rem 所以这里给每个实例指向自己的 APPDATA，clientDeviceId 各自独立。
rem ============================================================
set "IROOT=%ROOT%\%~2"
set "IAPPDATA=%IROOT%\AppData\Roaming"
if not exist "%IAPPDATA%" mkdir "%IAPPDATA%"
if not exist "%IAPPDATA%\office-raccoon" mkdir "%IAPPDATA%\office-raccoon"
set "IIDENT=%IAPPDATA%\office-raccoon\desktop-device-identity.json"
if not exist "%IIDENT%" powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_DIR%new_identity.ps1" "%IIDENT%"

rem start 的第一个参数是窗口标题，必须非空（空引号会被当成程序名），且用 ASCII。
set "APPDATA=%IAPPDATA%"
start "xhx-instance-%~2" "%CLIENT%" --client-profile "%IROOT%"
echo.
echo 已启动实例 %~2
echo   窗口会打开登录页，登录后凭据保存在:
echo   %ROOT%\%~2%\agent\config\auth.json
echo.
echo 下一步（等登录完成后）:
echo   1) 用 %~nx0 auth %~2 查看 auth.json 路径
echo   2) 用该路径在 Buddy2api 管理页的"小浣熊"通道导入
pause
exit /b 0

:do_start
if not exist "%ROOT%\%~2\%MARKER%" (
    echo [错误] 实例 %~2 不存在（缺 profile 标记）
    echo        用 %~nx0 new %~2 创建
    pause
    exit /b 1
)
goto launch

:do_stop
if not exist "%ROOT%\%~2%" (
    echo [错误] 实例 %~2 不存在
    pause
    exit /b 1
)
echo 正在关闭实例 %~2 ...
rem 按命令行匹配，只杀这一个实例的进程。绝不用 taskkill /T（会连带杀死其他实例）。
rem 注意：进程名是中文，PowerShell 5.1 读 -Command 里的中文会按 ANSI 解码成乱码，
rem 所以这里不过滤 Name，直接用 --client-profile 的参数值匹配，避免编码问题。
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_DIR%stop_instance.ps1" "%ROOT%\%~2%"
echo 已关闭（profile 数据保留）。
pause
exit /b 0

:do_stopall
echo 正在关闭所有小浣熊实例...
rem 只杀命令行里带 --client-profile 的进程（即本脚本启动的实例），不动用户手动启动的默认实例。
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_DIR%stop_all_instances.ps1"
echo 已全部关闭（profile 数据保留）。
pause
exit /b 0

:do_rm
if not exist "%ROOT%\%~2%" (
    echo [错误] 实例 %~2 不存在
    pause
    exit /b 1
)
echo !!! 警告 !!!
echo 将删除实例目录: %ROOT%\%~2%
echo 里面的登录凭据、会话、workspace 会全部丢失且无法恢复。
echo 如果只想暂时关闭，请用 %~nx0 stop %~2
set /p "ANS=确认删除？输入 YES 继续: "
if /i not "!ANS!"=="YES" (
    echo 已取消。
    pause
    exit /b 0
)
echo 先关闭该实例...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_DIR%stop_instance.ps1" "%ROOT%\%~2%"
timeout /t 2 /nobreak >nul
rmdir /s /q "%ROOT%\%~2%"
if exist "%ROOT%\%~2%" (
    echo [错误] 删除失败，可能有文件被占用
) else (
    echo 已删除实例 %~2
)
pause
exit /b 0

:do_auth
if not exist "%ROOT%\%~2%" (
    echo [错误] 实例 %~2 不存在
    pause
    exit /b 1
)
echo ============================================================
echo   实例 %~2 的凭据文件
echo ============================================================
echo   %ROOT%\%~2%\agent\config\auth.json
echo.
if exist "%ROOT%\%~2%\agent\config\auth.json" (
    echo 状态: 已登录
    echo.
    echo 网关导入方式（任选其一）:
    echo   A. 设环境变量后重启 Buddy2api:
    echo        set CB_XHX_AUTH_DIR=%ROOT%\%~2%\agent\config\auth.json
    echo   B. 把上面的 auth.json 复制到 ~/.office-raccoon/agent/config/ 覆盖后重新扫描
) else (
    echo 状态: 未登录（auth.json 不存在）
    echo 请先用 %~nx0 start %~2 启动并在窗口里登录。
)
pause
exit /b 0

rem ---- 子程序: write_marker <profile目录> ----
:write_marker
set "MDIR=%~1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_DIR%write_marker.ps1" "%MDIR%\%MARKER%"
if not exist "%MDIR%\%MARKER%" (
    echo [错误] 写 marker 失败: %~1
    pause
    exit /b 1
)
exit /b 0
