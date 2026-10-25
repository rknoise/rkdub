@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo rkdub - telepites: Python, PyTorch (NVIDIA), csomagok, modellek, asztali ikon
echo Minden ebbe a mappaba kerul. Eltavolitas: a mappa es az asztali ikon torlese.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install.ps1" %*
if errorlevel 1 (
  echo.
  echo A telepites nem sikerult, lasd a fenti uzeneteket.
  pause
  exit /b 1
)
echo.
pause
