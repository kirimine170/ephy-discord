@echo off
schtasks /Delete /F /TN "ephy-discord"
exit /b %ERRORLEVEL%
