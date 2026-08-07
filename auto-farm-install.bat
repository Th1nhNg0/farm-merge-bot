@echo off
title FMV Bot - Installer
setlocal
set "FARM_DIR=C:\Users\weepingangel89\Desktop\auto-farm"
set "DISCORD_URL=https://discord.com/channels/1158408201344135258/1315833045814738985"
set "PROFILE=%USERPROFILE%\.cache\chrome-devtools-mcp\chrome-profile"
set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"

echo [1/3] Launching Chrome with CDP on port 9222...
if exist "%CHROME%" (
    start "" "%CHROME%" --remote-debugging-port=9222 --enable-features=IsolateSandboxedIframes ^
        --disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows ^
        --user-data-dir="%PROFILE%" "%DISCORD_URL%"
) else (
    echo Chrome not found at "%CHROME%" - check the CHROME path.
    pause
    exit /b 1
)

echo [2/3] Waiting for CDP on port 9222...
set /a tries=0
:waitloop
set /a tries+=1
curl -s -o nul http://127.0.0.1:9222/json/version && goto cdp_ok
if %tries% geq 20 goto cdp_fail
timeout /t 1 /nobreak >nul
goto waitloop

:cdp_fail
echo CDP never became reachable on port 9222.
echo If another Chrome window opened without the flags, close ALL Chrome windows
echo (the default profile instance holds the debug port) and re-run this script.
pause
exit /b 1

:cdp_ok
echo CDP is up.
echo (background flags on: the farm keeps running when the Chrome window is hidden/occluded)
echo.
echo Open the farm activity in the Discord tab (voice channel ^> Activities ^> Farm Merge Valley),
echo then press any key to run src\install.mjs...
pause >nul

echo [3/3] Running src\install.mjs...
cd /d "%FARM_DIR%"
node src\install.mjs

echo.
echo Done - close this window or press any key.
pause >nul
