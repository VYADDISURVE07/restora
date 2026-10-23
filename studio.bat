@echo off
rem Copyright 2026 Vedant Surve
rem SPDX-License-Identifier: Apache-2.0
rem Restora Studio - the point-and-click app. Double-click it; your browser opens. Close this window to stop.
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
set PYTHONUTF8=1
title Restora Studio
"%~dp0.venv\Scripts\python.exe" "%~dp0run.py" --studio %*
