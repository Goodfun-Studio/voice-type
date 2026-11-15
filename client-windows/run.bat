@echo off
REM Voice Type (Windows) — foreground launcher with a console (useful for logs).
REM For silent/background autostart the installer uses pythonw.exe directly.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" -m voicetype_win %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo voicetype_win exited with code %RC%
  pause
)
endlocal & exit /b %RC%
