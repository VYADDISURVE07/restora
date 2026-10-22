@echo off
rem Record Restora's demo video on this PC: Microsoft Edge plays the script, the local AI model answers for real.
rem Needs setup.bat and ai_setup.bat first. Takes about five minutes. Output: demo_video_out\restora_demo.mp4
rem Usage:  tools\record_demo.bat            (watch it happen in a full-screen Edge window)
rem         tools\record_demo.bat --headless (no window)
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  if not defined RESTORA_NO_PAUSE pause
  exit /b 1
)
echo Installing the recording tools (Playwright, ffmpeg) into .venv...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements-demo.txt
if errorlevel 1 (
  echo Could not install the recording tools. See SETUP_GUIDE.md - company proxy.
  if not defined RESTORA_NO_PAUSE pause
  exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" tools\demo_video.py %*
set CODE=%errorlevel%
echo.
if "%CODE%"=="0" echo The video is ready: demo_video_out\restora_demo.mp4
if not defined RESTORA_NO_PAUSE pause
exit /b %CODE%
