@echo off
chcp 65001 >nul
cd /d "%~dp0"
title rkdub - helyi magyar szinkron (ezt az ablakot ne zard be, amig hasznalod)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" rkdub.py %*
) else (
  echo Meg nincs telepitve: futtasd az install.bat-ot.
)
echo.
pause
