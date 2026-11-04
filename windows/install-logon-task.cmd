@echo off
schtasks /Create /F /TN "ephy-discord" /SC ONLOGON /RL LIMITED /TR "\"%~dp0start.cmd\""
exit /b %ERRORLEVEL%
