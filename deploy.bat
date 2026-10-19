@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 "%~dp0tools\release\bootstrap.py" %*
) else (
    python "%~dp0tools\release\bootstrap.py" %*
)
set "NC_DEPLOY_EXIT=%ERRORLEVEL%"
if not "%NC_DEPLOY_EXIT%"=="0" (
    echo Deployment failed. Install Python 3.12 with pip and PATH enabled; see logs\install.log and docs\STANDALONE_RELEASE.md.
    if not defined NC_DEPLOY_NO_PAUSE pause
)
exit /b %NC_DEPLOY_EXIT%
