@echo off
REM Runs one Wizz Watch check. Point Windows Task Scheduler at this file.
REM Edit the next line if your script lives somewhere else.
cd /d "%~dp0"
python "%~dp0wizz_watch.py"
