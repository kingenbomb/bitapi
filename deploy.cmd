@echo off
setlocal

rem deploy.ps1 requires pwsh (PowerShell 7+). Windows PowerShell 5.1 does not
rem escape embedded double quotes when passing a command to bash.exe, so the
rem source upload reports success while nothing reaches the server. Do not
rem "fall back" to powershell.exe here -- that is the bug, not a safety net.
where pwsh >nul 2>&1
if errorlevel 1 (
  echo pwsh ^(PowerShell 7+^) was not found on PATH.
  echo deploy.ps1 does not work under Windows PowerShell 5.1 -- the upload
  echo silently sends nothing. Install PowerShell 7, then run this again.
  pause
  exit /b 1
)

pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"
set "deploy_exit=%errorlevel%"

echo.
if not "%deploy_exit%"=="0" (
  echo Deployment failed. Review the error above.
) else (
  echo Deployment completed successfully.
)
pause
exit /b %deploy_exit%
