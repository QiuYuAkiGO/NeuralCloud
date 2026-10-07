@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\deploy\llama-local.ps1" -Command start %*
set "NC_LLAMA_EXIT=%ERRORLEVEL%"
if not "%NC_LLAMA_EXIT%"=="0" pause
exit /b %NC_LLAMA_EXIT%
