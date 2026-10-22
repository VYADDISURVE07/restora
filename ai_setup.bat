@echo off
rem Restora - install the free local AI (Ollama + a small open model). Optional: Restora works without it.
rem Usage:  ai_setup.bat               (installs qwen3:4b-instruct, about 2.5 GB)
rem         ai_setup.bat qwen3:1.7b    (a smaller model for older PCs)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\ai_setup.ps1" %*
