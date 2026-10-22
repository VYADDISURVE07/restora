@echo off
rem Restora - one-time setup for Windows. Double-click it, or run:  setup.bat
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if errorlevel 1 goto :oldpython
echo Using:
%PY% --version

if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 goto :novenv
)

if exist wheels (
  echo Installing from the local "wheels" folder - offline mode...
  ".venv\Scripts\python.exe" -m pip install --no-index --find-links=wheels -r requirements.txt
) else (
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
)
if errorlevel 1 goto :nopip

echo.
echo Checking the installation (Restora's own tests)...
set PYTHONUTF8=1
".venv\Scripts\python.exe" run.py --selftest
echo.
echo Setup finished. Next:
echo   studio.bat           opens Restora Studio in your browser (point and click)
echo   run.bat --demo       runs the Demo Bank example in this window
echo   ai_setup.bat         optional: installs the free local AI model (Ollama)
if "%~1"=="" pause
exit /b 0

:nopython
echo [ERROR] Python was not found.
echo Install Python 3.9 or newer from https://www.python.org/downloads/
echo and TICK "Add python.exe to PATH" in the installer. Then run setup.bat again.
if "%~1"=="" pause
exit /b 1

:oldpython
echo [ERROR] Python 3.9 or newer is needed. This machine has:
%PY% --version
if "%~1"=="" pause
exit /b 1

:novenv
echo [ERROR] Could not create the virtual environment.
if "%~1"=="" pause
exit /b 1

:nopip
echo [ERROR] Could not install the packages. See SETUP_GUIDE.md - offline install and company proxy.
if "%~1"=="" pause
exit /b 1
