@echo off
rem Restora - run.   Examples:  run.bat   |   run.bat my_apis.xlsx   |   run.bat --demo   |   run.bat collection.json
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.bat first.
  exit /b 1
)
set PYTHONUTF8=1
"%~dp0.venv\Scripts\python.exe" "%~dp0run.py" %*
