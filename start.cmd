@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" %*
set "NC_LAUNCH_EXIT=%ERRORLEVEL%"
if not "%NC_LAUNCH_EXIT%"=="0" pause
exit /b %NC_LAUNCH_EXIT%
