@echo off
set "PIDFILE=%LOCALAPPDATA%\ephy-discord\ephy-discord.pid"
if not exist "%PIDFILE%" (
  echo ephy-discord is not running
  exit /b 0
)
set /p PID=<"%PIDFILE%"
taskkill /PID %PID% /T >nul 2>&1
del "%PIDFILE%" >nul 2>&1
echo stopped
exit /b 0
