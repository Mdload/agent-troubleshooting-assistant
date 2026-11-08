@echo off
cd /d "%~dp0"
python -B scripts\check_offline.py %*
exit /b %errorlevel%
