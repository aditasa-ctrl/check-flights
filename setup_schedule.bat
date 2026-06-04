@echo off
REM ===========================================================
REM  One-click: register Wizz Watch to run every hour, hidden.
REM  Run this file ONCE (double-click). That's it.
REM  Uses pythonw so no console window pops up each hour.
REM ===========================================================
setlocal
set "DIR=%~dp0"

schtasks /Create /TN "WizzWatch" /SC HOURLY /MO 1 /F ^
  /TR "pyt \"%DIR%wizz_watch.py\""

echo.
if %ERRORLEVEL%==0 (
  echo SUCCESS: 'WizzWatch' will now run every hour automatically.
  echo It only runs while you are logged in and the PC is awake.
  echo.
  echo To run it right now as a test:   schtasks /Run /TN WizzWatch
  echo To stop/remove it later:         schtasks /Delete /TN WizzWatch /F
) else (
  echo FAILED to create the task. Try running this file as Administrator
  echo ^(right-click -^> Run as administrator^).
)
echo.
pause
