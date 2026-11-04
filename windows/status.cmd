@echo off
set "PIDFILE=%LOCALAPPDATA%\ephy-discord\ephy-discord.pid"
if not exist "%PIDFILE%" (
  echo status=stopped
  exit /b 1
)
set /p PID=<"%PIDFILE%"
tasklist /FI "PID eq %PID%" | find "%PID%" >nul
if errorlevel 1 (
  echo status=stopped pidfile=%PID%
  exit /b 1
)
echo status=running pid=%PID%
echo mcp_port=8791
exit /b 0
